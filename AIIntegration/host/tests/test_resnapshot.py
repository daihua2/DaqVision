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
        self.assertIsNone(s.snapshot_age_sec)
        s.ensure_points()
        self.assertIsNotNone(s.snapshot_age_sec)
        s._need_resnapshot = True          # 模拟一拍失败
        self.assertTrue(s._need_resnapshot)

    def test_republish_pushes_once_and_clears(self):
        s, c = _mk()
        s._need_resnapshot = True
        before = c.snapshots
        s._republish_snapshot()
        self.assertEqual(c.snapshots, before + 1)
        self.assertFalse(s._need_resnapshot)
        self.assertIsNotNone(s.snapshot_age_sec)

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
        self.assertIsNone(s.snapshot_age_sec, "没推成就不该记推送时刻")
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

    def test_reports_only_facts(self):
        """★不许再出现"accepted"那种"当前有效"的断言（C-32：它会让人停止排查）。"""
        s, _ = _mk()
        self.assertIn("从未推送", self._state(can_write=True, scheduler=s))
        s.ensure_points()
        got = self._state(can_write=True, scheduler=s)
        self.assertIn("秒前推送", got)
        self.assertIn("重推", got)          # 要告诉人多久会自己再推一次
        self.assertNotIn("accepted", got)


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
        s.ensure_points()      # 模拟启动路径那一次推送 —— 否则"从没推过"本身就算到期
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
        """没断过、也没到周期，就不该平白多推 —— 否则每拍都推，白占对端。"""
        s, c, calls = self._sched(fail_first=False)
        s._stop = _Stop(3)
        before = c.snapshots
        s._loop("d", "b")
        self.assertEqual(c.snapshots, before, "没断过不该重推")


class TestPeriodicResnapshot(unittest.TestCase):
    """★到周期就无条件重推 —— C-31 受控验收证伪了"靠写失败触发"那一版。

    实测形态：实时库重启后写入**照样成功**（通道透明重连、引擎按 localId 收得下 VQT），
    调度循环从不抛异常 ⇒ 挂在异常上的标志永不置位 ⇒ 点定义一直不回来，
    而值照写、健康口照绿。**没有任何一处会报错。**
    """

    def test_due_when_never_pushed(self):
        s, _ = _mk()
        self.assertTrue(s._resnapshot_due(), "从没推过应视为到期")

    def test_not_due_right_after_push(self):
        s, _ = _mk()
        s.ensure_points()
        self.assertFalse(s._resnapshot_due())

    def test_due_after_interval(self):
        import time as _t
        from aiintegration.scheduler import RESNAPSHOT_INTERVAL_SEC
        s, _ = _mk()
        s.ensure_points()
        s._last_snapshot_mono = _t.monotonic() - RESNAPSHOT_INTERVAL_SEC - 1
        self.assertTrue(s._resnapshot_due())

    def test_loop_republishes_on_period_without_any_failure(self):
        """★关键一条：**一拍都没失败过**，到周期照样重推。"""
        import time as _t
        from aiintegration.scheduler import RESNAPSHOT_INTERVAL_SEC
        s, c = _mk()
        s._bindings = type("B", (), {
            "get": staticmethod(lambda d, b: _Binding()),
            "list": staticmethod(lambda only_enabled=True: []),
        })()
        s.run_once = lambda b, tick: None          # 从不抛异常 —— 正是现场那个形态
        s.ensure_points()                          # 启动那次
        before = c.snapshots
        s._last_snapshot_mono = _t.monotonic() - RESNAPSHOT_INTERVAL_SEC - 1
        s._stop = _Stop(2)
        s._loop("d", "b")
        self.assertGreater(c.snapshots, before, "到周期必须重推，哪怕一次异常都没有")
        self.assertFalse(s._need_resnapshot)


class TestPeerInstanceChange(unittest.TestCase):
    """★精确判据：对端换实例（引擎重启）就重推 —— 实时库 1.9.441 起的 PingRes.instanceId。

    这是 C-32 那条"没有精确判据可用"的正解：引擎重启后我方快照失效而**值照样写得进**，
    写入不失败、LookupGlobal 也答不了（映射仍在，丢的只是实体配置）——
    只有"对端是不是换了个实例"这一格答得了。
    """

    def _mk(self, ids):
        """ids: instance_id() 依次返回的值（模拟对端）。"""
        s, c = _mk()
        seq = list(ids)
        c.instance_id = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
        return s, c

    def test_same_instance_does_not_republish(self):
        s, c = self._mk(["inst-A"])
        s.ensure_points()
        before = c.snapshots
        self.assertFalse(s._peer_changed())      # 第一次见到，不算变
        self.assertFalse(s._peer_changed())      # 之后恒等
        self.assertEqual(c.snapshots, before)

    def test_changed_instance_triggers(self):
        s, c = self._mk(["inst-A", "inst-A", "inst-B"])
        s.ensure_points()
        self.assertFalse(s._peer_changed())      # 记住 A
        self.assertFalse(s._peer_changed())      # 仍是 A
        self.assertTrue(s._peer_changed())       # ★换成 B ⇒ 要重推

    def test_old_engine_empty_id_stays_unchanged(self):
        """老引擎恒回空串 ⇒ 恒等 ⇒ 不判变（靠周期兜底），不会每拍都推。"""
        s, c = self._mk([""])
        s.ensure_points()
        for _ in range(5):
            self.assertFalse(s._peer_changed())

    def test_upgrade_from_old_engine_counts_as_changed(self):
        """★老引擎升级成新引擎（空串 → 有 id）**要判变** —— 升级必然重启，快照确实失效。

        先前这里特判了空串（空就直接回 False），会让这一次该推的重推被漏掉；
        变异验证显示那个特判对其它情形也没有作用，已去掉。
        """
        s, c = self._mk(["", "", "inst-A"])
        s.ensure_points()
        self.assertFalse(s._peer_changed())      # 记住 ""
        self.assertFalse(s._peer_changed())      # 仍是老引擎
        self.assertTrue(s._peer_changed())       # ★升级后必须判变

    def test_ping_failure_is_not_a_change(self):
        """探活失败交给退避支路，不在这里当成"变了"。"""
        s, c = _mk()
        def boom():
            raise RuntimeError("连不上")
        c.instance_id = boom
        self.assertFalse(s._peer_changed())

    def test_loop_republishes_when_instance_changes(self):
        """★端到端：一拍都没失败、也没到周期，仅因为对端换了实例就重推。"""
        s, c = self._mk(["inst-A", "inst-A", "inst-B", "inst-B", "inst-B"])
        s._bindings = type("B", (), {
            "get": staticmethod(lambda d, b: _Binding()),
            "list": staticmethod(lambda only_enabled=True: []),
        })()
        s.run_once = lambda b, tick: None        # 从不抛异常
        s.ensure_points()
        s._peer_changed()                        # 记住 A
        before = c.snapshots
        s._stop = _Stop(3)
        s._loop("d", "b")
        self.assertGreater(c.snapshots, before, "对端换实例必须触发重推")


class TestResnapshotInterval(unittest.TestCase):
    """★兜底周期按**对端报不报实例身份**取值（AICloud C-34 §4.1 / historystore H-240 §4.1）。

    由来：我方这个每 5 分钟一次的全量重推，在实时库那侧 24 小时刷了 291 条 WARNING，
    占了该服务几乎全部的 WARNING。1.9.445 起对端报得出 `instanceId`，精确判据已现场验收
    （引擎起来 5 秒内重推，C-34 §1），周期退为兜底 ⇒ 拉到 1 小时；**不撤**，因为
    "点定义丢了而值照写、健康口照绿"是静默失败，精确判据只覆盖"换实例"这一种成因。
    """

    def _mk_peer(self, ids):
        s, c = _mk()
        seq = list(ids)
        c.instance_id = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
        return s, c

    def test_known_peer_uses_long_interval(self):
        from aiintegration.scheduler import RESNAPSHOT_INTERVAL_KNOWN_PEER_SEC
        s, c = self._mk_peer(["inst-A"])
        s._peer_changed()                     # 问到一次 ⇒ 记住 A
        self.assertTrue(s.peer_instance_known)
        self.assertEqual(s.resnapshot_interval_sec, RESNAPSHOT_INTERVAL_KNOWN_PEER_SEC)

    def test_old_engine_keeps_short_interval(self):
        """老引擎（恒空串）⇒ 周期**和升级前一模一样**，兜底不能被顺手拉长。"""
        from aiintegration.scheduler import RESNAPSHOT_INTERVAL_SEC
        s, c = self._mk_peer([""])
        s._peer_changed()
        self.assertFalse(s.peer_instance_known)
        self.assertEqual(s.resnapshot_interval_sec, RESNAPSHOT_INTERVAL_SEC)

    def test_known_peer_not_due_at_old_period(self):
        """★钉住实效：对端报实例时，过了 300 秒**不该**再推；过了 1 小时才推。"""
        import time as _t
        from aiintegration.scheduler import (RESNAPSHOT_INTERVAL_SEC,
                                             RESNAPSHOT_INTERVAL_KNOWN_PEER_SEC)
        s, c = self._mk_peer(["inst-A"])
        s.ensure_points()                     # 启动那次
        s._peer_changed()
        s._last_snapshot_mono = _t.monotonic() - RESNAPSHOT_INTERVAL_SEC - 1
        self.assertFalse(s._resnapshot_due(), "对端报得出实例身份时，300 秒不再是周期")
        s._last_snapshot_mono = _t.monotonic() - RESNAPSHOT_INTERVAL_KNOWN_PEER_SEC - 1
        self.assertTrue(s._resnapshot_due(), "兜底没撤：满 1 小时仍要推一次")

    def test_rollback_to_old_engine_settles(self):
        """★引擎**回退**到老版本（有 id → 空串）：推一次就稳住，并退回 300 秒兜底。

        先前 `_republish_snapshot` 里写的是 `instance_id() or 旧值`，空串会被兜回旧 id ⇒
        `_peer_changed` 从此**每拍都判变、每拍都重推**，正好把我方要治的刷屏放大到极致。
        """
        from aiintegration.scheduler import RESNAPSHOT_INTERVAL_SEC
        s, c = self._mk_peer(["inst-A", ""])
        s.ensure_points()
        self.assertFalse(s._peer_changed())   # 记住 A
        self.assertTrue(s._peer_changed())    # 回退成老引擎 ⇒ 判变一次，该推
        before = c.snapshots
        s._republish_snapshot()
        self.assertEqual(c.snapshots, before + 1)
        for _ in range(5):
            self.assertFalse(s._peer_changed(), "认下空串后不该再每拍判变")
        self.assertEqual(s.resnapshot_interval_sec, RESNAPSHOT_INTERVAL_SEC)

    def test_health_cell_tells_which_regime(self):
        """`/health` 那一格要说出**现在按哪套在跑**，不能写死一个周期（写死 = 有一半情形在说假话）。"""
        from aiintegration.httpapi import _snapshot_state
        s, c = self._mk_peer(["inst-A"])
        s.ensure_points()
        s._peer_changed()
        got = _snapshot_state({"can_write": True, "scheduler": s})
        self.assertIn("换实例", got)
        self.assertIn("3600", got)
        self.assertNotIn("每 300 秒", got)

        s2, _ = self._mk_peer([""])
        s2.ensure_points()
        s2._peer_changed()
        got2 = _snapshot_state({"can_write": True, "scheduler": s2})
        self.assertIn("300", got2)
        self.assertIn("未报实例身份", got2)
