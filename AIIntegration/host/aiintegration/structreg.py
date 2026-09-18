"""结构值：注册、绑点、解码 —— 骨架这一侧。

定案见 `doc/结构值点定案.md`（S1~S4、P1~P6），契约由 historystore `H-246` / `H-249` 定。

---

## 0 这个模块存在的理由

`H-246` 把**波形 / 压装 / 谱能量**从"多个标量点"改成**一个结构值**：
一帧波形 / 一次压装 = **一个值**。于是骨架多了三件事，域一件都不碰（硬规矩 3）：

1. **注册结构**（`PutStruct`）—— 把域自述的 `StructSpec` 翻成实时库的 `StructDef`；
2. **建点时带 `StructRef`** —— 点与结构的绑定；
3. **解码** —— 把 `VQT.StructVal` 的字节解回 `StructSample` 交给域。

## 1 ★三条会静默出事的，逐条挡在这里

| # | 事 | 不挡会怎样 |
| --- | --- | --- |
| ★1 | **写结构值之前必须先探能力位** | 老引擎不认识 `StructValue`，会把它**存成"无值"且回成功** —— 写入侧一点异常都没有，数据静默丢（`H-246 §2.4`）|
| ★2 | **`allowNewVersion` 恒 `false`** | 带上它，"改错一个字段"会悄悄变成新版本、版本号还被刷成启动次数（`D-242 §7.1`）。⇒ 描述变了就**当场报错**，要人去看 |
| ★3 | **按值自带的版本号取描述符** | 拿最新版去解老值是**静默错读** —— 字段号虽不复用，但新版本加的字段在老值里根本没有，而类型签名变过的字段会落到新号上 |

## 2 为什么解码走 hs 给的 descriptor，而不是我方写死

`GetStruct(withDescriptor=true)` 直接回 **`FileDescriptorProto`**（契约明写"可直接喂动态解码库"）。
⇒ 我方**不写死字段号**：字段号是 hs 分配的（`StructField.number`），
写死就等于假定它永远不变，而契约只保证"同名同类型签名沿用原号"，不保证我方猜得对。

★**数值缓冲不在这里转 numpy**：骨架不 import 第三方库（`types.py` 模块头），
  原样交给域做 `np.frombuffer`，少一次拷贝。
"""

from __future__ import annotations

import logging
import struct as _struct
import threading

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .hsproto import historystore_pb2 as hs
from .types import NumBuf, StructFieldSpec, StructSample, StructSpec

logger = logging.getLogger(__name__)

#: 实时库的能力位名（`H-249`）。探不到就整条路不启用。
FEATURE_STRUCT_VALUE = "struct-value"

#: 结构值点的 `Paras` 两个键（`H-247 §1`、`H-249 §1.1`）。
PARAS_STRUCT_REF = "StructRef"
PARAS_ACC = "Acc"
#: 结构值点的 `Acc` = `VT_RECORD`。★必须与字节点（`VT_BLOB=65`）分开，
#: 否则点表、选择器 dataType 门控、组态类型筛选都分不出结构值点（`D-242 §1.3`）。
ACC_VT_RECORD = 36

#: 域自述的类型 → 实时库的 `StructFieldType`。
_TYPE_MAP = {
    "bool": hs.SFT_BOOL,
    "int32": hs.SFT_INT32,
    "int64": hs.SFT_INT64,
    "uint32": hs.SFT_UINT32,
    "uint64": hs.SFT_UINT64,
    "float": hs.SFT_FLOAT,
    "double": hs.SFT_DOUBLE,
    "string": hs.SFT_STRING,
    "bytes": hs.SFT_BYTES,
    "timestamp": hs.SFT_TIMESTAMP,
    "numbuf": hs.SFT_NUMBUF,
}
_DTYPE_MAP = {
    "i8": hs.NBD_I8, "u8": hs.NBD_U8, "i16": hs.NBD_I16, "u16": hs.NBD_U16,
    "i32": hs.NBD_I32, "u32": hs.NBD_U32, "i64": hs.NBD_I64, "u64": hs.NBD_U64,
    "f32": hs.NBD_F32, "f64": hs.NBD_F64,
}
_DTYPE_BACK = {v: k for k, v in _DTYPE_MAP.items()}


#: protobuf 里 `LABEL_REPEATED` 的值（老版 `FieldDescriptor.label` 用它）。
_LABEL_REPEATED = 3


def is_repeated(field) -> bool:
    """这个字段是不是 repeated —— ★**又一处两版 API 互斥**，与 `message_class` 同病。

    实测（2026-09-18）：

    | protobuf | `FieldDescriptor.label` | `FieldDescriptor.is_repeated` |
    | --- | --- | --- |
    | 4.21.12（3.12 环境） | ✓ | ✗ 没有 |
    | 7.36.1（py310 / vision / **现场**） | ✗ 没有（upb 实现） | ✓ |

    ★这一条是**三环境串行**逼出来的：3.12 全绿、py310 五条全错。
      只在一个环境跑过就会漏掉，而**现场恰好是没有 `label` 的那一边**。
    """
    r = getattr(field, "is_repeated", None)
    if r is not None:
        return bool(r)
    return field.label == _LABEL_REPEATED


def message_class(descriptor, pool):
    """按描述符取动态消息类 —— ★**两个 protobuf 大版本的 API 互斥**，在这里收口。

    实测（2026-09-18，我方三个环境 + 现场）：

    | protobuf | `GetMessageClass` | `MessageFactory(...).GetPrototype` |
    | --- | --- | --- |
    | 4.21.12（3.12 环境） | ✗ 没有 | ✓ |
    | 7.36.1（py310 / vision / **现场**） | ✓ | ✗ 没有 |

    ★注意这**不是"新版兼容旧版"**，是两边各有各的、互不相容。
      我方测试环境正好横跨两边 —— 只按一边写，另一边直接 `AttributeError`，
      而**现场是 7.36.1 那一边**。
    """
    get_cls = getattr(message_factory, "GetMessageClass", None)
    if get_cls is not None:
        return get_cls(descriptor)
    return message_factory.MessageFactory(pool).GetPrototype(descriptor)


class StructRegistryError(RuntimeError):
    """注册/解码本身出的错。★**不拦服务启动**（定案 P2），由调用方落降级项。"""


def to_struct_def(spec: StructSpec) -> hs.StructDef:
    """域自述 → 实时库的 `StructDef`。**不填任何输出字段**（id / version / number 由 hs 分配）。"""
    d = hs.StructDef(name=spec.name, displayName=spec.display, description=spec.description)
    for f in spec.fields:
        sf = d.fields.add(name=f.name, type=_TYPE_MAP[f.type], repeated=f.repeated,
                          displayName=f.display, description=f.description, unit=f.unit)
        if f.type == "numbuf":
            sf.dtype = _DTYPE_MAP[f.dtype]
            sf.bigEndian = f.big_endian
            sf.shape.extend(f.shape)
    return d


class StructRegistry:
    """注册过的结构 + 它们的描述符。**线程安全**（一把锁，与 `PointMap` 同规矩）。

    ★整个类在**探不到能力位时不做任何事** —— `available` 为假，
      调用方据此走降级（不建结构值点、那条诊断落坏码并说清）。
    """

    def __init__(self, client) -> None:
        self._client = client
        self._lock = threading.RLock()
        #: 结构名 → 生效的 `StructDef`（含 hs 分配的 id / version / number）
        self._defs: dict[str, hs.StructDef] = {}
        #: (结构名, 版本) → 动态消息类。★按**值自带的版本**取，不拿最新版解老值
        self._msgs: dict[tuple[str, int], type] = {}
        self._pool = descriptor_pool.DescriptorPool()
        self._available: bool | None = None
        self._degraded: list[str] = []

    # ── 能力位 ────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        """对端支持结构值吗。★**探不到一律当作不支持**（缺失 = 未知，不是否定）。"""
        if self._available is None:
            self._available = bool(self._client.has_feature(FEATURE_STRUCT_VALUE))
            if not self._available:
                logger.warning(
                    "实时库没有 %s 能力位 —— 结构值那条路整条不启用（波形/压装诊断会落坏码并说清）。"
                    "★这不是缺陷：老引擎不认识 StructValue，写进去会被存成「无值」且回成功，"
                    "那才是真正要命的静默丢数据。", FEATURE_STRUCT_VALUE)
        return self._available

    @property
    def degraded(self) -> list[str]:
        """注册没成功的那些，原样交给 `/health` 与日志 —— **降级要可见**。"""
        with self._lock:
            return list(self._degraded)

    # ── 注册 ──────────────────────────────────────────────────────────────
    def register(self, specs) -> int:
        """把域自述的结构注册上去。返回成功几个。

        ★`allowNewVersion` **恒 `false`**（定案 S3）：描述没变 = 幂等成功；
          描述变了 = 当场报错并落降级项，**绝不自动发新版本**。
        ★**失败不抛**（定案 P2）：结构注册不上，受影响的只有要用它的那条诊断，
          不该让整个服务起不来。
        """
        if not specs:
            return 0
        if not self.available:
            with self._lock:
                self._degraded = [f"{s.name}：实时库无 {FEATURE_STRUCT_VALUE} 能力位" for s in specs]
            return 0
        ok = 0
        with self._lock:
            self._degraded = []
            for spec in specs:
                try:
                    self._register_one(spec)
                    ok += 1
                except Exception as exc:  # noqa: BLE001 —— 见方法文档
                    why = f"{spec.name}：{type(exc).__name__}: {exc}"
                    self._degraded.append(why)
                    logger.error("结构注册失败（本条诊断会落坏码，服务照常起）：%s", why)
        return ok

    def _register_one(self, spec: StructSpec) -> None:
        res = self._client.put_struct(to_struct_def(spec), allow_new_version=False)
        # ★契约里这个字段字面就叫 `def`（Python 关键字）⇒ 只能 getattr 取，没有 `def_` 别名。
        d = getattr(res, "def")
        self._defs[spec.name] = d
        logger.info("结构已注册：%s id=%d version=%d created=%s newVersion=%s",
                    d.name, d.id, d.version, res.created, res.newVersion)

    # ── 建点用 ────────────────────────────────────────────────────────────
    def paras_for(self, struct_name: str) -> dict[str, str]:
        """结构值点建点时要带的 `Paras`。没注册上 ⇒ 回空（⇒ 不建这个点）。"""
        with self._lock:
            d = self._defs.get(struct_name)
        if d is None:
            return {}
        # ★`StructRef` 一旦绑定**不许换**（换结构 = 新建点）；缺键/空串 = 保持原绑定。
        return {PARAS_STRUCT_REF: str(d.id), PARAS_ACC: str(ACC_VT_RECORD)}

    def struct_id(self, struct_name: str) -> int:
        with self._lock:
            d = self._defs.get(struct_name)
        return d.id if d is not None else 0

    # ── 解码 ──────────────────────────────────────────────────────────────
    def decode(self, name: str, sv, t, quality) -> StructSample:
        """`VQT.StructVal` → `StructSample`。

        ★**按 `sv.StructVersion` 取描述符**，不拿最新版解老值（定案 P6、契约约束 4）。
        """
        msg_cls = self._message_class(name, int(sv.StructVersion))
        m = msg_cls()
        m.ParseFromString(bytes(sv.Data))
        spec_fields = {f.name: f for f in self._defs[name].fields}
        out: dict[str, object] = {}
        for f in m.DESCRIPTOR.fields:
            sf = spec_fields.get(f.name)
            v = getattr(m, f.name)
            if sf is not None and sf.type == hs.SFT_NUMBUF:
                out[f.name] = NumBuf(data=bytes(v), dtype=_DTYPE_BACK.get(sf.dtype, ""),
                                     shape=tuple(sf.shape), big_endian=bool(sf.bigEndian))
            elif is_repeated(f):
                out[f.name] = list(v)
            else:
                out[f.name] = v
        return StructSample(t=t, quality=quality, fields=out,
                            struct_name=name, struct_version=int(sv.StructVersion))

    def _message_class(self, name: str, version: int):
        key = (name, version)
        with self._lock:
            cls = self._msgs.get(key)
            if cls is not None:
                return cls
        res = self._client.get_struct(name=name, version=version, with_descriptor=True)
        if not res.fileDescriptor:
            raise StructRegistryError(
                f"结构 {name} v{version} 没有描述符 —— 解不了这个值（不猜字段号）")
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(bytes(res.fileDescriptor))
        with self._lock:
            self._defs.setdefault(name, getattr(res, "def"))
            try:
                file_desc = self._pool.Add(fdp)
            except Exception:  # 同名文件已加过（版本各一份），直接取
                file_desc = self._pool.FindFileByName(fdp.name)
            full = f"{fdp.package + '.' if fdp.package else ''}{fdp.message_type[0].name}"
            desc = self._pool.FindMessageTypeByName(full)
            cls = message_class(desc, self._pool)
            self._msgs[key] = cls
            return cls


def numbuf_to_list(buf: NumBuf) -> list:
    """把数值缓冲解成 Python 数字列表 —— **只给用例与零依赖场景用**。

    ★正经路径上域应当自己 `np.frombuffer` 零拷贝；这里逐个解包是为了让
      不装 numpy 的环境也能验证编解码对不对。
    """
    fmt = {"i8": "b", "u8": "B", "i16": "h", "u16": "H", "i32": "i", "u32": "I",
           "i64": "q", "u64": "Q", "f32": "f", "f64": "d"}[buf.dtype]
    endian = ">" if buf.big_endian else "<"
    n = len(buf.data) // _struct.calcsize(fmt)
    return list(_struct.unpack(f"{endian}{n}{fmt}", buf.data[:n * _struct.calcsize(fmt)]))


def list_to_numbuf(values, dtype: str, big_endian: bool = False) -> NumBuf:
    """反向 —— 同样只给用例与造数用。"""
    fmt = {"i8": "b", "u8": "B", "i16": "h", "u16": "H", "i32": "i", "u32": "I",
           "i64": "q", "u64": "Q", "f32": "f", "f64": "d"}[dtype]
    endian = ">" if big_endian else "<"
    data = _struct.pack(f"{endian}{len(values)}{fmt}", *values)
    return NumBuf(data=data, dtype=dtype, shape=(-1,), big_endian=big_endian)
