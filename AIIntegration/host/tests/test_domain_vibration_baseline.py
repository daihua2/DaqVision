"""AI 模型自训振动诊断（vibration_baseline）的回归。

钉的东西按重要性排：
1. **基线用中位数与四分位距**：少量离群值不能把尺度撑大；
2. **只用正常样本**、**少于 5 帧不出基线**；
3. **没有基线就说没有基线**：整组 `MODEL_NOT_LOADED`；
4. 温升、比例漂移按测点各算，第二测点参与；
5. 坏值不当成 0，没选的通道不硬比。
"""

import dataclasses
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.types import ArtifactBlob, Dataset, Frame, LabeledFrame, ProgressSink, Sample

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
T0 = datetime(2026, 9, 17, 8, 0, 0, tzinfo=timezone.utc)
PARAMS = {"axial_axis": "z"}


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration_baseline.py"]
    assert not mine, f"AI 模型自训振动诊断装载失败：{mine}"
    by_key = {d.key: d for d in loaded}
    assert "vibration_baseline" in by_key, f"没装上，只装到 {sorted(by_key)}"
    return by_key["vibration_baseline"]


def _samples(*values):
    out = []
    for i, v in enumerate(values):
        q = Quality.OK
        if isinstance(v, tuple):
            v, q = v
        out.append(Sample(t=T0 + timedelta(seconds=i), value=v, quality=q,
                          status_code=1 if q is Quality.OK else -1000))
    return out


def _frame(channels, params=None):
    return Frame(domain="vibration_baseline", binding="dev1",
                 t_start=T0 - timedelta(seconds=1), t_end=T0 + timedelta(seconds=60),
                 channels=channels, params=dict(PARAMS if params is None else params))


def _ch(**vals):
    return {k: _samples(v) for k, v in vals.items() if v is not None}


def _by_key(findings):
    return {f.key: f for f in findings}


def _dataset(rows, label="正常", params=None):
    items = tuple(LabeledFrame(frame=_frame(_ch(**r), params), label=label, sample_id=i + 1)
                  for i, r in enumerate(rows))
    return Dataset(domain="vibration_baseline", binding="dev1", name="基线集", items=items)


def _artifact(trained):
    return ArtifactBlob(id=1, kind=trained.kind, name="基线", blob=trained.blob,
                        algo=trained.algo, accuracy=trained.accuracy,
                        meta=dict(trained.meta), created_at="2026-09-17")


class TestDeclaration(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_能力位含推理与训练(self):
        self.assertEqual(self.d.caps, frozenset({"infer", "train"}))

    def test_参数归属与缺省(self):
        specs = {p.key: p for p in self.d.declaration.params}
        self.assertEqual(set(specs), {"axial_axis", "normal_label"})
        self.assertEqual(specs["axial_axis"].level, "position")
        self.assertTrue(specs["axial_axis"].required)
        self.assertEqual(specs["axial_axis"].default, "")
        self.assertEqual(specs["normal_label"].default, "正常")

    def test_每个输入项都有显示名(self):
        for i in self.d.declaration.inputs:
            self.assertTrue(i.display, i.role)

    def test_不出ISO类结论(self):
        keys = {o.key for o in self.d.declaration.outputs}
        self.assertFalse(keys & {"iso_zone", "vel_max", "direction_hint"},
                         "经典判据已拆到 vibration_iso，本模块不重复出")


class TestBaselineTraining(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance

    def test_采出来的是基线且没有准确率(self):
        art = self.d.train(_dataset([{"x_vel": v} for v in (1.0, 1.1, 0.9, 1.05, 0.95)]), ProgressSink())
        self.assertEqual(art.kind, "baseline")
        self.assertIsNone(art.accuracy)
        model = json.loads(art.blob)
        self.assertEqual(model["format"], "vibration_baseline/baseline@1")
        self.assertIn("median", model["channels"]["x_vel"])
        self.assertIn("iqr", model["channels"]["x_vel"])

    def test_中位数与四分位距按线性插值(self):
        art = self.d.train(_dataset([{"x_vel": v} for v in (1.0, 2.0, 3.0, 4.0, 5.0)]), ProgressSink())
        c = json.loads(art.blob)["channels"]["x_vel"]
        self.assertAlmostEqual(c["median"], 3.0)
        self.assertAlmostEqual(c["iqr"], 4.0 - 2.0)

    def test_只用标成正常的样本(self):
        rows = [{"x_vel": 1.0}] * 5
        ds = _dataset(rows)
        bad = tuple(LabeledFrame(frame=_frame(_ch(x_vel=50.0)), label="不平衡", sample_id=100 + i)
                    for i in range(3))
        ds = dataclasses.replace(ds, items=ds.items + bad)
        model = json.loads(self.d.train(ds, ProgressSink()).blob)
        self.assertAlmostEqual(model["channels"]["x_vel"]["median"], 1.0)
        self.assertEqual(model["frames"], 5)

    def test_少于5帧当场失败并说清(self):
        with self.assertRaises(ValueError) as c:
            self.d.train(_dataset([{"x_vel": 1.0}] * 4), ProgressSink())
        self.assertIn("基线样本不足", str(c.exception))

    def test_正好5帧可以采(self):
        self.d.train(_dataset([{"x_vel": 1.0}] * 5), ProgressSink())

    def test_可改哪个标签算正常(self):
        art = self.d.train(_dataset([{"x_vel": 1.0}] * 5, label="良好",
                                    params={**PARAMS, "normal_label": "良好"}), ProgressSink())
        self.assertEqual(json.loads(art.blob)["label"], "良好")

    def test_一路速度都没有时失败(self):
        with self.assertRaises(ValueError) as c:
            self.d.train(_dataset([{"temp": 40.0}] * 5), ProgressSink())
        self.assertIn("一路速度都没能", str(c.exception))

    def test_两个测点的三轴比例都进基线(self):
        rows = [{"x_vel": 1.0, "z_vel": 0.5, "x2_vel": 2.0, "z2_vel": 2.0}] * 5
        model = json.loads(self.d.train(_dataset(rows), ProgressSink()).blob)
        self.assertAlmostEqual(model["axial_ratios"]["1"], 0.5)
        self.assertAlmostEqual(model["axial_ratios"]["2"], 1.0)


class TestDeviation(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance
        rows = [{"x_vel": x, "z_vel": z, "temp": t} for x, z, t in (
            (1.0, 0.5, 40.0), (1.1, 0.52, 40.2), (0.9, 0.48, 39.8),
            (1.05, 0.51, 40.1), (0.95, 0.49, 39.9), (1.0, 0.5, 40.0))]
        self.art = _artifact(self.d.train(_dataset(rows), ProgressSink()))

    def _infer(self, artifact=True, **vals):
        f = _frame(_ch(**vals))
        if artifact:
            f = dataclasses.replace(f, artifacts={"baseline": self.art})
        return _by_key(self.d.infer(f))

    def test_没有基线时整组落MODEL_NOT_LOADED(self):
        out = self._infer(artifact=False, x_vel=3.0)
        for k in ("vel_z_max", "ratio_drift", "temp_rise", "anomaly_score"):
            self.assertIs(out[k].quality, Quality.MODEL_NOT_LOADED, k)
            self.assertIsNone(out[k].value)
        self.assertIn("无可用基线", out["evidence"].value)

    def test_正常波动时偏离小(self):
        out = self._infer(x_vel=1.02, z_vel=0.5, temp=40.0)
        self.assertLess(abs(out["vel_z_max"].value), 3.0)
        self.assertLess(out["anomaly_score"].value, 30)

    def test_明显升高时偏离大异常分上去(self):
        out = self._infer(x_vel=3.0, z_vel=0.5, temp=40.0)
        self.assertGreater(out["vel_z_max"].value, 3.0)
        self.assertGreater(out["anomaly_score"].value, 30)

    def test_温升算得出(self):
        self.assertAlmostEqual(self._infer(x_vel=1.0, z_vel=0.5, temp=48.0)["temp_rise"].value, 8.0, places=1)

    def test_没选温度不给温升(self):
        out = self._infer(x_vel=1.0, z_vel=0.5)
        self.assertIs(out["temp_rise"].quality, Quality.NO_INPUT)

    def test_三轴比例漂移算得出(self):
        out = self._infer(x_vel=1.0, z_vel=1.0, temp=40.0)
        self.assertAlmostEqual(out["ratio_drift"].value, 0.5, places=2)
        self.assertIn("不对中", out["evidence"].value)

    def test_缺轴向只影响比例漂移(self):
        f = dataclasses.replace(_frame(_ch(x_vel=1.0, z_vel=0.5), params={}),
                                artifacts={"baseline": self.art})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["ratio_drift"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIs(out["vel_z_max"].quality, Quality.OK)

    def test_基线工件读不懂时落坏码(self):
        bad = ArtifactBlob(id=9, kind="baseline", name="坏", blob=b"{not json")
        f = dataclasses.replace(_frame(_ch(x_vel=2.0)), artifacts={"baseline": bad})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["anomaly_score"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIn("读不懂", out["evidence"].value)

    def test_格式不认的基线被拒(self):
        old = ArtifactBlob(id=9, kind="baseline", name="旧格式", blob=json.dumps(
            {"format": "vibration_lowfreq/baseline@1", "channels": {"x_vel": {"mean": 1, "std": 0.1}}}
        ).encode("utf-8"))
        f = dataclasses.replace(_frame(_ch(x_vel=2.0)), artifacts={"baseline": old})
        self.assertIs(_by_key(self.d.infer(f))["vel_z_max"].quality, Quality.MODEL_NOT_LOADED,
                      "拆分前的老基线格式不同，必须拒读而不是误读")

    def test_基线没采过的通道不参与比较(self):
        out = self._infer(x_vel=1.0, y_vel=99.0, z_vel=0.5)
        self.assertLess(out["vel_z_max"].value, 3.0)

    def test_一条都算不出来时每个输出都有锚点(self):
        declared = {o.key for o in self.d.declare().outputs}
        out = _by_key(self.d.infer(_frame({"x_vel": []})))
        self.assertEqual(set(out), declared)


class TestRobustness(unittest.TestCase):
    """★定案 2.1 的理由本身：基线里混进一个离群值，尺度不能被撑大。"""

    def test_一个离群值不会让真实偏离变得不显著(self):
        d = _load().instance
        rows = [{"x_vel": v} for v in (1.0, 1.02, 0.98, 1.01, 0.99, 5.0)]   # 最后一帧是离群值
        art = _artifact(d.train(_dataset(rows), ProgressSink()))
        f = dataclasses.replace(_frame(_ch(x_vel=1.5)), artifacts={"baseline": art})
        z = _by_key(d.infer(f))["vel_z_max"].value
        # 用均值/标准差：μ≈1.67、σ≈1.63 ⇒ z≈-0.1，1.5 会被判成"比平时还低"。
        self.assertGreater(z, 3.0, f"1.5 相对正常的 1.0 是明显偏高，z={z}")


class TestSecondPoint(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance
        rows = [{"x_vel": 1.0, "z_vel": 0.5, "temp": 40.0,
                 "x2_vel": v, "z2_vel": 0.5 * v, "temp2": 50.0}
                for v in (2.0, 2.1, 1.9, 2.05, 1.95)]
        self.art = _artifact(self.d.train(_dataset(rows), ProgressSink()))

    def _infer(self, **vals):
        f = dataclasses.replace(_frame(_ch(**vals)), artifacts={"baseline": self.art})
        return _by_key(self.d.infer(f))

    def test_第二测点偏离参与速度偏离(self):
        out = self._infer(x_vel=1.0, z_vel=0.5, x2_vel=6.0, z2_vel=1.0)
        self.assertGreater(out["vel_z_max"].value, 3.0)
        self.assertIn("x2", out["evidence"].value)

    def test_温升取升得最多的测点(self):
        out = self._infer(x_vel=1.0, z_vel=0.5, temp=41.0, x2_vel=2.0, z2_vel=1.0, temp2=58.0)
        self.assertAlmostEqual(out["temp_rise"].value, 8.0, places=1)

    def test_比例漂移取变化最大的测点(self):
        out = self._infer(x_vel=1.0, z_vel=0.5, x2_vel=2.0, z2_vel=2.0)
        self.assertAlmostEqual(out["ratio_drift"].value, 0.5, places=2)


if __name__ == "__main__":
    unittest.main()
