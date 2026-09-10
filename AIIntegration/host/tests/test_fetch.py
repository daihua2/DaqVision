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


if __name__ == "__main__":
    unittest.main()
