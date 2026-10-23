"""结构值读侧 —— 按字段绑定（契约 1.11，定义文档 §10.3 / §11）的回归。

钉的几条：
  · 解码按值自带的 **(StructId, StructVersion)**，字段元数据取**同一版本**的描述；带 TIMESTAMP 的结构解得开；
  · 取数带 `supportsStructValue`、同一个点只请求一份；每条结构值**按字段拆开**，T/Q 逐条照搬；
  · 这一版没有该字段 / 不是结构值 / 解不了 ⇒ **该条落坏样本**，不跳过、不猜；
  · 按字段绑定的绑定，在线取数窗右端离节拍 5 秒（`H-263`）；纯标量绑定不变；
  · 绑定时核对：字段存在、数值标量、单位、物理量（缺键也拒）、同记录组同点；点无值即拒；
  · 没有字段绑定的绑定**一次 hs 都不拨**。
"""

import tempfile
import unittest
from concurrent import futures
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, timestamp_pb2

from aiintegration.api import ApiService, SERVICE, build_handler
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import Domain, LoadedDomain, discover
from aiintegration.fetch import Fetcher
from aiintegration.hsproto import daqcontract_pb2 as daq
from aiintegration.hsproto import historystore_pb2 as hs
from aiintegration.logstore import LogStore
from aiintegration.pointmap import PointMap
from aiintegration.quality import Quality
from aiintegration.scheduler import STRUCT_RIGHT_MARGIN_SEC, Scheduler
from aiintegration.structbind import (
    PointStruct, StructBindError, check_binding, find_point_struct, is_bindable,
)
from aiintegration.structreg import StructRegistry, StructRegistryError, message_class
from aiintegration.types import Declaration, Frame, InputSpec, OutputSpec

UTC = timezone.utc
T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
FD = descriptor_pb2.FieldDescriptorProto

_PBT = {hs.SFT_BOOL: FD.TYPE_BOOL, hs.SFT_INT32: FD.TYPE_INT32, hs.SFT_INT64: FD.TYPE_INT64,
        hs.SFT_UINT32: FD.TYPE_UINT32, hs.SFT_UINT64: FD.TYPE_UINT64,
        hs.SFT_FLOAT: FD.TYPE_FLOAT, hs.SFT_DOUBLE: FD.TYPE_DOUBLE,
        hs.SFT_STRING: FD.TYPE_STRING, hs.SFT_BYTES: FD.TYPE_BYTES,
        hs.SFT_NUMBUF: FD.TYPE_BYTES, hs.SFT_TIMESTAMP: FD.TYPE_MESSAGE}


def fld(name, t=hs.SFT_DOUBLE, *, unit="", quantity="", axis="", repeated=False, dtype=0):
    f = hs.StructField(name=name, type=t, unit=unit, repeated=repeated, dtype=dtype)
    if quantity:
        f.attrs["quantity"] = quantity
    if axis:
        f.attrs["axis"] = axis
    return f


def vel3(unit="mm/s", quantity="velocity"):
    return [fld(a, hs.SFT_FLOAT, unit=unit, quantity=quantity, axis=a) for a in "xyz"]


class StructHs:
    """按契约形状应答的假实时库：结构描述按 (id, 版本) 取；点上存原始 VQT；按时间窗回。

    ★照真 hs 的几处行为：字段号**由它分配**、同名同类型签名沿用原号；描述符**不带** unit/attrs；
      带 TIMESTAMP 字段时描述符加 `google/protobuf/timestamp.proto` 依赖；
      请求不带 `supportsStructValue` ⇒ 结构值降级成 `NullValue + -1035`。
    """

    def __init__(self):
        self.defs: dict[tuple[int, int], hs.StructDef] = {}
        self.points: dict[int, list] = {}
        self._nums: dict[tuple, int] = {}
        self.get_calls = []
        self.queries = []
        self.fail_get = False

    # ── 结构 ──
    def add_struct(self, sid, ver, name, fields):
        d = hs.StructDef(id=sid, version=ver, name=name)
        for f in fields:
            nf = d.fields.add()
            nf.CopyFrom(f)
            sig = (sid, f.name, f.type, f.repeated, f.dtype)
            nf.number = self._nums.setdefault(sig, 7 + len(self._nums))
        self.defs[(sid, ver)] = d
        return d

    def get_struct(self, *, name="", struct_id=0, version=0, with_descriptor=False):
        self.get_calls.append((struct_id, version))
        if self.fail_get:
            raise RuntimeError("UNAVAILABLE")
        d = self.defs.get((struct_id, version))
        if d is None:
            raise RuntimeError(f"NOT_FOUND {(struct_id, version)}")
        res = hs.GetStructRes()
        getattr(res, "def").CopyFrom(d)
        if with_descriptor:
            res.fileDescriptor = self._fdp(d).SerializeToString()
        return res

    @staticmethod
    def _fdp(d):
        fdp = descriptor_pb2.FileDescriptorProto(
            name=f"hs_structs/{d.name}_v{d.version}.proto", package="hs.structs", syntax="proto3")
        msg = fdp.message_type.add(name=f"{d.name}_v{d.version}")
        for f in d.fields:
            ff = msg.field.add(name=f.name, number=f.number, type=_PBT[f.type],
                               label=FD.LABEL_REPEATED if f.repeated else FD.LABEL_OPTIONAL)
            if f.type == hs.SFT_TIMESTAMP:
                ff.type_name = ".google.protobuf.Timestamp"
                if "google/protobuf/timestamp.proto" not in fdp.dependency:
                    fdp.dependency.append("google/protobuf/timestamp.proto")
        return fdp

    def encode(self, sid, ver, **values) -> bytes:
        d = self.defs[(sid, ver)]
        pool = descriptor_pool.DescriptorPool()
        ts = descriptor_pb2.FileDescriptorProto()
        timestamp_pb2.DESCRIPTOR.CopyToProto(ts)
        pool.Add(ts)
        pool.Add(self._fdp(d))
        m = message_class(pool.FindMessageTypeByName(f"hs.structs.{d.name}_v{ver}"), pool)()
        for k, v in values.items():
            if isinstance(v, datetime):
                getattr(m, k).FromDatetime(v)
            elif isinstance(v, list):
                getattr(m, k).extend(v)
            else:
                setattr(m, k, v)
        return m.SerializeToString()

    # ── 点 ──
    def put(self, gid, t, sid=None, ver=None, *, code=1, r8=None, null=False, **values):
        v = daq.VQT(TagId=gid)
        v.Quality.Code = code
        v.TimStampUtc.FromDatetime(t)
        if null:
            v.NullValue = True
        elif r8 is not None:
            v.R8 = r8
        else:
            v.StructVal.StructId = sid
            v.StructVal.StructVersion = ver
            v.StructVal.Data = self.encode(sid, ver, **values)
        self.points.setdefault(gid, []).append(v)
        return v

    def _read_channel(self):
        return object()

    def _unary(self, ch, method, req, resp_cls, timeout=15.0):
        assert method == "QueryHistory", method
        self.queries.append(req)
        beg = req.hisReq.begTime.ToDatetime().replace(tzinfo=UTC)
        end = req.hisReq.endTime.ToDatetime().replace(tzinfo=UTC)
        res = hs.VQTArrayRes()
        for p in req.pointsReq:
            arr = res.VQTs.add(TagId=p.id)
            got = [v for v in self.points.get(p.id, [])
                   if beg <= v.TimStampUtc.ToDatetime().replace(tzinfo=UTC) <= end]
            if req.hisReq.method == hs.kLastTime:
                got = got[-1:]                       # 样本本身，时刻照旧
            elif req.hisReq.method == hs.kLastValue:
                got = got[-1:]
                if got:                              # ★真 hs：时刻打成桶起点（= 窗左端），不是样本时刻
                    v = daq.VQT(); v.CopyFrom(got[0]); v.TimStampUtc.CopyFrom(req.hisReq.begTime)
                    got = [v]
            for v in got:
                nv = arr.VQTs.add()
                nv.CopyFrom(v)
                if nv.WhichOneof("Value") == "StructVal" and not req.supportsStructValue:
                    nv.NullValue = True
                    nv.Quality.Code = -1035
        return res


# ═══════════════════════════ structreg 读侧 ═══════════════════════════

class TestDecodeById(unittest.TestCase):
    def setUp(self):
        self.hs = StructHs()
        self.reg = StructRegistry(self.hs)

    def test_按值自带的id与版本解_老值不拿新版解(self):
        self.hs.add_struct(5, 1, "V", [fld("a")])
        self.hs.add_struct(5, 2, "V", [fld("a"), fld("b")])
        v1 = self.hs.put(1, T0, 5, 1, a=1.5)
        s = self.reg.decode_value(v1.StructVal, T0, Quality.OK)
        self.assertEqual(s.struct_version, 1)
        self.assertEqual(s.fields, {"a": 1.5}, "v1 的值被按 v2 解，多出一个 b")
        self.assertIn((5, 1), self.hs.get_calls)

    def test_字段元数据取同一版本(self):
        # v1 的 w 是 f32 缓冲，v2 改成 f64（类型签名变 ⇒ 新号）。先解 v1 再解 v2，
        # v2 的缓冲必须按 f64 —— 原先取"第一次见到的那版"的 dtype，这里会错成 f32。
        self.hs.add_struct(6, 1, "W", [fld("w", hs.SFT_NUMBUF, dtype=hs.NBD_F32)])
        self.hs.add_struct(6, 2, "W", [fld("w", hs.SFT_NUMBUF, dtype=hs.NBD_F64)])
        a = self.hs.put(1, T0, 6, 1, w=b"\0" * 8)
        b = self.hs.put(1, T0, 6, 2, w=b"\0" * 16)
        self.assertEqual(self.reg.decode_value(a.StructVal, T0, Quality.OK).fields["w"].dtype, "f32")
        self.assertEqual(self.reg.decode_value(b.StructVal, T0, Quality.OK).fields["w"].dtype, "f64")

    def test_带TIMESTAMP字段的结构解得开(self):
        self.hs.add_struct(7, 1, "T", [fld("at", hs.SFT_TIMESTAMP), fld("v")])
        v = self.hs.put(1, T0, 7, 1, at=T0 - timedelta(seconds=3), v=2.0)
        s = self.reg.decode_value(v.StructVal, T0, Quality.OK)
        self.assertEqual(s.fields["at"], T0 - timedelta(seconds=3))
        self.assertEqual(s.fields["v"], 2.0)

    def test_描述按id版本只取一次(self):
        self.hs.add_struct(5, 1, "V", [fld("a")])
        for i in range(5):
            v = self.hs.put(1, T0, 5, 1, a=float(i))
            self.reg.decode_value(v.StructVal, T0, Quality.OK)
        self.assertEqual(self.hs.get_calls.count((5, 1)), 1)

    def test_取不到描述_短时间内不重复去拨(self):
        self.hs.add_struct(5, 1, "V", [fld("a")])
        v = self.hs.put(1, T0, 5, 1, a=1.0)
        self.hs.fail_get = True
        for _ in range(3):
            with self.assertRaises(StructRegistryError):
                self.reg.decode_value(v.StructVal, T0, Quality.OK)
        self.assertEqual(len(self.hs.get_calls), 1, "一帧几万条同版本的值，每条都去拨一次")

    def test_decode按名核对值确属该结构(self):
        self.hs.add_struct(5, 1, "V", [fld("a")])
        v = self.hs.put(1, T0, 5, 1, a=1.0)
        self.assertEqual(self.reg.decode("V", v.StructVal, T0, Quality.OK).fields["a"], 1.0)
        with self.assertRaises(StructRegistryError):
            self.reg.decode("别的结构", v.StructVal, T0, Quality.OK)

    # ── AI-122 §3 / D-319 第 0 步 ──
    def test_两个结构描述同名_各解各的(self):
        # A 改名后旧名被另一个结构再用：两份描述的消息全名都是 hs.structs.A_v1。
        # 共用一个池时第二份加不进去，id 9 的值被按 id 5 的字段号解成 {'x': 0.0}、不报错。
        self.hs.add_struct(5, 1, "A", [fld("x")])
        self.hs.add_struct(9, 1, "A", [fld("y", hs.SFT_FLOAT)])
        a = self.hs.put(1, T0, 5, 1, x=1.25)
        b = self.hs.put(2, T0, 9, 1, y=2.5)
        self.assertEqual(self.reg.decode_value(a.StructVal, T0, Quality.OK).fields, {"x": 1.25})
        self.assertEqual(self.reg.decode_value(b.StructVal, T0, Quality.OK).fields, {"y": 2.5})

    def _clock(self, t):
        return mock.patch("aiintegration.structreg.time.monotonic", return_value=t)

    def test_原地修改_到期重取后看得到(self):
        # 字段原地改名 + 改单位，号与版本都不变（D-302 / D-319）。原先要我方重启才看得到。
        d = self.hs.add_struct(5, 1, "V", [fld("x", hs.SFT_FLOAT, unit="mm/s")])
        v = self.hs.put(1, T0, 5, 1, x=1.5)
        with self._clock(1000.0):
            self.assertEqual(self.reg.decode_value(v.StructVal, T0, Quality.OK).fields, {"x": 1.5})
        d.fields[0].name, d.fields[0].unit = "x_vel", "m/s"
        with self._clock(1000.0 + 30):
            self.assertEqual(self.reg.decode_value(v.StructVal, T0, Quality.OK).fields, {"x": 1.5},
                             "未到期就重取 —— 一帧几万条值每条都去拨")
        with self._clock(1000.0 + 61):
            self.assertEqual(self.reg.decode_value(v.StructVal, T0, Quality.OK).fields, {"x_vel": 1.5})
            self.assertEqual(self.reg.describe(5, 1).fields[0].unit, "m/s")
        self.assertEqual(self.hs.get_calls.count((5, 1)), 2)

    def test_到期重取_没变就不重建(self):
        self.hs.add_struct(5, 1, "V", [fld("a")])
        with self._clock(1000.0):
            self.reg.describe(5, 1)
        before = self.reg._entries[(5, 1)].cls
        with self._clock(1000.0 + 61):
            self.reg.describe(5, 1)
        self.assertIs(self.reg._entries[(5, 1)].cls, before)
        self.assertEqual(self.hs.get_calls.count((5, 1)), 2)

    def test_到期重取失败_暂用缓存的那份(self):
        self.hs.add_struct(5, 1, "V", [fld("a")])
        v = self.hs.put(1, T0, 5, 1, a=2.0)
        with self._clock(1000.0):
            self.reg.decode_value(v.StructVal, T0, Quality.OK)
        self.hs.fail_get = True
        with self._clock(1000.0 + 61):
            for _ in range(3):
                self.assertEqual(self.reg.decode_value(v.StructVal, T0, Quality.OK).fields, {"a": 2.0})
        self.assertEqual(len(self.hs.get_calls), 2, "重取失败后同一周期内又去拨")

    def test_fresh_不走缓存_也不受失败缓存拦(self):
        self.hs.add_struct(5, 1, "V", [fld("a", unit="mm/s")])
        self.reg.describe(5, 1)
        self.hs.defs[(5, 1)].fields[0].unit = "m/s"
        self.assertEqual(self.reg.describe(5, 1, fresh=True).fields[0].unit, "m/s")
        self.hs.fail_get = True
        with self.assertRaises(StructRegistryError):
            self.reg.describe(5, 1, fresh=True)     # 现取失败如实报，不拿缓存顶


# ═══════════════════════════ fetch：按字段拆 ═══════════════════════════

def fb(roles, fields, **kw):
    return Binding("vib", "dev1", roles, fields=fields, window_sec=kw.pop("window_sec", 60), **kw)


XYZ = {"x_vel": "x", "y_vel": "y", "z_vel": "z"}


class TestFetchFields(unittest.TestCase):
    def setUp(self):
        self.hs = StructHs()
        self.hs.add_struct(3, 1, "Vib", vel3())
        self.f = Fetcher(self.hs, StructRegistry(self.hs))

    def test_同一条值拆成三路_T与Q逐条照搬(self):
        self.hs.put(9, T0 - timedelta(seconds=2), 3, 1, x=1.0, y=2.0, z=3.0)
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 1, code=-1004, x=4.0, y=5.0, z=6.0)
        fr = self.f.fetch(fb({r: 9 for r in XYZ}, XYZ), T0)
        self.assertEqual([s.value for s in fr.channels["y_vel"]], [2.0, 5.0])
        for r in XYZ:
            self.assertEqual([s.t for s in fr.channels[r]],
                             [T0 - timedelta(seconds=2), T0 - timedelta(seconds=1)])
            self.assertEqual([s.quality for s in fr.channels[r]], [Quality.OK, Quality.INPUT_BAD])

    def test_请求带supportsStructValue且同点只要一份(self):
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 1, x=1.0, y=2.0, z=3.0)
        fr = self.f.fetch(fb({r: 9 for r in XYZ}, XYZ), T0)
        req = self.hs.queries[-1]
        self.assertTrue(req.supportsStructValue, "不带它 hs 把结构值降级成 NullValue + -1035")
        self.assertEqual([p.id for p in req.pointsReq], [9])
        self.assertEqual(req.hisReq.method, hs.kRawData)
        self.assertEqual(fr.channels["x_vel"][0].value, 1.0)

    def test_按号取_字段原地改名照样取得到(self):
        # 号不变、名变了（D-319 第 4 步的字段原地改名）：按号的绑定照取，按名的老绑定落坏。
        d = self.hs.defs[(3, 1)]
        nx = next(f.number for f in d.fields if f.name == "x")
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 1, x=1.0, y=2.0, z=3.0)
        next(f for f in d.fields if f.name == "x").name = "x_vel_rms"
        by_num = self.f.fetch(fb({"x_vel": 9}, {"x_vel": "x"}, field_numbers={"x_vel": nx}), T0)
        self.assertEqual([s.value for s in by_num.channels["x_vel"]], [1.0])
        by_name = Fetcher(self.hs, StructRegistry(self.hs)).fetch(fb({"x_vel": 9}, {"x_vel": "x"}), T0)
        self.assertEqual([s.quality for s in by_name.channels["x_vel"]], [Quality.INPUT_BAD])

    def test_按号_这一版没有该号_落坏(self):
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 1, x=1.0, y=2.0, z=3.0)
        fr = self.f.fetch(fb({"x_vel": 9}, {"x_vel": "x"}, field_numbers={"x_vel": 999}), T0)
        self.assertEqual([s.quality for s in fr.channels["x_vel"]], [Quality.INPUT_BAD])

    def test_这一版没有该字段_该条落坏而不是跳过(self):
        self.hs.add_struct(3, 2, "Vib", [fld("x", hs.SFT_FLOAT, unit="mm/s", quantity="velocity")])
        self.hs.put(9, T0 - timedelta(seconds=2), 3, 1, x=1.0, y=2.0, z=3.0)
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 2, x=4.0)
        fr = self.f.fetch(fb({"x_vel": 9, "y_vel": 9}, {"x_vel": "x", "y_vel": "y"}), T0)
        self.assertEqual([s.value for s in fr.channels["x_vel"]], [1.0, 4.0])
        y = fr.channels["y_vel"]
        self.assertEqual(len(y), 2, "缺字段那条被跳过了 —— 那会看成这段采样稀疏")
        self.assertEqual((y[1].value, y[1].quality), (None, Quality.INPUT_BAD))

    def test_整点角色收到结构值_落坏而不是把对象交给域(self):
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 1, x=1.0, y=2.0, z=3.0)
        s = self.f.fetch(fb({"x_vel": 9}, {}), T0).channels["x_vel"][0]
        self.assertIsNone(s.value)
        self.assertIs(s.quality, Quality.INPUT_BAD)

    def test_字段角色收到标量_落坏(self):
        self.hs.put(9, T0 - timedelta(seconds=1), r8=1.0)
        s = self.f.fetch(fb({"x_vel": 9}, {"x_vel": "x"}), T0).channels["x_vel"][0]
        self.assertEqual((s.value, s.quality), (None, Quality.INPUT_BAD))

    def test_坏值锚点原样带下来(self):
        self.hs.put(9, T0 - timedelta(seconds=1), null=True, code=-1007)
        s = self.f.fetch(fb({"x_vel": 9}, {"x_vel": "x"}), T0).channels["x_vel"][0]
        self.assertEqual((s.value, s.status_code), (None, -1007))

    def test_没接结构描述_落坏而不是崩(self):
        self.hs.put(9, T0 - timedelta(seconds=1), 3, 1, x=1.0, y=2.0, z=3.0)
        s = Fetcher(self.hs).fetch(fb({"x_vel": 9}, {"x_vel": "x"}), T0).channels["x_vel"][0]
        self.assertEqual((s.value, s.quality), (None, Quality.INPUT_BAD))

    def test_标量绑定照旧(self):
        self.hs.put(8, T0 - timedelta(seconds=1), r8=7.0)
        s = self.f.fetch(fb({"x_vel": 8}, {}), T0).channels["x_vel"][0]
        self.assertEqual((s.value, s.quality), (7.0, Quality.OK))


# ═══════════════════════════ 调度：右端留 5 秒 ═══════════════════════════

class _RecFetcher:
    def __init__(self):
        self.ends = []

    def fetch(self, b, end_time, artifacts=None):
        self.ends.append(end_time)
        return Frame(domain=b.domain, binding=b.binding,
                     t_start=end_time - timedelta(seconds=60), t_end=end_time, channels={})


class _Client:
    def post_vqt(self, items, timeout=15.0):
        pass


class _Dom(Domain):
    key = "vib"
    display = "振动"
    version = "1"

    def declare(self):
        return DECL

    def infer(self, frame):
        return []


DECL = Declaration(
    inputs=tuple(InputSpec(role=r, unit="mm/s", quantity="velocity", axis=r[0], record="p1",
                           required=(r == "x_vel")) for r in XYZ)
    + (InputSpec(role="temp", unit="℃", required=False),),
    outputs=(OutputSpec(key="h", display="h", value_type="float"),))


class TestRightMargin(unittest.TestCase):
    def test_字段绑定离节拍5秒_标量绑定不平移(self):
        with tempfile.TemporaryDirectory() as d:
            f = _RecFetcher()
            s = Scheduler(client=_Client(), fetcher=f,
                          domains={"vib": LoadedDomain(_Dom(), DECL, frozenset({"infer"}), Path("m.py"))},
                          bindings=BindingStore(Path(d) / "b.db"), points=PointMap(Path(d) / "p.db"))
            s.run_once(fb({"x_vel": 9}, {"x_vel": "x"}), T0)
            s.run_once(fb({"x_vel": 9}, {}), T0)
            s.run_once(fb({"x_vel": 9}, {}, field_numbers={"x_vel": 7}), T0)   # 1.20：只给号也是字段绑定
        self.assertEqual(f.ends, [T0 - timedelta(seconds=STRUCT_RIGHT_MARGIN_SEC), T0,
                                  T0 - timedelta(seconds=STRUCT_RIGHT_MARGIN_SEC)])
        self.assertGreaterEqual(STRUCT_RIGHT_MARGIN_SEC, 5.0, "H-263：右端至少留 5 秒")


# ═══════════════════════════ 绑定时核对 ═══════════════════════════

class TestFindPointStruct(unittest.TestCase):
    def setUp(self):
        self.hs = StructHs()
        self.hs.add_struct(3, 1, "Vib", vel3())
        self.reg = StructRegistry(self.hs)

    def test_回看窗逐级放大_找到即止(self):
        self.hs.put(9, T0 - timedelta(hours=2), 3, 1, x=1.0, y=2.0, z=3.0)
        ps = find_point_struct(self.hs, self.reg, 9, now=T0)
        self.assertEqual((ps.struct.name, ps.struct.version), ("Vib", 1))
        self.assertEqual(ps.seen_at, T0 - timedelta(hours=2))
        self.assertEqual(len(self.hs.queries), 3, "1 分钟、1 小时都没有，1 天里找到")
        self.assertTrue(all(q.supportsStructValue and q.hisReq.method == hs.kLastTime
                            for q in self.hs.queries))

    def test_原地改单位_绑定核对立即看得到(self):
        # 取数先把描述缓存上，采集侧随即原地改单位（D-302）：绑定核对要拿到新的，不等缓存到期。
        v = self.hs.put(9, T0 - timedelta(seconds=10), 3, 1, x=1.0, y=2.0, z=3.0)
        self.reg.decode_value(v.StructVal, T0, Quality.OK)
        for f in self.hs.defs[(3, 1)].fields:
            f.unit = "m/s"
        ps = find_point_struct(self.hs, self.reg, 9, now=T0)
        self.assertEqual({f.unit for f in ps.struct.fields}, {"m/s"})

    def test_7天内没有值_拒(self):
        self.hs.put(9, T0 - timedelta(days=8), 3, 1, x=1.0, y=2.0, z=3.0)
        with self.assertRaisesRegex(StructBindError, "没有任何结构值"):
            find_point_struct(self.hs, self.reg, 9, now=T0)

    def test_标量点_拒(self):
        self.hs.put(8, T0 - timedelta(seconds=10), r8=1.0)
        with self.assertRaisesRegex(StructBindError, "不是结构值点"):
            find_point_struct(self.hs, self.reg, 8, now=T0)


class TestCheckBinding(unittest.TestCase):
    def setUp(self):
        self.hs = StructHs()
        self.calls = []

    def lookup_for(self, fields, sid=3, ver=1):
        d = self.hs.add_struct(sid, ver, "S", fields)

        def lookup(gid):
            self.calls.append(gid)
            return PointStruct(gid=gid, struct=d, seen_at=T0)
        return lookup

    def check(self, roles, fields, struct_fields):
        check_binding(fb(roles, fields), DECL.inputs, self.lookup_for(struct_fields))

    def test_全对放行(self):
        self.check({r: 9 for r in XYZ}, XYZ, vel3())

    def test_没有字段绑定一次hs都不拨(self):
        self.check({"x_vel": 9}, {}, vel3())
        self.assertEqual(self.calls, [])

    def test_字段不存在(self):
        with self.assertRaisesRegex(StructBindError, "没有字段 'w'"):
            self.check({"x_vel": 9}, {"x_vel": "w"}, vel3())

    def test_不是数值标量(self):
        for bad in (fld("x", hs.SFT_STRING, unit="mm/s", quantity="velocity"),
                    fld("x", hs.SFT_FLOAT, unit="mm/s", quantity="velocity", repeated=True),
                    fld("x", hs.SFT_NUMBUF, unit="mm/s", quantity="velocity", dtype=hs.NBD_F32),
                    fld("x", hs.SFT_BOOL, unit="mm/s", quantity="velocity")):
            with self.subTest(type=bad.type, repeated=bad.repeated):
                self.assertFalse(is_bindable(bad))
                with self.assertRaisesRegex(StructBindError, "不是数值标量"):
                    self.check({"x_vel": 9}, {"x_vel": "x"}, [bad])

    def test_单位不符_不换算只拒绝(self):
        with self.assertRaisesRegex(StructBindError, "单位是 g"):
            self.check({"x_vel": 9}, {"x_vel": "x"}, vel3(unit="g"))

    def test_物理量不符(self):
        with self.assertRaisesRegex(StructBindError, "物理量是 acceleration"):
            self.check({"x_vel": 9}, {"x_vel": "x"}, vel3(quantity="acceleration"))

    def test_物理量缺键也拒(self):
        with self.assertRaisesRegex(StructBindError, "未声明 quantity"):
            self.check({"x_vel": 9}, {"x_vel": "x"}, vel3(quantity=""))

    def test_域没声明物理量就不核(self):
        # temp 没声明 quantity：字段没有 attrs 也放行（只核单位）。
        self.check({"x_vel": 8, "temp": 9}, {"temp": "t"}, [fld("t", unit="℃")])

    def test_同记录组跨点_拒(self):
        lookup = self.lookup_for(vel3())
        with self.assertRaisesRegex(StructBindError, "同一个"):
            check_binding(fb({"x_vel": 9, "y_vel": 9, "z_vel": 10}, XYZ), DECL.inputs, lookup)

    def test_同记录组一半字段一半整点_拒(self):
        with self.assertRaisesRegex(StructBindError, "绑的却是整点"):
            self.check({"x_vel": 9, "y_vel": 9, "z_vel": 12}, {"x_vel": "x", "y_vel": "y"}, vel3())

    def test_组外角色可绑别的点(self):
        self.check({"x_vel": 9, "y_vel": 9, "z_vel": 9, "temp": 11},
                   {**XYZ, "temp": "t"}, vel3() + [fld("t", unit="℃")])

    def test_未声明的角色(self):
        with self.assertRaisesRegex(StructBindError, "不在域"):
            self.check({"nope": 9}, {"nope": "x"}, vel3())

    # ── 1.20 按字段号 ──
    def test_只给名_核对补上号与当前名(self):
        lookup = self.lookup_for(vel3())
        nums = {f.name: f.number for f in self.hs.defs[(3, 1)].fields}
        r = check_binding(fb({k: 9 for k in XYZ}, XYZ), DECL.inputs, lookup)
        self.assertEqual(r.numbers, {k: nums[v] for k, v in XYZ.items()})
        self.assertEqual((r.names, r.notes), (XYZ, []))

    def test_按号绑定_名不符按号并说明(self):
        # 字段原地改过名（号不变），平台送来的名还是旧的：按号取，不拒，但要说。
        lookup = self.lookup_for(vel3())
        nx = next(f.number for f in self.hs.defs[(3, 1)].fields if f.name == "x")
        r = check_binding(fb({"x_vel": 9}, {"x_vel": "x_old"}, field_numbers={"x_vel": nx}),
                          DECL.inputs, lookup)
        self.assertEqual((r.numbers, r.names), ({"x_vel": nx}, {"x_vel": "x"}))
        self.assertEqual(len(r.notes), 1)
        self.assertIn("x_old", r.notes[0])

    def test_只给号也行(self):
        lookup = self.lookup_for(vel3())
        ny = next(f.number for f in self.hs.defs[(3, 1)].fields if f.name == "y")
        r = check_binding(fb({"y_vel": 9}, {}, field_numbers={"y_vel": ny}), DECL.inputs, lookup)
        self.assertEqual(r.names, {"y_vel": "y"})

    def test_号不存在_拒(self):
        lookup = self.lookup_for(vel3())
        with self.assertRaisesRegex(StructBindError, "没有字段号 999"):
            check_binding(fb({"x_vel": 9}, {"x_vel": "x"}, field_numbers={"x_vel": 999}),
                          DECL.inputs, lookup)

    def test_按号的字段照样核单位(self):
        lookup = self.lookup_for(vel3(unit="g"))
        nx = next(f.number for f in self.hs.defs[(3, 1)].fields if f.name == "x")
        with self.assertRaisesRegex(StructBindError, "单位是 g"):
            check_binding(fb({"x_vel": 9}, {}, field_numbers={"x_vel": nx}), DECL.inputs, lookup)

    def test_同记录组_只给号的也算字段绑定(self):
        lookup = self.lookup_for(vel3())
        nx = next(f.number for f in self.hs.defs[(3, 1)].fields if f.name == "x")
        with self.assertRaisesRegex(StructBindError, "绑的却是整点"):
            check_binding(fb({"x_vel": 9, "y_vel": 12}, {}, field_numbers={"x_vel": nx}),
                          DECL.inputs, lookup)


# ═══════════════════════════ 绑定表 ═══════════════════════════

class TestBindingStoreFields(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "b.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_字段存得下读得回(self):
        s = BindingStore(self.db)
        s.put(fb({r: 9 for r in XYZ}, XYZ))
        self.assertEqual(s.get("vib", "dev1").fields, XYZ)
        s.close()

    def test_老库补列_老绑定一律整点(self):
        import sqlite3
        con = sqlite3.connect(str(self.db))
        con.execute("CREATE TABLE bindings (domain TEXT NOT NULL, binding TEXT NOT NULL,"
                    " roles_json TEXT NOT NULL, interval_sec REAL NOT NULL, window_sec REAL NOT NULL,"
                    " enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT, PRIMARY KEY (domain, binding))")
        con.execute("INSERT INTO bindings(domain,binding,roles_json,interval_sec,window_sec)"
                    " VALUES('vib','dev1','{\"x_vel\": 9}',60,60)")
        con.commit(); con.close()
        s = BindingStore(self.db)
        b = s.get("vib", "dev1")
        self.assertEqual((b.roles, b.fields, b.field_numbers), ({"x_vel": 9}, {}, {}))
        s.close()

    def test_字段号存得下读得回(self):
        s = BindingStore(self.db)
        s.put(fb({"x_vel": 9}, {"x_vel": "x"}, field_numbers={"x_vel": 7}))
        self.assertEqual(s.get("vib", "dev1").field_numbers, {"x_vel": 7})
        s.close()

    def test_1_19的库补列_老字段绑定照旧按名(self):
        import sqlite3
        s = BindingStore(self.db)
        s.put(fb({"x_vel": 9}, {"x_vel": "x"}))
        s.close()
        con = sqlite3.connect(str(self.db))
        con.execute("ALTER TABLE bindings DROP COLUMN field_numbers_json")
        con.commit(); con.close()
        s = BindingStore(self.db)
        b = s.get("vib", "dev1")
        self.assertEqual((b.fields, b.field_numbers, b.field_roles), ({"x_vel": "x"}, {}, ["x_vel"]))
        s.close()

    def test_字段号非法或没点_拒(self):
        s = BindingStore(self.db)
        for nums, msg in (({"x_vel": 0}, "非法"), ({"x_vel": True}, "非法"), ({"y_vel": 7}, "却没绑点")):
            with self.subTest(nums=nums), self.assertRaisesRegex(ValueError, msg):
                s.put(fb({"x_vel": 9}, {}, field_numbers=nums))
        s.close()

    def test_有字段没点_拒(self):
        s = BindingStore(self.db)
        with self.assertRaisesRegex(ValueError, "却没绑点"):
            s.put(fb({"x_vel": 9}, {"y_vel": "y"}))
        s.close()

    def test_空字段名_拒(self):
        s = BindingStore(self.db)
        with self.assertRaisesRegex(ValueError, "字段名为空"):
            s.put(fb({"x_vel": 9}, {"x_vel": ""}))
        s.close()


# ═══════════════════════════ 对外口 ═══════════════════════════

DOM = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec

class D(Domain):
    key = "vib"
    display = "振动"
    version = "1"
    def declare(self):
        return Declaration(
            inputs=tuple(InputSpec(role=a + "_vel", unit="mm/s", quantity="velocity", axis=a,
                                   record="p1", required=(a == "x")) for a in "xyz"),
            outputs=(OutputSpec(key="h", display="h", value_type="float"),))
    def infer(self, frame):
        return []
'''


class TestApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "domains").mkdir()
        (root / "domains" / "vib.py").write_text(DOM, encoding="utf-8")
        loaded, _ = discover(root / "domains")
        self.hs = StructHs()
        self.hs.add_struct(3, 1, "Vib", vel3())
        now = datetime.now(UTC)
        self.hs.put(9, now - timedelta(seconds=5), 3, 1, x=1.0, y=2.0, z=3.0)
        self.hs.put(8, now - timedelta(seconds=5), r8=1.0)
        self.bindings = BindingStore(root / "b.db")
        self.svc = ApiService(guid="g", version="0", logstore=LogStore(capacity=10),
                              domains={d.key: d for d in loaded}, bindings=self.bindings,
                              hs_client=self.hs, structs=StructRegistry(self.hs))
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=2),
                                  handlers=(build_handler(self.svc),))
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.ch = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)])

    def tearDown(self):
        self.ch.close(); self.server.stop(0); self.bindings.close(); self._tmp.cleanup()

    def call(self, method, req, res_cls):
        return self.ch.unary_unary(f"/{SERVICE}/{method}",
                                   request_serializer=lambda m: m.SerializeToString(),
                                   response_deserializer=res_cls.FromString)(req, timeout=5)

    def put(self, roles, fields):
        b = pb.Binding(domain="vib", binding="dev1")
        b.roles.update(roles)
        b.role_fields.update(fields)
        return self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)

    def test_字段绑定核对通过即存_列出时带字段(self):
        r = self.put({k: 9 for k in XYZ}, XYZ)
        self.assertTrue(r.ok, r.message)
        got = self.call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply).bindings[0]
        self.assertEqual(dict(got.role_fields), XYZ)

    def test_核对不过_拒且不存(self):
        r = self.put({"x_vel": 9}, {"x_vel": "w"})
        self.assertFalse(r.ok)
        self.assertIn("没有字段", r.message)
        self.assertIsNone(self.bindings.get("vib", "dev1"))

    def test_没接实时库_字段绑定一律拒(self):
        self.svc._hs = None
        r = self.put({"x_vel": 9}, {"x_vel": "x"})
        self.assertFalse(r.ok)
        self.assertIn("无法核对", r.message)

    def test_没接实时库_纯标量绑定照旧能存(self):
        self.svc._hs = None
        self.assertTrue(self.put({"x_vel": 8}, {}).ok)

    def test_域声明带出物理量与同记录组(self):
        d = self.call("ListDomains", pb.DomainsRequest(), pb.DomainsReply).domains[0]
        x = next(i for i in d.inputs if i.role == "x_vel")
        self.assertEqual((x.quantity, x.record), ("velocity", "p1"))

    def test_DescribeStructPoint列出字段(self):
        r = self.call("DescribeStructPoint", pb.DescribeStructPointReq(point_id=9),
                      pb.DescribeStructPointRes)
        self.assertTrue(r.ok, r.message)
        self.assertEqual((r.struct_name, r.struct_id, r.struct_version), ("Vib", 3, 1))
        y = next(f for f in r.fields if f.name == "y")
        self.assertEqual((y.type, y.unit, y.quantity, y.axis, y.bindable),
                         ("float", "mm/s", "velocity", "y", True))
        self.assertEqual(y.number, next(f.number for f in self.hs.defs[(3, 1)].fields if f.name == "y"))

    # ── 1.20 按字段号 ──
    def _nums(self):
        return {f.name: f.number for f in self.hs.defs[(3, 1)].fields}

    def test_老平台只给名_存下时补号_列出两格都全(self):
        r = self.put({k: 9 for k in XYZ}, XYZ)
        self.assertTrue(r.ok, r.message)
        got = self.call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply).bindings[0]
        self.assertEqual(dict(got.role_fields), XYZ)
        self.assertEqual(dict(got.role_field_numbers), {k: self._nums()[v] for k, v in XYZ.items()})

    def test_名与号不符_按号存_回执写明(self):
        b = pb.Binding(domain="vib", binding="dev1")
        b.roles.update({"x_vel": 9})
        b.role_fields.update({"x_vel": "x_旧名"})
        b.role_field_numbers.update({"x_vel": self._nums()["x"]})
        r = self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)
        self.assertTrue(r.ok, r.message)
        self.assertIn("x_旧名", r.message)
        saved = self.bindings.get("vib", "dev1")
        self.assertEqual((saved.fields, saved.field_numbers), ({"x_vel": "x"}, {"x_vel": self._nums()["x"]}))

    def test_号不存在_拒且不存(self):
        b = pb.Binding(domain="vib", binding="dev1")
        b.roles.update({"x_vel": 9})
        b.role_field_numbers.update({"x_vel": 999})
        r = self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)
        self.assertFalse(r.ok)
        self.assertIn("没有字段号 999", r.message)
        self.assertIsNone(self.bindings.get("vib", "dev1"))

    def test_DescribeStructPoint标量点与0号如实拒(self):
        for gid, why in ((8, "不是结构值点"), (0, "不是可用的 globalId")):
            r = self.call("DescribeStructPoint", pb.DescribeStructPointReq(point_id=gid),
                          pb.DescribeStructPointRes)
            self.assertFalse(r.ok)
            self.assertIn(why, r.message)


if __name__ == "__main__":
    unittest.main()
