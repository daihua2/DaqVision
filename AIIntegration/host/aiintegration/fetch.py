"""取数 —— 从 hs 拉一个时间窗的点值，成帧交给模块。

走**明文回环全量连接**，用 **globalId**（见 `hsclient` 模块头「两个 id 空间」）。

三条不肯让步的：

1. **质量码原样带下来**。上游是坏值就标坏值，绝不"过滤掉坏点让曲线好看" ——
   那是把"设备坏了"伪装成"设备正常但采样稀疏"。模块自己决定坏点怎么处置。
2. **时刻用样本自己的**，不是取数时刻。窗口只决定"取哪一段"，不决定"这笔数据是几点的"。
3. **取不到就是取不到**。空窗口如实成一帧空 `Frame`，让模块落 `NO_INPUT` 质量码；
   **不拿上一帧顶替**（那会让"断了一小时"看起来像"值一直没变"）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .bindings import Binding
from .hsclient import HsClient
from .hsproto import daqcontract_pb2 as daq
from .hsproto import historystore_pb2 as hs
from .quality import Quality
from .types import Frame, Sample

logger = logging.getLogger(__name__)


def vqt_to_sample(v: daq.VQT) -> Sample:
    """VQT → Sample。**质量与时刻原样搬**，值按 oneof 取。"""
    which = v.WhichOneof("Value")
    if which in (None, "NullValue", "EmptyValue"):
        value = None
    else:
        value = getattr(v, which)
        if which == "DateTime":
            value = value.ToDatetime().isoformat()
    code = int(v.Quality.Code)
    return Sample(
        t=v.TimStampUtc.ToDatetime().replace(tzinfo=timezone.utc),
        value=value,
        quality=Quality.from_status_code(code),
        status_code=code,
    )


class Fetcher:
    """按绑定取一个窗口的数据。"""

    def __init__(self, client: HsClient) -> None:
        self._client = client

    def fetch(self, b: Binding, end_time: datetime) -> Frame:
        """取 `[end - window, end]` 这一段，成一帧。

        ★`end_time` 由调度器给，且**通常是"上一个整节拍"而不是 `now()`** ——
        取到 `now()` 的窗口右端多半是空的（数据还没到），那不是缺数据，是取早了。
        """
        start = end_time - timedelta(seconds=b.window_sec)
        gids = list(b.roles.values())
        by_gid: dict[int, list[daq.VQT]] = {}
        if gids:
            req = hs.HisDataQueryReq()
            req.hisReq.method = hs.kRawData
            req.hisReq.begTime.FromDatetime(start)
            req.hisReq.endTime.FromDatetime(end_time)
            for gid in gids:
                req.pointsReq.add().id = gid
            res = self._client._unary(              # noqa: SLF001 —— 同包内，刻意复用
                self._client._read_channel(), "QueryHistory", req, hs.VQTArrayRes, timeout=30.0)
            by_gid = {arr.TagId: list(arr.VQTs) for arr in res.VQTs}

        channels: dict[str, Sequence[Sample]] = {}
        empty_roles: list[str] = []
        for role, gid in b.roles.items():
            vqts = by_gid.get(gid, [])
            if not vqts:
                empty_roles.append(role)
            # ★空的也放进去（空列表），不是"这一路不存在" —— 两者对模块的含义不同：
            #   不在字典里 = 这一路没绑；在字典里但为空 = 绑了但这段没数据。
            channels[role] = [vqt_to_sample(v) for v in vqts]

        if empty_roles:
            # 每个周期都报，不做"只记一次"限流：取不到数是决定结论可信度的降级态。
            logger.warning(
                "取数为空：%s/%s 的 %d/%d 路在 [%s, %s] 内没有样本（角色 %s）；"
                "本帧的结论会带坏质量码",
                b.domain, b.binding, len(empty_roles), len(b.roles),
                start.isoformat(), end_time.isoformat(), empty_roles,
            )

        return Frame(domain=b.domain, binding=b.binding,
                     t_start=start, t_end=end_time, channels=channels)
