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
                 points: PointMap) -> None:
        self._client = client
        self._fetcher = fetcher
        self._domains = domains
        self._bindings = bindings
        self._points = points
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_tick: dict[tuple[str, str], datetime] = {}

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
        return len(rows)

    # ── 一拍 ──────────────────────────────────────────────────────────────
    def run_once(self, b: Binding, tick: datetime) -> list[Finding]:
        """处理一个绑定的一拍。返回本拍回流的结论（供自检/测试）。"""
        loaded = self._domains.get(b.domain)
        if loaded is None:
            logger.warning("绑定 %s/%s 指向未装载的域，跳过", b.domain, b.binding)
            return []

        missing = b.missing_required(
            [i.role for i in loaded.declaration.inputs if i.required])
        if missing:
            # 必填角色没绑 —— 这不是"取不到数"，是**配置不全**，要吵，而且每拍都吵。
            logger.warning("绑定 %s/%s 缺必填角色 %s，本拍不推理", b.domain, b.binding, missing)
            return []

        frame = self._fetcher.fetch(b, tick)      # hs 不可用会抛，由调用方按退避处置
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
                tick = aligned_tick(datetime.now(timezone.utc), b.interval_sec)
                if self._last_tick.get((domain, binding)) != tick:
                    self.run_once(b, tick)
                backoff = 0.0
            except Exception as exc:  # noqa: BLE001
                # ★hs 不可用这一支:**整拍跳过、不落锚点** —— 我方连取没取到数都不知道，
                #   落锚点等于替上游断言"这段没数据"。
                self._client.log_degraded(f"调度 {domain}/{binding}", exc)
                backoff = min(MAX_BACKOFF_SEC, backoff * 2 if backoff else 5.0)
            self._stop.wait(backoff if backoff else min(b.interval_sec / 4, 5.0))

    def start(self) -> None:
        for b in self._bindings.list(only_enabled=True):
            t = threading.Thread(target=self._loop, args=(b.domain, b.binding),
                                 name=f"sched-{b.domain}-{b.binding}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("调度已启动：%d 个绑定", len(self._threads))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
