"""结构值：注册、绑点、解码（定案 `doc/结构值点定案.md` S1~S4 / P1~P6）。

★这一组钉的全是**静默出事**那一类 —— 它们的共同点是运行期不报错：

  ① 探不到能力位还照写 ⇒ 老引擎把 `StructValue` **存成「无值」且回成功**，数据静默丢；
  ② `allowNewVersion` 带上 ⇒ "改错一个字段"悄悄变成新版本、版本号被刷成启动次数；
  ③ 拿**最新版**描述符去解**老值** ⇒ 静默错读；
  ④ 注册失败却拦住启动 ⇒ 一条诊断的问题变成整个服务起不来（反向的错）。

不连真实时库：这里用**假 hs 客户端**，它按契约的形状回消息，
并且**自己造 `FileDescriptorProto`** —— 也就是把 hs 那一侧"分配字段号 + 出描述符"
的行为照契约模拟一遍，于是编解码这条路能被完整走通。
"""

import unittest
from datetime import datetime, timezone

from google.protobuf import descriptor_pb2

from aiintegration.hsproto import historystore_pb2 as hs
from aiintegration.quality import Quality
from aiintegration.structreg import (
    ACC_VT_RECORD, FEATURE_STRUCT_VALUE, PARAS_ACC, PARAS_STRUCT_REF,
    StructRegistry, list_to_numbuf, numbuf_to_list, to_struct_def,
)
from aiintegration.types import StructFieldSpec, StructSpec

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)

SPEC = StructSpec(
    name="AI_PressCurve", display="压装曲线",
    fields=(
        StructFieldSpec(name="sample_rate", type="int32", display="采样率", unit="Hz"),
        StructFieldSpec(name="position", type="numbuf", dtype="f32", shape=(-1,),
                        display="位移", unit="mm"),
        StructFieldSpec(name="force", type="numbuf", dtype="f32", shape=(-1,),
                        display="力", unit="N"),
    ))

#: 域自述类型 → protobuf 字段类型（假 hs 用它造描述符，与真 hs 的职责对应）
_PB_TYPE = {
    hs.SFT_INT32: descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    hs.SFT_NUMBUF: descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
    hs.SFT_STRING: descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
}


class FakeHs:
    """按契约形状应答的假实时库。**字段号由它分配** —— 与真 hs 的分工一致。"""

    def __init__(self, *, feature=True, put_error=None, start_number=7):
        self.feature = feature
        self.put_error = put_error
        self.start_number = start_number   # 故意不从 1 开始：钉住我方不写死字段号
        self.defs = {}
        self.put_calls = []
        self.get_calls = []

    def has_feature(self, name):
        return self.feature and name == FEATURE_STRUCT_VALUE

    def put_struct(self, struct_def, allow_new_version=False):
        self.put_calls.append((struct_def.name, allow_new_version))
        if self.put_error:
            raise RuntimeError(self.put_error)
        d = hs.StructDef()
        d.CopyFrom(struct_def)
        d.id = 42
        d.version = 1
        for i, f in enumerate(d.fields):
            f.number = self.start_number + i
        self.defs[(d.name, d.version)] = d
        res = hs.PutStructRes(created=True, newVersion=False)
        getattr(res, "def").CopyFrom(d)
        return res

    def get_struct(self, *, name="", struct_id=0, version=0, with_descriptor=False):
        self.get_calls.append((name, version, with_descriptor))
        key = (name, version if version else 1)
        d = self.defs.get(key)
        if d is None:
            raise RuntimeError(f"NOT_FOUND {key}")
        res = hs.GetStructRes()
        getattr(res, "def").CopyFrom(d)
        if with_descriptor:
            res.fileDescriptor = self._descriptor(d)
        return res

    @staticmethod
    def _descriptor(d) -> bytes:
        """照契约造 `FileDescriptorProto`：message 全名 = `hs.structs.<name>_v<version>`。"""
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.name = f"hs/structs/{d.name}_v{d.version}.proto"
        fdp.package = "hs.structs"
        fdp.syntax = "proto3"
        msg = fdp.message_type.add(name=f"{d.name}_v{d.version}")
        for f in d.fields:
            msg.field.add(name=f.name, number=f.number, type=_PB_TYPE[f.type],
                          label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL)
        return fdp.SerializeToString()

    def encode(self, name, version, values: dict) -> bytes:
        """按已注册的描述造一个值（模拟采集侧写入）。"""
        from google.protobuf import descriptor_pool
        from aiintegration.structreg import message_class
        pool = descriptor_pool.DescriptorPool()
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(self._descriptor(self.defs[(name, version)]))
        pool.Add(fdp)
        # ★用与生产同一个兼容层取类 —— 两个 protobuf 大版本的 API 互斥，见 structreg.message_class
        cls = message_class(pool.FindMessageTypeByName(f"hs.structs.{name}_v{version}"), pool)
        m = cls()
        for k, v in values.items():
            setattr(m, k, v)
        return m.SerializeToString()


def sv(struct_id, version, data):
    from aiintegration.hsproto import daqcontract_pb2 as daq
    return daq.StructValue(StructId=struct_id, StructVersion=version, Data=data)


class TestCapabilityGate(unittest.TestCase):
    """★探不到能力位 ⇒ 整条路不启用。这是**最要命**的一条。"""

    def test_探不到能力位就不注册(self):
        fake = FakeHs(feature=False)
        reg = StructRegistry(fake)
        with self.assertLogs("aiintegration.structreg", level="WARNING") as log:
            n = reg.register([SPEC])
        self.assertEqual(n, 0)
        self.assertEqual(fake.put_calls, [],
                         "★没有能力位却发了 PutStruct —— 老引擎会把结构值存成「无值」且回成功")
        self.assertIn("struct-value", "\n".join(log.output))
        self.assertTrue(reg.degraded, "降级了却不可见")

    def test_探不到能力位就不给建点的Paras(self):
        reg = StructRegistry(FakeHs(feature=False))
        reg.register([SPEC])
        self.assertEqual(reg.paras_for(SPEC.name), {},
                         "★回了 Paras 就会去建结构值点，而对端根本存不了")

    def test_能力位探测只做一次(self):
        class Counting(FakeHs):
            def __init__(self):
                super().__init__()
                self.n = 0

            def has_feature(self, name):
                self.n += 1
                return True

        fake = Counting()
        reg = StructRegistry(fake)
        reg.available, reg.available, reg.available
        self.assertEqual(fake.n, 1, "每次都去拨一遍，等于给每拍加一次往返")


class TestRegister(unittest.TestCase):
    def test_注册后能拿到绑点要的两个键(self):
        reg = StructRegistry(FakeHs())
        self.assertEqual(reg.register([SPEC]), 1)
        paras = reg.paras_for(SPEC.name)
        self.assertEqual(paras[PARAS_STRUCT_REF], "42")
        self.assertEqual(paras[PARAS_ACC], str(ACC_VT_RECORD),
                         "★结构值点的 Acc 必须是 VT_RECORD(36)，与字节点 VT_BLOB(65) 分开")

    def test_allowNewVersion恒为假(self):
        """★带上它，'改错一个字段'会悄悄变成新版本、版本号还被刷成启动次数。"""
        fake = FakeHs()
        StructRegistry(fake).register([SPEC])
        self.assertEqual(fake.put_calls, [("AI_PressCurve", False)])

    def test_注册失败不抛且降级可见(self):
        """★一条结构注册不上，受影响的只有用它的那条诊断，不该让整个服务起不来。"""
        fake = FakeHs(put_error="FAILED_PRECONDITION 同名不同描述")
        reg = StructRegistry(fake)
        with self.assertLogs("aiintegration.structreg", level="ERROR"):
            n = reg.register([SPEC])      # 不抛
        self.assertEqual(n, 0)
        self.assertEqual(len(reg.degraded), 1)
        self.assertIn("同名不同描述", reg.degraded[0])
        self.assertEqual(reg.paras_for(SPEC.name), {}, "没注册上却给了 Paras")

    def test_域自述翻成StructDef不填任何输出字段(self):
        """id / version / number 都由 hs 分配，我方填了就是替对方发明事实。"""
        d = to_struct_def(SPEC)
        self.assertEqual(d.id, 0)
        self.assertEqual(d.version, 0)
        self.assertTrue(all(f.number == 0 for f in d.fields))
        self.assertEqual([f.name for f in d.fields], ["sample_rate", "position", "force"])
        self.assertEqual(d.fields[1].dtype, hs.NBD_F32)
        self.assertEqual(list(d.fields[1].shape), [-1])


class TestDecode(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHs()
        self.reg = StructRegistry(self.fake)
        self.reg.register([SPEC])

    def test_解出标量与数值缓冲(self):
        pos = list_to_numbuf([0.0, 1.5, 3.0], "f32")
        force = list_to_numbuf([10.0, 220.0, 55.0], "f32")
        data = self.fake.encode("AI_PressCurve", 1,
                                {"sample_rate": 50, "position": pos.data, "force": force.data})
        s = self.reg.decode("AI_PressCurve", sv(42, 1, data), T0, Quality.OK)
        self.assertEqual(s.struct_name, "AI_PressCurve")
        self.assertEqual(s.struct_version, 1)
        self.assertEqual(s.fields["sample_rate"], 50)
        self.assertEqual(numbuf_to_list(s.fields["position"]), [0.0, 1.5, 3.0])
        self.assertEqual(numbuf_to_list(s.fields["force"]), [10.0, 220.0, 55.0])
        self.assertEqual(s.fields["position"].dtype, "f32", "dtype 没带出来，域就不知道按什么解")

    def test_不写死字段号(self):
        """★字段号是 hs 分配的。假 hs 故意从 7 开始编号 —— 写死 1/2/3 的实现会在这里解错。"""
        self.assertEqual([f.number for f in self.fake.defs[("AI_PressCurve", 1)].fields],
                         [7, 8, 9])
        pos = list_to_numbuf([2.0], "f32")
        data = self.fake.encode("AI_PressCurve", 1, {"sample_rate": 7, "position": pos.data})
        s = self.reg.decode("AI_PressCurve", sv(42, 1, data), T0, Quality.OK)
        self.assertEqual(s.fields["sample_rate"], 7)

    def test_按值自带的版本取描述符(self):
        """★不拿最新版解老值 —— 那是静默错读（契约约束 4）。"""
        self.fake.get_calls.clear()
        data = self.fake.encode("AI_PressCurve", 1, {"sample_rate": 50})
        self.reg.decode("AI_PressCurve", sv(42, 1, data), T0, Quality.OK)
        self.assertTrue(self.fake.get_calls, "根本没去取描述符")
        self.assertEqual(self.fake.get_calls[0][1], 1,
                         "★取描述符时没带版本（= 会拿最新版去解老值）")

    def test_描述符只取一次(self):
        data = self.fake.encode("AI_PressCurve", 1, {"sample_rate": 50})
        self.fake.get_calls.clear()
        for _ in range(3):
            self.reg.decode("AI_PressCurve", sv(42, 1, data), T0, Quality.OK)
        self.assertEqual(len(self.fake.get_calls), 1, "每个值都去拨一次描述符，太贵")

    def test_没有描述符就抛而不是猜(self):
        class NoDesc(FakeHs):
            def get_struct(self, **kw):
                res = super().get_struct(**kw)
                res.fileDescriptor = b""
                return res

        fake = NoDesc()
        reg = StructRegistry(fake)
        reg.register([SPEC])
        data = fake.encode("AI_PressCurve", 1, {"sample_rate": 1})
        with self.assertRaises(Exception):
            reg.decode("AI_PressCurve", sv(42, 1, data), T0, Quality.OK)

    def test_质量与时刻原样带过去(self):
        data = self.fake.encode("AI_PressCurve", 1, {"sample_rate": 50})
        s = self.reg.decode("AI_PressCurve", sv(42, 1, data), T0, Quality.INPUT_BAD)
        self.assertIs(s.quality, Quality.INPUT_BAD, "质量码被吞了")
        self.assertEqual(s.t, T0)


class TestProtobufCompat(unittest.TestCase):
    """★两处 protobuf API **两版互斥**，都由三环境串行逼出来（3.12 绿、py310 红）。

    只在一个环境跑过就会漏掉，而**现场恰好是 7.36.1 那一边**。
    """

    def test_repeated判定两版都通用(self):
        from google.protobuf import descriptor_pb2, descriptor_pool
        from aiintegration.structreg import is_repeated
        pool = descriptor_pool.DescriptorPool()
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.name, fdp.package, fdp.syntax = "c.proto", "c", "proto3"
        m = fdp.message_type.add(name="M")
        m.field.add(name="one", number=1,
                    type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
                    label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL)
        m.field.add(name="many", number=2,
                    type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
                    label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED)
        pool.Add(fdp)
        by = {f.name: f for f in pool.FindMessageTypeByName("c.M").fields}
        self.assertFalse(is_repeated(by["one"]))
        self.assertTrue(is_repeated(by["many"]))

    def test_取消息类两版都work(self):
        from google.protobuf import descriptor_pb2, descriptor_pool
        from aiintegration.structreg import message_class
        pool = descriptor_pool.DescriptorPool()
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.name, fdp.package, fdp.syntax = "c2.proto", "c2", "proto3"
        m = fdp.message_type.add(name="M")
        m.field.add(name="a", number=1,
                    type=descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
                    label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL)
        pool.Add(fdp)
        cls = message_class(pool.FindMessageTypeByName("c2.M"), pool)
        self.assertEqual(cls(a=5).a, 5)


class TestNumBufRoundTrip(unittest.TestCase):
    def test_往返(self):
        for dtype in ("f32", "f64", "i32", "u8"):
            with self.subTest(dtype=dtype):
                vals = [1, 2, 3] if dtype not in ("f32", "f64") else [1.5, 2.5, 3.5]
                self.assertEqual(numbuf_to_list(list_to_numbuf(vals, dtype)), vals)

    def test_大端也认(self):
        b = list_to_numbuf([1.5, 2.5], "f32", big_endian=True)
        self.assertTrue(b.big_endian)
        self.assertEqual(numbuf_to_list(b), [1.5, 2.5])


if __name__ == "__main__":
    unittest.main()
