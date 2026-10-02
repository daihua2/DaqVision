"""取数 —— 从 hs 拉一个时间窗的点值，成帧交给模块。

走**明文回环全量连接**，用 **globalId**（见 `hsclient` 模块头「两个 id 空间」）。

三条不肯让步的：

1. **质量码原样带下来**。上游是坏值就标坏值，绝不"过滤掉坏点让曲线好看" ——
   那是把"设备坏了"伪装成"设备正常但采样稀疏"。模块自己决定坏点怎么处置。
2. **时刻用样本自己的**，不是取数时刻。窗口只决定"取哪一段"，不决定"这笔数据是几点的"。
3. **取不到就是取不到**。空窗口如实成一帧空 `Frame`，让模块落 `NO_INPUT` 质量码；
   **不拿上一帧顶替**（那会让"断了一小时"看起来像"值一直没变"）。

**按字段绑定**（契约 1.11，定义文档 §10.3）：绑到结构值点字段的角色，骨架把每条结构值**按字段拆开**，
交给域的仍是按角色排的普通 `Sample` 序列 —— 同一条值拆出的几路 T、Q 必然一致，**域不知道数据来自结构值**。
  · 每条的 T 用该条结构值自己的，Q 用整条的（结构值的质量码管整条）；
  · 这一版结构里没有该字段 / 描述取不到 / 根本不是结构值 ⇒ **该条落坏样本**（值空、`INPUT_BAD`），
    不跳过（跳过就成了"这段采样稀疏"）、不猜；同一原因每路只警告一次；
  · 请求带 `supportsStructValue`（定案 P5）：不带的话 hs 把结构值降级成 `NullValue + -1035`，
    整条路看着像"上游全是坏值"。
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
from .structreg import StructRegistry, StructRegistryError
from .types import Frame, Sample, StructSample

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


def _bad(v: daq.VQT) -> Sample:
    """这一条**有**，但这一路用不了 —— 值空、`INPUT_BAD`，时刻与原始码照搬（排查时看得到上游原样）。"""
    return Sample(t=v.TimStampUtc.ToDatetime().replace(tzinfo=timezone.utc), value=None,
                  quality=Quality.INPUT_BAD, status_code=int(v.Quality.Code))


class Fetcher:
    """按绑定取一个窗口的数据。"""

    def __init__(self, client: HsClient, structs: StructRegistry | None = None) -> None:
        self._client = client
        # 结构值描述与解码。没接 ⇒ 字段角色逐条落坏样本并说清（不是"没数据"）。
        self._structs = structs
        self._warned: set[tuple] = set()

    def _warn_once(self, key: tuple, msg: str, *args) -> None:
        # ★同一原因每路只警告一次：一帧几万条同版本的值，逐条打会把日志淹掉，
        #   而"这一路落了坏样本"本身已经由质量码带到结论上。
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(msg, *args)

    def fetch(self, b: Binding, end_time: datetime,
              artifacts: dict | None = None) -> Frame:
        """取 `[end - window, end]` 这一段，成一帧。

        ★`end_time` 由调度器给，且**通常是"上一个整节拍"而不是 `now()`** ——
        取到 `now()` 的窗口右端多半是空的（数据还没到），那不是缺数据，是取早了。
        """
        start = end_time - timedelta(seconds=b.window_sec)
        # 去重：按字段绑定时 x/y/z 三路是同一个点，请求里只要一份。
        gids = sorted(set(b.roles.values()))
        by_gid: dict[int, list[daq.VQT]] = {}
        if gids:
            # ★结构值点一律 `kRawData` 按时间范围（`H-263`：保持族对结构值不适用）。
            req = hs.HisDataQueryReq(supportsStructValue=True)
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
        decoded: dict[int, list[StructSample | str]] = {}
        for role, gid in b.roles.items():
            vqts = by_gid.get(gid, [])
            if not vqts:
                empty_roles.append(role)
            # ★空的也放进去（空列表），不是"这一路不存在" —— 两者对模块的含义不同：
            #   不在字典里 = 这一路没绑；在字典里但为空 = 绑了但这段没数据。
            fname = b.fields.get(role)
            if fname is None:
                channels[role] = [self._scalar(b, role, v) for v in vqts]
            else:
                if gid not in decoded:   # 同一个点的几路只解一遍
                    decoded[gid] = [self._decode(v) for v in vqts]
                channels[role] = [self._field(b, role, fname, v, d)
                                  for v, d in zip(vqts, decoded[gid])]

        if empty_roles:
            # 每个周期都报，不做"只记一次"限流：取不到数是决定结论可信度的降级态。
            logger.warning(
                "取数为空：%s/%s 的 %d/%d 路在 [%s, %s] 内没有样本（角色 %s）；"
                "本帧的结论会带坏质量码",
                b.domain, b.binding, len(empty_roles), len(b.roles),
                start.isoformat(), end_time.isoformat(), empty_roles,
            )

        return Frame(domain=b.domain, binding=b.binding,
                     t_start=start, t_end=end_time, channels=channels,
                     # 台账参数照抄，**不解释、不填缺省** —— 缺什么由模块自己发现并落码。
                     params=dict(b.params),
                     # 当前启用的工件（推理路径才给；**训练路径不给** —— 训的时候拿旧模型
                     # 当输入，就成了模型喂自己，漂了也看不出来）。
                     artifacts=dict(artifacts or {}))

    # ── 逐条 ──────────────────────────────────────────────────────────────
    def _scalar(self, b: Binding, role: str, v: daq.VQT) -> Sample:
        if v.WhichOneof("Value") == "StructVal":
            # ★整点绑到了结构值点：别把一个 protobuf 对象塞进 `Sample.value` 交给域。
            self._warn_once((b.domain, b.binding, role, "struct-on-scalar"),
                            "%s/%s 角色 %s 绑的是整点，但该点写的是结构值 —— 逐条落坏样本；"
                            "请改为按字段绑定", b.domain, b.binding, role)
            return _bad(v)
        return vqt_to_sample(v)

    def _decode(self, v: daq.VQT) -> StructSample | str:
        """一条结构值 → `StructSample`；不是结构值或解不了 ⇒ 回原因（字符串）。"""
        which = v.WhichOneof("Value")
        if which != "StructVal":
            return f"not-struct:{which}"
        if self._structs is None:
            return "no-registry"
        q = Quality.from_status_code(int(v.Quality.Code))
        t = v.TimStampUtc.ToDatetime().replace(tzinfo=timezone.utc)
        try:
            return self._structs.decode_value(v.StructVal, t, q)
        except StructRegistryError as exc:
            return f"decode:{v.StructVal.StructId}/{v.StructVal.StructVersion}:{exc}"

    def _field(self, b: Binding, role: str, fname: str, v: daq.VQT,
               d: StructSample | str) -> Sample:
        if isinstance(d, StructSample):
            if fname in d.fields:
                val = d.fields[fname]
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    return Sample(t=d.t, value=val, quality=d.quality,
                                  status_code=int(v.Quality.Code))
                reason = f"字段 {fname} 在 {d.struct_name} v{d.struct_version} 里不是数值标量"
            else:
                # ★采集侧发了新版本、删了这个字段：如实落坏，**不猜**换成哪个字段。
                reason = f"{d.struct_name} v{d.struct_version} 里没有字段 {fname}"
            self._warn_once((b.domain, b.binding, role, d.struct_name, d.struct_version),
                            "%s/%s 角色 %s：%s —— 这一版的值逐条落坏样本",
                            b.domain, b.binding, role, reason)
            return _bad(v)
        if d.startswith("not-struct:"):
            which = d.split(":", 1)[1]
            if which in ("None", "NullValue", "EmptyValue"):
                return vqt_to_sample(v)   # 坏值锚点：原样带下来（它本来就是坏的，码也是上游的）
            self._warn_once((b.domain, b.binding, role, "scalar-on-field"),
                            "%s/%s 角色 %s 按字段 %s 绑定，但该点写的不是结构值（%s）—— 逐条落坏样本",
                            b.domain, b.binding, role, fname, which)
        elif d == "no-registry":
            self._warn_once((b.domain, b.binding, role, "no-registry"),
                            "%s/%s 角色 %s 按字段绑定，但取数没接结构描述 —— 逐条落坏样本（骨架接线缺陷）",
                            b.domain, b.binding, role)
        else:
            self._warn_once((b.domain, b.binding, role, d.split(":", 2)[1]),
                            "%s/%s 角色 %s：结构值解不了（%s）—— 这一版的值逐条落坏样本",
                            b.domain, b.binding, role, d.split(":", 2)[2])
        return _bad(v)
