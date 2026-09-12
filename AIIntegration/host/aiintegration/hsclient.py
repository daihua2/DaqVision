"""historystore 客户端 —— 骨架八件里的"身份与连接"与"结论回流"。

★★ **只有骨架进程能用本模块。** worker 不持证书、不连 hs、不知道 hs 存在。

  为什么（AICloud C-9 §3 提问触发，答案在 AI-10）：hs 已钉死三条语义 ——
  ② 同一 guid 并发两条流**后到者胜**，先到的被主动 CANCEL；
  ③ `SNAPSHOT_BEGIN…END` 之间断流 = **半份快照**被丢弃，重连必须重推全量；
  `SNAPSHOT_END` = **原子提交，本轮未出现的旧实体一律删除**。
  ⇒ 两个 worker 各推各的，会**互相删点且循环**：B 建流踢掉 A → A 半份快照被丢 →
    B 提交时把 A 那批点全删 → A 重推轮到 B 被踢。
  这不是 hs 的缺陷：那三条是为"**一个 guid = 一个配置源**"设计的。

两条连接，各干各的（AI-1 §2 定，实测见 AI-4）：

  · **读**：明文回环 `127.0.0.1:5400` —— 无 cert guid ⇒ **全量来源**，看得见被诊断设备的点；
  · **写**：mTLS 跨机口 —— 带 cert guid ⇒ **受限段**，只动得了自己那些点。

  两个口在现网是**同时监听**的（`gRPC listening: 127.0.0.1:5400(明文) + …(mTLS)`），
  所以这条路不需要任何一方改代码或重启。

过渡期：**不发 `op=IDENTITY`**（AI-3 §3.1 定、AI-4 实跑验证）。
  身份表里没有我方的行 ⇒ AICloud 对账器的 `HasIdentity=false` ⇒ 不会被建成"网关"实体。

★★ **两个 id 空间 —— 用错了不报错，只是查不到**（2026-09-10 隔离实例实测，不是推断）：

  | 连接 | `PostVQT` 投递 | `QueryHistory` 查询 |
  | --- | --- | --- |
  | **受限**（mTLS，带 cert guid） | **localId** | **localId** |
  | **全量**（明文回环，无 guid） | —— | **globalId** |

  实测：同一批数据，受限连接用 gid 查回 **0 条**、用 localId 查回全部；
  全量回环连接用 gid 查回全部。**两种错法都返回 `Code=1` 成功、0 条数据** ——
  与"这段时间确实没数据"一模一样，这正是它危险的地方。

  ⇒ 本模块的规矩：**受限连接一律 localId，全量连接一律 globalId**，
    `lookup_global()` 只用来把 localId 翻给**别人**（比如告诉 AICloud 该查哪个号），
    我方自己**从不**拿 gid 去受限连接上查。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import grpc

from .hsproto import daqcontract_pb2 as daq
from .hsproto import historystore_pb2 as hs
from .pointmap import PointRow
from .quality import Quality
from .types import Finding

logger = logging.getLogger(__name__)

SERVICE = "/HistoryStore.HistoryStoreGRPCService"

#: 连不上时的重试间隔（秒）。固定间隔即天然节流 —— 降级态 WARN 每个周期都打，
#: **不做"只记一次"限流**：连不上实时库是决定可用性的事，不担心偏密。
RETRY_INTERVAL_SEC = 10.0

#: 采集点的判别键。hs 侧 `parseEntity` 认的就是 Paras 里有没有 `Acc`；
#: 它的值同时是 daqgate 的 VARENUM 值类型。**不是我方发明的约定，勿改。**
PARAS_ACC = "Acc"

# daqgate VARENUM（取 hs 侧 `Acc` 的取值口径）
_VARENUM = {"bool": 11, "int": 3, "float": 5, "string": 8}


@dataclass(frozen=True, slots=True)
class HsConfig:
    read_addr: str = "127.0.0.1:5400"
    """明文回环，全量来源。**不带证书** —— 带了就会被降级成受限连接，读不到别人的点。"""

    write_addr: str = ""
    """mTLS 跨机口。空 = 未配置写路径（只读运行，结论不回流）。"""

    ca_file: Path | None = None
    cert_file: Path | None = None
    key_file: Path | None = None

    def can_write(self) -> bool:
        return bool(self.write_addr and self.ca_file and self.cert_file and self.key_file)


# ── 纯函数：消息构造（不连网，可单测）──────────────────────────────────────

def value_type_to_varenum(value_type: str) -> int:
    if value_type not in _VARENUM:
        raise ValueError(f"未知值类型 {value_type!r}；只支持 {sorted(_VARENUM)}")
    return _VARENUM[value_type]


def build_entity(row: PointRow, *, description: str = "") -> daq.EntityConfig:
    """把一个结论点变成 hs 认得的实体配置。

    ★`CategoryId` 用 12（采集点）。真正让 hs 判定"这是采集标签点"的是 `Paras` 里的 `Acc`，
      不是类别号 —— 见 hs 侧 `parseEntity`。两处都给，是为了让点表里的类别列也对。
    """
    paras = {PARAS_ACC: value_type_to_varenum(row.value_type)}
    if row.unit:
        paras["Unit"] = row.unit
    if description:
        paras["Des"] = description
    return daq.EntityConfig(
        Id=row.local_id,
        Name=row.name,
        CategoryId=12,
        Paras=json.dumps(paras, ensure_ascii=False),
        Version="1",
    )


def build_snapshot_frames(rows: list[PointRow]) -> list[hs.EntityConfigPush]:
    """一整轮快照的帧序列。

    ★**必须全量**：`SNAPSHOT_END` 是原子提交，**本轮未出现的旧实体一律删除** ——
      漏发一个就是删一个。所以这里接的是 `PointMap.all()`，不是"变化的那些"。
    ★**不含 `op=IDENTITY`**：过渡期定案，见模块头。
    """
    frames = [hs.EntityConfigPush(op=hs.EntityConfigPush.SNAPSHOT_BEGIN)]
    for row in rows:
        frames.append(hs.EntityConfigPush(
            op=hs.EntityConfigPush.PUT, id=row.local_id, entity=build_entity(row)))
    frames.append(hs.EntityConfigPush(op=hs.EntityConfigPush.SNAPSHOT_END))
    return frames


def build_vqt(local_id: int, finding: Finding) -> daq.VQT:
    """结论 → VQT。**V/Q/T 三者都来自 `Finding`，一个都不在这里现编。**

    · `TagId` = **localId** —— 受限连接上一律用本源的 localId，见模块头「两个 id 空间」；
    · `Quality` = 结论自己的质量码，不是恒 Ok；
    · `TimStampUtc` = **结论所依据的数据时刻**，不是 `now()`。
      在这里填 `now()` 就是 AICloud C-9 §2.3 说的那种伪造：图上会显示一个不属于那个值的时刻，
      而且看不出来。
    """
    v = daq.VQT(TagId=local_id)
    v.Quality.Code = finding.quality.to_status_code()
    v.TimStampUtc.FromDatetime(finding.t)

    val = finding.value
    if val is None:
        # 坏值锚点：有质量、有时刻、没有值。**不是不发** —— 不发等于"这段没数据"，
        # 而真相是"这段算不出来"，两者对下游的含义不同。
        v.NullValue = True
    elif isinstance(val, bool):
        v.Bool = val
    elif isinstance(val, int):
        v.I4 = val
    elif isinstance(val, float):
        if not math.isfinite(val):  # 类型层已挡过一道，这里是最后一道
            raise ValueError(f"拒绝把非有限值写进实时库: {local_id} = {val!r}")
        v.R8 = val
    elif isinstance(val, str):
        v.String = val
    else:
        raise TypeError(f"不支持的结论值类型 {type(val).__name__}")
    return v


# ── 连接 ──────────────────────────────────────────────────────────────────

def _channel_options() -> list[tuple[str, object]]:
    return [
        # ★必须关代理：gRPC 会认 http_proxy 环境变量，于是连跨机口恒 DEADLINE_EXCEEDED，
        #   而 openssl 握手完全正常 —— 必然往证书方向查，全查错，引擎侧日志一个字都没有。
        #   回环目标不受影响，所以"回环通、跨机不通"这个组合极具误导性。踩过，见 AI-4 §4。
        ("grpc.enable_http_proxy", 0),
        ("grpc.max_receive_message_length", 64 * 1024 * 1024),
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
    ]


class HsClient:
    """对 hs 的**唯一**出口。骨架单进程持有它；串行化所有配置流。"""

    def __init__(self, cfg: HsConfig) -> None:
        self._cfg = cfg
        self._read_ch: grpc.Channel | None = None
        self._write_ch: grpc.Channel | None = None
        self._push_lock = threading.Lock()   # 保证同一时刻只有一条 PushEntityConfigs
        self._last_degrade_log = 0.0

    # ── 通道 ──────────────────────────────────────────────────────────────
    def _read_channel(self) -> grpc.Channel:
        if self._read_ch is None:
            self._read_ch = grpc.insecure_channel(self._cfg.read_addr, options=_channel_options())
        return self._read_ch

    def _write_channel(self) -> grpc.Channel:
        if not self._cfg.can_write():
            raise RuntimeError("未配置写路径（need write_addr + ca/cert/key）")
        if self._write_ch is None:
            creds = grpc.ssl_channel_credentials(
                root_certificates=Path(self._cfg.ca_file).read_bytes(),
                private_key=Path(self._cfg.key_file).read_bytes(),
                certificate_chain=Path(self._cfg.cert_file).read_bytes(),
            )
            self._write_ch = grpc.secure_channel(
                self._cfg.write_addr, creds, options=_channel_options())
        return self._write_ch

    def close(self) -> None:
        for ch in (self._read_ch, self._write_ch):
            if ch is not None:
                ch.close()
        self._read_ch = self._write_ch = None

    # ── 调用 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _unary(ch: grpc.Channel, method: str, req, resp_cls, timeout: float = 15.0):
        return ch.unary_unary(
            f"{SERVICE}/{method}",
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=resp_cls.FromString,
        )(req, timeout=timeout)

    def instance_id(self) -> str:
        """对端**进程实例**的身份（`PingRes.instanceId`，实时库 1.9.441 起）。

        ★用途：引擎重启后我方的**实体配置快照失效**（点定义没了），而**值照样写得进** ——
          写入不失败、`LookupGlobal` 也答不了（映射仍在，丢的只是实体配置）。
          于是"该不该重推"只能靠这一格：**记住它，变了就重推全量快照**。
        ★老引擎回**空串**（proto3 未知字段静默丢）⇒ 调用方按「缺失即不支持」退回定期重推，
          **别把空串当成"实例变了"** —— 那会变成每拍都推。
        """
        try:
            return self.ping().instanceId or ""
        except Exception:                       # 探活失败交给调用方的退避，不在这里吞
            raise

    def ping(self) -> hs.PingRes:
        """探活。★受限连接**不能**用 `GetServerStatus`（那是全量门，必被拒），用 `Ping`。"""
        ch = self._write_channel() if self._cfg.can_write() else self._read_channel()
        return self._unary(ch, "Ping", hs.PingReq(), hs.PingRes)

    def lookup_global(self, local_ids: list[int]) -> list[int]:
        """localId → globalId。**0 = 尚未分配**（点刚建、还没推过 VQT）。

        ★拿到 0 只能判"查不到历史"，**绝不能拿 0 去查、更不能拿 localId 兜底查** ——
        那会静默查到别人的点（hs 契约原文点名的坑）。
        """
        res = self._unary(self._write_channel(),
                          "LookupGlobal", hs.LookupGlobalReq(localIds=local_ids),
                          hs.LookupGlobalRes)
        return list(res.globalIds)

    # ── 结论回流 ──────────────────────────────────────────────────────────
    def push_snapshot(self, rows: list[PointRow], timeout: float = 60.0) -> int:
        """把**全部**结论点作为一份快照推上去。返回被接收的帧数。

        串行化：同一 guid 同一时刻只允许一条流（并发会互相踢，见模块头）。
        """
        frames = build_snapshot_frames(rows)
        with self._push_lock:
            res = self._write_channel().stream_unary(
                f"{SERVICE}/PushEntityConfigs",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=hs.PushEntityConfigsRes.FromString,
            )(iter(frames), timeout=timeout)
        logger.info("结论点快照已推送：%d 个点，accepted=%d", len(rows), res.accepted)
        return res.accepted

    def post_vqt(self, items: list[tuple[int, Finding]], timeout: float = 15.0) -> daq.Status:
        """写结论。`items` 是 `(localId, Finding)` 对。"""
        vqts = daq.VQTs(VQTs=[build_vqt(lid, f) for lid, f in items])
        return self._unary(self._write_channel(), "PostVQT", vqts, daq.Status, timeout=timeout)

    # ── 降级态 ────────────────────────────────────────────────────────────
    def log_degraded(self, what: str, exc: BaseException) -> None:
        """连不上时的告警。

        ★**每个重试周期都打**，不做"只记一次"的限流 —— 连不上实时库是决定功能可用性的
        降级态，不担心偏密；固定重试间隔本身就是天然节流。
        "只记一次"的后果是：现场翻日志时看到的是几小时前的一行，无法判断"现在还断着没有"。
        """
        now = time.monotonic()
        if now - self._last_degrade_log >= RETRY_INTERVAL_SEC - 0.5:
            self._last_degrade_log = now
            logger.warning("实时库不可用（%s）：%s；%.0fs 后重试。"
                           "此期间结论不回流，平台侧看不到 AI 结果。",
                           what, exc, RETRY_INTERVAL_SEC)

    # ── 回读（自检用；正经的历史查询归 AICloud 走它自己的路）────────────────
    def query_history_raw(self, local_ids: list[int], beg: datetime, end: datetime,
                          timeout: float = 20.0) -> dict[int, list[daq.VQT]]:
        """按 **localId** 查原始点（受限连接）。见模块头「两个 id 空间」。

        ★用 gid 查这条连接会**成功返回 0 条**，与"这段确实没数据"不可区分 —— 别混。
        """
        req = hs.HisDataQueryReq()
        req.hisReq.method = hs.kRawData
        req.hisReq.begTime.FromDatetime(beg)
        req.hisReq.endTime.FromDatetime(end)
        for lid in local_ids:
            req.pointsReq.add().id = lid
        res = self._unary(self._write_channel(), "QueryHistory", req,
                          hs.VQTArrayRes, timeout=timeout)
        return {arr.TagId: list(arr.VQTs) for arr in res.VQTs}
