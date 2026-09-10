"""HTTP 口 —— **只放大对象与健康**（C-8 §3：控制面 gRPC，大对象 HTTP）。

放这里的：模型工件下载、抓拍/图片、数据集导出 —— 字节流，要断点续传/缓存，浏览器原生。
**不放**结构化控制面：那些走 gRPC，契约只有一份。

★同样**只面向 AICloud 后端**（默认只绑回环）。浏览器不直连我方 —— 见 `api.py` 模块头。

用标准库 `http.server`：骨架**零重依赖**，而这个口的负载是"偶尔下一个文件"，
起一个框架不值当。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    server_version = "AIIntegration"
    ctx = None  # 由 make_server 注入

    def log_message(self, fmt, *args):
        # 默认实现直接写 stderr，绕过我方日志体系（也就绕过了 SubscribeLogs）。
        logger.info("http %s - %s", self.address_string(), fmt % args)

    # ── 工具 ──────────────────────────────────────────────────────────────
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    # ── 路由 ──────────────────────────────────────────────────────────────
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/health":
            return self._health()
        if path.startswith("/artifacts/"):
            return self._artifact(path[len("/artifacts/"):])
        self._err(404, f"没有这个路径: {path}")

    def _health(self):
        c = self.ctx
        self._json({
            "status": "ok",
            "guid": c["guid"],
            "version": c["version"],
            "domains": sorted(c["domains"]),
            # ★写路径没配就明说，不装作正常：没有它，结论不回流，平台侧永远看不到 AI 结果。
            "writePath": "ready" if c["can_write"] else "未配置（结论不回流）",
        })

    def _artifact(self, rel: str):
        """下载模型工件等大对象。

        ★路径穿越防护：解析成绝对路径后必须仍在工件根之下。
        `..` 这类在 URL 里是**合法字符**，不防就等于把整个文件系统开出去。
        """
        root: Path = self.ctx["artifacts_dir"]
        try:
            target = (root / rel).resolve()
            root_resolved = root.resolve()
            if not target.is_relative_to(root_resolved):
                return self._err(403, "路径越界")
        except (OSError, ValueError):
            return self._err(400, "路径非法")
        if not target.is_file():
            return self._err(404, "没有这个工件")

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        with target.open("rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)


def make_server(listen: str, *, guid: str, version: str, domains, artifacts_dir: Path,
                can_write: bool) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    handler = type("_Bound", (_Handler,), {"ctx": {
        "guid": guid, "version": version, "domains": domains,
        "artifacts_dir": artifacts_dir, "can_write": can_write,
    }})
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), handler)
    srv.daemon_threads = True
    return srv


def serve_in_thread(srv: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, name="http-api", daemon=True)
    t.start()
    return t
