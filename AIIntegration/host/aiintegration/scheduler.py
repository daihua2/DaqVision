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

#: 无条件重推全量快照的周期（秒）。
#  ★为什么是「定期无条件」而不是「检测到重连再推」—— AICloud C-32 的实测结论：
#    实时库重启后**写入照样成功**（通道透明重连；引擎按 localId 收得下 VQT，
#    哪怕它手上已经没有这些点的实体配置）⇒ 调度循环**从不抛异常** ⇒
#    挂在异常支路上的「要重推」标志**永不置位**，于是点定义一直不回来。
#    本方 AI-32 那一版就修在这个错信号上，C-31 的受控验收把它证伪了。
#  ★而现有契约里**拿不到引擎实例身份**：`PingRes` 只有 token / listenAddrs；
#    `/health` 的 idSpaceEpoch 是**持久化**的（INSERT OR IGNORE），重启不变。
#    ⇒ 没有精确判据可用，只能定期推。
#  ★全量快照幂等、本例只有 12 行 ⇒ 定期重推代价可忽略；
#    而「永不触发」的代价是点定义一直缺着，且**值照写、健康口照绿**，没人会去查。
#  ★这是**过渡**：正解是引擎报实例身份（本方作为 historystore owner 会给 PingRes 加一格），
#    届时改成「实例变了就推」，这个周期可以拉长或去掉。
#  ★2026-09-15 已到那一步：AISERVER 实时库升到 1.9.445，`PingRes.instanceId` 真有了，
#    「对端换了实例」现场 5 秒内触发（AICloud C-34 §1/§2，最终验收通过）。
#    ⇒ 本值退为**老引擎（报不出实例身份）时的兜底**；对端报得出时用下面那个长周期。
RESNAPSHOT_INTERVAL_SEC = 300.0

#: 对端**报得出实例身份**时的重推周期（秒）。
#  ★为什么还留着周期、不干脆关掉：C-27/C-32 这条线的失败形态是**静默的** —— 点定义丢了而
#    **值照样写得进、健康口照样绿**，没人会去查。精确判据只覆盖"引擎换了实例"这一种成因；
#    成因未知的那类没有判据可挂（C-34 §3 记的点表整表替换点数对不上就还没查清）。
#    留一格兜底，代价是一天 24 次幂等重推；撤掉它，代价是下次静默丢定义再没人发现。
#  ★为什么从 300 拉到 3600：historystore H-240 §4.1 实测，我方这个每 5 分钟一次的全量重推
#    **占了实时库这个服务几乎全部的 WARNING**（24 小时 291 条，该服务共约 300 条），
#    并建议有了实例编号之后改成"实例变了才推"；AICloud C-34 §4.1 转来请我方定。
#    ⇒ 291/天 → 约 24/天，兜底不撤。
RESNAPSHOT_INTERVAL_KNOWN_PEER_SEC = 3600.0

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
                 points: PointMap, artifacts=None, states=None) -> None:
        self._client = client
        self._fetcher = fetcher
        self._domains = domains
        self._bindings = bindings
        self._points = points
        # 当前启用工件的提供者（`ActiveArtifacts`）。没接 = 模块永远拿不到工件，
        # 于是靠模型/基线的那些结论一律落 MODEL_NOT_LOADED —— 那是**如实**的降级，不是缺陷。
        self._artifacts = artifacts
        # 跨帧状态的存放处（`Workbench`）。没接 = 声明了 stateful 的域每拍都拿到空状态，
        # 于是永远从头攒 —— 那是**降级**，不是缺陷，但它该被看见，所以装载时吵一句。
        self._states = states
        self._warned_stateless: set[str] = set()
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
        #: 上次见到的对端**实例身份**（`PingRes.instanceId`，实时库 1.9.441 起）。
        #  ★这才是精确判据：引擎重启即换实例 ⇒ 我方的快照失效 ⇒ 必须重推。
        #    空串 = 对端是老引擎（没有这一格）⇒ 退回下面那个周期兜底，**不当成"变了"**。
        self._peer_instance: str | None = None
        #: 最近一次**成功推送**全量快照的单调时刻；None = 从没推过。
        #  ★不叫"已被当前引擎实例接受" —— 那是本方 AI-32 版里说过而做不到的话：
        #    推送成功只证明"推的那一刻对端收了"，证明不了"现在这个实例手上还有"。
        #    C-32 实测：点定义已丢的两分钟里，那一格一直报 accepted，**在说假话**。
        self._last_snapshot_mono: float | None = None

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
        self._last_snapshot_mono = time.monotonic()
        return len(rows)

    def _warn_stateless_once(self) -> None:
        """声明了 `stateful` 的域，却没接状态存放处 —— 吵一句，每个域只吵一次。

        ★为什么非吵不可：没接的表现是**每拍都拿到空状态**，于是跟踪永远编不出同一个目标、
          趋势永远从头攒。而这些结论**看上去完全正常**（有值、质量 OK），
          没人会想到去查"状态到底存没存" —— 与"写路径未就绪"要每次启动都吵是同一条。
        """
        if self._states is not None:
            return
        for key, dom in self._domains.items():
            if getattr(dom.declaration, "stateful", False) and key not in self._warned_stateless:
                self._warned_stateless.add(key)
                logger.warning(
                    "域 %s 声明了 stateful（要跨帧状态），但调度没接工作台库 —— "
                    "**它每拍都会拿到空状态**（跟踪接不上、趋势从头攒），结论却看着正常。", key)

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
        result = run_domain(loaded, frame, states=self._states)

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
                # ★重推全量快照要排在这一拍之前(AICloud C-27):顺序反了的话,
                #   那一拍的值会落在"引擎还不认识这个点"的窗口里。
                # ★两个触发:① 断连过(写失败);② **到周期**——后者才是主力,
                #   因为实时库重启时写入根本不会失败(C-32 实测)。
                if self._need_resnapshot or self._peer_changed() or self._resnapshot_due():
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
            if not (self._need_resnapshot or self._peer_changed() or self._resnapshot_due()):
                return                      # 别的线程刚推过
            why = ("断而复连" if self._need_resnapshot
                   else "对端换了实例" if self._peer_changed() else "到周期")
            n = self.ensure_points()        # 抛出 ⇒ 标志不清,外层退避后重来
            self._need_resnapshot = False
            try:                                # 推成了才认下新实例身份
                # ★**空串也要认下**，不能 `or 旧值` 兜回去：那样引擎一旦从新版**回退**到老版
                #   （有 id → 空串），旧 id 会一直留着，`_peer_changed` 从此**每拍都判变、每拍都推**。
                #   认下空串则推一次就稳住，并由 `_resnapshot_interval_sec` 退回 300 秒兜底。
                #   探活抛异常那一支才留旧值 —— 那是"问不到"，不是"变了"。
                self._peer_instance = self._client.instance_id()
            except Exception:                   # noqa: BLE001
                pass
            logger.info("%s:已重推全量结论点快照(%d 个点)—— 引擎重启会丢掉未重推的点定义,"
                        "而值照样写得进、健康口照样绿(AICloud C-27/C-32)", why, n)

    def _peer_changed(self) -> bool:
        """对端是不是换实例了（引擎重启）。★这是**精确判据**，周期那条只是兜底。

        ★**空串不特判**（老引擎没有这一格）：直接记住并比较，三种情形都对 ——
          · 老引擎恒空：`"" == ""` ⇒ 不变，靠周期兜底；
          · **老引擎升级成新引擎**：`"" → "inst-A"` ⇒ **判变、立刻重推** ——
            升级必然重启，快照确实失效，这一推是该推的；
          · 新引擎重启：`"A" → "B"` ⇒ 判变。
          ★先前这里特判了空串（`if not cur: return False`），**变异验证发现它没有作用**，
            而且它会让上面第二种情形错过一次该推的重推。去掉。
        ★探活失败回 False：那一支由调用方的退避与 `_need_resnapshot` 管，
          不在这里把"问不到"当成"变了"。
        """
        try:
            cur = self._client.instance_id()
        except Exception:                       # noqa: BLE001 —— 连不上，交给退避支路
            return False
        if self._peer_instance is None:
            self._peer_instance = cur           # 第一次见到，不算"变了"（启动已推过）
            return False
        return cur != self._peer_instance

    def _resnapshot_due(self) -> bool:
        """到周期没有。从没推过 ⇒ 到期（启动路径会先推一次，这里是兜底）。"""
        if self._last_snapshot_mono is None:
            return True
        return (time.monotonic() - self._last_snapshot_mono) >= self._resnapshot_interval_sec()

    def _resnapshot_interval_sec(self) -> float:
        """这一刻的周期取哪个 —— **看对端报不报实例身份**，不看我方版本号。

        ★判据是"最近一次问到的 `instanceId` 非空"：非空 ⇒ 精确判据可用，周期只是兜底（1 小时）；
          空串（老引擎）或还没问到过 ⇒ 精确判据不可用，**退回 300 秒，和升级前一模一样**。
        ★所以引擎**回退**到老版本也是对的：`_republish_snapshot` 会认下空串身份，
          下一拍这里自动退回 300 秒，不需要谁去改配置、也不需要重启本服务。
        """
        if self._peer_instance:
            return RESNAPSHOT_INTERVAL_KNOWN_PEER_SEC
        return RESNAPSHOT_INTERVAL_SEC

    @property
    def resnapshot_interval_sec(self) -> float:
        """当前生效的兜底周期（秒）。供 `/health` 用 —— 那一格若写死 300 就会说假话。"""
        return self._resnapshot_interval_sec()

    @property
    def peer_instance_known(self) -> bool:
        """对端最近一次报得出实例身份没有。False = 老引擎或还没问到 ⇒ 精确判据不可用。"""
        return bool(self._peer_instance)

    @property
    def snapshot_age_sec(self) -> float | None:
        """距最近一次**成功推送**多少秒；None = 从没推过。供 `/health` 用。

        ★它回答的是"多久以前推过一次",**不是**"当前这个引擎实例手上有没有" ——
          后者本方现在答不了（契约里拿不到实例身份），所以**不装作答得了**。
          C-32 那一格报 `accepted` 而点定义已丢，就是装作答得了的后果。
        """
        if self._last_snapshot_mono is None:
            return None
        return time.monotonic() - self._last_snapshot_mono

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
            self._warn_stateless_once()
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
