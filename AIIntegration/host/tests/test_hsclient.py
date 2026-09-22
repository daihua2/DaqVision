"""hs 客户端的回归 —— 全部是**离线**用例（只验消息构造，不连网）。

钉的四件：
  · 快照**必须全量、且永不含 IDENTITY(5)**；自报身份走 op 7，三道闸（开关 / 能力位 / kind 非空）缺一不发；
  · VQT 的 V/Q/T 三者都来自 Finding，一个都不在客户端现编（尤其 T 不许填 now()）；
  · 坏结论走**坏值锚点**（NullValue + 质量码），而不是"不发"；
  · 能力位**缺失 = 不支持**，且"探不到"与"没有这一位"在 `has_feature` 上处置相同。
"""

import unittest
from datetime import datetime, timedelta, timezone

from aiintegration.hsclient import (
    PARAS_ACC, build_entity, build_snapshot_frames, build_vqt, value_type_to_varenum,
)
from aiintegration.hsproto import historystore_pb2 as hs
from aiintegration.pointmap import PointRow
from aiintegration.quality import STATUS_OK, STATUS_QUALITY_NOT_CONNECTED, Quality
from aiintegration.types import Finding

UTC = timezone.utc
T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def row(local_id=1000, key="health_score", vt="float", unit="分"):
    return PointRow("vib", "dev1", key, local_id, f"AI.vib.dev1.{key}", unit, vt)


class TestSnapshot(unittest.TestCase):
    def test_首尾是BEGIN和END(self):
        f = build_snapshot_frames([row()])
        self.assertEqual(f[0].op, hs.EntityConfigPush.SNAPSHOT_BEGIN)
        self.assertEqual(f[-1].op, hs.EntityConfigPush.SNAPSHOT_END)

    def test_不含IDENTITY帧(self):
        # 过渡期定案：不发身份帧 ⇒ AICloud 对账器 HasIdentity=false ⇒ 不会被建成"网关"实体。
        f = build_snapshot_frames([row(), row(1001, "fault_type", "string", "")])
        self.assertFalse(any(x.op == hs.EntityConfigPush.IDENTITY for x in f))

    def test_每个点一帧PUT且带实体(self):
        rows = [row(1000), row(1001, "fault_type", "string", "")]
        f = build_snapshot_frames(rows)
        puts = [x for x in f if x.op == hs.EntityConfigPush.PUT]
        self.assertEqual(len(puts), 2)
        self.assertEqual([p.id for p in puts], [1000, 1001])
        self.assertEqual(puts[0].entity.Id, 1000)

    def test_空点表也发完整的一轮(self):
        # 一个点都没有时仍要发 BEGIN+END：那表达的是"我这轮就是没有点"，
        # 与"没发快照"（hs 保留旧快照）是两回事。
        f = build_snapshot_frames([])
        self.assertEqual([x.op for x in f],
                         [hs.EntityConfigPush.SNAPSHOT_BEGIN, hs.EntityConfigPush.SNAPSHOT_END])


class TestEntity(unittest.TestCase):
    def test_Paras带Acc这是采集点的判别键(self):
        import json
        e = build_entity(row())
        paras = json.loads(e.Paras)
        self.assertIn(PARAS_ACC, paras)
        self.assertEqual(paras[PARAS_ACC], value_type_to_varenum("float"))

    def test_单位进Paras(self):
        import json
        self.assertEqual(json.loads(build_entity(row()).Paras)["Unit"], "分")

    def test_未知值类型被拒(self):
        with self.assertRaises(ValueError):
            value_type_to_varenum("双精度")


class TestBuildVQT(unittest.TestCase):
    def test_TagId用localId(self):
        # 受限连接上投递用本源 localId（查询时才是 gid）。
        v = build_vqt(1234, Finding("k", 1.0, Quality.OK, T0))
        self.assertEqual(v.TagId, 1234)

    def test_时刻来自Finding而不是now(self):
        # 在这里填 now() 就是"把算完的时刻当成样本时刻"，图上会显示一个不属于那个值的时刻。
        v = build_vqt(1, Finding("k", 1.0, Quality.OK, T0))
        self.assertEqual(v.TimStampUtc.ToDatetime().replace(tzinfo=UTC), T0)

        t1 = T0 - timedelta(hours=3)
        v1 = build_vqt(1, Finding("k", 1.0, Quality.OK, t1))
        self.assertEqual(v1.TimStampUtc.ToDatetime().replace(tzinfo=UTC), t1)

    def test_质量来自Finding而不是恒Ok(self):
        self.assertEqual(build_vqt(1, Finding("k", 1.0, Quality.OK, T0)).Quality.Code, STATUS_OK)
        v = build_vqt(1, Finding("k", None, Quality.NO_INPUT, T0))
        self.assertEqual(v.Quality.Code, STATUS_QUALITY_NOT_CONNECTED)

    def test_坏结论发坏值锚点而不是不发(self):
        # "不发"等于"这段没数据"，而真相是"这段算不出来"，两者对下游含义不同。
        v = build_vqt(1, Finding("k", None, Quality.MODEL_NOT_LOADED, T0))
        self.assertEqual(v.WhichOneof("Value"), "NullValue")
        self.assertTrue(v.NullValue)
        self.assertNotEqual(v.Quality.Code, STATUS_OK)

    def test_各值类型落到对应字段(self):
        cases = [
            (1.5, "R8"), (7, "I4"), (True, "Bool"), ("不平衡", "String"),
        ]
        for val, field in cases:
            with self.subTest(val=val):
                v = build_vqt(1, Finding("k", val, Quality.OK, T0))
                self.assertEqual(v.WhichOneof("Value"), field)

    def test_bool先于int判定(self):
        # Python 里 bool 是 int 的子类，判反了 True 会变成 I4=1。
        v = build_vqt(1, Finding("k", True, Quality.OK, T0))
        self.assertEqual(v.WhichOneof("Value"), "Bool")


if __name__ == "__main__":
    unittest.main()


class TestFeatures(unittest.TestCase):
    """能力位探测 —— `PingRes.features`（`H-247 §5` 起放上 `Ping`）。

    ★为什么非有这一条不可（`H-246 §2.4`）：老引擎不认识 `StructValue`，
      会把它**存成"无值"且回成功** —— 静默丢数据，写入侧一点异常都看不到。
      能力位是这一类的唯一防线，所以"探不到"绝不能被当成"那就当它有"。
    """

    def client(self, features=None, boom=False):
        from aiintegration.hsclient import HsClient, HsConfig
        c = HsClient(HsConfig(read_addr="127.0.0.1:1", write_addr=""))

        def fake_ping():
            if boom:
                raise RuntimeError("连不上")
            return hs.PingRes(features=list(features or ()))

        c.ping = fake_ping
        return c

    def test_读得到能力位(self):
        c = self.client(["struct-value", "media"])
        self.assertEqual(c.features(), frozenset({"struct-value", "media"}))
        self.assertTrue(c.has_feature("struct-value"))

    def test_老引擎回空集合而不是报错(self):
        """proto3 未知字段静默丢 ⇒ 老引擎这一格就是空的。"""
        c = self.client([])
        self.assertEqual(c.features(), frozenset())
        self.assertFalse(c.has_feature("struct-value"),
                         "缺失被当成了'有' —— 那正是静默丢数据的入口")

    def test_探不到时has_feature按不支持处理(self):
        c = self.client(boom=True)
        with self.assertLogs("aiintegration.hsclient", level="WARNING"):
            self.assertFalse(c.has_feature("struct-value"))

    def test_探不到时features把异常交给调用方(self):
        """★与 has_feature 有意不同：调用方要分得清"连不上"与"连上了但没这一位"。"""
        c = self.client(boom=True)
        with self.assertRaises(RuntimeError):
            c.features()


class TestSourceIdentity(unittest.TestCase):
    """自报身份（op 7，`SOURCE_IDENTITY`）—— 三道闸与摆放位置。

    ★为什么每一道都要钉（`C-50 §4`、`AI-61 §3`、`H-272 §4.3`）：
      发错的后果是平台把我方建成一台"网关"，而那台**删不掉**；误点「整台移出」则连历史一起清、不可逆。
      ① 开关关着就不发 —— 何时打开由往来函定，不由代码判断；
      ② 对端没有能力位就不发 —— 老引擎静默吞掉 op 7、回成功；
      ③ kind 空不构造 —— 空 kind 的身份行在读侧与网关**逐位相同**。
    """

    def setUp(self):
        from aiintegration.hsclient import SourceIdentityInfo
        self.info = SourceIdentityInfo(
            name="AI 集成服务(AISERVER)", des="AI 算法集成服务 · 契约 1.10 · 4 个域",
            attrs=(("hostName", "AISERVER"), ("version", "0.1.0+src1"), ("app", "AIIntegration")))

    def client(self, *, info, features=(), boom=False):
        from aiintegration.hsclient import HsClient, HsConfig
        c = HsClient(HsConfig(read_addr="127.0.0.1:1", write_addr="", source_identity=info))
        self.pings = 0

        def fake_ping():
            self.pings += 1
            if boom:
                raise RuntimeError("连不上")
            return hs.PingRes(features=list(features))

        c.ping = fake_ping
        return c

    # ── 帧本身 ────────────────────────────────────────────────────────────
    def test_身份帧是op7且kind为ai_service(self):
        from aiintegration.hsclient import SOURCE_KIND, build_source_identity_frame
        f = build_source_identity_frame(self.info)
        self.assertEqual(f.op, hs.EntityConfigPush.SOURCE_IDENTITY)
        self.assertEqual(f.source.kind, "ai-service")
        self.assertEqual(SOURCE_KIND, "ai-service", "改 kind 就是改对外约定（AI-61 §4），要先发函")
        self.assertEqual(f.source.name, "AI 集成服务(AISERVER)")
        self.assertEqual(dict(f.source.attrs)["app"], "AIIntegration")
        # 载荷走 source（字段 7），不借网关那一格
        self.assertFalse(f.HasField("gateway"))

    def test_kind空拒绝构造(self):
        from aiintegration.hsclient import build_source_identity_frame
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                build_source_identity_frame(self.info, kind=bad)

    def test_name空拒绝构造(self):
        from aiintegration.hsclient import SourceIdentityInfo, build_source_identity_frame
        with self.assertRaises(ValueError):
            build_source_identity_frame(SourceIdentityInfo(name=" "))

    def test_身份帧在最前_在快照事务之外(self):
        f = build_snapshot_frames([row(), row(1001, "fault_type", "string", "")], identity=self.info)
        ops = [x.op for x in f]
        self.assertEqual(ops[0], hs.EntityConfigPush.SOURCE_IDENTITY)
        self.assertEqual(ops[1], hs.EntityConfigPush.SNAPSHOT_BEGIN)
        self.assertEqual(ops[-1], hs.EntityConfigPush.SNAPSHOT_END)
        self.assertEqual(ops.count(hs.EntityConfigPush.SOURCE_IDENTITY), 1)

    def test_带身份时也永不含IDENTITY5(self):
        f = build_snapshot_frames([row()], identity=self.info)
        self.assertFalse(any(x.op == hs.EntityConfigPush.IDENTITY for x in f))

    def test_不给身份时帧序列与原来逐帧相同(self):
        rows = [row(), row(1001, "fault_type", "string", "")]
        self.assertEqual(build_snapshot_frames(rows), build_snapshot_frames(rows, identity=None))
        self.assertFalse(any(x.op == hs.EntityConfigPush.SOURCE_IDENTITY
                             for x in build_snapshot_frames(rows)))

    # ── 闸 ① ② ────────────────────────────────────────────────────────────
    def test_开关关着不发且不去探能力位(self):
        c = self.client(info=None, features=["identity-source-kind"])
        f = c.snapshot_frames([row()])
        self.assertEqual(f[0].op, hs.EntityConfigPush.SNAPSHOT_BEGIN)
        self.assertEqual(self.pings, 0, "开关关着还去 Ping 对端 —— 没有理由多打一次")

    def test_开关开_对端有能力位才发(self):
        c = self.client(info=self.info, features=["identity-source-kind", "struct-value"])
        f = c.snapshot_frames([row()])
        self.assertEqual(f[0].op, hs.EntityConfigPush.SOURCE_IDENTITY)
        self.assertEqual(f[0].source.kind, "ai-service")

    def test_开关开_老引擎没有能力位不发且吵一句(self):
        """现网 1.9.445 就是这个情况：发了会被静默吞掉、回成功（H-272 §4.3）。"""
        c = self.client(info=self.info, features=["struct-value"])
        with self.assertLogs("aiintegration.hsclient", level="WARNING") as cm:
            f = c.snapshot_frames([row()])
        self.assertEqual(f[0].op, hs.EntityConfigPush.SNAPSHOT_BEGIN)
        self.assertTrue(any("identity-source-kind" in m for m in cm.output))

    def test_开关开_探不到能力位按没有处理(self):
        c = self.client(info=self.info, boom=True)
        with self.assertLogs("aiintegration.hsclient", level="WARNING"):
            f = c.snapshot_frames([row()])
        self.assertFalse(any(x.op == hs.EntityConfigPush.SOURCE_IDENTITY for x in f))

    def test_每次推快照都重新判_hs重启后第一次重推即补回(self):
        """身份不落盘，hs 重启即丢；我方每次推快照都是一条新流，每条都要带（H-272 §4.2）。"""
        c = self.client(info=self.info, features=["identity-source-kind"])
        for _ in range(3):
            self.assertEqual(c.snapshot_frames([row()])[0].op,
                             hs.EntityConfigPush.SOURCE_IDENTITY)
        self.assertEqual(self.pings, 3)
