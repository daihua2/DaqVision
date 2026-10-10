"""结论点登记（授权数据点数）—— 用户 10-10 定「甲 + 乙」。

钉的几条：
  · 甲：会让**新**结论点进快照的那次保存，实时库预检回 `Deny` ⇒ 不存、原话回给平台；
    改参数、停用这类不新增点的保存，满额时**照样能存**；问不到实时库 ⇒ 放行。
  · 乙：推完快照问一次，`newPoints > 0` ⇒ 逐点找出没登记上的，`ConclusionPoint.unregistered`
    与 `/health` 报出；问不到 ⇒ **留着上一次的结论**，不清成「全登记上了」。
  · 预检走**写连接**（受限连接按证书 guid 算）；老引擎没有这个口 ⇒ None，不抛。

由来：`C-85`、`C-86`、`H-290`、`H-294`。实时库拒登记不报错（推流照收、`PostVQT` 回 `Ok`），
只靠这一层把它说出来。
"""

import json
import logging
import tempfile
import unittest
import urllib.request
from pathlib import Path

import grpc

from aiintegration.api import ApiService
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import discover
from aiintegration.hsclient import HsClient, HsConfig
from aiintegration.hsproto import daqcontract_pb2 as daq
from aiintegration.hsproto import historystore_pb2 as hs
from aiintegration.httpapi import make_server, serve_in_thread
from aiintegration.logstore import LogStore
from aiintegration.pointmap import PointMap
from aiintegration.registration import Registration
from aiintegration.scheduler import Scheduler

DOM = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec

class D(Domain):
    key = "vib"
    display = "振动"
    version = "1.0.0"
    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x_acc", unit="g"),),
            outputs=(OutputSpec(key="score", display="分", value_type="float"),
                     OutputSpec(key="zone", display="区", value_type="string")),
        )
    def infer(self, frame):
        return []
'''


class FakeQuota:
    """按实时库 `CheckPointQuota` 的口径算（`H-294 §2`、`C-82 §2`）。

    `registered` = 已作为存储点登记的我方 localId；`limit` 0 不限、-1 不许新增。
    """

    def __init__(self, *, limit=0, used=0, registered=(), fail=False, unsupported=False):
        self.limit, self.used = limit, used
        self.registered = set(registered)
        self.fail, self.unsupported = fail, unsupported
        self.calls = []

    def check_point_quota(self, *, new_count=0, local_ids=()):
        self.calls.append((new_count, tuple(local_ids)))
        if self.fail:
            raise RuntimeError("实时库连不上")
        if self.unsupported:
            return None
        n = new_count + sum(1 for lid in local_ids if lid not in self.registered)
        deny = n > 0 and (self.limit == -1 or (self.limit > 0 and self.used + n > self.limit))
        msg = (f"已达授权数据点数(已用 {self.used} / 授权 {self.limit},本次新增 {n}),"
               f"请购买授权后再添加数据点") if deny else ""
        return hs.CheckPointQuotaRes(result=daq.Status(Code=daq.Deny if deny else daq.Ok, Message=msg),
                                     limit=self.limit, used=self.used, newPoints=n)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "domains").mkdir()
        (root / "domains" / "vib.py").write_text(DOM, encoding="utf-8")
        loaded, failed = discover(root / "domains")
        assert not failed, failed
        self.domains = {d.key: d for d in loaded}
        self.bindings = BindingStore(root / "b.db")
        self.points = PointMap(root / "p.db")
        self.hs = FakeQuota(limit=10, used=10)          # 满额，与 AISERVER 现状同形（`C-86 §1`）
        self.reg = Registration(self.hs)
        self.synced = 0
        self.svc = ApiService(guid="g", version="0", logstore=LogStore(capacity=50),
                              domains=self.domains, bindings=self.bindings, points=self.points,
                              registration=self.reg, on_bindings_changed=self._sync)

    def _sync(self):
        self.synced += 1

    def tearDown(self):
        self.points.close(); self.bindings.close(); self._tmp.cleanup()

    def put(self, binding="dev1", enabled=True, display_name=""):
        b = pb.Binding(domain="vib", binding=binding, enabled=enabled, interval_sec=60, window_sec=60,
                       display_name=display_name)
        b.roles["x_acc"] = 101
        return self.svc.PutBinding(pb.PutBindingRequest(binding=b), None)

    def ensure(self, binding="dev1"):
        return [self.points.ensure("vib", binding, k, name=f"AI.vib.{binding}.{k}", unit="",
                                   value_type="float").local_id for k in ("score", "zone")]


# ═══════════════════════════ 甲：建前拦 ═══════════════════════════

class TestAdmit(_Base):
    def test_满额时新建绑定_拒且不存_带实时库原话(self):
        r = self.put()
        self.assertFalse(r.ok)
        self.assertIn("未保存", r.message)
        self.assertIn("已达授权数据点数", r.message, "原话没带上，平台界面只能显示一句含糊的失败")
        self.assertIsNone(self.bindings.get("vib", "dev1"), "拒了却存下了 —— 点照样会被推去登记")
        self.assertEqual(self.synced, 0)
        self.assertEqual(self.hs.calls, [(2, ())], "该问「新增 2 个」—— 两个结论都还没有号")

    def test_授权够时照存(self):
        self.hs.limit = 12
        r = self.put()
        self.assertTrue(r.ok, r.message)
        self.assertIsNotNone(self.bindings.get("vib", "dev1"))

    def test_不许新增时也拒(self):
        self.hs.limit = -1
        self.assertFalse(self.put().ok)

    def test_满额时改已有绑定的参数照样能存(self):
        """★只拦会新增点的那次保存。否则满额之后连已有绑定的台账都改不了。"""
        self.hs.limit = 0
        self.assertTrue(self.put().ok)
        self.ensure()
        self.hs.limit, self.hs.calls = 10, []
        r = self.put(display_name="1#泵")
        self.assertTrue(r.ok, r.message)
        self.assertEqual(self.hs.calls, [], "点都在快照里了，不该再问")

    def test_停用的绑定不建点_不问(self):
        r = self.put(enabled=False)
        self.assertTrue(r.ok, r.message)
        self.assertEqual(self.hs.calls, [])

    def test_停用后重建_带原号去问(self):
        """停用的点不在快照里，重建 = 重新进快照，要过授权闸（实时库按 localIds 判新旧）。"""
        lids = self.ensure()
        self.points.retire("vib", "dev1")
        r = self.put()
        self.assertFalse(r.ok)
        self.assertEqual(self.hs.calls, [(0, tuple(sorted(lids)))])

    def test_问不到实时库_放行(self):
        self.hs.fail = True
        with self.assertLogs("aiintegration.registration", logging.WARNING):
            r = self.put()
        self.assertTrue(r.ok, "实时库一抖就建不了绑定 —— 授权是实时库的闸，它自己会拦")

    def test_实时库没给原话时_备用话按C89口径(self):
        """「本次新增」写实际加进去的个数，被拒即 0；-1 与满额分开说（`C-89`）。"""
        res = self.hs.check_point_quota(new_count=2)
        res.result.Message = ""
        self.hs.check_point_quota = lambda **kw: res
        r = self.put()
        self.assertFalse(r.ok)
        self.assertIn("已达授权数据点数（已用 10 / 授权 10，本次新增 0）", r.message)
        res.limit = -1
        self.assertIn("未授权，不能新增数据点（已用 10，本次新增 0）", self.put().message)

    def test_老引擎没有预检口_放行(self):
        self.hs.unsupported = True
        self.assertTrue(self.put().ok)


# ═══════════════════════════ 乙：建后查 ═══════════════════════════

class TestRefresh(_Base):
    def test_全登记上_一次调用_无告警(self):
        lids = self.ensure()
        self.hs.registered = set(lids)
        v = self.reg.refresh(self.points.all())
        self.assertEqual(v.unregistered, frozenset())
        self.assertEqual(v.note, "")
        self.assertIsNotNone(v.checked_at)
        self.assertEqual(len(self.hs.calls), 1, "常态应一次调用，不逐点问")

    def test_有没登记上的_逐点找出_带原因(self):
        a, b = self.ensure()
        self.hs.registered = {a}
        with self.assertLogs("aiintegration.registration", logging.WARNING) as cm:
            v = self.reg.refresh(self.points.all())
        self.assertEqual(v.unregistered, frozenset({b}))
        self.assertIn("已达授权数据点数", v.note)
        self.assertIn("vib/dev1/zone", "\n".join(cm.output), "告警里要说出是哪个点")

    def test_授权未满却没登记上_不归咎于授权(self):
        a, b = self.ensure()
        self.hs.limit, self.hs.registered = 0, {a}
        v = self.reg.refresh(self.points.all())
        self.assertEqual(v.unregistered, frozenset({b}))
        self.assertIn("不是授权的原因", v.note)

    def test_问不到_留着上一次的结论(self):
        """★没问到不等于没事 —— 清成空就是在说「全登记上了」。"""
        a, b = self.ensure()
        self.hs.registered = {a}
        self.reg.refresh(self.points.all())
        self.hs.fail = True
        v = self.reg.refresh(self.points.all())
        self.assertEqual(v.unregistered, frozenset({b}))
        self.assertIn("核对失败", v.note)

    def test_登记上之后清掉(self):
        a, b = self.ensure()
        self.hs.registered = {a}
        self.reg.refresh(self.points.all())
        self.hs.registered = {a, b}
        with self.assertLogs("aiintegration.registration", logging.INFO) as cm:
            v = self.reg.refresh(self.points.all())
        self.assertEqual(v.unregistered, frozenset())
        self.assertIn("已全部登记", "\n".join(cm.output))

    def test_ListBindings逐点带出未登记(self):
        self.hs.limit = 0
        self.assertTrue(self.put().ok)
        a, b = self.ensure()
        self.hs.registered = {a}
        self.reg.refresh(self.points.all())
        got = {p.local_id: p.unregistered
               for p in self.svc.ListBindings(pb.ListBindingsRequest(), None).bindings[0].points}
        self.assertEqual(got, {a: False, b: True})


class TestPointMapToAdd(_Base):
    def test_没号的算新_在用的不算_停用的给原号(self):
        self.assertEqual(self.points.to_add("vib", "dev1", ["score", "zone"]), (2, []))
        a, b = self.ensure()
        self.assertEqual(self.points.to_add("vib", "dev1", ["score", "zone", "new"]), (1, []))
        self.points.retire("vib", "dev1")
        self.assertEqual(self.points.to_add("vib", "dev1", ["score", "zone"]), (0, [a, b]))


# ═══════════════════════════ 调度：推完即查 ═══════════════════════════

class _Client:
    def __init__(self):
        self.pushed = []

    def push_snapshot(self, rows, timeout=60.0, *, extras=None):
        self.pushed.append([r.local_id for r in rows])
        return len(rows)


class _Reg:
    def __init__(self, client):
        self.client, self.seen = client, []

    def refresh(self, rows):
        # 必须在推之后：推之前问，新点必然没登记，会误报。
        self.seen.append((len(self.client.pushed), [r.local_id for r in rows]))


class TestScheduler(_Base):
    def test_推完快照紧接着核对_核对的是这次推的全量(self):
        self.bindings.put(Binding("vib", "dev1", {"x_acc": 101}, interval_sec=60, window_sec=60))
        client = _Client()
        reg = _Reg(client)
        s = Scheduler(client=client, fetcher=None, domains=self.domains, bindings=self.bindings,
                      points=self.points, registration=reg)
        s.ensure_points()
        self.assertEqual(reg.seen, [(1, client.pushed[0])])


# ═══════════════════════════ /health ═══════════════════════════

class TestHealth(_Base):
    def test_health带出未登记的点与原因(self):
        a, b = self.ensure()
        self.hs.registered = {a}
        self.reg.refresh(self.points.all())
        srv = make_server("127.0.0.1:0", guid="g", version="0", domains=["vib"],
                          artifacts_dir=Path(self._tmp.name) / "art", can_write=True,
                          registration=self.reg)
        serve_in_thread(srv)
        try:
            port = srv.server_address[1]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                h = json.loads(r.read())
        finally:
            srv.shutdown(); srv.server_close()
        self.assertEqual(h["registration"]["unregistered"], [b])
        self.assertIn("已达授权数据点数", h["registration"]["note"])
        self.assertEqual(h["registration"]["limit"], 10)


# ═══════════════════════════ hsclient ═══════════════════════════

class _Unimplemented(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNIMPLEMENTED


class TestHsClient(unittest.TestCase):
    def _client(self, behavior):
        c = HsClient(HsConfig(read_addr="127.0.0.1:1", write_addr="127.0.0.1:2",
                              ca_file="ca", cert_file="c", key_file="k"))
        c.used = []
        c._read_channel = lambda: "READ"
        c._write_channel = lambda: "WRITE"

        def unary(ch, method, req, resp_cls, timeout=15.0):
            c.used.append((ch, method))
            return behavior(req)
        c._unary = unary
        return c

    def test_走写连接(self):
        """★读连接是回环全量连接：guid 留空会被算成实时库自己的，答非所问且不报错。"""
        c = self._client(lambda req: hs.CheckPointQuotaRes(newPoints=len(req.localIds)))
        res = c.check_point_quota(local_ids=[1, 2])
        self.assertEqual(res.newPoints, 2)
        self.assertEqual(c.used, [("WRITE", "CheckPointQuota")])

    def test_老引擎没有这个口_回None不抛(self):
        def boom(req):
            raise _Unimplemented()
        self.assertIsNone(self._client(boom).check_point_quota(new_count=1))

    def test_别的错误照抛(self):
        def boom(req):
            raise RuntimeError("连不上")
        with self.assertRaises(RuntimeError):
            self._client(boom).check_point_quota(new_count=1)


if __name__ == "__main__":
    unittest.main()
