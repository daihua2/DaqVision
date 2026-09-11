"""事件驱动入口（图片类输入）的回归 —— **零第三方依赖**，用纯 Python 的假图片域。

钉的重点：
  ① **拍照时刻没有缺省**：缺了、不带时区一律拒；结论的 T 就是它，不是上传时刻；
  ② **没有绑定不收**：台账与结论点都挂在绑定上，没有就只能猜；
  ③ **只读运行如实说没写**；写路径就绪时真写、写失败不吞推理结果；
  ④ **忙了明说**（503），不无限排队；
  ⑤ HTTP 口：`Z` 结尾的时刻在 Python 3.10 上也要认（标准库本身不认）。
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration import events as events_mod
from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import Domain, LoadedDomain
from aiintegration.events import EventError, EventRunner
from aiintegration.httpapi import make_server, serve_in_thread
from aiintegration.pointmap import PointMap
from aiintegration.quality import Quality
from aiintegration.types import Declaration, Finding, InputSpec, OutputSpec

CN = timezone(timedelta(hours=8))
SHOT = datetime(2026, 9, 11, 9, 30, 0, tzinfo=CN)       # 拍照时刻（北京时间）


class EchoImage(Domain):
    """回显图片的字节数与类型，T 用拍照时刻。"""
    key = "echo_img"
    display = "回显图片"
    version = "1.0.0"

    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="image", kind="image"),),
            outputs=(OutputSpec(key="size", display="字节数", value_type="int"),
                     OutputSpec(key="ctype", display="类型", value_type="string")))

    def infer(self, frame):
        b = frame.blobs["image"]
        return [Finding(key="size", value=len(b.data), quality=Quality.OK, t=b.t),
                Finding(key="ctype", value=b.content_type, quality=Quality.OK, t=b.t)]


class PointOnly(Domain):
    key = "point_only"
    display = "只有测点"
    version = "1.0.0"

    def declare(self):
        return Declaration(inputs=(InputSpec(role="x", unit="mm/s"),),
                           outputs=(OutputSpec(key="v", display="v", value_type="float"),))

    def infer(self, frame):
        return []


class TwoImages(EchoImage):
    key = "two_img"

    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="front", kind="image"), InputSpec(role="side", kind="image")),
            outputs=(OutputSpec(key="size", display="字节数", value_type="int"),))

    def infer(self, frame):
        (role, b), = frame.blobs.items()
        return [Finding(key="size", value=len(b.data), quality=Quality.OK, t=b.t)]


class Blocking(EchoImage):
    key = "blocking"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def infer(self, frame):
        self.entered.set()
        self.release.wait(5)
        return super().infer(frame)


def _loaded(inst: Domain) -> LoadedDomain:
    return LoadedDomain(inst, inst.declare(), frozenset(inst.capabilities()), Path("用例内造"))


class FakeClient:
    def __init__(self, fail=False):
        self.posted = []
        self.fail = fail

    def post_vqt(self, items):
        if self.fail:
            raise RuntimeError("实时库连不上")
        self.posted.append(items)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.bindings = BindingStore(root / "b.db")
        self.points = PointMap(root / "p.db")
        self.blocking = Blocking()
        self.domains = {d.key: _loaded(d) for d in
                        (EchoImage(), PointOnly(), TwoImages(), self.blocking)}
        for key in ("echo_img", "two_img", "blocking"):
            self.bindings.put(Binding(key, "cam1", {}), allow_no_roles=True)
        self.client = FakeClient()

    def tearDown(self):
        self.blocking.release.set()
        self.bindings.close(); self.points.close(); self._tmp.cleanup()

    def runner(self, *, can_write=False, client=None, max_concurrent=1):
        return EventRunner(domains=self.domains, bindings=self.bindings, points=self.points,
                           client=client if client is not None else self.client,
                           can_write=can_write, max_concurrent=max_concurrent)

    def submit(self, r, **kw):
        args = dict(domain="echo_img", binding="cam1", data=b"\x89PNG-bytes",
                    content_type="image/png", captured_at=SHOT)
        args.update(kw)
        return r.submit(**args)

    def assertStatus(self, status, fn):
        with self.assertRaises(EventError) as c:
            fn()
        self.assertEqual(c.exception.status, status, c.exception.message)
        return c.exception.message


class TestSubmit(Base):
    def test_成功时结论的T是拍照时刻(self):
        res = self.submit(self.runner())
        self.assertTrue(res.run.ok)
        got = {f.key: f for f in res.run.findings}
        self.assertEqual(got["size"].value, len(b"\x89PNG-bytes"))
        self.assertEqual(got["ctype"].value, "image/png")
        # ★T = 拍照时刻（换算到 UTC 后同一时刻），不是现在
        self.assertEqual(got["size"].t, SHOT.astimezone(timezone.utc))
        self.assertEqual(res.frame.t_start, res.frame.t_end)
        self.assertEqual(res.frame.channels, {})

    def test_缺拍照时刻被拒(self):
        msg = self.assertStatus(400, lambda: self.submit(self.runner(), captured_at=None))
        self.assertIn("拍照时刻", msg)

    def test_不带时区的时刻被拒(self):
        msg = self.assertStatus(
            400, lambda: self.submit(self.runner(), captured_at=datetime(2026, 9, 11, 9, 30)))
        self.assertIn("时区", msg)

    def test_未装载的域404(self):
        self.assertStatus(404, lambda: self.submit(self.runner(), domain="没这个域"))

    def test_没有图片输入的域409(self):
        msg = self.assertStatus(409, lambda: self.submit(self.runner(), domain="point_only"))
        self.assertIn("没有图片类输入", msg)

    def test_没有绑定404且说清为什么要绑定(self):
        msg = self.assertStatus(404, lambda: self.submit(self.runner(), binding="没建过"))
        self.assertIn("台账", msg)

    def test_停用的绑定409(self):
        self.bindings.put(Binding("echo_img", "cam2", {}, enabled=False), allow_no_roles=True)
        self.assertStatus(409, lambda: self.submit(self.runner(), binding="cam2"))

    def test_空正文400(self):
        self.assertStatus(400, lambda: self.submit(self.runner(), data=b""))

    def test_太大413(self):
        big = b"x" * (events_mod.MAX_BLOB_BYTES + 1)
        self.assertStatus(413, lambda: self.submit(self.runner(), data=big))

    def test_缺类型400(self):
        self.assertStatus(400, lambda: self.submit(self.runner(), content_type=""))

    def test_多路图片输入时必须指明role(self):
        r = self.runner()
        self.assertStatus(400, lambda: self.submit(r, domain="two_img"))
        self.assertStatus(400, lambda: self.submit(r, domain="two_img", role="top"))
        res = self.submit(r, domain="two_img", role="side")
        self.assertEqual(set(res.frame.blobs), {"side"})

    def test_台账照抄进帧(self):
        self.bindings.put(Binding("echo_img", "cam1", {}, params={"conf_threshold": "0.4"}),
                          allow_no_roles=True)
        res = self.submit(self.runner())
        self.assertEqual(res.frame.params, {"conf_threshold": "0.4"})


class TestWrite(Base):
    def test_只读运行如实说没写(self):
        res = self.submit(self.runner(can_write=False))
        self.assertFalse(res.written)
        self.assertIn("只读", res.write_note)
        self.assertEqual(self.client.posted, [])

    def test_写路径就绪时真写且按点位对上(self):
        res = self.submit(self.runner(can_write=True))
        self.assertTrue(res.written, res.write_note)
        (items,) = self.client.posted
        keys = {f.key for _lid, f in items}
        self.assertEqual(keys, {"size", "ctype"})
        for lid, f in items:
            self.assertEqual(lid, self.points.local_id_of("echo_img", "cam1", f.key))

    def test_写失败不吞推理结果(self):
        res = self.submit(self.runner(can_write=True, client=FakeClient(fail=True)))
        self.assertFalse(res.written)
        self.assertIn("写实时库失败", res.write_note)
        self.assertEqual(len(res.run.findings), 2, "推理结果照样回给调用方")


class TestBusy(Base):
    def test_满了回503而不是无限排队(self):
        r = self.runner(max_concurrent=1)
        old = events_mod.BUSY_WAIT_SEC
        events_mod.BUSY_WAIT_SEC = 0.2
        try:
            t = threading.Thread(target=lambda: self.submit(r, domain="blocking"))
            t.start()
            self.assertTrue(self.blocking.entered.wait(5), "第一张没进推理")
            msg = self.assertStatus(503, lambda: self.submit(r))
            self.assertIn("繁忙", msg)
            self.blocking.release.set()
            t.join(5)
            # 放开之后恢复正常
            self.assertTrue(self.submit(r).run.ok)
        finally:
            events_mod.BUSY_WAIT_SEC = old


class TestHttpInfer(Base):
    def setUp(self):
        super().setUp()
        self.srv = make_server("127.0.0.1:0", guid="g", version="0.1.0", domains=list(self.domains),
                               artifacts_dir=Path(self._tmp.name) / "artifacts", can_write=False,
                               events=self.runner())
        self.port = self.srv.server_address[1]
        serve_in_thread(self.srv)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def post(self, path, data=b"\x89PNG-bytes", headers=None):
        h = {"Content-Type": "image/png", "X-Captured-At": "2026-09-11T09:30:00+08:00"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_成功回结论且时刻是拍照时刻(self):
        code, body = self.post("/infer/echo_img/cam1")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertFalse(body["written"])
        self.assertIn("只读", body["write_note"])
        got = {f["key"]: f for f in body["findings"]}
        self.assertEqual(got["size"]["value"], len(b"\x89PNG-bytes"))
        self.assertEqual(got["size"]["quality"], "ok")
        self.assertEqual(got["size"]["status_code"], 1)
        self.assertEqual(datetime.fromisoformat(got["size"]["t"]), SHOT)

    def test_Z结尾的时刻也认(self):
        """★现场是 Python 3.10，标准库 fromisoformat 不认 Z —— 不手工换，最常见的写法会被拒。"""
        code, body = self.post("/infer/echo_img/cam1",
                               headers={"X-Captured-At": "2026-09-11T01:30:00Z"})
        self.assertEqual(code, 200, body)
        t = datetime.fromisoformat(body["findings"][0]["t"])
        self.assertEqual(t, SHOT)

    def test_缺时刻400(self):
        code, body = self.post("/infer/echo_img/cam1", headers={"X-Captured-At": None})
        self.assertEqual(code, 400)
        self.assertIn("拍照时刻", body["error"])

    def test_裸时刻400(self):
        code, body = self.post("/infer/echo_img/cam1",
                               headers={"X-Captured-At": "2026-09-11T09:30:00"})
        self.assertEqual(code, 400)
        self.assertIn("时区", body["error"])

    def test_乱写的时刻400(self):
        # 请求头只能放 latin-1：中文值在客户端就发不出去（第一版用例正是这么炸的，没打到服务端）。
        code, body = self.post("/infer/echo_img/cam1", headers={"X-Captured-At": "yesterday-afternoon"})
        self.assertEqual(code, 400)
        self.assertIn("ISO 8601", body["error"])

    def test_事件错误码原样透出(self):
        from urllib.parse import quote
        # ★路径里的中文必须百分号编码（第一版没编码，客户端就炸了，服务端一次都没被打到）。
        code, body = self.post(quote("/infer/没这个域/cam1"))
        self.assertEqual(code, 404)
        self.assertIn("没这个域", body["error"], "服务端要把编码后的中文还原出来")
        self.assertEqual(self.post("/infer/point_only/cam1")[0], 409)
        code, body = self.post(quote("/infer/echo_img/没建过"))
        self.assertEqual(code, 404)
        self.assertIn("没建过", body["error"])

    def test_声明超上限的请求不读正文直接413(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/infer/echo_img/cam1")
        conn.putheader("Content-Type", "image/png")
        conn.putheader("X-Captured-At", "2026-09-11T09:30:00+08:00")
        conn.putheader("Content-Length", str(events_mod.MAX_BLOB_BYTES + 1))
        conn.endheaders()                    # 故意一个字节正文都不发
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_未知POST路径404(self):
        self.assertEqual(self.post("/infer/only-two-parts")[0], 404)
        self.assertEqual(self.post("/whatever/a/b")[0], 404)

    def test_没接事件入口时503(self):
        srv = make_server("127.0.0.1:0", guid="g", version="0.1.0", domains=[],
                          artifacts_dir=Path(self._tmp.name) / "a2", can_write=False)
        serve_in_thread(srv)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/infer/echo_img/cam1",
                data=b"x", headers={"Content-Type": "image/png",
                                    "X-Captured-At": "2026-09-11T09:30:00+08:00"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(c.exception.code, 503)
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
