"""调度 —— 按节拍把闭环转起来：取数 → 推理 → 结论回流。

一个绑定一个节拍。**节拍对齐到整点边界**，且取的是**上一个已完结的节拍**，
不是 `now()`：

    now = 12:00:07，interval=60s  ⇒  本轮处理的窗口右端是 **12:00:00**，不是 12:00:07。

  ★为什么：数据是采上来再写进库的，`now()` 那一刻窗口右端**必然还没到齐**。
    按 `now()` 取会稳定地少最后几笔，而且**看起来像"数据稀疏"**，查不出原因。
    对齐之后，同一个绑定每轮的窗口首尾相接、不重不漏，历史可复算。

失败的处置分两种，**不混为一谈**：

  · **hs 不可用**（连不上/超时）—— 这一轮**整个跳过**，退避后重试。
    此时**不落锚点**：我方连"取没取到数"都不知道，落锚点等于替上游断言"这段没数据"。
  · **模块算不出来** —— 由 `runner` 代落坏值锚点（见那边模块头）。**照常回流**。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from .bindings import Binding, BindingStore
from .domains import LoadedDomain
from .fetch import Fetcher
from .hsclient import HsClient
from .pointmap import PointMap, default_point_name
from .runner import run_domain
from .types import Finding

logger = logging.getLogger(__name__)

#: hs 不可用时的退避上限（秒）。与 `hsclient.RETRY_INTERVAL_SEC` 同量级 ——
#: 退避不是为了少打扰对端，是为了不让日志被刷爆；降级态 WARN 仍每周期都打。
MAX_BACKOFF_SEC = 60.0


def aligned_tick(now: datetime, interval_sec: float) -> datetime:
    """`now` 之前最近的一个整节拍边界（含相等）。

    以 UTC 纪元为基准取整，故**不同进程、不同重启，边界恒相同** ——
    重启后不会跟自己错开半拍，历史窗口首尾仍然接得上。
    """
    if interval_sec <= 0:
        raise ValueError(f"节拍必须为正: {interval_sec}")
    epoch = now.timestamp()
    return datetime.fromtimestamp(
        math.floor(epoch / interval_sec) * interval_sec, tz=timezone.utc)


class Scheduler:
    """把域、绑定、取数、回流串起来。**单进程持有 hs 连接**（见 `hsclient` 模块头）。"""

    def __init__(self, *, client: HsClient, fetcher: Fetcher,
                 domains: dict[str, LoadedDomain], bindings: BindingStore,
                 points: PointMap, artifacts=None) -> None:
        self._client = client
        self._fetcher = fetcher
        self._domains = domains
        self._bindings = bindings
        self._points = points
        # 当前启用工件的提供者（`ActiveArtifacts`）。没接 = 模块永远拿不到工件，
        # 于是靠模型/基线的那些结论一律落 MODEL_NOT_LOADED —— 那是**如实**的降级，不是缺陷。
        self._artifacts = artifacts
        self._stop = threading.Event()
        self._threads: dict[tuple[str, str], threading.Thread] = {}
        self._last_tick: dict[tuple[str, str], datetime] = {}
        self._sync_lock = threading.Lock()
        # ★「写路径断而复连 ⇒ 必须重推全量快照」的标志（AICloud C-27）。
        #   由来：实时库 1.9.437→1.9.438 升级重启后，12 个结论点的**点定义**从实体流里没了
        #   （平台镜像 2130→2118、按来源 guid 过滤 12→0），而**值照样写得进**、`/health` 照样 ready。
        #   引擎重启 = 这一侧的快照要重新提交；而本类此前只在**服务启动**与**绑定变更**时推快照，
        #   调度线程的异常支路只退避重试 —— 于是没有任何路径会再推一次点定义，**不会自愈**。
        #   ★这条规矩是我方自己写下的（`hsclient` 模块头 ③：SNAPSHOT_BEGIN…END 之间断流 =
        #   半份快照被丢弃，**重连必须重推全量**），却没在这里兑现。网关侧（ProtocolGate
        #   entitystream.go）每次建立订阅都重走一遍全量，所以它那两段点重启后原数回来了。
        self._resnap_lock = threading.Lock()
        self._need_resnapshot = False
        #: 最近一次全量快照是否已被**当前这个引擎实例**接受（供 /health 用）。
        self._snapshot_ok = False

    # ── 点表 ──────────────────────────────────────────────────────────────
    def ensure_points(self) -> int:
        """为所有（启用的）绑定 × 该域声明的输出，确保结论点存在，然后**推一份全量快照**。

        ★快照必须全量：`SNAPSHOT_END` 是原子提交，本轮未出现的旧实体一律删除。
          所以这里推的是 `PointMap.all()`，不是"这次新增的那些"。
        """
        for b in self._bindings.list(only_enabled=True):
            loaded = self._domains.get(b.domain)
            if loaded is None:
                logger.warning("绑定 %s/%s 指向未装载的域，已跳过", b.domain, b.binding)
                continue
            for o in loaded.declaration.outputs:
                self._points.ensure(
                    b.domain, b.binding, o.key,
                    name=default_point_name(b.domain, b.binding, o.key),
                    unit=o.unit, value_type=o.value_type)
        rows = self._points.all()
        self._client.push_snapshot(rows)
        self._snapshot_ok = True
        return len(rows)

    # ── 一拍 ──────────────────────────────────────────────────────────────
    def run_once(self, b: Binding, tick: datetime) -> list[Finding]:
        """处理一个绑定的一拍。返回本拍回流的结论（供自检/测试）。"""
        loaded = self._domains.get(b.domain)
        if loaded is None:
            logger.warning("绑定 %s/%s 指向未装载的域，跳过", b.domain, b.binding)
            return []

        missing = b.missing_required(
            [i.role for i in loaded.declaration.inputs if i.required and i.kind == "point"])
        if missing:
            # 必填角色没绑 —— 这不是"取不到数"，是**配置不全**，要吵，而且每拍都吵。
            logger.warning("绑定 %s/%s 缺必填角色 %s，本拍不推理", b.domain, b.binding, missing)
            return []

        # ★推理路径**带上当前启用的工件**；训练路径不带（拿旧模型当输入 = 模型喂自己）。
        arts = self._artifacts.for_binding(b.domain, b.binding) if self._artifacts else {}
        frame = self._fetcher.fetch(b, tick, artifacts=arts)   # hs 不可用会抛，调用方按退避处置
        result = run_domain(loaded, frame)

        items: list[tuple[int, Finding]] = []
        for f in result.findings:
            lid = self._points.local_id_of(b.domain, b.binding, f.key)
            if lid is None:
                # 点还没建（新加的输出）—— 补建并记下，下一次 ensure_points 会把它带进快照。
                spec = next(o for o in loaded.declaration.outputs if o.key == f.key)
                lid = self._points.ensure(
                    b.domain, b.binding, f.key,
                    name=default_point_name(b.domain, b.binding, f.key),
                    unit=spec.unit, value_type=spec.value_type).local_id
                logger.info("为新结论 %s/%s/%s 补建了点 localId=%d",
                            b.domain, b.binding, f.key, lid)
            items.append((lid, f))

        if items:
            self._client.post_vqt(items)
        self._last_tick[(b.domain, b.binding)] = tick
        return result.findings

    # ── 循环 ──────────────────────────────────────────────────────────────
    def _loop(self, domain: str, binding: str) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            b = self._bindings.get(domain, binding)
            if b is None or not b.enabled:
                # 绑定被删/停用 —— 本线程退出。**不删已经写进去的结论点**（见 BindingStore.delete）。
                logger.info("绑定 %s/%s 已停用，调度线程退出", domain, binding)
                return
            try:
                # ★断而复连的第一件事:重推全量快照,再谈这一拍(AICloud C-27)。
                #   顺序不能反 —— 先写值再推快照,那一拍的值会落在"引擎还不认识这个点"的窗口里。
                if self._need_resnapshot:
                    self._republish_snapshot()
                tick = aligned_tick(datetime.now(timezone.utc), b.interval_sec)
                if self._last_tick.get((domain, binding)) != tick:
                    self.run_once(b, tick)
                backoff = 0.0
            except Exception as exc:  # noqa: BLE001
                # ★hs 不可用这一支:**整拍跳过、不落锚点** —— 我方连取没取到数都不知道,
                #   落锚点等于替上游断言"这段没数据"。
                # ★并标记"下次通了要重推快照":连接断过,对端可能已经是**另一个引擎实例**,
                #   它手上没有我方这份快照。宁可多推一次(全量快照是幂等的),不可少推。
                self._need_resnapshot = True
                self._snapshot_ok = False
                self._client.log_degraded(f"调度 {domain}/{binding}", exc)
                backoff = min(MAX_BACKOFF_SEC, backoff * 2 if backoff else 5.0)
            self._stop.wait(backoff if backoff else min(b.interval_sec / 4, 5.0))

    def _republish_snapshot(self) -> None:
        """断而复连后重推一次全量快照。**多个调度线程只推一次**。

        ★失败不吞:让它抛给调用方的退避支路 —— 那说明连接还没真好,下一轮再试,
          标志留着。吞掉的话会把"没推成"记成"已推过",而这正是 C-27 那类
          「失败长得像一切正常」的做法。
        """
        with self._resnap_lock:
            if not self._need_resnapshot:
                return                      # 别的线程已经推过了
            n = self.ensure_points()        # 抛出 ⇒ 标志不清,外层退避后重来
            self._need_resnapshot = False
            logger.warning("写路径断而复连:已重推全量结论点快照(%d 个点)—— "
                           "引擎重启会丢掉未重推的点定义,值却照样写得进(AICloud C-27)", n)

    @property
    def snapshot_ok(self) -> bool:
        """全量快照是否已被**当前这个引擎实例**接受。供 `/health` 用。

        ★与 `writePath: ready` 不是一回事:后者只说"配了写路径且证书齐",
          C-27 那次就是 `ready` 而点定义已经丢了 —— 健康口当时说不出这件事。
        """
        return self._snapshot_ok

    # ── 与绑定表同步 ──────────────────────────────────────────────────────
    def sync(self) -> int:
        """建点 + 推快照 + 为**还没有线程**的启用绑定起线程。返回在跑的绑定数。

        ★启动时调一次，**每次绑定变更后也要调** —— 否则运行期新加的绑定既不会建点、
          也不会有线程跑它，界面上看着配好了、实际一拍都不走。
          （这正是整机自检里"推送快照：0 个"暴露出来的那个缺口。）

        幂等：重复调只会补齐缺的，不会重复建点（`PointMap.ensure` 恒回同一个 localId）、
        也不会起第二个线程。
        """
        with self._sync_lock:
            if self._stop.is_set():
                return 0
            self.ensure_points()
            for b in self._bindings.list(only_enabled=True):
                key = (b.domain, b.binding)
                dom = self._domains.get(b.domain)
                if dom is not None and not any(i.kind == "point" for i in dom.declaration.inputs):
                    # ★纯图片域（事件驱动）**不起轮询线程**：它的数据是现场送来的，不是去取的。
                    #   起了的样子是每拍拿空帧推理、落一串"没数据"。点照建（上面 ensure_points 已含）。
                    continue
                t = self._threads.get(key)
                if t is not None and t.is_alive():
                    continue
                th = threading.Thread(target=self._loop, args=key,
                                      name=f"sched-{b.domain}-{b.binding}", daemon=True)
                th.start()
                self._threads[key] = th
            alive = sum(1 for t in self._threads.values() if t.is_alive())
            logger.info("调度已同步：在跑 %d 个绑定", alive)
            return alive

    def start(self) -> None:
        self.sync()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in list(self._threads.values()):
            t.join(timeout=timeout)
        self._threads.clear()
