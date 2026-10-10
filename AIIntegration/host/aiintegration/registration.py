"""结论点在实时库登记上没有 —— 授权数据点数（实时库 `admitByQuota`，AICloud `C-81` 起）。

由来（`C-85`、`C-86`、`H-290`、`H-294`）：

  · 实时库按**授权数据点数**把关登记：已用 + 新增 > 授权、或授权为 `-1`（不许新增）时，新存储点不登记。
    「已用」含 AI 结论点。
  · 我方结论点由骨架经 `PushEntityConfigs` 自己登记，**不经平台的三个加点入口**，平台拦不到。
  · 被拒**不报错**：推流照收、`accepted` 照算（`H-294 §1`）；`PostVQT` 照收、回 `Ok`（`H-290 §2`），
    样本落在映射编号下、点表里没有。⇒ 平台看不到这些结论，我方健康口照绿 —— 与 `C-27` 同一类静默失败。

两件事（用户 10-10 定「甲 + 乙」）：

  **甲 建前拦**：`PutBinding` 会让新点进快照时，先问 `CheckPointQuota`；回 `Deny` 就不存，原话回给平台。
    ★只拦「会新增点」的那次保存 —— 改参数、停启都不新增点，满额时照样能改；
      否则满额之后连已有绑定的台账都改不了。
  **乙 建后查**：每次推完快照问一次，`newPoints > 0` 即有点没登记上（`H-294 §2`），逐点找出是哪几个，
    记告警、`/health` 与 `ConclusionPoint.unregistered`（契约 1.18）报出。
    甲拦不住的都靠它：域升级新增结论项、授权变成 `-1`、多个来源同时抢最后几个名额（`H-291 §1`）。

★**问不到一律放行**（连不上、老引擎没有这个口）：授权是实时库的闸，它自己会拦；
  我方这一层只是把它的结论早一点、说明白。拿「问不到」去拦保存，等于实时库一抖就建不了绑定。
  问不到时乙那一格**留着上一次的结论**，不清成「全登记上了」—— 没问到不等于没事。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .hsproto import daqcontract_pb2 as daq
from .pointmap import PointRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegistrationView:
    """最近一次核对的结论。"""

    checked_at: datetime | None       # 最近一次**问到**的时刻（UTC）；None = 还没问到过
    unregistered: frozenset[int]      # 没登记上的结论点 localId
    limit: int                        # 实时库当时的授权上限：0 不限制，-1 不许新增
    used: int                         # 实时库当时的已用存储点数（全库，含自指标）
    note: str                         # 原因或「问不到」的说明；全登记上时为空


class Registration:
    """甲、乙两处共用一个实例（调度推完快照后写，`PutBinding` / `ListBindings` / `/health` 读）。"""

    def __init__(self, client) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._view = RegistrationView(None, frozenset(), 0, 0, "尚未核对")

    # ── 甲 ────────────────────────────────────────────────────────────────
    def admit(self, *, new_count: int, local_ids: list[int], what: str) -> str:
        """这次要进快照的点能不能登记上。能（或问不到）回空串，不能回给界面的原话。

        `new_count` = 还没有点号的新点个数；`local_ids` = 已有点号、这次要重新进快照的（停用后重建）。
        """
        if new_count <= 0 and not local_ids:
            return ""
        try:
            res = self._client.check_point_quota(new_count=new_count, local_ids=local_ids)
        except Exception as exc:  # noqa: BLE001 —— 问不到放行，见模块头
            logger.warning("%s：问实时库授权数据点数失败（%s），先放行 —— 若登记不上，推完快照会查出来",
                           what, exc)
            return ""
        if res is None or res.newPoints <= 0 or res.result.Code != daq.Deny:
            return ""
        return res.result.Message or (
            f"超出授权数据点数（已用 {res.used} / 授权 {res.limit}，本次新增 {res.newPoints}）")

    # ── 乙 ────────────────────────────────────────────────────────────────
    def refresh(self, rows: list[PointRow]) -> RegistrationView:
        """推完快照后核对 `rows`（本次快照里的全部结论点）登记上没有。**不抛**：问不到只记告警。"""
        lids = [r.local_id for r in rows]
        try:
            res = self._client.check_point_quota(local_ids=lids)
            if res is None:
                return self._keep("实时库没有授权预检口（老引擎），无从核对")
            missing: frozenset[int] = frozenset()
            if res.newPoints > 0:
                # 一次问全部只给个数；逐点再问找出是哪几个。只在有缺时才走这一步，常态一次调用。
                missing = frozenset(lid for lid in lids
                                    if self._client.check_point_quota(local_ids=[lid]).newPoints > 0)
        except Exception as exc:  # noqa: BLE001
            return self._keep(f"核对失败（{type(exc).__name__}: {exc}）")
        quota = bool(missing) and res.result.Code == daq.Deny
        if not missing:
            note = ""
        elif quota:
            note = res.result.Message or f"授权数据点数已满（已用 {res.used} / 授权 {res.limit}）"
        else:
            note = (f"授权未满（已用 {res.used} / 授权 {res.limit or '不限'}），不是授权的原因 —— "
                    f"请查实时库日志")
        view = RegistrationView(datetime.now(timezone.utc), missing, res.limit, res.used, note)
        with self._lock:
            before = self._view.unregistered
            self._view = view
        if missing:
            names = "、".join(f"{r.domain}/{r.binding}/{r.key}" for r in rows if r.local_id in missing)
            # ★每次推完都吵（与 `log_degraded` 同理）：只吵一次的话，现场翻日志看到的是几小时前的一行。
            logger.warning("实时库有 %d 个结论点没登记上（%s）：%s。这些点的结论照常写入，但平台点表里没有、看不到%s",
                           len(missing), names, note,
                           "；授权扩容或腾出名额后，随下一次重推快照（最迟约 1 小时）登记上" if quota else "")
        elif before:
            logger.info("原先没登记上的 %d 个结论点已全部登记", len(before))
        return view

    def _keep(self, why: str) -> RegistrationView:
        """问不到：结论留着上一次的，只换说明。"""
        logger.warning("结论点登记核对：%s；沿用上一次的结论", why)
        with self._lock:
            v = self._view
            self._view = RegistrationView(v.checked_at, v.unregistered, v.limit, v.used, why)
            return self._view

    # ── 读 ────────────────────────────────────────────────────────────────
    @property
    def view(self) -> RegistrationView:
        with self._lock:
            return self._view

    def is_unregistered(self, local_id: int) -> bool:
        return local_id in self.view.unregistered
