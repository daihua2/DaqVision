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

**永不发 `op=IDENTITY`(5)**（AI-3 §3.1 定、AI-4 实跑验证）。那是网关的身份帧：
  发了，身份表里我方就是 `isGateway=true` + 无种类，与真网关逐位相同 ⇒ 会被建成"网关"实体。

自报身份走 **`op=SOURCE_IDENTITY`(7)**（`AI-2 §3.2` / `C-2 §4.2` 定案，hs `H-272` 于 1.9.515 落地），
  三道闸，缺一不发：
  ① **开关**：`HsConfig.source_identity` 为 None（缺省）就不发。何时打开由往来函定
     （`C-50 §4`：AICloud 消费侧上线并函告、`AI-61 §3` 演练通过之后），不由代码自己判断；
  ② **能力位** `identity-source-kind`：老引擎（< 1.9.515）收到 op 7 **静默忽略、回成功**
     （`H-272 §4.3`），不探就不知道自己白发了；
  ③ **`kind` 非空**：hs 对空 kind 的 op 7 拒收（`H-272 §4.3`）—— 空 kind 的行在读侧与网关逐位相同。
  放在**每一条新流的开头**：身份不落盘、hs 重启即丢，靠发方重报；我方每次推快照都是一条新流，
  于是 hs 重启后第一次重推就补回来了（`H-272 §4.2`：不老化，不必另配心跳）。

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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import grpc

from .hsproto import daqcontract_pb2 as daq
from .hsproto import historystore_pb2 as hs
from .pointmap import PointRow, container_name
from .quality import Quality
from .types import STATUS_ATTENTION, STATUS_DANGER, STATUS_WARNING, Finding

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

#: 我方自报的来源种类（`AI-61 §4` 定：小写、逐字、恒定，不随版本/主机/域数变）。
#: AICloud 只认"空 / 非空"决定建不建网关，具体值只用于显示 —— 但**改它就是改对外约定**，要发函。
#: ★`C-62 §3`（2026-10-04）起平台按**这个值**给「诊断模块 / 绑定」挂层名标签、关掉点名跳网关；
#:   不先函告就改，平台会退回按普通采集连接 / 通道显示。
SOURCE_KIND = "ai-service"

#: 对端支持 op 7 的能力位（`H-272 §1`）。
FEATURE_SOURCE_IDENTITY = "identity-source-kind"

# 类别族根（`EntityConfigPush.categoryRootId`，daqgate categoryRootSet 成员）。
# hs 拼归属链**只认 17（通道）/ 15（连接）**（`ancestry.h`），故域当连接、绑定当通道
# （`C-57`，2026-10-02 用户定「甲 + 族根 15/17」）。改它们就是改对外约定，要发函。
CATEGORY_POINT = 12
CATEGORY_LINK = 15      # 域
CATEGORY_CHANNEL = 17   # 绑定

#: 检测状态点的报警行：(词表词, 显示, 严重度)。严重度用实时库现成量表（OPC UA 1~1000），`AI-74 §4` 定；
#: 「正常」「停机」不点名 ⇒ 不命中即 NR（`H-282 §3`：只打一条日志、不进事件流）。
#: ★词表是契约（1.13），所有模块共用这一张表 —— 不是替模块发明语义。改严重度就是改对外约定，要发函。
STATUS_ALARM_ROWS: tuple[tuple[str, str, int], ...] = (
    (STATUS_ATTENTION, "注意", 300),     # Warn
    (STATUS_WARNING, "警告", 600),       # MeAlarm
    (STATUS_DANGER, "危险", 800),        # HiAlarm
)
#: 恢复态显示文本。空着的话界面上恢复那一行没有文字（`H-282 §3`）。
STATUS_ALARM_NR_LABEL = "正常"


@dataclass(frozen=True, slots=True)
class SourceIdentityInfo:
    """`SourceIdentity` 的内容。`kind` 不在这里 —— 它是常量 `SOURCE_KIND`，不给配置改的口子。"""

    name: str
    """显示名，`AI-61 §4` 定为 `AI 集成服务(<主机名>)`。平台日志与界面按它说话。"""

    des: str = ""
    """描述。**只作描述、不作判据**（`AI-61 §4`）。"""

    attrs: tuple[tuple[str, str], ...] = ()
    """补充属性，键名照 AICloud `C-1`：`hostName` / `version` / `commit` / `app`。**只作并排显示、不作分流**。"""


@dataclass(frozen=True, slots=True)
class HsConfig:
    read_addr: str = "127.0.0.1:5400"
    """明文回环，全量来源。**不带证书** —— 带了就会被降级成受限连接，读不到别人的点。"""

    write_addr: str = ""
    """mTLS 跨机口。空 = 未配置写路径（只读运行，结论不回流）。"""

    ca_file: Path | None = None
    cert_file: Path | None = None
    key_file: Path | None = None

    source_identity: SourceIdentityInfo | None = None
    """自报身份（op 7）。**None = 不发**，这是缺省。见模块头「三道闸」第 ①。"""

    def can_write(self) -> bool:
        return bool(self.write_addr and self.ca_file and self.cert_file and self.key_file)


# ── 纯函数：消息构造（不连网，可单测）──────────────────────────────────────

def value_type_to_varenum(value_type: str) -> int:
    if value_type not in _VARENUM:
        raise ValueError(f"未知值类型 {value_type!r}；只支持 {sorted(_VARENUM)}")
    return _VARENUM[value_type]


@dataclass(frozen=True, slots=True)
class SnapshotExtras:
    """快照里点行带不出来的东西：说明（`Paras.Des`）与报警配置（契约 1.15）。

    由调度按绑定与域声明算好交来 —— 本模块不认识绑定表与域声明，只管摆进实体。
    ★报警配置**每轮都要带**：整条去掉 `Alarm`（字段 14）会卸载该点的报警条件（`H-282 §4`）；
      内容相同的重推按字节比、不重置当前态，所以每轮无脑带上是安全的。
    """

    binding_des: dict[tuple[str, str], str] = field(default_factory=dict)
    """(域, 绑定) → 绑定实体的说明（显示名）。"""
    point_des: dict[int, str] = field(default_factory=dict)
    """localId → 点的说明。实时库报警描述取**点自己的** `Des`，不取绑定实体的（`H-282 §5`）。"""
    alarms: dict[int, daq.AlarmConfig] = field(default_factory=dict)
    """localId → 报警配置。"""


def build_status_alarm(choices, on_delay_sec: int = 0) -> daq.AlarmConfig | None:
    """检测状态点（`OutputSpec.role=status`）的状态报警（`AI-74 §5.2`）。本域不出任何报警档 ⇒ None。

    ★字符串状态行按**逐字节相等**命中（`H-282 §1`），Lo 与 Hi 写同一个词 —— 写成不同的词，
      后写的 Hi 会覆盖 Lo 且不报错。
    ★`on_delay_sec=0` 不写这一格：未设置与设为 0 在实时库等价（都是不去抖）。
    ★坏质量不经状态表、一律落 BAD（严重度 500、不去抖，`H-282 §2.1`）—— 这里管不着，由各域只在确实判不了时落码。
    """
    rows = [r for r in STATUS_ALARM_ROWS if r[0] in choices]
    if not rows:
        return None
    alarm = daq.AlarmConfig()
    st = alarm.State
    st.NRLabel = STATUS_ALARM_NR_LABEL
    for word, label, severity in rows:
        item = st.States.add(StateKey=word, StateLabel=label, StateSeverity=severity)
        item.StateLo.String = word
        item.StateHi.String = word
    if on_delay_sec:
        st.OnDelaySec = on_delay_sec
    return alarm


def build_entity(row: PointRow, *, description: str = "",
                 alarm: daq.AlarmConfig | None = None) -> daq.EntityConfig:
    """把一个结论点变成 hs 认得的实体配置。

    ★`CategoryId` 用 12（采集点）。真正让 hs 判定"这是采集标签点"的是 `Paras` 里的 `Acc`，
      不是类别号 —— 见 hs 侧 `parseEntity`。两处都给，是为了让点表里的类别列也对。
    ★`Name` 恒为稳定键，**不放显示名**：实时库 8086 查询口按点名逐字节精确匹配，
      报警事件的点名也取它（`H-282 §5`）。显示名只进 `Des`。
    """
    paras = {PARAS_ACC: value_type_to_varenum(row.value_type)}
    if row.unit:
        paras["Unit"] = row.unit
    if description:
        paras["Des"] = description
    ent = daq.EntityConfig(
        Id=row.local_id,
        Name=row.name,
        CategoryId=CATEGORY_POINT,
        # 点 → 它的绑定（hs 视作采集通道）。0 = 不挂，hs 那边链就是空的（`C-57` 报的那个态）。
        RelationId=row.binding_entity_id,
        Paras=json.dumps(paras, ensure_ascii=False),
        Version="1",
    )
    if alarm is not None:
        ent.Alarm.CopyFrom(alarm)
    return ent


def build_container_entities(rows: list[PointRow], *,
                             binding_des: dict[tuple[str, str], str] | None = None,
                             ) -> list[tuple[int, daq.EntityConfig]]:
    """由点行归纳出上级实体：每个域一个（族根 15），每个 (域, 绑定) 一个（族根 17）。

    → `[(categoryRootId, entity)]`，域在前、绑定在后，各按 localId 排。
    ★**不另读库**：上级号随点行带来（`PointMap.all()` 一次 JOIN 取齐），快照里的点与上级
      出自同一次读，不会出现"点指向一个本轮没推的上级"。没挂上级的行（号为 0）不产生实体。
    ★绑定实体 `ContainerId` = 域实体号：hs 由通道的 containerId 走到连接（`normalizedParentOf` ②）；
      域实体 `ContainerId` = 0，hs 由族根 15 合成最后一跳到我方身份 gid（同 ③）。
    ★`binding_des` 里有的绑定实体带 `Paras={"Des": 显示名}`（契约 1.15，`H-282 §5`）；没有的不带 `Paras`，
      与 1.14 逐字节相同。改 `Des` 只产生一次点表增量：号不变、历史不受影响。
    """
    binding_des = binding_des or {}
    domains: dict[int, str] = {}
    bindings: dict[int, tuple[str, str, int]] = {}
    for r in rows:
        if r.domain_entity_id:
            domains[r.domain_entity_id] = r.domain
        if r.binding_entity_id:
            bindings[r.binding_entity_id] = (r.domain, r.binding, r.domain_entity_id)
    out = [(CATEGORY_LINK, daq.EntityConfig(
        Id=i, Name=container_name(d), CategoryId=CATEGORY_LINK, Version="1"))
        for i, d in sorted(domains.items())]
    for i, (d, b, did) in sorted(bindings.items()):
        ent = daq.EntityConfig(
            Id=i, Name=container_name(d, b), CategoryId=CATEGORY_CHANNEL, ContainerId=did, Version="1")
        if binding_des.get((d, b)):
            ent.Paras = json.dumps({"Des": binding_des[(d, b)]}, ensure_ascii=False)
        out.append((CATEGORY_CHANNEL, ent))
    return out


def build_source_identity_frame(info: SourceIdentityInfo, *,
                                kind: str = SOURCE_KIND) -> hs.EntityConfigPush:
    """自报身份帧（op 7）。

    ★`kind` 空一律拒绝构造：hs 会拒收这一帧（`H-272 §4.3`），而更要紧的是它为什么拒 ——
      空 kind 的身份行在读侧与网关的 `IDENTITY` **逐位相同**，就是 `C-50 §4` 那台删不掉的网关。
      在我方这一侧先挡一道，不指望对端兜底。`kind` 形参只为了让这条能被单测到，调用方不传。
    """
    if not kind.strip():
        raise ValueError("SOURCE_IDENTITY 的 kind 不能为空 —— 空 kind 在读侧与网关逐位相同")
    if not info.name.strip():
        raise ValueError("SOURCE_IDENTITY 的 name 不能为空 —— 平台日志要按它说出我方是谁")
    return hs.EntityConfigPush(
        op=hs.EntityConfigPush.SOURCE_IDENTITY,
        source=hs.SourceIdentity(name=info.name, des=info.des, kind=kind,
                                 attrs=dict(info.attrs)))


def build_snapshot_frames(rows: list[PointRow], *,
                          identity: SourceIdentityInfo | None = None,
                          extras: SnapshotExtras | None = None) -> list[hs.EntityConfigPush]:
    """一整轮快照的帧序列（一条流的全部内容）。

    ★**必须全量**：`SNAPSHOT_END` 是原子提交，**本轮未出现的旧实体一律删除** ——
      漏发一个就是删一个。所以这里接的是 `PointMap.all()`，不是"变化的那些"。
    ★**永不含 `op=IDENTITY`(5)**，见模块头。
    ★`identity` 给了就在**最前面**放一帧 op 7：它不属于快照事务（`H-272 §4.1`），
      放在 `SNAPSHOT_BEGIN` 之前，流断在快照中途时身份也已经送到了。
      要不要给由调用方决定（开关 + 能力位），这里只管摆放。
    ★上级实体（域、绑定）在点之前，同属这一份快照 —— 漏推上级同样是删上级。
    ★每帧 PUT 都带 `categoryRootId`（present）：hs 按它判这一跳是通道还是连接，
      AICloud 按 presence 决定要不要退回自己的判据（`historystore.proto` 该字段注释）。
    """
    extras = extras or SnapshotExtras()
    frames = [build_source_identity_frame(identity)] if identity is not None else []
    frames.append(hs.EntityConfigPush(op=hs.EntityConfigPush.SNAPSHOT_BEGIN))
    for root, ent in build_container_entities(rows, binding_des=extras.binding_des):
        frames.append(hs.EntityConfigPush(
            op=hs.EntityConfigPush.PUT, id=ent.Id, entity=ent, categoryRootId=root))
    for row in rows:
        ent = build_entity(row, description=extras.point_des.get(row.local_id, ""),
                           alarm=extras.alarms.get(row.local_id))
        frames.append(hs.EntityConfigPush(
            op=hs.EntityConfigPush.PUT, id=row.local_id, entity=ent,
            categoryRootId=CATEGORY_POINT))
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

    def features(self) -> frozenset[str]:
        """对端**行为能力位**全集（`PingRes.features`，historystore `H-247 §5` 起放上 `Ping`）。

        ★**缺失 = 未知/不支持，不是否定** —— 这是 hs 契约反复强调的一条：
          老引擎不认识这个字段，proto3 静默丢 ⇒ 回空集合。空集合要当作
          "这台引擎没有这些能力"来走降级路径，**不能当作"探不到所以就当有"**。

        ★为什么非探不可（`H-246 §2.4`）：老引擎不认识 `StructValue`，会把它
          **存成"无值"且回成功** —— 静默丢数据，写入侧一点异常都看不到。
          能力位是这一类的唯一防线。
        """
        try:
            return frozenset(self.ping().features or ())
        except Exception:                       # 探不到交给调用方的退避，不在这里吞成"没有"
            raise

    def has_feature(self, name: str) -> bool:
        """对端是否具备某个能力位。探不到（连不上）**一律回 False**，按"不支持"走。

        ★这里与 `features()` 的区别是有意的：`features()` 让调用方分得清
          "连不上"与"连上了但没有这一位"；`has_feature` 是给"要不要启用某条路"用的，
          那条路上**两者的处置相同 —— 都不启用**，所以在这里收口，省得每个调用点都写一遍。
        """
        try:
            return name in self.features()
        except Exception:                       # noqa: BLE001
            logger.warning("探能力位 %s 失败（按不支持处理）", name, exc_info=True)
            return False

    # ── 结构注册表（非标量定稿 §2.1；能力位 struct-registry / struct-value）────
    def put_struct(self, struct_def, allow_new_version: bool = False):
        """注册结构 / 发新版本。★`allow_new_version` **缺省 False**，这是有意的。

        带上它，"改错了一个字段"会**悄悄变成一个新版本**、版本号还会被刷成启动次数
        （daqgate `D-242 §7.1` 点名要避免的正是这个）。⇒ 同名不同描述一律当场报错。
        同名**相同**描述恒为幂等成功，所以每次启动无脑注册是安全的。
        """
        req = hs.PutStructReq(allowNewVersion=allow_new_version)
        # ★契约里这个字段字面就叫 `def`（Python 关键字）⇒ 只能 getattr 取，没有 `def_` 别名。
        getattr(req, "def").CopyFrom(struct_def)
        return self._unary(self._write_channel(), "PutStruct", req, hs.PutStructRes)

    def get_struct(self, *, name: str = "", struct_id: int = 0, version: int = 0,
                   with_descriptor: bool = False):
        """取结构描述。`version=0` = 最新版。

        ★`with_descriptor=True` 时另回 `FileDescriptorProto` —— 我方据它动态解码，
          **不写死字段号**（字段号是 hs 分配的，写死等于假定它永远不变）。
        """
        req = hs.GetStructReq(version=version, withDescriptor=with_descriptor)
        if name:
            req.name = name
        else:
            req.id = struct_id
        # 读口对所有连接开放；用读通道（worker 只读直连也走得通）。
        ch = self._read_channel() if not self._cfg.can_write() else self._write_channel()
        return self._unary(ch, "GetStruct", req, hs.GetStructRes)

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
    def source_identity_to_send(self) -> SourceIdentityInfo | None:
        """这一条流要不要带自报身份。模块头「三道闸」的 ① ② 在这里判（③ 在构造帧时判）。

        ★开关关着就**不探能力位**：探一次是一次 `Ping`，关着的时候没有理由多打对端。
        ★能力位探不到（连不上）按"没有"处理 —— `has_feature` 已收口；紧接着推快照也会失败，
          那一路有自己的退避与告警，这里不重复吵。
        """
        info = self._cfg.source_identity
        if info is None:
            return None
        if not self.has_feature(FEATURE_SOURCE_IDENTITY):
            # ★每次推快照都打（兜底一小时一次，不密）：开关开着却发不出去，是现场要知道的事 ——
            #   典型原因是实时库还没升到 1.9.515，此时发了也会被静默忽略（H-272 §4.3）。
            logger.warning("自报身份已开启，但对端没有能力位 %s（实时库 < 1.9.515？）"
                           "—— 本次不发 SOURCE_IDENTITY，平台仍认不出我方是谁",
                           FEATURE_SOURCE_IDENTITY)
            return None
        return info

    def snapshot_frames(self, rows: list[PointRow], *,
                        extras: SnapshotExtras | None = None) -> list[hs.EntityConfigPush]:
        """本次推送的完整帧序列：闸门判完的身份帧（可能没有）+ 全量快照。"""
        return build_snapshot_frames(rows, identity=self.source_identity_to_send(), extras=extras)

    def push_snapshot(self, rows: list[PointRow], timeout: float = 60.0, *,
                      extras: SnapshotExtras | None = None) -> int:
        """把**全部**结论点作为一份快照推上去。返回被接收的帧数。

        串行化：同一 guid 同一时刻只允许一条流（并发会互相踢，见模块头）。
        """
        frames = self.snapshot_frames(rows, extras=extras)
        with self._push_lock:
            res = self._write_channel().stream_unary(
                f"{SERVICE}/PushEntityConfigs",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=hs.PushEntityConfigsRes.FromString,
            )(iter(frames), timeout=timeout)
        with_identity = bool(frames) and frames[0].op == hs.EntityConfigPush.SOURCE_IDENTITY
        logger.info("结论点快照已推送：%d 个点（另 %d 个上级实体，%d 个点带报警配置），accepted=%d%s",
                    len(rows), sum(f.op == hs.EntityConfigPush.PUT for f in frames) - len(rows),
                    sum(f.op == hs.EntityConfigPush.PUT and f.entity.HasField("Alarm") for f in frames),
                    res.accepted,
                    "（含自报身份 SOURCE_IDENTITY）" if with_identity else "")
        return res.accepted

    def post_vqt(self, items: list[tuple[int, Finding]], timeout: float = 15.0) -> daq.Status:
        """写结论。`items` 是 `(localId, Finding)` 对。"""
        vqts = daq.VQTs(VQTs=[build_vqt(lid, f) for lid, f in items])
        return self._unary(self._write_channel(), "PostVQT", vqts, daq.Status, timeout=timeout)

    def check_point_quota(self, *, new_count: int = 0, local_ids: list[int] | tuple[int, ...] = (),
                          timeout: float = 10.0) -> hs.CheckPointQuotaRes | None:
        """授权数据点数预检（hs `CheckPointQuota`，能力位 `point-quota`）。老引擎没有这个口 → None。

        ★**必须走写连接**：受限连接按证书里的 guid 算，问的才是**我方**的点（`H-294 §2`）。
          读连接是明文回环全量连接，`guid` 留空会被算成实时库自己的 `--self-guid` —— 不报错，只是答非所问。
        ★`newPoints` 回答「这些点号里有几个还没作为存储点登记」，`result` 回答「再加这么多会不会超」，
          两件事别混（`H-294 §2`）。快照提交是同步的（`SNAPSHOT_END` 在推流的 RPC 线程里提交），
          推完紧接着问不会误报。
        """
        req = hs.CheckPointQuotaReq(newCount=new_count, localIds=list(local_ids))
        try:
            return self._unary(self._write_channel(), "CheckPointQuota", req,
                               hs.CheckPointQuotaRes, timeout=timeout)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                return None
            raise

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
