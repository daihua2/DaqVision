"""当前启用工件的读取与缓存 + **基线闭环**。

前半钉 `artifactcache.py` 那几条；后半是**真闭环**：
标注 → 训练集 → 开训（真域采基线）→ 启用 → 推理时真的用上了。

★为什么闭环要单独有一条：这条路穿过 5 个件（工作台 / 执行器 / 域 / 工件缓存 / 成帧），
  每个件的单测都绿，而"工件到底有没有到模块手里"只有把它们串起来才看得见。
  与整机那条用例同一个理由（`AI-13 §6.1`）。
"""

import dataclasses
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.artifactcache import ActiveArtifacts
from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.trainer import Trainer
from aiintegration.types import Frame, Sample
from aiintegration.workbench import KIND_BASELINE, KIND_MODEL, Workbench

UTC = timezone.utc
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=UTC)
DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"

FULL_PARAMS = {"iso_group": "2", "mount_type": "rigid",
               "vel_is_rms": "true", "axial_axis": "z"}


class TestActiveArtifacts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.wb = Workbench(root / "wb.db")
        self.dir = root / "artifacts"
        (self.dir / "vib").mkdir(parents=True)
        (self.dir / "vib" / "m1.bin").write_bytes(b"MODEL-1")
        (self.dir / "vib" / "b1.json").write_bytes(b'{"format":"x"}')
        self.cache = ActiveArtifacts(self.wb, self.dir)

    def tearDown(self):
        self.wb.close(); self._tmp.cleanup()

    def test_没启用就没有这一档(self):
        self.wb.add_artifact(domain="vib", name="m", binding="dev1", path="vib/m1.bin")
        # ★不给空的、更不"随便挑个最新的顶上" —— 顶上去的结论看着完全正常。
        self.assertEqual(self.cache.for_binding("vib", "dev1"), {})

    def test_启用后拿得到且字节对(self):
        aid = self.wb.add_artifact(domain="vib", name="m", binding="dev1",
                                   path="vib/m1.bin", algo="决策树", accuracy=0.9)
        self.wb.activate_artifact(aid)
        got = self.cache.for_binding("vib", "dev1")
        self.assertEqual(set(got), {"model"})
        self.assertEqual(got["model"].blob, b"MODEL-1")
        self.assertEqual(got["model"].algo, "决策树")
        self.assertEqual(got["model"].accuracy, 0.9)

    def test_模型与基线各占各的档(self):
        m = self.wb.add_artifact(domain="vib", name="m", binding="dev1",
                                 kind=KIND_MODEL, path="vib/m1.bin")
        b = self.wb.add_artifact(domain="vib", name="b", binding="dev1",
                                 kind=KIND_BASELINE, path="vib/b1.json")
        self.wb.activate_artifact(m); self.wb.activate_artifact(b)
        got = self.cache.for_binding("vib", "dev1")
        self.assertEqual(set(got), {"model", "baseline"})

    def test_按id缓存不重读磁盘(self):
        aid = self.wb.add_artifact(domain="vib", name="m", binding="dev1", path="vib/m1.bin")
        self.wb.activate_artifact(aid)
        first = self.cache.for_binding("vib", "dev1")["model"]
        (self.dir / "vib" / "m1.bin").write_bytes(b"CHANGED-ON-DISK")
        second = self.cache.for_binding("vib", "dev1")["model"]
        # 工件是只增不改的：同一个 id 就该是同一份内容，不必也不该每拍重读。
        self.assertIs(first, second)
        self.assertEqual(second.blob, b"MODEL-1")

    def test_换了启用件下一拍就换过来(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1", path="vib/m1.bin")
        b = self.wb.add_artifact(domain="vib", name="m2", binding="dev1", path="vib/b1.json")
        self.wb.activate_artifact(a)
        self.assertEqual(self.cache.for_binding("vib", "dev1")["model"].name, "m1")
        self.wb.activate_artifact(b)
        self.assertEqual(self.cache.for_binding("vib", "dev1")["model"].name, "m2")

    def test_文件没了按无可用模型处置而不是抛(self):
        aid = self.wb.add_artifact(domain="vib", name="m", binding="dev1",
                                   path="vib/不存在.bin")
        self.wb.activate_artifact(aid)
        with self.assertLogs("aiintegration.artifactcache", level="WARNING") as logs:
            got = self.cache.for_binding("vib", "dev1")
        self.assertEqual(got, {})
        self.assertIn("读不到", "\n".join(logs.output))

    def test_文件缺失每次都吵不做只记一次(self):
        """★记录说有启用件、磁盘上却没有 = 两边对不上，比"没有模型"更需要有人看。"""
        aid = self.wb.add_artifact(domain="vib", name="m", binding="dev1", path="vib/无.bin")
        self.wb.activate_artifact(aid)
        for _ in range(2):
            with self.assertLogs("aiintegration.artifactcache", level="WARNING"):
                self.cache.for_binding("vib", "dev1")

    def test_空文件也按无可用处置(self):
        (self.dir / "vib" / "empty.bin").write_bytes(b"")
        aid = self.wb.add_artifact(domain="vib", name="m", binding="dev1",
                                   path="vib/empty.bin")
        self.wb.activate_artifact(aid)
        with self.assertLogs("aiintegration.artifactcache", level="WARNING"):
            self.assertEqual(self.cache.for_binding("vib", "dev1"), {})

    def test_对象没有就退回全域通用件(self):
        g = self.wb.add_artifact(domain="vib", name="全域", binding="", path="vib/m1.bin")
        self.wb.activate_artifact(g)
        got = self.cache.for_binding("vib", "dev9")
        self.assertEqual(got["model"].name, "全域")

    def test_对象自己的优先于全域件(self):
        g = self.wb.add_artifact(domain="vib", name="全域", binding="", path="vib/m1.bin")
        own = self.wb.add_artifact(domain="vib", name="专用", binding="dev1",
                                   path="vib/b1.json")
        self.wb.activate_artifact(g); self.wb.activate_artifact(own)
        self.assertEqual(self.cache.for_binding("vib", "dev1")["model"].name, "专用")

    def test_meta坏了不影响工件可用(self):
        aid = self.wb.add_artifact(domain="vib", name="m", binding="dev1",
                                   path="vib/m1.bin", meta_json="{不是 json")
        self.wb.activate_artifact(aid)
        with self.assertLogs("aiintegration.artifactcache", level="WARNING"):
            got = self.cache.for_binding("vib", "dev1")
        self.assertEqual(got["model"].blob, b"MODEL-1")
        self.assertEqual(got["model"].meta, {})


class FakeFetcher:
    """给固定值的取数。★用它是为了让闭环里"数据"这一段可控，别的都是真件。"""

    def __init__(self, values):
        self.values = values      # role -> 值

    def fetch(self, b, end_time, artifacts=None):
        ch = {}
        for role in b.roles:
            v = self.values.get(role)
            if v is None:
                ch[role] = []
            else:
                ch[role] = [Sample(t=end_time, value=v, quality=Quality.OK, status_code=1)]
        return Frame(domain=b.domain, binding=b.binding,
                     t_start=end_time - timedelta(seconds=b.window_sec), t_end=end_time,
                     channels=ch, params=dict(b.params), artifacts=dict(artifacts or {}))


class TestBaselineLoop(unittest.TestCase):
    """★真闭环：标注 → 训练集 → 采基线 → 启用 → 推理真的用上。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.wb = Workbench(root / "wb.db")
        self.bindings = BindingStore(root / "b.db")
        loaded, failed = discover(DOMAINS_DIR)
        mine = [(p, e) for p, e in failed if p.name == "vibration_baseline.py"]
        assert not mine, mine
        self.domains = {d.key: d for d in loaded}
        self.dom = self.domains["vibration_baseline"]
        self.bindings.put(Binding("vibration_baseline", "dev1",
                                  {"x_vel": 101, "z_vel": 103, "temp": 100},
                                  params=dict(FULL_PARAMS)))
        self.fetcher = FakeFetcher({"x_vel": 1.0, "z_vel": 0.5, "temp": 40.0})
        self.artifacts_dir = root / "artifacts"
        self.tr = Trainer(workbench=self.wb, bindings=self.bindings,
                          domains=self.domains, fetcher=self.fetcher,
                          artifacts_dir=self.artifacts_dir)
        self.cache = ActiveArtifacts(self.wb, self.artifacts_dir)

    def tearDown(self):
        self.tr.stop(timeout=5)
        self.bindings.close(); self.wb.close(); self._tmp.cleanup()

    def _train(self, n=6, label="正常", name="正常段"):
        ds = self.wb.put_dataset(domain="vibration_baseline", name=name)
        anns = [self.wb.put_annotation(
            domain="vibration_baseline", binding="dev1", label=label,
            t_from=T0 + timedelta(hours=i), t_to=T0 + timedelta(hours=i, minutes=5))
            for i in range(n)]
        self.wb.add_samples(ds, anns)
        jid = self.tr.submit(domain="vibration_baseline", dataset_id=ds,
                             binding="dev1", algo="基线")
        self.tr.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.wb.get_job(jid)
            if job.status in ("ready", "failed", "canceled"):
                return job
            time.sleep(0.05)
        self.fail("训练超时")

    def test_采基线到用上的整条路(self):
        job = self._train()
        self.assertEqual(job.status, "ready", job.message)

        # ① 存成的是 baseline，不是 model —— 两者各占各的激活位
        art = self.wb.list_artifacts(domain="vibration_baseline").items[0]
        self.assertEqual(art.kind, "baseline")
        self.assertIsNone(art.accuracy, "基线没有准确率这回事")
        self.assertIn("/baseline/", art.path)

        # ② 落盘的确实是能解析的基线
        model = json.loads((self.artifacts_dir / art.path).read_bytes())
        self.assertEqual(model["frames"], 6)
        self.assertAlmostEqual(model["channels"]["x_vel"]["median"], 1.0, places=6)

        # ③ 没启用之前，推理拿不到它 ⇒ 那四条落 MODEL_NOT_LOADED
        b = self.bindings.get("vibration_baseline", "dev1")
        frame = self.fetcher.fetch(b, T0 + timedelta(days=1),
                                   artifacts=self.cache.for_binding("vibration_baseline", "dev1"))
        out = {f.key: f for f in self.dom.instance.infer(frame)}
        self.assertIs(out["anomaly_score"].quality, Quality.MODEL_NOT_LOADED)

        # ④ 启用之后，下一拍就用上了
        self.wb.activate_artifact(art.id)
        frame2 = self.fetcher.fetch(b, T0 + timedelta(days=1),
                                    artifacts=self.cache.for_binding("vibration_baseline", "dev1"))
        self.assertIn("baseline", frame2.artifacts)
        out2 = {f.key: f for f in self.dom.instance.infer(frame2)}
        self.assertIs(out2["anomaly_score"].quality, Quality.OK)
        self.assertIs(out2["vel_z_max"].quality, Quality.OK)
        self.assertIn("基线采自", out2["evidence"].value)

        # ⑤ 值抬上去，z 分数与异常分跟着上去（基线真的在起作用）
        self.fetcher.values["x_vel"] = 3.0
        frame3 = self.fetcher.fetch(b, T0 + timedelta(days=1),
                                    artifacts=self.cache.for_binding("vibration_baseline", "dev1"))
        out3 = {f.key: f for f in self.dom.instance.infer(frame3)}
        self.assertGreater(out3["vel_z_max"].value, out2["vel_z_max"].value)
        self.assertGreater(out3["anomaly_score"].value, out2["anomaly_score"].value)

    def test_训练路径不给工件免得模型喂自己(self):
        """★训练时拿旧模型当输入，漂了也看不出来。"""
        job = self._train()
        self.assertEqual(job.status, "ready", job.message)
        art = self.wb.list_artifacts(domain="vibration_baseline").items[0]
        self.wb.activate_artifact(art.id)
        # 再训一次：执行器组装数据集时**不带**工件
        seen = {}

        real_fetch = self.fetcher.fetch

        def spy(b, end_time, artifacts=None):
            seen["artifacts"] = artifacts
            return real_fetch(b, end_time, artifacts)

        self.fetcher.fetch = spy
        self._train(n=6, name="正常段-第二次")
        self.assertIn("artifacts", seen)
        self.assertFalse(seen["artifacts"], "训练路径不该带工件")

    def test_正常样本不够时任务失败且原因说清(self):
        job = self._train(n=3)
        self.assertEqual(job.status, "failed")
        self.assertIn("至少", job.message)


if __name__ == "__main__":
    unittest.main()
