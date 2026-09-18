"""hs 客户端的回归 —— 全部是**离线**用例（只验消息构造，不连网）。

钉的四件：
  · 快照**必须全量、且不含 IDENTITY**；
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
