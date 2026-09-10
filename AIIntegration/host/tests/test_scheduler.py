"""调度与推理执行的回归。

钉的几条：
  · 节拍**对齐**且取"上一个已完结的节拍"，不是 now()；
  · 模块**抛异常** ⇒ 骨架代落坏值锚点并照常回流（不是什么都不发）；
  · 模块**正常返回** ⇒ 返回什么就是什么，骨架不替它补；
  · 未声明的结论 / 帧区间外的时刻 ⇒ **拒收并记录**，不静默丢；
  · **hs 不可用** ⇒ 整拍跳过、**不落锚点**（与"模块算不出来"不是一回事）。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import Domain, LoadedDomain
from aiintegration.pointmap import PointMap
from aiintegration.quality import Quality
from aiintegration.runner import anchor_all, run_domain
from aiintegration.scheduler import Scheduler, aligned_tick
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


DECL = Declaration(
    inputs=(InputSpec(role="x_acc"), InputSpec(role="temp", required=False)),
    outputs=(OutputSpec(key="health_score", display="健康分", value_type="float", unit="分"),
             OutputSpec(key="fault_type", display="故障类型", value_type="string")),
)


class _Dom(Domain):
    key = "vib"
    display = "振动"
    version = "1.0.0"

    def __init__(self, behavior="ok"):
        self.behavior = behavior

    def declare(self):
        return DECL

    def infer(self, frame):
        if self.behavior == "boom":
            raise RuntimeError("模块自己炸了")
        if self.behavior == "none":
            return None
        if self.behavior == "junk":
            return ["不是 Finding"]
        if self.behavior == "undeclared":
            return [Finding("没声明过的", 1.0, Quality.OK, frame.t_end)]
        if self.behavior == "out_of_range":
            return [Finding("health_score", 1.0, Quality.OK,
                            frame.t_end + timedelta(hours=1))]
        if self.behavior == "partial":
            return [Finding("health_score", 90.0, Quality.OK, frame.t_end)]
        return [Finding("health_score", 90.0, Quality.OK, frame.t_end),
                Finding("fault_type", "不平衡", Quality.OK, frame.t_end)]


def loaded(behavior="ok"):
    inst = _Dom(behavior)
    return LoadedDomain(inst, DECL, frozenset({"infer"}), Path("mem.py"))


def frame(t_end=T0, window=60):
    return Frame(domain="vib", binding="dev1",
                 t_start=t_end - timedelta(seconds=window), t_end=t_end, channels={})


class TestAlignedTick(unittest.TestCase):
    def test_取上一个整节拍不是now(self):
        # 按 now() 取，窗口右端必然还没到齐 —— 稳定少最后几笔，看起来像"数据稀疏"。
        now = datetime(2026, 9, 10, 12, 0, 7, tzinfo=UTC)
        self.assertEqual(aligned_tick(now, 60), datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC))

    def test_正好在边界上取自己(self):
        self.assertEqual(aligned_tick(T0, 60), T0)

    def test_边界与重启无关(self):
        # 以纪元为基准取整 ⇒ 重启后不会跟自己错开半拍。
        a = aligned_tick(datetime(2026, 9, 10, 12, 0, 59, tzinfo=UTC), 60)
        b = aligned_tick(datetime(2026, 9, 10, 12, 0, 1, tzinfo=UTC), 60)
        self.assertEqual(a, b)

    def test_节拍必须为正(self):
        with self.assertRaises(ValueError):
            aligned_tick(T0, 0)


class TestRunDomain(unittest.TestCase):
    def test_正常返回原样带出(self):
        r = run_domain(loaded("ok"), frame())
        self.assertTrue(r.ok)
        self.assertEqual({f.key for f in r.findings}, {"health_score", "fault_type"})

    def test_抛异常时代落全部输出的坏值锚点(self):
        # "什么都不发"在下游看来是"这段没数据",而真相是"这段算不出来"。
        r = run_domain(loaded("boom"), frame())
        self.assertFalse(r.ok)
        self.assertEqual({f.key for f in r.findings}, {"health_score", "fault_type"})
        for f in r.findings:
            self.assertIs(f.quality, Quality.COMPUTE_ERROR)
            self.assertIsNone(f.value)
            self.assertEqual(f.t, T0)

    def test_正常返回少给一条时骨架不补(self):
        # 少给可能正是模块的判断(本帧就不该有那条)。补 = 替它发明语义。
        r = run_domain(loaded("partial"), frame())
        self.assertTrue(r.ok)
        self.assertEqual([f.key for f in r.findings], ["health_score"])

    def test_返回None当作没有结论而不是出错(self):
        r = run_domain(loaded("none"), frame())
        self.assertTrue(r.ok)
        self.assertEqual(r.findings, [])

    def test_返回非Finding对象被丢弃(self):
        # 不接受"看起来像结论"的东西 —— 那正是 V/Q/T 被绕过的方式。
        r = run_domain(loaded("junk"), frame())
        self.assertEqual(r.findings, [])

    def test_未声明的结论被拒收(self):
        r = run_domain(loaded("undeclared"), frame())
        self.assertEqual(r.findings, [])

    def test_帧区间外的时刻被拒收(self):
        r = run_domain(loaded("out_of_range"), frame())
        self.assertEqual(r.findings, [])

    def test_锚点覆盖所有声明输出(self):
        a = anchor_all(loaded(), frame(), Quality.NO_INPUT)
        self.assertEqual({f.key for f in a}, {"health_score", "fault_type"})


class _FakeClient:
    def __init__(self, fail=False):
        self.posted = []
        self.snapshots = []
        self.fail = fail
        self.degraded = 0

    def push_snapshot(self, rows, timeout=60.0):
        self.snapshots.append(list(rows))
        return len(rows) + 2

    def post_vqt(self, items, timeout=15.0):
        self.posted.extend(items)

    def log_degraded(self, what, exc):
        self.degraded += 1


class _FakeFetcher:
    def __init__(self, fail=False):
        self.fail = fail

    def fetch(self, b, end_time):
        if self.fail:
            raise RuntimeError("实时库连不上")
        return frame(end_time, b.window_sec)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.points = PointMap(d / "p.db")
        self.bindings = BindingStore(d / "b.db")
        self.bindings.put(Binding("vib", "dev1", {"x_acc": 101}, interval_sec=60, window_sec=60))
        self.client = _FakeClient()

    def tearDown(self):
        self.points.close(); self.bindings.close(); self._tmp.cleanup()

    def sched(self, behavior="ok", fetch_fail=False):
        return Scheduler(client=self.client, fetcher=_FakeFetcher(fetch_fail),
                         domains={"vib": loaded(behavior)},
                         bindings=self.bindings, points=self.points)

    def test_建点并推全量快照(self):
        n = self.sched().ensure_points()
        self.assertEqual(n, 2)                        # 两个声明输出
        self.assertEqual(len(self.client.snapshots), 1)
        self.assertEqual(len(self.client.snapshots[0]), 2)   # 快照是全量

    def test_一拍走完会回流结论(self):
        s = self.sched(); s.ensure_points()
        got = s.run_once(self.bindings.get("vib", "dev1"), T0)
        self.assertEqual(len(got), 2)
        self.assertEqual(len(self.client.posted), 2)
        lids = {lid for lid, _ in self.client.posted}
        self.assertEqual(len(lids), 2)

    def test_模块炸了照常回流坏值锚点(self):
        s = self.sched("boom"); s.ensure_points()
        s.run_once(self.bindings.get("vib", "dev1"), T0)
        self.assertEqual(len(self.client.posted), 2)
        for _, f in self.client.posted:
            self.assertIs(f.quality, Quality.COMPUTE_ERROR)

    def test_hs不可用时整拍跳过且不落锚点(self):
        # 与"模块算不出来"不是一回事:我方连取没取到数都不知道,
        # 落锚点等于替上游断言"这段没数据"。
        s = self.sched(fetch_fail=True)
        with self.assertRaises(RuntimeError):
            s.run_once(self.bindings.get("vib", "dev1"), T0)
        self.assertEqual(self.client.posted, [])

    def test_缺必填角色不推理(self):
        self.bindings.put(Binding("vib", "dev2", {"temp": 102}))   # 只绑了选填的
        s = self.sched()
        got = s.run_once(self.bindings.get("vib", "dev2"), T0)
        self.assertEqual(got, [])
        self.assertEqual(self.client.posted, [])

    def test_域没装载时跳过(self):
        self.bindings.put(Binding("nosuch", "dev9", {"x": 1}))
        s = self.sched()
        self.assertEqual(s.run_once(self.bindings.get("nosuch", "dev9"), T0), [])


if __name__ == "__main__":
    unittest.main()


class TestSyncOnChange(unittest.TestCase):
    """★运行期新增的绑定必须建点 + 起线程 —— 否则界面上看着配好了、实际一拍都不走。

    这个缺口是整机自检里"推送快照：0 个"暴露出来的：`ensure_points` 原先只在启动时跑一次。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.points = PointMap(d / "p.db")
        self.bindings = BindingStore(d / "b.db")
        self.client = _FakeClient()
        self.sched = Scheduler(client=self.client, fetcher=_FakeFetcher(),
                               domains={"vib": loaded()},
                               bindings=self.bindings, points=self.points)

    def tearDown(self):
        self.sched.stop(timeout=1); self.points.close()
        self.bindings.close(); self._tmp.cleanup()

    def test_启动时没有绑定则点表为空(self):
        self.assertEqual(self.sched.sync(), 0)
        self.assertEqual(self.points.count(), 0)

    def test_运行期加绑定后同步会建点并起线程(self):
        self.sched.sync()
        self.bindings.put(Binding("vib", "dev1", {"x_acc": 101}))
        alive = self.sched.sync()
        self.assertEqual(alive, 1)
        self.assertEqual(self.points.count(), 2)          # 该域两个声明输出
        self.assertEqual(len(self.client.snapshots[-1]), 2)  # 快照是全量

    def test_同步是幂等的不会重复建点也不会起第二个线程(self):
        self.bindings.put(Binding("vib", "dev1", {"x_acc": 101}))
        self.assertEqual(self.sched.sync(), 1)
        n = self.points.count()
        self.assertEqual(self.sched.sync(), 1)   # 仍是 1 个线程
        self.assertEqual(self.points.count(), n)  # 没重复建点

    def test_停用的绑定不起线程(self):
        self.bindings.put(Binding("vib", "dev1", {"x_acc": 101}, enabled=False))
        self.assertEqual(self.sched.sync(), 0)
