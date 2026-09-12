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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .artifact_import import MAX_IMPORT_BYTES, ImportRejected
from .events import MAX_BLOB_BYTES, EventError

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
            return self._download(self.ctx["artifacts_dir"], path[len("/artifacts/"):], "工件")
        if path.startswith("/reports/"):
            # 片段报告（`C-11 §3.4`：报告是大对象 ⇒ HTTP，与 C-8 定的一致）。
            return self._download(self.ctx["reports_dir"], path[len("/reports/"):], "报告")
        self._err(404, f"没有这个路径: {path}")

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["artifacts", "import"]:
            return self._import_artifact(parts[2])
        if len(parts) == 3 and parts[0] == "infer":
            return self._infer(parts[1], parts[2])
        self._err(404, f"没有这个路径: {path}")

    def _import_artifact(self, domain: str):
        """`POST /artifacts/import/{域}?name=…&source=…&training_data=…&license=…`：正文是工件字节。"""
        importer = self.ctx.get("importer")
        if importer is None:
            return self._err(503, "本实例未接工件导入")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._err(411, "必须带 Content-Length（不收分块上传）")
        if length > MAX_IMPORT_BYTES:
            self.close_connection = True
            return self._err(413, f"工件 {length} 字节，超过上限 {MAX_IMPORT_BYTES} 字节")
        data = self.rfile.read(length) if length > 0 else b""
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        fields = {k: v[0] for k, v in query.items() if v}
        try:
            res = importer.import_blob(domain=domain, data=data, fields=fields)
        except ImportRejected as exc:
            return self._err(exc.status, exc.message)
        self._json({"ok": True, "artifact_id": res.artifact_id, "path": res.path, "size": res.size,
                    "sha256": res.sha256, "validated": res.validated, "facts": res.facts,
                    "warnings": res.warnings})

    def _infer(self, domain: str, binding: str):
        """`POST /infer/{域}/{绑定}`：正文是图片原始字节。来一张算一次。

        请求头：`Content-Type`（必填）、**`X-Captured-At`（拍照时刻，ISO 8601，必须带时区）**、
        `X-Role`（域有多路图片输入时指明）、`X-Source`（来源说明，可选）。
        """
        events = self.ctx.get("events")
        if events is None:
            return self._err(503, "本实例未接事件入口")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._err(411, "必须带 Content-Length（不收分块上传）")
        if length > MAX_BLOB_BYTES:
            # 不读正文就回 —— 并断开连接，免得残留的正文被当成下一个请求。
            self.close_connection = True
            return self._err(413, f"上传 {length} 字节，超过上限 {MAX_BLOB_BYTES} 字节")
        data = self.rfile.read(length) if length > 0 else b""
        captured, why = _parse_captured_at(self.headers.get("X-Captured-At"))
        if why:
            return self._err(400, why)
        try:
            res = events.submit(domain=domain, binding=binding, data=data,
                                content_type=self.headers.get("Content-Type", ""),
                                captured_at=captured, role=self.headers.get("X-Role") or None,
                                source=self.headers.get("X-Source", ""))
        except EventError as exc:
            return self._err(exc.status, exc.message)
        self._json({
            "ok": res.run.ok,
            "error": res.run.error,
            "domain": domain,
            "binding": binding,
            "captured_at": res.frame.t_end.isoformat(),
            # ★没写就明说没写、为什么 —— 调用方不该以为平台上已经有这条记录。
            "written": res.written,
            "write_note": res.write_note,
            "findings": [_finding_json(f) for f in res.run.findings],
        })

    def _health(self):
        c = self.ctx
        self._json({
            "status": "ok",
            "guid": c["guid"],
            "version": c["version"],
            "domains": sorted(c["domains"]),
            # ★写路径没配就明说，不装作正常：没有它，结论不回流，平台侧永远看不到 AI 结果。
            "writePath": "ready" if c["can_write"] else "未配置（结论不回流）",
            # ★与 writePath 不是一回事（AICloud C-27 §3.2 要的那一格）：
            #   writePath 只说"配了写路径且证书齐"，而实体流是否认得这些点，它说不出来 ——
            #   实时库重启后点定义会丢，值却照样写得进、writePath 照样 ready，
            #   现场只能靠数平台镜像的行数才发现。这一格直说：快照被**当前这个引擎实例**接受了没有。
            "snapshot": _snapshot_state(c),
        })

    def _download(self, root: Path, rel: str, what: str):
        """下载大对象（工件 / 报告）。**两条路走同一段代码**。

        ★为什么不各写一遍：这段里有路径穿越防护，复制一份就等于给了它一次退化的机会 ——
          hs 那个"离散点 Raw 恒回空"的缺陷，成因正是同一个方法在两条路各写一遍、其中一条忘了。

        ★路径穿越防护：解析成绝对路径后必须仍在根之下。
        `..` 这类在 URL 里是**合法字符**，不防就等于把整个文件系统开出去。
        """
        try:
            target = (root / rel).resolve()
            root_resolved = root.resolve()
            if not target.is_relative_to(root_resolved):
                return self._err(403, "路径越界")
        except (OSError, ValueError):
            return self._err(400, "路径非法")
        if not target.is_file():
            return self._err(404, "没有这个" + what)

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


def _parse_captured_at(raw: str | None) -> tuple[datetime | None, str]:
    """解析拍照时刻。★没有缺省：缺了、不带时区，一律拒。

    ★Python **3.10** 的 `datetime.fromisoformat` 不认末尾的 `Z`（3.11 起才认），
      而现场（AISERVER）正是 3.10 —— 不手工换，`2026-09-11T08:00:00Z` 这种最常见的写法在现场会被拒。
    """
    if not raw or not raw.strip():
        return None, ("缺请求头 X-Captured-At（拍照时刻，ISO 8601，必须带时区）"
                      "—— 结论的时刻是拍照时刻，不是上传时刻，不给就不算")
    s = raw.strip()
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, f"X-Captured-At 不是合法的 ISO 8601 时刻: {raw!r}"
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None, f"X-Captured-At 必须带时区（裸时刻跨机就是另一个时刻）: {raw!r}"
    return dt, ""


def _finding_json(f) -> dict:
    return {
        "key": f.key,
        "value": f.value,                           # 坏质量下恒为 null
        "quality": f.quality.value,                 # 我方语义（ok / model_not_loaded / …）
        "status_code": f.quality.to_status_code(),  # daq.StatusCode（-1001 = 台账没填 …）
        "t": f.t.isoformat(),
    }


def _snapshot_state(c) -> str:
    """健康口里那一格的取值。`scheduler` 没接（只读运行/夹具）时说"不适用"，不谎称正常。"""
    if not c.get("can_write"):
        return "不适用（未配置写路径）"
    sched = c.get("scheduler")
    if sched is None:
        return "未知（调度未接）"
    return "accepted" if sched.snapshot_ok else "stale（断连后尚未重推，点定义可能已丢）"


def make_server(listen: str, *, guid: str, version: str, domains, artifacts_dir: Path,
                can_write: bool, reports_dir: Path | None = None,
                events=None, importer=None, scheduler=None) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = Path(reports_dir) if reports_dir else artifacts_dir.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    handler = type("_Bound", (_Handler,), {"ctx": {
        "guid": guid, "version": version, "domains": domains,
        "artifacts_dir": artifacts_dir, "reports_dir": reports_dir,
        "can_write": can_write, "events": events, "importer": importer,
        "scheduler": scheduler,
    }})
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), handler)
    srv.daemon_threads = True
    return srv


def serve_in_thread(srv: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, name="http-api", daemon=True)
    t.start()
    return t
