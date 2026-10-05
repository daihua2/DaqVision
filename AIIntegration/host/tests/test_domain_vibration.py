"""低频采集AI振动诊断（`vibration`）—— 合并带来的东西（2026-10-05 用户定把模块 1、2 合成一个）。

两部分各自的判据在 `test_domain_vibration_classic.py` / `test_domain_vibration_selftrained.py`。这里钉：
1. **启用算法**必填、无缺省；没填整组落 `CONFIG_INCOMPLETE`，不猜；
2. **检测状态怎么合**：高于正常就照报，另一部分算不出压不下去；全是正常却有启用部分算不出 ⇒ 不报正常；
3. **两部分不一致时**判据摘要写明怎么读；
4. ★**采基线先用国标查**：所选时段有 C / D 区的帧就拒采 —— 否则把异常当常态；
5. **按绑定算要不要基线**（契约 1.14 `Binding.requires_artifacts`）。
"""

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.api import ApiService
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import Domain, LoadedDomain, discover
from aiintegration.logstore import LogStore
from aiintegration.quality import Quality
from aiintegration.types import (
    ArtifactBlob, Dataset, Declaration, Frame, LabeledFrame, OutputSpec, ProgressSink, Sample,
)

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
T0 = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)

#: 第 2 组刚性 ⇒ A/B 1.4、B/C 2.8、C/D 4.5。
BOTH = {"algorithms": "both", "machineGroup": "group2", "supportClass": "rigid",
        "vel_is_rms": "true", "axialAxis": "z", "ratedSpeedRpm": "1480"}


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration.py"]
    assert not mine, f"低频采集AI振动诊断装载失败：{mine}"
    return {d.key: d for d in loaded}["vibration"]


def _frame(params, **vals):
    return Frame(domain="vibration", binding="dev1",
                 t_start=T0 - timedelta(seconds=60), t_end=T0,
                 channels={k: [Sample(t=T0, value=v, quality=Quality.OK)] for k, v in vals.items()},
                 params=dict(params))


def _baseline(d, params, values):
    items = tuple(LabeledFrame(frame=_frame(params, x_vel=v), label="正常", sample_id=i + 1)
                  for i, v in enumerate(values))
    art = d.train(Dataset(domain="vibration", binding="dev1", name="基线", items=items), ProgressSink())
    return ArtifactBlob(id=1, kind=art.kind, name="基线", blob=art.blob, meta=dict(art.meta))


def _by_key(findings):
    return {f.key: f for f in findings}


class TestDeclaration(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_名字与能力(self):
        self.assertEqual(self.d.instance.display, "低频采集AI振动诊断")
        self.assertEqual(self.d.caps, frozenset({"infer", "train"}))
        self.assertEqual(self.d.declaration.requires_artifacts, ("baseline",), "可能要的全集")

    def test_一个传感器三轴加一路温度(self):
        got = {i.role: (i.quantity, i.axis, i.group) for i in self.d.declaration.inputs}
        self.assertEqual(got, {"x_vel": ("velocity", "x", ""), "y_vel": ("velocity", "y", ""),
                               "z_vel": ("velocity", "z", ""), "temp": ("temperature", "", "")})

    def test_启用算法必填无缺省(self):
        p = {x.key: x for x in self.d.declaration.params}["algorithms"]
        self.assertTrue(p.required)
        self.assertEqual((p.default, p.has_default), ("", False))
        self.assertEqual(p.choices, ("classic", "baseline", "both"))

    def test_一条检测状态五档都可能出_主要结论三项(self):
        outs = self.d.declaration.outputs
        status = [o for o in outs if o.role == "status"]
        self.assertEqual([o.key for o in status], ["status"])
        self.assertEqual(status[0].choices, ("normal", "attention", "warning", "danger", "stopped"))
        self.assertEqual([o.key for o in outs if o.headline], ["vel_max", "iso_zone", "anomaly_score"])


class TestRequiredArtifacts(unittest.TestCase):
    def test_按启用算法算(self):
        d = _load()
        for algos, want in (("classic", ()), ("baseline", ("baseline",)), ("both", ("baseline",)),
                            ("", ("baseline",)), ("bogus", ("baseline",))):
            self.assertEqual(d.required_artifacts({"algorithms": algos}), want, algos)

    def test_ListBindings按绑定带出(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BindingStore(Path(tmp) / "b.db")
            try:
                svc = ApiService(guid="g", version="0", logstore=LogStore(capacity=10),
                                 domains={"vibration": _load()}, bindings=store)
                for key, algos in (("a", "classic"), ("b", "both")):
                    store.put(Binding("vibration", key, {"x_vel": 810}, params={"algorithms": algos}))
                got = {b.binding: list(b.requires_artifacts)
                       for b in svc.ListBindings(pb.ListBindingsRequest(), None).bindings}
                self.assertEqual(got, {"a": [], "b": ["baseline"]})
            finally:
                store.close()

    def test_域按绑定回的超出声明就退回声明(self):
        class D(Domain):
            key, display, version = "d", "d", "1"

            def declare(self):
                return Declaration(inputs=(), outputs=(OutputSpec(key="o", display="o", value_type="float"),),
                                   requires_artifacts=("baseline",))

            def infer(self, frame):
                return []

            def required_artifacts(self, params):
                return ("model",)

        dom = D()
        loaded = LoadedDomain(dom, dom.declare(), frozenset({"infer"}), Path("d.py"))
        self.assertEqual(loaded.required_artifacts({}), ("baseline",))


class TestAlgorithmsParam(unittest.TestCase):
    def test_没填或非法整组落配置不全(self):
        d = _load()
        for raw in ("", "iso"):
            out = _by_key(d.instance.infer(_frame({**BOTH, "algorithms": raw}, x_vel=3.0)))
            self.assertEqual(set(out), {o.key for o in d.declaration.outputs}, raw)
            for k, f in out.items():
                self.assertIs(f.quality, Quality.CONFIG_INCOMPLETE, (raw, k))
            self.assertIn("启用算法", out["evidence"].value)

    def test_只启用经典时不写自训那一半(self):
        d = _load()
        out = _by_key(d.instance.infer(_frame({**BOTH, "algorithms": "classic"}, x_vel=3.0)))
        self.assertFalse(set(out) & {"vel_z_max", "ratio_drift", "temp_rise", "anomaly_score"})
        self.assertEqual(out["status"].value, "warning")
        self.assertIn("AI 自训未启用", out["evidence"].value)

    def test_只启用经典时不采基线(self):
        d = _load().instance
        with self.assertRaises(ValueError) as cm:
            _baseline(d, {**BOTH, "algorithms": "classic"}, (1.0, 1.0, 1.0, 1.0, 1.0))
        self.assertIn("不需要也不采基线", str(cm.exception))


class TestCombinedStatus(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance
        # 基线中位数 1.0（A 区），尺度 0.1/1.349 ⇒ 1.4 以上就偏离 ≥ 3。
        self.art = _baseline(self.d, BOTH, (0.9, 1.0, 1.1, 1.05, 0.95))

    def _out(self, vel, params=BOTH, artifact=True):
        f = _frame(params, x_vel=vel)
        if artifact:
            f = dataclasses.replace(f, artifacts={"baseline": self.art})
        return _by_key(self.d.infer(f))

    def test_两者都正常(self):
        s = self._out(1.0)["status"]
        self.assertEqual((s.value, s.quality), ("normal", Quality.OK))

    def test_国标正常_偏离自身_报注意并说明早期变化(self):
        out = self._out(1.35)                         # A 区，但偏离 ≫ 3
        self.assertEqual(out["iso_zone"].value, "A")
        self.assertEqual(out["status"].value, "attention")
        self.assertIn("早期变化", out["evidence"].value)

    def test_两者都高_取较高档(self):
        self.assertEqual(self._out(5.0)["status"].value, "danger")     # D 区 + 注意

    def test_没基线时国标警告照报_不被压下去(self):
        s = self._out(3.0, artifact=False)["status"]
        self.assertEqual((s.value, s.quality), ("warning", Quality.OK))

    def test_没基线时国标正常_不报正常(self):
        s = self._out(1.0, artifact=False)["status"]
        self.assertIs(s.quality, Quality.MODEL_NOT_LOADED)
        self.assertIsNone(s.value)

    def test_国标配置不全时自训注意照报(self):
        s = self._out(1.35, params={**BOTH, "vel_is_rms": "false"})["status"]
        self.assertEqual((s.value, s.quality), ("attention", Quality.OK))

    def test_国标配置不全而自训正常_落经典的码(self):
        s = self._out(1.0, params={**BOTH, "vel_is_rms": "false"})["status"]
        self.assertIs(s.quality, Quality.CONFIG_INCOMPLETE)
        self.assertIsNone(s.value)

    def test_停机写停机(self):
        out = self._out(0.1, params={**BOTH, "stop_threshold": "0.3"})
        self.assertEqual(out["status"].value, "stopped")


class TestDisagreement(unittest.TestCase):
    def test_国标偏大而相对自身没变_说明长期偏高或参数需核对(self):
        """★基线采在偏大的时段（判级参数当时不全，没做国标核查），之后国标说危险、自训说没变。"""
        d = _load().instance
        art = _baseline(d, {**BOTH, "vel_is_rms": "false"}, (4.9, 5.0, 5.1, 5.05, 4.95))
        out = _by_key(d.infer(dataclasses.replace(_frame(BOTH, x_vel=5.0), artifacts={"baseline": art})))
        self.assertEqual(out["iso_zone"].value, "D")
        self.assertLess(out["vel_z_max"].value, 3.0)
        self.assertEqual(out["status"].value, "danger", "自训的「没变」不能把国标的危险压下去")
        self.assertIn("长期偏高", out["evidence"].value)
        self.assertIn("判级参数需核对", out["evidence"].value)


class TestBaselineIsoCheck(unittest.TestCase):
    """★采基线先用国标查（§0.4）：用偏大的那段作基线，之后再大也显得「没变」。"""

    def setUp(self):
        self.d = _load().instance

    def test_所选时段有C区帧就拒采并说清(self):
        with self.assertRaises(ValueError) as cm:
            _baseline(self.d, BOTH, (1.0, 1.0, 3.0, 1.0, 1.0))       # 一帧落 C 区
        msg = str(cm.exception)
        self.assertIn("C / D 区", msg)
        self.assertIn("把异常当常态", msg)
        self.assertIn("3.000", msg)

    def test_都在AB区可以采并记下核查结论(self):
        art = _baseline(self.d, BOTH, (1.0, 1.2, 2.0, 1.1, 0.9))     # 2.0 在 B 区
        self.assertIn("已按 GB/T 6075.3-2011", art.meta["iso_check"])

    def test_判级参数不全时不查但写明(self):
        art = _baseline(self.d, {**BOTH, "machineGroup": ""}, (5.0, 5.0, 5.0, 5.0, 5.0))
        self.assertIn("未做国标核查", art.meta["iso_check"])


if __name__ == "__main__":
    unittest.main()
