"""低频采集AI振动诊断（`vibration`）AI 自训部分的回归（`algorithms=baseline`）。原 `vibration_baseline` 的用例，合并后照跑。

钉的东西按重要性排：
1. **基线用中位数与四分位距**：少量离群值不能把尺度撑大；
2. **只用正常样本**、**少于 5 帧不出基线**；
3. **没有基线就说没有基线**：整组 `MODEL_NOT_LOADED`；
4. ★**轴向改了不拿旧基线的比例去减**（2026-10-05 补的缺陷）；
5. 坏值不当成 0，没选的通道不硬比；一条绑定只管一个传感器（`C-64 §2.1`）。
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
PARAMS = {"algorithms": "baseline", "axialAxis": "z"}
#: 只启用自训时不写的那一半（与 `vibration.CLASSIC_KEYS` 同）。
CLASSIC_KEYS = ("vel_max", "dominant_axis", "iso_zone", "iso_zone_code", "iso_margin",
                "axial_ratio", "direction_hint")


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration.py"]
    assert not mine, f"AI 模型自训振动诊断装载失败：{mine}"
    by_key = {d.key: d for d in loaded}
    assert "vibration" in by_key, f"没装上，只装到 {sorted(by_key)}"
    return by_key["vibration"]


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
    return Frame(domain="vibration", binding="dev1",
                 t_start=T0 - timedelta(seconds=1), t_end=T0 + timedelta(seconds=60),
                 channels=channels, params=dict(PARAMS if params is None else params))


def _ch(**vals):
    return {k: _samples(v) for k, v in vals.items() if v is not None}


def _by_key(findings):
    return {f.key: f for f in findings}


def _dataset(rows, label="正常", params=None):
    items = tuple(LabeledFrame(frame=_frame(_ch(**r), params), label=label, sample_id=i + 1)
                  for i, r in enumerate(rows))
    return Dataset(domain="vibration", binding="dev1", name="基线集", items=items)


def _artifact(trained):
    return ArtifactBlob(id=1, kind=trained.kind, name="基线", blob=trained.blob,
                        algo=trained.algo, accuracy=trained.accuracy,
                        meta=dict(trained.meta), created_at="2026-09-17")


class TestDeclaration(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_自训用的参数归属与缺省(self):
        specs = {p.key: p for p in self.d.declaration.params}
        self.assertFalse(specs["stop_threshold"].required)
        self.assertEqual(specs["stop_threshold"].default, "")
        self.assertEqual(specs["axialAxis"].level, "sensor", "轴向由振动传感器给（C-64 §2.3）")
        self.assertTrue(specs["axialAxis"].required)
        self.assertEqual(specs["axialAxis"].default, "")
        self.assertEqual(specs["normal_label"].default, "正常")

    def test_只启用自训时不写经典那一半(self):
        art = _artifact(self.d.instance.train(_dataset([{"x_vel": 1.0}] * 5), ProgressSink()))
        f = dataclasses.replace(_frame(_ch(x_vel=1.0)), artifacts={"baseline": art})
        keys = {x.key for x in self.d.instance.infer(f)}
        self.assertFalse(keys & set(CLASSIC_KEYS))
        self.assertIn("经典算法未启用", _by_key(self.d.instance.infer(f))["evidence"].value)


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

    def test_三轴比例与采集时的轴向都进基线(self):
        rows = [{"x_vel": 1.0, "z_vel": 0.5}] * 5
        model = json.loads(self.d.train(_dataset(rows), ProgressSink()).blob)
        self.assertEqual(model["axial_ratios"], {"1": 0.5})
        self.assertEqual(model["axial_axis"], "z")


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

    def test_判据摘要只说基线多久以前采的不写绝对时刻(self):
        # ★铁律：后台存 UTC、前端按当地时间显示。判据摘要每拍进实时库、平台原样照显 ⇒ 不许夹绝对时刻
        #   （原先写的是截掉时区的 UTC「2026-09-17T08:00」，人会读错 8 小时）。
        later = T0 + timedelta(hours=3)
        ok = lambda v: [Sample(t=later, value=v, quality=Quality.OK, status_code=1)]
        f = Frame(domain="vibration", binding="dev1", t_start=later - timedelta(seconds=60), t_end=later,
                  channels={"x_vel": ok(1.0), "z_vel": ok(0.5)}, params=dict(PARAMS),
                  artifacts={"baseline": self.art})
        ev = _by_key(self.d.infer(f))["evidence"].value
        self.assertIn("基线采自约 3 小时前", ev)
        self.assertNotRegex(ev, r"\d{4}-\d{2}-\d{2}", "判据摘要里夹了绝对日期")

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
        f = dataclasses.replace(_frame(_ch(x_vel=1.0, z_vel=0.5), params={"algorithms": "baseline"}),
                                artifacts={"baseline": self.art})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["ratio_drift"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIs(out["vel_z_max"].quality, Quality.OK)

    def test_轴向改了不拿旧基线的比例去减(self):
        """★基线按 z 为轴向采；平台把轴向改成 x 后，x/z 与基线里的 z/x 是两把尺子。
        照减出来的漂移看着像个结论（这里会是 1.0/1.0−0.5=+0.5，摘要还会说「可能不对中」）。"""
        f = dataclasses.replace(_frame(_ch(x_vel=1.0, z_vel=1.0, temp=40.0), {**PARAMS, "axialAxis": "x"}),
                                artifacts={"baseline": self.art})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["ratio_drift"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIsNone(out["ratio_drift"].value)
        self.assertIn("请重采基线", out["evidence"].value)
        self.assertNotIn("不对中", out["evidence"].value)
        self.assertIs(out["vel_z_max"].quality, Quality.OK, "速度偏离与轴向无关，照出")
        self.assertIs(out["temp_rise"].quality, Quality.OK)

    def test_第二测点角色不再认(self):
        """`C-64 §2.1`：第二测点就是另一个传感器 = 另一条绑定。帧里多出的通道本模块不读。"""
        out = self._infer(x_vel=1.0, z_vel=0.5, x2_vel=99.0, temp2=99.0)
        self.assertLess(out["vel_z_max"].value, 3.0)
        self.assertIs(out["temp_rise"].quality, Quality.NO_INPUT)

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
        declared = {o.key for o in self.d.declare().outputs} - set(CLASSIC_KEYS)
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


class TestStatus(unittest.TestCase):
    """检测状态：速度偏离 ≥ 3 → 注意，否则正常；算不出就落码、**不报正常**；不防抖。"""

    def setUp(self):
        self.d = _load().instance
        rows = [{"x_vel": v} for v in (1.0, 2.0, 3.0, 4.0, 5.0)]     # 中位数 3、尺度 2/1.349
        self.art = _artifact(self.d.train(_dataset(rows), ProgressSink()))
        self.scale = 2.0 / 1.349

    def _out(self, artifact=True, **vals):
        f = _frame(_ch(**vals))
        if artifact:
            f = dataclasses.replace(f, artifacts={"baseline": self.art})
        return _by_key(self.d.infer(f))

    def test_偏离小为正常_偏离大为注意_3本身算注意(self):
        for z, want in ((0.0, "normal"), (2.99, "normal"), (3.0, "attention"), (9.0, "attention")):
            s = self._out(x_vel=3.0 + z * self.scale)["status"]
            self.assertIs(s.quality, Quality.OK, z)
            self.assertEqual(s.value, want, z)

    def test_偏低不算注意(self):
        """★z 远低于 −3 也是正常：振动比平时小不是异常征兆（停机由停机门槛管）。
        用离散度很小的基线，让 z 真的落到 −3 以下 —— 否则这条用例分不出「取绝对值」的错。"""
        tight = _artifact(self.d.train(_dataset([{"x_vel": v} for v in (1.0, 1.02, 0.98, 1.01, 0.99)]),
                                       ProgressSink()))
        out = _by_key(self.d.infer(dataclasses.replace(_frame(_ch(x_vel=0.5)),
                                                       artifacts={"baseline": tight})))
        self.assertLess(out["vel_z_max"].value, -3.0)
        self.assertEqual(out["status"].value, "normal")

    def test_没有基线不报正常(self):
        s = self._out(artifact=False, x_vel=9.0)["status"]
        self.assertIs(s.quality, Quality.MODEL_NOT_LOADED)
        self.assertIsNone(s.value)

    def test_速度偏离算不出时状态同码(self):
        s = self._out(y_vel=1.0)["status"]          # 基线只采了 x
        self.assertIs(s.quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIsNone(s.value)

    def test_走骨架校验结论齐全(self):
        from aiintegration.runner import run_domain
        loaded = _load()
        f = dataclasses.replace(_frame(_ch(x_vel=9.0)), artifacts={"baseline": self.art})
        res = run_domain(loaded, f)
        self.assertTrue(res.ok, res.error)
        self.assertEqual({x.key for x in res.findings}, {o.key for o in loaded.declaration.outputs} - set(CLASSIC_KEYS))


_RUN_ROWS = [{"x_vel": x, "z_vel": z} for x, z in (
    (1.0, 0.5), (1.1, 0.52), (0.9, 0.48), (1.05, 0.51), (0.95, 0.49))]
_STOP_ROWS = [{"x_vel": 0.05, "z_vel": 0.02}] * 3


class TestStopState(unittest.TestCase):
    """定案 2.5：停机时只写运行状态与摘要；采基线剔除停机帧。"""

    def setUp(self):
        self.d = _load().instance
        self.art = _artifact(self.d.train(_dataset(_RUN_ROWS), ProgressSink()))

    def _infer(self, thr, artifact=True, **vals):
        f = _frame(_ch(**vals), dict(PARAMS, stop_threshold=thr))
        if artifact:
            f = dataclasses.replace(f, artifacts={"baseline": self.art})
        return _by_key(self.d.infer(f))

    def test_停机时只写运行状态_检测状态与摘要(self):
        out = self._infer("0.3", x_vel=0.05, z_vel=0.02)
        self.assertEqual(set(out), {"run_state", "status", "evidence"})
        self.assertEqual(out["run_state"].value, "停机")
        self.assertEqual(out["status"].value, "stopped")
        self.assertIs(out["run_state"].quality, Quality.OK)
        self.assertIn("停机", out["evidence"].value)

    def test_没有基线时停机也照判停机(self):
        out = self._infer("0.3", artifact=False, x_vel=0.05)
        self.assertEqual(set(out), {"run_state", "status", "evidence"})
        self.assertEqual(out["status"].value, "stopped")

    def test_运行时照常出偏离并带运行状态(self):
        out = self._infer("0.3", x_vel=1.0, z_vel=0.5)
        self.assertEqual(out["run_state"].value, "运行")
        self.assertIs(out["vel_z_max"].quality, Quality.OK)

    def test_不填门槛不判(self):
        out = self._infer("", x_vel=0.05, z_vel=0.02)
        self.assertEqual(out["run_state"].value, "未判")
        self.assertEqual(set(out), {o.key for o in self.d.declare().outputs} - set(CLASSIC_KEYS))

    def test_采基线剔除停机帧(self):
        params = dict(PARAMS, stop_threshold="0.3")
        with_stop = self.d.train(_dataset(_RUN_ROWS + _STOP_ROWS, params=params), ProgressSink())
        model = json.loads(with_stop.blob)
        self.assertEqual(model["frames"], 5)
        self.assertEqual(model["stopped_excluded"], 3)
        self.assertEqual(with_stop.meta["stopped_excluded"], "3")
        # 剔除后与只用运行帧采的基线逐通道相同。
        self.assertEqual(model["channels"], json.loads(self.art.blob)["channels"])

    def test_不填门槛时停机帧照旧进基线(self):
        model = json.loads(self.d.train(_dataset(_RUN_ROWS + _STOP_ROWS), ProgressSink()).blob)
        self.assertEqual(model["frames"], 8)
        self.assertEqual(model["stopped_excluded"], 0)

    def test_剔除后不足5帧失败并说清剔了几帧(self):
        params = dict(PARAMS, stop_threshold="0.3")
        with self.assertRaises(ValueError) as cm:
            self.d.train(_dataset(_RUN_ROWS[:3] + _STOP_ROWS, params=params), ProgressSink())
        self.assertIn("3 帧判为停机已剔除", str(cm.exception))

    def test_门槛非法时不采基线(self):
        with self.assertRaises(ValueError) as cm:
            self.d.train(_dataset(_RUN_ROWS, params=dict(PARAMS, stop_threshold="abc")),
                         ProgressSink())
        self.assertIn("停机门槛", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
