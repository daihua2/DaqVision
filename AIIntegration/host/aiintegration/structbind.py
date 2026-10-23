"""按字段绑定 —— 绑定时核对，与「这个结构值点有哪些字段」的描述（契约 1.11）。

定案见 `doc/结构VQT与波形数组定义.md` §10.3：结构由采集侧定义，我方**按字段绑定**；
绑定时**核对，不换算、只拒绝**；振动 x/y/z 这类同记录组**必须绑同一个点**。

## 0 先得知道一个点挂的是哪个结构

hs 的点表**不暴露 `StructRef`**（`StoredPoint` / `EntityIdentity` 都没有这一格）。
能拿到的只有**值**：每个结构值自带 `StructId` + `StructVersion`。⇒ 取这个点**最近一条值**，
再按它的 (id, 版本) 去 `GetStruct`。

  · 用 **`kLastTime`**：回窗内最后一条**样本本身**（它自己的时刻 + 整条结构值）。
    ★**不用 `kLastValue`**：它也照发整值，但**时刻是桶起点**（= 窗左端），不是样本时刻 ——
      2026-10-03 WSL 联机实测（gid 1557）：7 天窗回的时刻恰等于窗左端到微秒，真末样本在 3 天后。
      拿它当「据以判断的那条值的时刻」回给界面，就是一个编出来的时刻；
  · 回看窗**逐级放大**（1 分钟 → 1 小时 → 1 天 → 7 天）：多数点在第一级就答上，
    不为了找一条值让 hs 去扫一周的高频数据；
  · 7 天内一条结构值都没有 ⇒ **拒绝**（用户 2026-10-02 定）：无从得知它的结构，就无从核对字段。
    「先放行、运行时再核」的后果是绑错了要到跑起来才暴露，而那时界面上已经显示"配好了"。

★取的是**当前**的版本。采集侧日后发新版本、删了这个字段 ⇒ 取数时那一路逐条落坏质量码并写明（`fetch`），
  不在这里预判。

## 1 按字段号（契约 1.20，`D-319`、`C-122`）

字段可以原地改名、号不变 ⇒ 绑定**以号为准**。核对时有号按号找、没号按名找（老平台），
找到后把号与该号**当前**的名一并交回（`Resolved`），调用方存下 —— 只给名的老平台从此也有号。
名与号指向不同字段：**按号**，不拒，但写进 `notes` 回给配置者（`C-122 §2` 第 3 条：不静默）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from .bindings import Binding
from .hsproto import historystore_pb2 as hs
from .structreg import BINDABLE_TYPES, StructRegistry, StructRegistryError
from .types import InputSpec

logger = logging.getLogger(__name__)

#: 找点的结构时逐级放大的回看窗（秒）。
LOOKBACK_SEC = (60, 3600, 86400, 7 * 86400)

#: 物理映射描述的约定键（`H-261 §2.1`，hs 只保管不解释）。
ATTR_QUANTITY = "quantity"
ATTR_AXIS = "axis"

_TYPE_NAME = {hs.SFT_BOOL: "bool", hs.SFT_INT32: "int32", hs.SFT_INT64: "int64",
              hs.SFT_UINT32: "uint32", hs.SFT_UINT64: "uint64", hs.SFT_FLOAT: "float",
              hs.SFT_DOUBLE: "double", hs.SFT_STRING: "string", hs.SFT_BYTES: "bytes",
              hs.SFT_TIMESTAMP: "timestamp", hs.SFT_NUMBUF: "numbuf"}


class StructBindError(ValueError):
    """绑定核对不过。消息**原样回给配置者**，所以要写明是哪一路、哪一条、该怎么办。"""


@dataclass(frozen=True, slots=True)
class PointStruct:
    """一个结构值点**当前**挂的结构（取自它最近一条值）。"""

    gid: int
    struct: hs.StructDef
    """该版本的完整描述（含 `unit` / `attrs`）。"""
    seen_at: datetime
    """据以判断的那条值的时刻 —— 回给界面，让人知道"这是按多久以前的数据说的"。"""


def type_name(f: hs.StructField) -> str:
    base = _TYPE_NAME.get(f.type, f"未知({f.type})")
    return f"{base}[]" if f.repeated else base


def is_bindable(f: hs.StructField) -> bool:
    """能不能当成一路采样：**非 repeated 的数值标量**。"""
    return f.type in BINDABLE_TYPES and not f.repeated


def find_point_struct(client, registry: StructRegistry, gid: int,
                      now: datetime | None = None) -> PointStruct:
    """取点 `gid`（globalId，全量连接）最近一条值，答出它挂的结构。见模块头 §0。"""
    now = now or datetime.now(timezone.utc)
    for sec in LOOKBACK_SEC:
        req = hs.HisDataQueryReq(supportsStructValue=True)
        req.hisReq.method = hs.kLastTime   # ★不是 kLastValue —— 见模块头 §0
        req.hisReq.begTime.FromDatetime(now - timedelta(seconds=sec))
        req.hisReq.endTime.FromDatetime(now)
        req.pointsReq.add().id = gid
        res = client._unary(                       # noqa: SLF001 —— 同包内，刻意复用
            client._read_channel(), "QueryHistory", req, hs.VQTArrayRes, timeout=30.0)
        for arr in res.VQTs:
            for v in reversed(arr.VQTs):
                which = v.WhichOneof("Value")
                if which == "StructVal":
                    sid, ver = int(v.StructVal.StructId), int(v.StructVal.StructVersion)
                    try:
                        # ★现取不走缓存：单位 / 物理量 / 字段名可能刚在采集侧原地改过（AI-122 §3.2）
                        d = registry.describe(sid, ver, fresh=True)
                    except StructRegistryError as exc:
                        raise StructBindError(
                            f"点 {gid} 的值属于结构 id {sid} v{ver}，但取不到它的描述：{exc}") from exc
                    return PointStruct(gid=gid, struct=d,
                                       seen_at=v.TimStampUtc.ToDatetime().replace(tzinfo=timezone.utc))
                if which not in (None, "NullValue", "EmptyValue"):
                    raise StructBindError(
                        f"点 {gid} 不是结构值点（最近一条值是 {which}）—— 整点标量请直接绑点，不要指定字段")
    raise StructBindError(
        f"点 {gid} 近 {LOOKBACK_SEC[-1] // 86400} 天内没有任何结构值 —— 无从得知它挂的是哪个结构，"
        "也就无从核对字段。请等采集侧写入数据后再绑（或确认绑的是不是这个点）")


@dataclass(frozen=True, slots=True)
class Resolved:
    """核对通过后，各字段角色**实际**对到的字段（契约 1.20）。调用方拿它覆盖绑定再存。"""

    numbers: dict[str, int]
    """角色 → 字段号。只给了名的（老平台）在这里补上号。"""
    names: dict[str, str]
    """角色 → 该号**当前**的字段名（显示用）。"""
    notes: list[str]
    """名与号对不上等**不拒、但要说**的事（`C-122 §2` 第 3 条：按号取、报出、不静默）。"""


def check_binding(b: Binding, inputs: Sequence[InputSpec],
                  lookup: Callable[[int], PointStruct]) -> Resolved:
    """按字段绑定的全部核对。不过就抛 `StructBindError`，**一次只报第一条**（逐条改、逐条再提交）。

    `lookup(gid)` 由调用方给（真路径是 `find_point_struct`，带本次调用内的缓存）。
    ★没有字段绑定的绑定**一次 hs 都不拨** —— 纯标量的老绑定行为与 1.10 完全相同。
    ★**有号以号为准**（契约 1.20）：字段可以原地改名、号不变。名与号指向不同字段 ⇒ 按号、记进 `notes`。
    """
    specs = {i.role: i for i in inputs}
    numbers: dict[str, int] = {}
    names: dict[str, str] = {}
    notes: list[str] = []
    for role in b.field_roles:
        fname, fnum = b.fields.get(role), b.field_numbers.get(role)
        asked = f"字段号 {fnum}" if fnum is not None else f"字段 {fname!r}"
        spec = specs.get(role)
        if spec is None:
            raise StructBindError(f"角色 {role} 不在域 {b.domain} 的声明里")
        if spec.kind != "point":
            raise StructBindError(f"角色 {role} 的形态是 {spec.kind}，只有测点类角色能绑到字段")
        if role not in b.roles:
            raise StructBindError(f"角色 {role} 指定了{asked}，却没绑点")
        ps = lookup(b.roles[role])
        d = ps.struct
        where = f"角色 {role}（{spec.display or role}）→ 点 {ps.gid} 的结构 {d.name} v{d.version}"
        if fnum is not None:
            field = next((f for f in d.fields if f.number == fnum), None)
            if field is None:
                raise StructBindError(
                    f"{where} 里没有字段号 {fnum}"
                    + (f"（同时给的字段名是 {fname!r}）" if fname else "")
                    + f"；有的是 {[(f.number, f.name) for f in d.fields]}")
            if fname is not None and fname != field.name:
                notes.append(f"{where}：字段号 {fnum} 现名 {field.name!r}，与给的字段名 {fname!r} 不符"
                             " —— 已按号绑定，名字以实时库为准")
        else:
            field = next((f for f in d.fields if f.name == fname), None)
            if field is None:
                raise StructBindError(
                    f"{where} 里没有字段 {fname!r}；有的是 {[f.name for f in d.fields]}")
        fname = field.name
        if not is_bindable(field):
            raise StructBindError(
                f"{where} 的字段 {fname} 是 {type_name(field)}，不是数值标量 —— 不能当成一路采样")
        if spec.unit and field.unit != spec.unit:
            # ★不换算：g 与 m/s² 差 9.81 倍，换错了"数值看着合理但全错"。
            raise StructBindError(
                f"{where} 的字段 {fname} 单位是 {field.unit or '（未填）'!s}，域要的是 {spec.unit} —— "
                "我方不做单位换算，请换一个单位相符的字段，或请采集侧在结构里写明单位")
        if spec.quantity:
            q = field.attrs.get(ATTR_QUANTITY, "")
            if q != spec.quantity:
                raise StructBindError(
                    f"{where} 的字段 {fname} 物理量是 {q or '（未声明 quantity）'}，域要的是 {spec.quantity}"
                    + ("" if q else " —— 请采集侧按 H-261 §2.1 在字段 attrs 里填 quantity"))
        numbers[role], names[role] = int(field.number), field.name

    field_roles = set(b.field_roles)
    records: dict[str, list[str]] = {}
    for i in inputs:
        if i.record:
            records.setdefault(i.record, []).append(i.role)
    for rec, roles in records.items():
        bound = [r for r in roles if r in b.roles]
        by_field = [r for r in bound if r in field_roles]
        if not by_field:
            continue
        if len(by_field) != len(bound):
            raise StructBindError(
                f"同记录组 {rec}：{by_field} 绑的是结构值字段，{sorted(set(bound) - set(by_field))} "
                "绑的却是整点 —— 同一时刻的保证就没了。请全组绑到同一个结构值点的字段上")
        gids = {b.roles[r] for r in by_field}
        if len(gids) > 1:
            raise StructBindError(
                f"同记录组 {rec}：{by_field} 分别绑到了点 {sorted(gids)} —— 必须是**同一个**点的字段，"
                "否则同一时刻的保证就没了，且不报错")
    return Resolved(numbers=numbers, names=names, notes=notes)


def cached_lookup(client, registry: StructRegistry) -> Callable[[int], PointStruct]:
    """本次调用内按 gid 缓存的 `find_point_struct`（x/y/z 三路同点只拨一次）。"""
    cache: dict[int, PointStruct] = {}

    def lookup(gid: int) -> PointStruct:
        if gid not in cache:
            cache[gid] = find_point_struct(client, registry, gid)
        return cache[gid]
    return lookup
