"""外部工件导入（契约 1.5）的回归 —— 零第三方依赖（用纯 Python 的假域）。

钉的：
  ① **来历三项 + 名字必填**，缺了 400 并说清缺哪几项；
  ② 只有声明了 `artifact` 能力位的域才收；
  ③ **域先校验**：不过 422；校验抛异常也算不过；没提供校验照收但回执明说；
  ④ 同一文件重复导入 409 并指明已有工件；
  ⑤ 落盘走临时文件改名；域读出的事实原样进工件；`origin=imported`；**不自动启用**；
  ⑥ 老库（1.4 时代）开库即补列，老工件读得出且 `origin=trained`；
  ⑦ 训练产出自动写来历；
  ⑧ HTTP 口：中文查询参数、错误码透出、超上限不读正文；控制面 `ListArtifacts` 带出来历；
  ⑨ 管理命令行只走服务的口。
"""

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent import futures
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from aiintegration import artifact_import as imp_mod
from aiintegration.artifact_import import ArtifactImporter, ImportRejected
from aiintegration.domains import Domain, LoadedDomain
from aiintegration.types import Declaration, InputSpec, OutputSpec
from aiintegration.workbench import Workbench, WorkbenchError

FIELDS = {"name": "安全帽 n 640", "source": "AISERVER:/home/x/models/a.onnx",
          "training_data": "未知（原作者训练，训练集未随模型交付）", "license": "未知；元数据标 AGPL-3.0",
          "ext": ".onnx"}


class _Base(Domain):
    version = "1.0.0"

    def declare(self):
        return Declaration(inputs=(InputSpec(role="image", kind="image"),),
                           outputs=(OutputSpec(key="n", display="n", value_type="int"),))

    def infer(self, frame):
        return []


class WithValidate(_Base):
    key = "with_validate"
    display = "带校验"

    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"infer", "artifact"}

    def validate_artifact(self, kind, blob):
        self.calls.append((kind, len(blob)))
        if blob.startswith(b"BAD"):
            return "文件头不对", {}
        if blob.startswith(b"BOOM"):
            raise RuntimeError("解析器炸了")
        return "", {"description": "Ultralytics YOLOv5n model", "license": "AGPL-3.0"}


class NoValidate(_Base):
    key = "no_validate"
    display = "不校验"

    def capabilities(self):
        return {"infer", "artifact"}


class NoArtifactCap(_Base):
    key = "no_cap"
    display = "不收工件"


def _loaded(inst):
    return LoadedDomain(inst, inst.declare(), frozenset(inst.capabilities()), Path("用例内造"))


class ImportBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.wb = Workbench(self.root / "wb.db")
        self.dom = WithValidate()
        self.domains = {d.key: _loaded(d) for d in (self.dom, NoValidate(), NoArtifactCap())}
        self.art_dir = self.root / "artifacts"
        self.imp = ArtifactImporter(workbench=self.wb, domains=self.domains, artifacts_dir=self.art_dir)

    def tearDown(self):
        self.wb.close(); self._tmp.cleanup()

    def assertStatus(self, status, fn):
        with self.assertRaises(ImportRejected) as c:
            fn()
        self.assertEqual(c.exception.status, status, c.exception.message)
        return c.exception.message


class TestImporter(ImportBase):
    def test_成功导入且来历齐全不自动启用(self):
        data = b"ONNX-MODEL-BYTES"
        res = self.imp.import_blob(domain="with_validate", data=data, fields=FIELDS)
        a = self.wb.list_artifacts(domain="with_validate").items[0]
        self.assertEqual(a.id, res.artifact_id)
        self.assertEqual((a.origin, a.source, a.training_data, a.license),
                         ("imported", FIELDS["source"], FIELDS["training_data"], FIELDS["license"]))
        self.assertFalse(a.active, "导入不自动启用")
        self.assertEqual((a.size, a.sha256), (len(data), hashlib.sha256(data).hexdigest()))
        self.assertIsNone(a.accuracy, "外部模型没经本系统测过，准确率是空不是 0")
        f = self.art_dir / a.path
        self.assertEqual(f.read_bytes(), data)
        self.assertTrue(a.path.endswith(".onnx"))
        self.assertFalse(list(f.parent.glob("*.part")), "临时文件该被改名")
        self.assertTrue(res.validated)
        self.assertIn("训练数据未经本系统核实", " ".join(res.warnings))
        self.assertIn("未自动启用", " ".join(res.warnings))

    def test_域读出的事实原样进工件(self):
        res = self.imp.import_blob(domain="with_validate", data=b"ok", fields=FIELDS)
        meta = json.loads(self.wb.list_artifacts(domain="with_validate").items[0].meta_json)
        self.assertEqual(meta["model_description"], "Ultralytics YOLOv5n model")
        self.assertEqual(meta["model_license"], "AGPL-3.0")
        self.assertEqual(res.facts["license"], "AGPL-3.0")

    def test_来历三项与名字缺一不可(self):
        for k in ("name", "source", "training_data", "license"):
            with self.subTest(缺=k):
                f = dict(FIELDS); f[k] = "   "
                msg = self.assertStatus(400, lambda: self.imp.import_blob(domain="with_validate", data=b"x", fields=f))
                self.assertIn(k, msg)
                self.assertIn("未知", msg, "要告诉人：不清楚就写未知，不是留空")
        self.assertEqual(self.wb.list_artifacts().total, 0)

    def test_写未知是可以的(self):
        f = dict(FIELDS, source="未知", training_data="未知", license="未知")
        self.imp.import_blob(domain="with_validate", data=b"x", fields=f)
        self.assertEqual(self.wb.list_artifacts().total, 1)

    def test_不收工件的域409(self):
        msg = self.assertStatus(409, lambda: self.imp.import_blob(domain="no_cap", data=b"x", fields=FIELDS))
        self.assertIn("artifact", msg)

    def test_未装载的域404(self):
        self.assertStatus(404, lambda: self.imp.import_blob(domain="没这个", data=b"x", fields=FIELDS))

    def test_域校验不过422且不落盘不入库(self):
        msg = self.assertStatus(422, lambda: self.imp.import_blob(domain="with_validate", data=b"BAD...", fields=FIELDS))
        self.assertIn("文件头不对", msg)
        self.assertEqual(self.wb.list_artifacts().total, 0)
        self.assertFalse(self.art_dir.exists() and any(self.art_dir.rglob("*.onnx")))

    def test_校验抛异常也算不过(self):
        msg = self.assertStatus(422, lambda: self.imp.import_blob(domain="with_validate", data=b"BOOM", fields=FIELDS))
        self.assertIn("解析器炸了", msg)

    def test_域没提供校验照收但回执明说(self):
        res = self.imp.import_blob(domain="no_validate", data=b"x", fields=FIELDS)
        self.assertFalse(res.validated)
        self.assertIn("没有经过", " ".join(res.warnings))

    def test_同一文件重复导入409并指明已有工件(self):
        first = self.imp.import_blob(domain="with_validate", data=b"same", fields=FIELDS)
        msg = self.assertStatus(409, lambda: self.imp.import_blob(domain="with_validate", data=b"same",
                                                                  fields=dict(FIELDS, name="换个名字")))
        self.assertIn(str(first.artifact_id), msg)

    def test_空正文与非法参数400(self):
        self.assertStatus(400, lambda: self.imp.import_blob(domain="with_validate", data=b"", fields=FIELDS))
        self.assertStatus(400, lambda: self.imp.import_blob(domain="with_validate", data=b"x",
                                                            fields=dict(FIELDS, kind="Bad Kind")))
        self.assertStatus(400, lambda: self.imp.import_blob(domain="with_validate", data=b"x",
                                                            fields=dict(FIELDS, ext="onnx")))

    def test_太大413(self):
        old = imp_mod.MAX_IMPORT_BYTES
        imp_mod.MAX_IMPORT_BYTES = 4
        try:
            self.assertStatus(413, lambda: self.imp.import_blob(domain="with_validate", data=b"12345", fields=FIELDS))
        finally:
            imp_mod.MAX_IMPORT_BYTES = old


class TestWorkbenchProvenance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "wb.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_外部导入的工件库层也要求来历(self):
        wb = Workbench(self.path)
        try:
            with self.assertRaises(WorkbenchError):
                wb.add_artifact(domain="d", name="m", origin="imported", source="x", training_data="", license="y")
        finally:
            wb.close()

    def test_老库开库即补列且老工件读得出(self):
        """★AISERVER 上已有一份 1.4 时代的库：CREATE TABLE IF NOT EXISTS 不会补列。"""
        conn = sqlite3.connect(str(self.path))
        conn.executescript("""
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'model', binding TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL, algo TEXT NOT NULL DEFAULT '', dataset_id INTEGER,
            sample_count INTEGER NOT NULL DEFAULT 0, feature_count INTEGER NOT NULL DEFAULT 0,
            accuracy REAL, active INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO artifacts(domain,name,dataset_id) VALUES('vib','老基线',7);
        """)
        conn.commit(); conn.close()
        wb = Workbench(self.path)
        try:
            (a,) = wb.list_artifacts(domain="vib").items
            self.assertEqual(a.name, "老基线")
            self.assertEqual(a.origin, "trained")
            self.assertIn("训练集 id=7", a.training_data)
            self.assertIn("未记录详情", a.training_data, "回填不编造细节")
            wb.add_artifact(domain="vib", name="新的", origin="imported",
                            source="s", training_data="t", license="l")
            self.assertEqual(wb.list_artifacts(domain="vib").total, 2)
        finally:
            wb.close()

    def test_按sha256找工件(self):
        wb = Workbench(self.path)
        try:
            aid = wb.add_artifact(domain="d", name="m", kind="model", sha256="abc")
            self.assertEqual(wb.find_artifact_by_sha256("d", "model", "abc").id, aid)
            self.assertIsNone(wb.find_artifact_by_sha256("d", "baseline", "abc"), "种类不同不算重复")
        finally:
            wb.close()


class TestTrainedProvenance(unittest.TestCase):
    def test_训练产出自动写来历(self):
        from aiintegration.bindings import Binding, BindingStore
        from aiintegration.quality import Quality
        from aiintegration.trainer import Trainer
        from aiintegration.types import Frame, Sample, TrainedArtifact

        class Trainable(_Base):
            key = "vib"
            display = "振动"

            def declare(self):
                return Declaration(inputs=(InputSpec(role="x", unit="mm/s"),),
                                   outputs=(OutputSpec(key="n", display="n", value_type="int"),))

            def train(self, dataset, report):
                return TrainedArtifact(blob=b"M", algo="决策树")

        class Fetch:
            def fetch(self, b, end_time, artifacts=None):
                return Frame(domain=b.domain, binding=b.binding,
                             t_start=end_time - timedelta(seconds=b.window_sec), t_end=end_time,
                             channels={"x": [Sample(t=end_time, value=1.0, quality=Quality.OK, status_code=1)]})

        with tempfile.TemporaryDirectory() as d:
            wb = Workbench(Path(d) / "wb.db")
            bs = BindingStore(Path(d) / "b.db")
            t0 = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)
            try:
                bs.put(Binding("vib", "dev1", {"x": 5}))
                ds = wb.put_dataset(domain="vib", name="正常段")
                wb.add_samples(ds, [wb.put_annotation(domain="vib", binding="dev1", label="正常",
                                                      t_from=t0, t_to=t0 + timedelta(minutes=5))])
                inst = Trainable()
                tr = Trainer(workbench=wb, bindings=bs, domains={"vib": _loaded(inst)}, fetcher=Fetch(),
                             artifacts_dir=Path(d) / "artifacts")
                jid = tr.submit(domain="vib", dataset_id=ds)
                tr.start()
                deadline = time.monotonic() + 10
                while wb.get_job(jid).status not in ("ready", "failed") and time.monotonic() < deadline:
                    time.sleep(0.05)
                tr.stop(timeout=5)
                self.assertEqual(wb.get_job(jid).status, "ready", wb.get_job(jid).message)
                (a,) = wb.list_artifacts(domain="vib").items
                self.assertEqual(a.origin, "trained")
                self.assertEqual(a.source, f"训练任务 {jid}")
                self.assertIn("训练集「正常段」", a.training_data)
                self.assertIn("1 条样本", a.training_data)
                self.assertEqual(a.license, "")
            finally:
                bs.close(); wb.close()


class TestHttpImport(ImportBase):
    def setUp(self):
        super().setUp()
        from aiintegration.httpapi import make_server, serve_in_thread
        self.srv = make_server("127.0.0.1:0", guid="g", version="0.1.0", domains=list(self.domains),
                               artifacts_dir=self.art_dir, can_write=False, importer=self.imp)
        self.port = self.srv.server_address[1]
        serve_in_thread(self.srv)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def post(self, domain, fields, data=b"ONNX"):
        url = f"http://127.0.0.1:{self.port}/artifacts/import/{quote(domain)}?{urlencode(fields)}"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_中文参数导入成功(self):
        code, body = self.post("with_validate", FIELDS)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["validated"])
        a = self.wb.list_artifacts().items[0]
        self.assertEqual(a.training_data, FIELDS["training_data"], "中文要原样还原")
        self.assertEqual(body["facts"]["license"], "AGPL-3.0")

    def test_缺必填400(self):
        code, body = self.post("with_validate", dict(FIELDS, license=""))
        self.assertEqual(code, 400)
        self.assertIn("license", body["error"])

    def test_错误码透出(self):
        self.assertEqual(self.post("no_cap", FIELDS)[0], 409)
        self.assertEqual(self.post("没这个", FIELDS)[0], 404)
        self.assertEqual(self.post("with_validate", FIELDS, data=b"BAD")[0], 422)

    def test_声明超上限不读正文413(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", f"/artifacts/import/with_validate?{urlencode(FIELDS)}")
        conn.putheader("Content-Length", str(imp_mod.MAX_IMPORT_BYTES + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_没接导入时503(self):
        from aiintegration.httpapi import make_server, serve_in_thread
        srv = make_server("127.0.0.1:0", guid="g", version="0.1.0", domains=[],
                          artifacts_dir=self.root / "a2", can_write=False)
        serve_in_thread(srv)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/artifacts/import/with_validate?{urlencode(FIELDS)}",
                data=b"x", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(c.exception.code, 503)
        finally:
            srv.shutdown(); srv.server_close()


class TestApiProvenance(ImportBase):
    def test_控制面带出来历(self):
        import grpc

        from aiintegration.api import SERVICE, ApiService, build_handler
        from aiintegration.apiproto import aiintegration_pb2 as pb
        from aiintegration.bindings import BindingStore
        from aiintegration.logstore import LogStore

        self.imp.import_blob(domain="with_validate", data=b"x", fields=FIELDS)
        bs = BindingStore(self.root / "b.db")
        svc = ApiService(guid="g", version="0.1.0", logstore=LogStore(capacity=10),
                         domains=self.domains, bindings=bs, workbench=self.wb)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2), handlers=(build_handler(svc),))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        ch = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)])
        try:
            res = ch.unary_unary(f"/{SERVICE}/ListArtifacts",
                                 request_serializer=lambda m: m.SerializeToString(),
                                 response_deserializer=pb.ListArtifactsRes.FromString)(
                pb.ListArtifactsReq(domain="with_validate"), timeout=5)
            (x,) = res.items
            self.assertEqual((x.origin, x.source, x.training_data, x.license),
                             ("imported", FIELDS["source"], FIELDS["training_data"], FIELDS["license"]))
            self.assertFalse(x.has_accuracy)
        finally:
            ch.close(); server.stop(0); bs.close()


class TestAdminCli(ImportBase):
    def test_命令行走HTTP口导入(self):
        from aiintegration import admin
        from aiintegration.httpapi import make_server, serve_in_thread
        srv = make_server("127.0.0.1:0", guid="g", version="0.1.0", domains=list(self.domains),
                          artifacts_dir=self.art_dir, can_write=False, importer=self.imp)
        serve_in_thread(srv)
        model = self.root / "m.onnx"
        model.write_bytes(b"CLI-MODEL")
        try:
            rc = admin.main(["import-artifact", "--domain", "with_validate", "--file", str(model),
                             "--name", "命令行导入", "--source", "本机文件", "--training-data", "未知",
                             "--license", "未知", "--http", f"127.0.0.1:{srv.server_address[1]}"])
            self.assertEqual(rc, 0)
            (a,) = self.wb.list_artifacts().items
            self.assertEqual((a.name, a.origin), ("命令行导入", "imported"))
            self.assertTrue(a.path.endswith(".onnx"), "扩展名取自文件")
        finally:
            srv.shutdown(); srv.server_close()

    def test_启用必须带yes(self):
        from aiintegration import admin
        self.assertEqual(admin.main(["activate-artifact", "--id", "1"]), 2)


if __name__ == "__main__":
    unittest.main()
