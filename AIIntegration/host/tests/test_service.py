"""配置与 HTTP 口的回归，以及"没有写路径也能起来"这条。"""

import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

from aiintegration.config import Config
from aiintegration.httpapi import make_server, serve_in_thread


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {k: v for k, v in os.environ.items() if k.startswith("AII_")}
        for k in list(os.environ):
            if k.startswith("AII_"):
                del os.environ[k]

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("AII_"):
                del os.environ[k]
        os.environ.update(self._saved)

    def test_缺省值可用(self):
        c = Config.from_env()
        self.assertEqual(c.hs_read_addr, "127.0.0.1:5400")
        self.assertEqual(c.hs_write_addr, "")      # 未配写路径 = 只读运行
        self.assertFalse(c.can_write())

    def test_对外口默认只绑回环(self):
        # 浏览器不直连我方；对外只面向 AICloud 后端。默认就该是回环。
        c = Config.from_env()
        self.assertTrue(c.api_listen.startswith("127.0.0.1:"))
        self.assertTrue(c.http_listen.startswith("127.0.0.1:"))

    def test_guid冗余两处且第二处在应用目录之外(self):
        # 只存一份，重铺应用目录就换身份 —— 换身份的后果是旧点变无主数据且清不掉。
        c = Config.from_env()
        self.assertEqual(len(c.guid_paths), 2)
        self.assertFalse(str(c.guid_paths[1]).startswith(str(c.root)))

    def test_环境变量覆盖并且来源标得出来(self):
        os.environ["AII_HS_READ"] = "10.0.0.1:5400"
        c = Config.from_env()
        self.assertEqual(c.hs_read_addr, "10.0.0.1:5400")
        desc = "\n".join(c.describe())
        self.assertIn("AII_HS_READ=10.0.0.1:5400(env)", desc)
        # 缺省值也要打出来 —— 日志里看不见的配置等于不存在。
        self.assertIn("(default)", desc)

    def test_写路径要证书齐全才算就绪(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AII_HS_WRITE"] = "1.2.3.4:5400"
            os.environ["AII_CERT_DIR"] = tmp
            self.assertFalse(Config.from_env().can_write())   # 证书还没有
            for n in ("ca.cer", "client.cer", "client.key"):
                (Path(tmp) / n).write_text("x")
            self.assertTrue(Config.from_env().can_write())


class TestHttpApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts" / "model.bin").write_bytes(b"\x00\x01model")
        self.srv = make_server("127.0.0.1:0", guid="g-1", version="0.1.0",
                               domains=["vib"], artifacts_dir=self.root / "artifacts",
                               can_write=False)
        self.port = self.srv.server_address[1]
        serve_in_thread(self.srv)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self._tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read()

    def test_health_明说写路径没配(self):
        # 静默降级 = 现场以为在跑其实没结果。
        code, body = self.get("/health")
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(d["guid"], "g-1")
        self.assertIn("未配置", d["writePath"])

    def test_下载工件(self):
        code, body = self.get("/artifacts/model.bin")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"\x00\x01model")

    def test_路径穿越被挡(self):
        # `..` 在 URL 里是合法字符，不防就等于把整个文件系统开出去。
        for evil in ("/artifacts/../../etc/passwd", "/artifacts/%2e%2e/%2e%2e/etc/passwd"):
            with self.subTest(evil=evil):
                try:
                    code, _ = self.get(evil)
                except urllib.error.HTTPError as e:
                    code = e.code
                self.assertIn(code, (403, 404))

    def test_不存在的工件回404(self):
        try:
            code, _ = self.get("/artifacts/nope.bin")
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 404)

    def test_未知路径回404(self):
        try:
            code, _ = self.get("/nope")
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
