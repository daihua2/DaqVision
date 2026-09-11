"""取数与绑定的回归。

钉的三条（`fetch.py` 模块头那三条不肯让步的）：
  · 质量码原样带下来，不过滤坏点；
  · 时刻用样本自己的，不是取数时刻；
  · 取不到就如实空着，不拿上一帧顶替。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.bindings import Binding, BindingStore
from aiintegration.fetch import Fetcher, vqt_to_sample
from aiintegration.hsproto import daqcontract_pb2 as daq
from aiintegration.hsproto import historystore_pb2 as hs
from aiintegration.quality import Quality

UTC = timezone.utc
T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def mkvqt(gid, t, *, r8=None, s=None, null=False, code=1):
    v = daq.VQT(TagId=gid)
    v.Quality.Code = code
    v.TimStampUtc.FromDatetime(t)
    if null:
        v.NullValue = True
    elif s is not None:
        v.String = s
    else:
        v.R8 = r8
    return v


class FakeClient:
    """只替掉两个方法，其余走真 HsClient 的代码路径。"""

    def __init__(self, by_gid):
        self.by_gid = by_gid
        self.last_req = None

    def _read_channel(self):
        return object()

    def _unary(self, ch, method, req, resp_cls, timeout=15.0):
        assert method == "QueryHistory"
        self.last_req = req
        res = hs.VQTArrayRes()
        for p in req.pointsReq:
            arr = res.VQTs.add()
            arr.TagId = p.id
            for v in self.by_gid.get(p.id, []):
                arr.VQTs.add().CopyFrom(v)
        return res


class TestVqtToSample(unittest.TestCase):
    def test_质量码原样带下来(self):
        s = vqt_to_sample(mkvqt(1, T0, r8=1.0, code=-1004))  # 传感器故障
        self.assertEqual(s.status_code, -1004)   # 原始码留着
        self.assertIs(s.quality, Quality.INPUT_BAD)   # 折成"不可信"

    def test_只有Ok算好(self):
        self.assertIs(vqt_to_sample(mkvqt(1, T0, r8=1.0, code=1)).quality, Quality.OK)
        for bad in (-1000, -1002, -1007, 0, 2):
            with self.subTest(code=bad):
                self.assertIs(vqt_to_sample(mkvqt(1, T0, r8=1.0, code=bad)).quality,
                              Quality.INPUT_BAD)

    def test_坏值锚点的值是None不是0(self):
        s = vqt_to_sample(mkvqt(1, T0, null=True, code=-1007))
        self.assertIsNone(s.value)

    def test_时刻来自样本(self):
        t = T0 - timedelta(minutes=17)
        self.assertEqual(vqt_to_sample(mkvqt(1, t, r8=1.0)).t, t)

    def test_字符串值取得到(self):
        self.assertEqual(vqt_to_sample(mkvqt(1, T0, s="不平衡")).value, "不平衡")


class TestFetcher(unittest.TestCase):
    def bind(self, roles=None, window=60.0):
        return Binding(domain="vib", binding="dev1",
                       roles=roles or {"x_acc": 101, "y_acc": 102},
                       window_sec=window)

    def test_窗口按window_sec取且右端是给定时刻(self):
        c = FakeClient({})
        Fetcher(c).fetch(self.bind(window=30.0), T0)
        self.assertEqual(c.last_req.hisReq.endTime.ToDatetime().replace(tzinfo=UTC), T0)
        self.assertEqual(c.last_req.hisReq.begTime.ToDatetime().replace(tzinfo=UTC),
                         T0 - timedelta(seconds=30))

    def test_按角色装配通道(self):
        c = FakeClient({101: [mkvqt(101, T0, r8=1.0)], 102: [mkvqt(102, T0, r8=2.0)]})
        f = Fetcher(c).fetch(self.bind(), T0)
        self.assertEqual(set(f.channels), {"x_acc", "y_acc"})
        self.assertEqual(f.channels["x_acc"][0].value, 1.0)
        self.assertEqual(f.channels["y_acc"][0].value, 2.0)

    def test_坏点不被过滤掉(self):
        # 过滤坏点 = 把"设备坏了"伪装成"采样稀疏"。
        c = FakeClient({101: [mkvqt(101, T0, r8=1.0, code=1),
                              mkvqt(101, T0, null=True, code=-1004)]})
        f = Fetcher(c).fetch(self.bind(roles={"x_acc": 101}), T0)
        self.assertEqual(len(f.channels["x_acc"]), 2)
        self.assertIs(f.channels["x_acc"][1].quality, Quality.INPUT_BAD)

    def test_取不到就空着而不是不给这一路(self):
        # 不在字典里 = 这一路没绑；在字典里但为空 = 绑了但这段没数据。两者含义不同。
        c = FakeClient({101: [mkvqt(101, T0, r8=1.0)]})
        f = Fetcher(c).fetch(self.bind(), T0)
        self.assertIn("y_acc", f.channels)
        self.assertEqual(list(f.channels["y_acc"]), [])

    def test_一路都没有时也成一帧空Frame(self):
        c = FakeClient({})
        f = Fetcher(c).fetch(self.bind(), T0)
        self.assertEqual(f.t_end, T0)
        self.assertTrue(all(len(v) == 0 for v in f.channels.values()))


class TestBindingStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = BindingStore(Path(self._tmp.name) / "b.db")

    def tearDown(self):
        self.store.close(); self._tmp.cleanup()

    def test_存取往返(self):
        b = Binding("vib", "dev1", {"x_acc": 101}, interval_sec=30, window_sec=45)
        self.store.put(b)
        got = self.store.get("vib", "dev1")
        self.assertEqual(got.roles, {"x_acc": 101})
        self.assertEqual((got.interval_sec, got.window_sec), (30.0, 45.0))

    def test_重复put是覆盖(self):
        self.store.put(Binding("vib", "dev1", {"x_acc": 101}))
        self.store.put(Binding("vib", "dev1", {"x_acc": 999}))
        self.assertEqual(self.store.get("vib", "dev1").roles, {"x_acc": 999})
        self.assertEqual(len(self.store.list()), 1)

    def test_globalId为0被拒(self):
        # 0 = hs 尚未分配映射。拿 0 去查会静默查到别人的点。
        with self.assertRaises(ValueError) as ctx:
            self.store.put(Binding("vib", "dev1", {"x_acc": 0}))
        self.assertIn("0", str(ctx.exception))

    def test_一个角色都没有的绑定被拒(self):
        with self.assertRaises(ValueError):
            self.store.put(Binding("vib", "dev1", {}))

    def test_列出可按域与启用态筛(self):
        self.store.put(Binding("vib", "a", {"x": 1}))
        self.store.put(Binding("vib", "b", {"x": 2}, enabled=False))
        self.store.put(Binding("vfd", "c", {"x": 3}))
        self.assertEqual(len(self.store.list()), 3)
        self.assertEqual(len(self.store.list("vib")), 2)
        self.assertEqual(len(self.store.list("vib", only_enabled=True)), 1)

    def test_缺必填角色查得出来(self):
        b = Binding("vib", "dev1", {"x_acc": 101})
        self.assertEqual(b.missing_required(["x_acc", "y_acc"]), ["y_acc"])

    def test_删绑定不碰结论点(self):
        # 绑定是"要不要继续算"，结论点是"算过的历史"。删绑定顺手删点会把历史抹了。
        self.store.put(Binding("vib", "dev1", {"x": 1}))
        self.assertTrue(self.store.delete("vib", "dev1"))
        self.assertIsNone(self.store.get("vib", "dev1"))
        self.assertFalse(self.store.delete("vib", "dev1"))


class TestBindingParams(unittest.TestCase):
    """台账参数（契约 1.1）—— 骨架**只搬运不解释**，但搬运本身要靠得住。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "b.db"
        self.store = BindingStore(self.path)

    def tearDown(self):
        self.store.close(); self._tmp.cleanup()

    def test_存取往返含中文与空(self):
        self.store.put(Binding("vib", "dev1", {"x": 1},
                               params={"iso_group": "2", "note": "1# 主泵 驱动端"}))
        self.assertEqual(self.store.get("vib", "dev1").params,
                         {"iso_group": "2", "note": "1# 主泵 驱动端"})
        self.store.put(Binding("vib", "dev2", {"x": 2}))
        self.assertEqual(self.store.get("vib", "dev2").params, {})

    def test_骨架不解释键名(self):
        # ★随便一个没人见过的键也要存下来 —— 枚举键名就等于"新增域要改骨架"。
        self.store.put(Binding("vib", "dev1", {"x": 1}, params={"没定义过的键": "值"}))
        self.assertEqual(self.store.get("vib", "dev1").params, {"没定义过的键": "值"})

    def test_非字符串取值被拒(self):
        # 契约里是 map<string,string>；存进去 int 会在回读时变成另一种类型，静默。
        with self.assertRaises(ValueError) as ctx:
            self.store.put(Binding("vib", "dev1", {"x": 1}, params={"iso_group": 2}))
        self.assertIn("字符串", str(ctx.exception))

    def test_覆盖时参数一并覆盖(self):
        self.store.put(Binding("vib", "dev1", {"x": 1}, params={"a": "1"}))
        self.store.put(Binding("vib", "dev1", {"x": 1}, params={"b": "2"}))
        self.assertEqual(self.store.get("vib", "dev1").params, {"b": "2"})

    def test_老库能补列而不是读时才炸(self):
        """★`CREATE TABLE IF NOT EXISTS` 对已存在的表一个字都不改。"""
        self.store.close()
        import sqlite3
        conn = sqlite3.connect(str(self.path))
        conn.execute("DROP TABLE bindings")
        conn.execute("CREATE TABLE bindings (domain TEXT NOT NULL, binding TEXT NOT NULL,"
                     " roles_json TEXT NOT NULL, interval_sec REAL NOT NULL,"
                     " window_sec REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,"
                     " updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
                     " PRIMARY KEY (domain, binding))")
        conn.execute("INSERT INTO bindings(domain,binding,roles_json,interval_sec,window_sec,enabled)"
                     " VALUES('vib','old','{\"x\": 5}',60,60,1)")
        conn.commit(); conn.close()

        store = BindingStore(self.path)          # 打开即迁移
        try:
            old = store.get("vib", "old")
            self.assertEqual(old.roles, {"x": 5}, "老绑定不能丢")
            self.assertEqual(old.params, {}, "老绑定没有台账参数，应为空而不是报错")
            store.put(Binding("vib", "new", {"x": 6}, params={"iso_group": "1"}))
            self.assertEqual(store.get("vib", "new").params, {"iso_group": "1"})
        finally:
            store.close()

    def test_取数时台账原样进帧(self):
        client = FakeClient({})
        b = Binding("vib", "dev1", {"x_vel": 101},
                    params={"iso_group": "2", "mount_type": "rigid"})
        frame = Fetcher(client).fetch(b, T0)
        self.assertEqual(frame.params, {"iso_group": "2", "mount_type": "rigid"})

    def test_帧里的台账是副本改不回绑定(self):
        client = FakeClient({})
        b = Binding("vib", "dev1", {"x_vel": 101}, params={"iso_group": "2"})
        frame = Fetcher(client).fetch(b, T0)
        frame.params["iso_group"] = "4"
        self.assertEqual(b.params, {"iso_group": "2"}, "模块改帧不该反噬配置")


if __name__ == "__main__":
    unittest.main()
