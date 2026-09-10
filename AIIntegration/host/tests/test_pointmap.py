"""结论点 localId 的回归 —— 钉的是"同一个三元组恒回同一个号、号绝不回收"。

换一次 localId 就等于新建一个点，旧点变成没人写的孤儿，而按对账铁律**绝不自动删**。
"""

import tempfile
import unittest
from pathlib import Path

from aiintegration.pointmap import DEFAULT_BASE_LOCAL_ID, PointMap, default_point_name


class TestPointMap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "points.db"

    def tearDown(self):
        self._tmp.cleanup()

    def pm(self):
        return PointMap(self.db)

    def ensure(self, m, domain="vib", binding="dev1", key="health_score",
               name=None, unit="分", vt="float"):
        return m.ensure(domain, binding, key,
                        name=name or default_point_name(domain, binding, key),
                        unit=unit, value_type=vt)

    def test_同一个三元组恒回同一个号(self):
        m = self.pm()
        a = self.ensure(m)
        b = self.ensure(m)
        self.assertEqual(a.local_id, b.local_id)
        m.close()

    def test_重启后仍是同一个号(self):
        m = self.pm(); first = self.ensure(m).local_id; m.close()
        m2 = self.pm(); self.assertEqual(self.ensure(m2).local_id, first); m2.close()

    def test_不同三元组拿不同的号(self):
        m = self.pm()
        ids = {
            self.ensure(m, key="health_score").local_id,
            self.ensure(m, key="fault_type").local_id,
            self.ensure(m, binding="dev2", key="health_score").local_id,
            self.ensure(m, domain="vfd", key="health_score").local_id,
        }
        self.assertEqual(len(ids), 4)
        m.close()

    def test_起始号从基数开始(self):
        m = self.pm()
        self.assertEqual(self.ensure(m).local_id, DEFAULT_BASE_LOCAL_ID)
        m.close()

    def test_改显示名和单位不动localId(self):
        m = self.pm()
        first = self.ensure(m, name="旧名", unit="分").local_id
        row = self.ensure(m, name="新名", unit="%")
        self.assertEqual(row.local_id, first)
        self.assertEqual(row.name, "新名")
        m.close()

    def test_全表按localId有序且发全量(self):
        # hs 的 SNAPSHOT_END 是原子提交，本轮未出现的旧实体一律删除 —— 漏发一个就是删一个。
        m = self.pm()
        self.ensure(m, key="a"); self.ensure(m, key="b"); self.ensure(m, binding="dev2", key="a")
        rows = m.all()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.local_id for r in rows], sorted(r.local_id for r in rows))
        self.assertEqual(m.count(), 3)
        m.close()

    def test_查不到的返回None而不是编一个(self):
        m = self.pm()
        self.assertIsNone(m.local_id_of("vib", "nope", "health_score"))
        m.close()

    def test_点名模板一眼看得出来源(self):
        # 点表是运维天天看的地方，名字含糊的点等于没有。
        self.assertEqual(default_point_name("vib", "dev1", "health_score"),
                         "AI.vib.dev1.health_score")


if __name__ == "__main__":
    unittest.main()
