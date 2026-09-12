"""地址校验的回归 —— 端口 0 与同类的「形态过关、语义不成立」。

写这个文件是因为 2026-09-12 的对称自查：AICloud `C-20 §4` 报出他们把 `127.0.0.1:0`
存进了库（拨号端），我方一查，**监听端有同一个洞的镜像**：

    grpc.add_insecure_port("127.0.0.1:0")      → 回 37511（真实端口，非 0）
    ThreadingHTTPServer(("127.0.0.1", 0))      → 实际绑到 37201

而我方当时的判据是「`add_insecure_port` 回 0 才算失败」⇒ **挡不住它**：
服务起得来、`/health` 正常、日志正常，**而 AICloud 拨 50070 永远连不上**。

钉四条：
  · 端口 0 一律拒（`:0`、`:00` 都拒 —— 判的是数值不是字符串）；
  · 监听地址不合规 → **拒绝启动**（起来也没人找得到）；
  · 写路径地址不合规 → **不掀翻服务**，降级只读 + 说清是地址错；
  · 那两个"静默随机化"的前提是真的（本文件最后一条用真 socket 钉住它）。
"""

import threading
import unittest

from aiintegration.config import Config, ConfigError, addr_problem, check_addr
from aiintegration.service import Service


class TestCheckAddr(unittest.TestCase):

    def test_ok(self):
        self.assertEqual(check_addr("127.0.0.1:50070", what="X"), ("127.0.0.1", 50070))
        self.assertEqual(check_addr("0.0.0.0:1", what="X"), ("0.0.0.0", 1))
        self.assertEqual(check_addr("example.host:65535", what="X"), ("example.host", 65535))

    def test_ipv6_bracketed(self):
        self.assertEqual(check_addr("[::1]:5400", what="X"), ("[::1]", 5400))

    def test_port_zero_rejected(self):
        """★这一条就是本文件的由来。"""
        with self.assertRaises(ConfigError) as cm:
            check_addr("127.0.0.1:0", what="AII_API_LISTEN")
        msg = str(cm.exception)
        self.assertIn("AII_API_LISTEN", msg)
        self.assertIn("0", msg)
        self.assertIn("拨不到", msg)          # 要说清后果，不能只说"非法"

    def test_port_zero_padded_also_rejected(self):
        """`:00` / `:000` —— 判的是数值，不是字符串长相。"""
        for bad in ("127.0.0.1:00", "127.0.0.1:000"):
            with self.subTest(addr=bad):
                with self.assertRaises(ConfigError):
                    check_addr(bad, what="X")

    def test_non_numeric_port_rejected(self):
        """服务名不认 —— 本进程的地址来自环境变量，不是人在界面上敲的。"""
        for bad in ("127.0.0.1:https", "127.0.0.1:", "127.0.0.1: 50070", "127.0.0.1:-1"):
            with self.subTest(addr=bad):
                with self.assertRaises(ConfigError):
                    check_addr(bad, what="X")

    def test_out_of_range_rejected(self):
        with self.assertRaises(ConfigError):
            check_addr("127.0.0.1:65536", what="X")

    def test_missing_parts_rejected(self):
        for bad in ("", "   ", "50070", ":50070"):
            with self.subTest(addr=bad):
                with self.assertRaises(ConfigError):
                    check_addr(bad, what="X")

    def test_message_names_the_variable(self):
        """现场要能一眼看出**改哪个环境变量**。"""
        with self.assertRaises(ConfigError) as cm:
            check_addr("nope", what="AII_HTTP_LISTEN")
        self.assertIn("AII_HTTP_LISTEN", str(cm.exception))


class TestAddrProblem(unittest.TestCase):

    def test_ok_is_none(self):
        self.assertIsNone(addr_problem("127.0.0.1:5400", what="X"))

    def test_bad_is_a_readable_reason(self):
        why = addr_problem("127.0.0.1:0", what="AII_HS_WRITE")
        self.assertIsNotNone(why)
        self.assertIn("AII_HS_WRITE", why)


def _cfg(case: unittest.TestCase | None = None, **kw) -> Config:
    """造一个 Config。

    ★给了 `case` 就**真建出三个证书文件**：否则 `can_write()` 会因为"证书不在"而回 False，
      于是"地址非法所以不可写"这条断言**根本没被钉住**（假绿）。
      —— 这正是本轮变异验证逮到的：去掉 can_write 里的地址检查，用例居然还全绿。
    """
    import tempfile
    from pathlib import Path
    root = Path("/tmp/x")
    cert_dir = root / "cert"
    if case is not None:
        d = tempfile.TemporaryDirectory()
        case.addCleanup(d.cleanup)
        root = Path(d.name)
        cert_dir = root / "cert"
        cert_dir.mkdir(parents=True, exist_ok=True)
        for name in ("ca.cer", "client.cer", "client.key"):
            (cert_dir / name).write_text("x", encoding="utf-8")
    base = dict(
        root=root, domains_dir=root / "domains", data_dir=root / "data",
        cert_dir=cert_dir, guid_paths=[root / "system.guid", root / "system.guid.bak"],  # 主+备，describe() 要两个
        hs_read_addr="127.0.0.1:5400", hs_write_addr="",
        api_listen="127.0.0.1:50070", http_listen="127.0.0.1:50071", log_capacity=100,
    )
    base.update(kw)
    return Config(**base)


class TestConfigPolicy(unittest.TestCase):
    """两类地址**两种处置**：监听错 → 拒绝启动；写路径错 → 降级只读。"""

    def test_listen_ok(self):
        _cfg().validate_listen()                       # 不抛即通过

    def test_listen_zero_raises(self):
        with self.assertRaises(ConfigError):
            _cfg(api_listen="127.0.0.1:0").validate_listen()
        with self.assertRaises(ConfigError):
            _cfg(http_listen="127.0.0.1:0").validate_listen()

    def test_empty_write_addr_is_legal_readonly(self):
        """没配写路径 = 合法的只读运行，**不是**错误。"""
        c = _cfg(hs_write_addr="")
        self.assertIsNone(c.write_addr_problem())
        self.assertFalse(c.can_write())

    def test_good_write_addr_with_certs_is_writable(self):
        """对照组：证书齐 + 地址合规 ⇒ 能写。没有这一条，下一条的 False 说明不了任何事。"""
        c = _cfg(self, hs_write_addr="192.168.1.135:5400")
        self.assertIsNone(c.write_addr_problem())
        self.assertTrue(c.can_write())

    def test_bad_write_addr_is_not_writable(self):
        """★别拿着 :0 去连 —— 那会把「配置错」伪装成「连不上」。

        ★证书**必须是齐的**（`_cfg(self, ...)` 真建了文件），否则 False 来自证书缺失，
          这条断言就成了假绿（本轮变异验证逮到过一次）。
        """
        c = _cfg(self, hs_write_addr="192.168.1.135:0")
        self.assertIsNotNone(c.write_addr_problem())
        self.assertFalse(c.can_write())

    def test_bad_write_addr_does_not_break_listen(self):
        """写路径坏掉不该掀翻服务：监听照样合规。"""
        _cfg(hs_write_addr="192.168.1.135:0").validate_listen()


class TestServiceRefusesBadListen(unittest.TestCase):
    """★钉住「启动时真的会拒」——光有 `validate_listen` 不够，得有人调它。

    （本轮变异验证逮到过：把 `service.run()` 里那句校验换成 `pass`，整机用例照样全绿。）
    """

    def _run(self, cfg, seconds: float = 8.0):
        """在**线程里**跑 `run()`，超时即判失败，返回退出码。

        ★不能直接 `Service(cfg).run()`：校验一旦失灵，`run()` 会真把服务起起来、
          阻塞在等停上 —— 用例就从"变红"退化成"挂死"（本轮变异验证真遇到：跑满 120 秒
          被外部杀掉）。**挂死比失败更糟**：看不出是哪一条断言，也没有任何信息。
        """
        box: dict = {}
        svc = Service(cfg)
        t = threading.Thread(target=lambda: box.__setitem__("rc", svc.run()), daemon=True)
        t.start()
        t.join(seconds)
        if t.is_alive():
            svc.stop()                      # 劝停，别把线程留给后面的用例
            t.join(5)
            self.fail("run() 没拒绝非法监听地址，而是真起了服务（等停超时）")
        return box.get("rc")

    def test_run_returns_2(self):
        self.assertEqual(self._run(_cfg(self, api_listen="127.0.0.1:0")), 2,
                         "监听地址不合规必须拒绝启动")

    def test_fails_before_touching_anything(self):
        """★fail fast 的证据：连身份文件都没生成 —— 校验排在 guid/建库/连 hs 之前。"""
        cfg = _cfg(self, http_listen="127.0.0.1:0")
        rc = self._run(cfg)
        self.assertEqual(rc, 2)
        for g in cfg.guid_paths:
            self.assertFalse(g.exists(), f"不该走到生成身份这一步：{g}")
        self.assertFalse(cfg.data_dir.exists(), "不该走到建库这一步")


class TestTheSilentRandomisationIsReal(unittest.TestCase):
    """钉住上面所有规矩的**前提**：端口 0 真的会被静默随机化，且旧判据看不出来。

    前提要是哪天变了（库改了行为），这条会红 —— 那时该重新想，而不是照抄本文件的结论。
    """

    def test_grpc_port_zero_returns_a_real_port(self):
        import grpc
        from concurrent import futures
        srv = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        try:
            got = srv.add_insecure_port("127.0.0.1:0")
            self.assertNotEqual(got, 0, "若回 0，旧判据就挡得住，本文件的前提要重想")
            self.assertGreater(got, 0)
        finally:
            srv.stop(0)

    def test_http_server_port_zero_binds_random(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            self.assertNotEqual(srv.server_address[1], 0)
        finally:
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
