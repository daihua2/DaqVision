"""结论点 localId 的回归 —— 钉的是"同一个三元组恒回同一个号、号绝不回收"。

换一次 localId 就等于新建一个点，旧点变成没人写的孤儿，而按对账铁律**绝不自动删**。
"""

import tempfile
import unittest
from pathlib import Path

from aiintegration.pointmap import (
    DEFAULT_BASE_LOCAL_ID, PointMap, container_name, default_point_name,
)


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

    # ── 上级实体（C-57，甲：服务 → 域 → 绑定 → 点）────────────────────────
    def test_新点带上绑定与域的实体号_且三者互不撞号(self):
        m = self.pm()
        r = self.ensure(m)
        self.assertNotEqual(r.binding_entity_id, 0)
        self.assertNotEqual(r.domain_entity_id, 0)
        self.assertEqual(len({r.local_id, r.binding_entity_id, r.domain_entity_id}), 3)
        m.close()

    def test_同域共用域实体_同名绑定跨域是两个实体(self):
        m = self.pm()
        a = self.ensure(m, binding="dev1")
        b = self.ensure(m, binding="dev2")
        c = self.ensure(m, domain="vfd", binding="dev1")
        self.assertEqual(a.domain_entity_id, b.domain_entity_id)
        self.assertNotEqual(a.binding_entity_id, b.binding_entity_id)
        self.assertNotEqual(a.binding_entity_id, c.binding_entity_id, "一条通道只属一条连接")
        self.assertNotEqual(a.domain_entity_id, c.domain_entity_id)
        m.close()

    def test_点与上级共用号段_后分的点不撞上级的号(self):
        m = self.pm()
        self.ensure(m, key="a")
        self.ensure(m, binding="dev2", key="a")
        self.ensure(m, key="b")
        ids = []
        for r in m.all():
            ids.append(r.local_id)
        ids += {x for r in m.all() for x in (r.binding_entity_id, r.domain_entity_id)}
        self.assertEqual(len(ids), len(set(ids)))
        m.close()

    def test_all与ensure给出同样的上级号_重启不变(self):
        m = self.pm(); r = self.ensure(m); m.close()
        m2 = self.pm()
        (got,) = m2.all()
        self.assertEqual((got.binding_entity_id, got.domain_entity_id),
                         (r.binding_entity_id, r.domain_entity_id))
        self.assertEqual(self.ensure(m2), got)
        m2.close()

    def test_老库开库补登上级_点号一个不动(self):
        # 现场 27 个点就是这种库：上级实体之前建的，points 表有、containers 表没有。
        import sqlite3
        con = sqlite3.connect(str(self.db))
        con.execute("CREATE TABLE points (domain TEXT NOT NULL, binding TEXT NOT NULL, key TEXT NOT NULL,"
                    " local_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',"
                    " value_type TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                    " PRIMARY KEY (domain, binding, key))")
        old = [("vib", "dev1", "a", 1000), ("vib", "dev1", "b", 1001), ("vfd", "dev1", "a", 1002)]
        for d, b, k, i in old:
            con.execute("INSERT INTO points(domain,binding,key,local_id,name,value_type) VALUES(?,?,?,?,?,?)",
                        (d, b, k, i, default_point_name(d, b, k), "float"))
        con.commit(); con.close()

        m = self.pm()
        rows = m.all()
        self.assertEqual([(r.domain, r.binding, r.key, r.local_id) for r in rows],
                         [(d, b, k, i) for d, b, k, i in old])
        self.assertTrue(all(r.binding_entity_id > 1002 and r.domain_entity_id > 1002 for r in rows))
        first = [(r.binding_entity_id, r.domain_entity_id) for r in rows]
        m.close()
        m2 = self.pm()
        self.assertEqual([(r.binding_entity_id, r.domain_entity_id) for r in m2.all()], first,
                         "再开库又分了一次上级号")
        self.assertEqual(self.ensure(m2, key="c").local_id,
                         max(max(first, key=max)) + 1)
        m2.close()

    def test_上级名与点名同一套前缀(self):
        self.assertEqual(container_name("vib"), "AI.vib")
        self.assertEqual(container_name("vib", "dev1"), "AI.vib.dev1")

    def test_点名模板一眼看得出来源(self):
        # 点表是运维天天看的地方，名字含糊的点等于没有。
        self.assertEqual(default_point_name("vib", "dev1", "health_score"),
                         "AI.vib.dev1.health_score")


if __name__ == "__main__":
    unittest.main()
