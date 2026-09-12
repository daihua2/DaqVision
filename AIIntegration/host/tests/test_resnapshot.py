"""断而复连必须重推全量快照的回归（AICloud C-27）。

由来：实时库 1.9.437→1.9.438 升级重启后，12 个结论点的**点定义**从实体流里没了
（平台镜像 2130→2118、按来源 guid 过滤 12→0），而**值照样写得进**、`/health` 照样 `ready`。
根因：本方只在「服务启动」与「绑定变更」时推快照，调度线程的异常支路只退避重试 ⇒ **不会自愈**。
★这条规矩本就是我方自己写下的（`hsclient` 模块头 ③「重连必须重推全量」），没在调度里兑现。

钉四条：
  · 一拍抛异常后，下一拍**先重推快照、再跑这一拍**（顺序不能反）；
  · 多个调度线程只重推**一次**；
  · 重推失败 ⇒ 标志**留着**，下一轮还会再试（不能把"没推成"记成"已推过"）；
  · `/health` 的 snapshot 那一格如实反映状态（断连后是 stale，不是 ready 一路绿）。
"""

import threading
import unittest


class FakeClient:
    def __init__(self):
        self.snapshots = 0
        self.fail_push = False

    def push_snapshot(self, rows, timeout=60.0):
        if self.fail_push:
            raise RuntimeError("推快照失败（连接还没好）")
        self.snapshots += 1
        return len(rows)

    def log_degraded(self, what, exc):
        pass


class FakePoints:
    def all(self):
        return []

    def ensure(self, *a, **kw):
        return 0


class FakeBindings:
    def list(self, only_enabled=True):
        return []


def _mk():
    from aiintegration.scheduler import Scheduler
    c = FakeClient()
    s = Scheduler(client=c, fetcher=object(), domains={},
                  bindings=FakeBindings(), points=FakePoints())
    return s, c


class TestResnapshot(unittest.TestCase):

    def test_marks_need_after_failure(self):
        s, c = _mk()
        self.assertFalse(s._need_resnapshot)
        self.assertFalse(s.snapshot_ok)
        s.ensure_points()
        self.assertTrue(s.snapshot_ok)
        s._need_resnapshot = True          # 模拟一拍失败
        s._snapshot_ok = False
        self.assertFalse(s.snapshot_ok)

    def test_republish_pushes_once_and_clears(self):
        s, c = _mk()
        s._need_resnapshot = True
        before = c.snapshots
        s._republish_snapshot()
        self.assertEqual(c.snapshots, before + 1)
        self.assertFalse(s._need_resnapshot)
        self.assertTrue(s.snapshot_ok)

    def test_only_once_across_threads(self):
        """★多个调度线程同时发现要重推 —— 只推一次。"""
        s, c = _mk()
        s._need_resnapshot = True
        ts = [threading.Thread(target=s._republish_snapshot) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual(c.snapshots, 1)

    def test_failure_keeps_the_flag(self):
        """★推失败不能把"没推成"记成"已推过" —— 标志留着，下一轮再试。"""
        s, c = _mk()
        s._need_resnapshot = True
        c.fail_push = True
        with self.assertRaises(RuntimeError):
            s._republish_snapshot()
        self.assertTrue(s._need_resnapshot, "推失败后标志必须留着")
        self.assertFalse(s.snapshot_ok)
        c.fail_push = False
        s._republish_snapshot()            # 下一轮
        self.assertEqual(c.snapshots, 1)
        self.assertFalse(s._need_resnapshot)


class TestHealthSnapshotCell(unittest.TestCase):
    """`/health` 那一格要能说出"点定义可能已丢"，而不是一路绿。"""

    def _state(self, **ctx):
        from aiintegration.httpapi import _snapshot_state
        return _snapshot_state(ctx)

    def test_no_write_path(self):
        self.assertIn("不适用", self._state(can_write=False))

    def test_no_scheduler(self):
        self.assertIn("未知", self._state(can_write=True, scheduler=None))

    def test_accepted_and_stale(self):
        s, _ = _mk()
        s._snapshot_ok = True
        self.assertEqual(self._state(can_write=True, scheduler=s), "accepted")
        s._snapshot_ok = False
        got = self._state(can_write=True, scheduler=s)
        self.assertIn("stale", got)
        self.assertIn("点定义", got)       # 要说清后果，不只说状态名


if __name__ == "__main__":
    unittest.main()


class _Stop:
    """替掉 `_stop`：跑固定圈数就停，`wait` 不真睡（否则一条用例要等退避的 5 秒）。"""

    def __init__(self, rounds: int) -> None:
        self.rounds, self.seen = rounds, 0

    def is_set(self) -> bool:
        self.seen += 1
        return self.seen > self.rounds

    def wait(self, timeout=None) -> bool:
        return False

    def set(self) -> None:
        self.rounds = -1


class _Binding:
    domain, binding, enabled, interval_sec, window_sec = "d", "b", True, 60.0, 60.0


class TestLoopActuallyRepublishes(unittest.TestCase):
    """★钉住 `_loop` **真的会去调**重推 —— 光有 `_republish_snapshot` 不够，得有人调它。

    （本轮变异验证逮到：把 `_loop` 里那两行删掉，上面那些用例照样全绿。）
    """

    def _sched(self, fail_first: bool):
        s, c = _mk()
        s._bindings = type("B", (), {
            "get": staticmethod(lambda d, b: _Binding()),
            "list": staticmethod(lambda only_enabled=True: []),
        })()
        calls = []

        def run_once(b, tick):
            calls.append(tick)
            if fail_first and len(calls) == 1:
                raise RuntimeError("hs 不可用（模拟实时库重启）")

        s.run_once = run_once
        return s, c, calls

    def test_republishes_after_a_failed_tick(self):
        s, c, calls = self._sched(fail_first=True)
        s._stop = _Stop(3)
        before = c.snapshots
        s._loop("d", "b")
        self.assertGreaterEqual(len(calls), 2, "应当还有后续拍")
        self.assertEqual(c.snapshots, before + 1, "失败一拍后必须重推一次全量快照")
        self.assertFalse(s._need_resnapshot)

    def test_no_republish_when_nothing_failed(self):
        """没断过就不该平白多推 —— 否则每拍都推，白占对端。"""
        s, c, calls = self._sched(fail_first=False)
        s._stop = _Stop(3)
        before = c.snapshots
        s._loop("d", "b")
        self.assertEqual(c.snapshots, before, "没断过不该重推")
