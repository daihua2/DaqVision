"""低频振动域的回归 —— 第一个真算法域。

★用**真的装载器**（`discover`）从真的 `domains/` 目录装，不在用例里手搓一个类：
  这样这套用例同时钉住"丢一个 .py 就多一个域"这件事本身。装载失败会当场红。

钉的东西按重要性排：

1. **不猜缺省** —— 台账缺一项，对应那几条结论落 `CONFIG_INCOMPLETE`，其余照出（不一坏全坏）；
2. **坏值不当成 0** —— 那是 v5 `_to_float` 的病，也是本域存在的理由之一；
3. **ISO 边界判在正确的一侧** —— 边界值本身归上一档（≤ 判 A），差一档就是差一个处置；
4. **T 取样本自己的时刻**，不是帧右端；
5. "没数据"与"有数据但全坏"**给不同的码**。
"""

import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.runner import run_domain
from aiintegration.types import Frame, Sample

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"

T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=timezone.utc)

FULL_PARAMS = {
    "iso_group": "2",
    "mount_type": "rigid",       # ⇒ 边界 1.4 / 2.8 / 4.5
    "vel_is_rms": "true",
    "axial_axis": "z",
}


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    # 只断言**本域**没装载失败：别的域（如视觉域）在零依赖环境里缺 cv2 是如实的，不该带红本用例。
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration_lowfreq.py"]
    assert not mine, f"低频振动域装载失败：{mine}"
    by_key = {d.key: d for d in loaded}
    assert "vibration_lowfreq" in by_key, f"没装上低频振动域，只装到 {sorted(by_key)}"
    return by_key["vibration_lowfreq"]


def _samples(*values, quality=Quality.OK, start=T0):
    """按秒排开的一串样本；`values` 里给 (值, 质量) 也行。"""
    out = []
    for i, v in enumerate(values):
        q = quality
        if isinstance(v, tuple):
            v, q = v
        out.append(Sample(t=start + timedelta(seconds=i), value=v, quality=q,
                          status_code=1 if q is Quality.OK else -1000))
    return out


def _frame(channels, params=None, *, t_end=None):
    end = t_end or (T0 + timedelta(seconds=60))
    return Frame(domain="vibration_lowfreq", binding="dev1",
                 t_start=T0 - timedelta(seconds=1), t_end=end,
                 channels=channels,
                 params=dict(FULL_PARAMS if params is None else params))


def _by_key(findings):
    return {f.key: f for f in findings}


#: 判据类台账 —— 猜错它们的后果**看不出来**（ISO 分级会把"该停机"说成"可长期运行"）。
#: ⇒ 这四项永远必填、永远没有缺省。加新参数时想给缺省，先问它属不属于这一类。
JUDGMENT_PARAMS = {"iso_group", "mount_type", "vel_is_rms", "axial_axis"}


class TestLoads(unittest.TestCase):
    def test_从真目录装得上且能力位含infer与train(self):
        d = _load()
        # ②层起本域会采基线，走的是训练面 ⇒ train 位自动出现（前端据此出训练页）。
        self.assertEqual(d.caps, frozenset({"infer", "train"}))

    def test_判据类台账永远必填且没有缺省(self):
        d = _load()
        specs = {p.key: p for p in d.declaration.params}
        self.assertTrue(JUDGMENT_PARAMS <= set(specs), f"少了判据参数：{specs.keys()}")
        for key in JUDGMENT_PARAMS:
            self.assertTrue(specs[key].required, f"{key} 应为必填")
            self.assertEqual(specs[key].default, "",
                             f"{key} 不该有缺省 —— 猜错它的后果看不出来")

    def test_normal_label允许有缺省且理由写在描述里(self):
        """★唯一一个有缺省的台账。它与那四项的区别是**猜错会当场可见**。"""
        spec = {p.key: p for p in _load().declaration.params}["normal_label"]
        self.assertFalse(spec.required)
        self.assertEqual(spec.default, "正常")
        self.assertIn("当场可见", spec.description)


class TestIsoClassification(unittest.TestCase):
    """ISO 边界要判在正确的一侧：2 组刚性 ⇒ 1.4 / 2.8 / 4.5。"""

    def setUp(self):
        self.d = _load()

    def _zone(self, vel):
        f = _frame({"x_vel": _samples(vel)})
        out = _by_key(self.d.instance.infer(f))
        return out["iso_zone"].value, out["iso_zone_code"].value, out["iso_margin"].value

    def test_边界值本身归上一档(self):
        # ★"≤ 边界" 归好的那一档。判反了就是把"可长期运行"说成"不宜长期运行"（或反过来）。
        self.assertEqual(self._zone(1.4)[0], "A")
        self.assertEqual(self._zone(1.4001)[0], "B")
        self.assertEqual(self._zone(2.8)[0], "B")
        self.assertEqual(self._zone(2.8001)[0], "C")
        self.assertEqual(self._zone(4.5)[0], "C")
        self.assertEqual(self._zone(4.5001)[0], "D")

    def test_区码与区名同步(self):
        for vel, zone, code in ((1.0, "A", 1), (2.0, "B", 2), (3.0, "C", 3), (9.0, "D", 4)):
            z, c, _ = self._zone(vel)
            self.assertEqual((z, c), (zone, code), f"{vel} mm/s")

    def test_余量在D区为负且等于超出量(self):
        _z, _c, margin = self._zone(6.5)
        self.assertAlmostEqual(margin, 4.5 - 6.5, places=6)

    def test_柔性支承限值确实放宽(self):
        f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "mount_type": "flexible"})
        # 2 组柔性 ⇒ 2.3/4.5/7.1，3.0 落 B；同样的值在刚性下是 C。
        self.assertEqual(_by_key(f and self.d.instance.infer(f))["iso_zone"].value, "B")
        self.assertEqual(self._zone(3.0)[0], "C")


class TestParamsMissing(unittest.TestCase):
    """缺台账 ⇒ 对应结论落 CONFIG_INCOMPLETE，**其余照出**。"""

    def setUp(self):
        self.d = _load()

    def test_口径未确认为RMS就不给ISO分级(self):
        for bad in ("false", "", "TRUE-ish"):
            f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "vel_is_rms": bad})
            out = _by_key(self.d.instance.infer(f))
            for k in ("iso_zone", "iso_zone_code", "iso_margin"):
                self.assertIs(out[k].quality, Quality.CONFIG_INCOMPLETE, f"{k} @ {bad!r}")
                self.assertIsNone(out[k].value, "坏质量下不许有值")
            # 数值本身照给 —— 它不依赖口径。
            self.assertIs(out["vel_max"].quality, Quality.OK)
            self.assertAlmostEqual(out["vel_max"].value, 3.0)

    def test_机组类别非法也不给分级(self):
        f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "iso_group": "7"})
        out = _by_key(self.d.instance.infer(f))
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)

    def test_缺轴向只影响方向性两条ISO照出(self):
        f = _frame({"x_vel": _samples(3.0), "z_vel": _samples(0.5)},
                   {k: v for k, v in FULL_PARAMS.items() if k != "axial_axis"})
        out = _by_key(self.d.instance.infer(f))
        self.assertIs(out["axial_ratio"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIs(out["direction_hint"].quality, Quality.CONFIG_INCOMPLETE)
        # ★这条是"不一坏全坏"的钉子。
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertEqual(out["iso_zone"].value, "C")

    def test_证据里说清了为什么没给(self):
        f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "vel_is_rms": "false"})
        ev = _by_key(self.d.instance.infer(f))["evidence"]
        self.assertIs(ev.quality, Quality.OK)
        self.assertIn("ISO 分级未给", ev.value)
        self.assertIn("RMS", ev.value)


class TestBadInput(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_坏值不参与判定而不是当成0(self):
        # ★若坏值被 float() 成 0 混进来，最大值仍是 5.0，看不出区别；
        #   所以这里让**坏值更大** —— 参与了就会把 9.9 判成 D 区。
        f = _frame({"x_vel": _samples(5.0, (9.9, Quality.INPUT_BAD))})
        out = _by_key(self.d.instance.infer(f))
        self.assertAlmostEqual(out["vel_max"].value, 5.0)
        self.assertEqual(out["iso_zone"].value, "D")   # 5.0 > 4.5，本就该 D
        f2 = _frame({"x_vel": _samples(2.0, (9.9, Quality.INPUT_BAD))})
        out2 = _by_key(self.d.instance.infer(f2))
        self.assertAlmostEqual(out2["vel_max"].value, 2.0)
        self.assertEqual(out2["iso_zone"].value, "B")

    def test_质量说好但值不是数按不可用处置(self):
        f = _frame({"x_vel": _samples("正常", 2.0)})
        out = _by_key(self.d.instance.infer(f))
        self.assertAlmostEqual(out["vel_max"].value, 2.0)

    def test_全是坏值与没有数据给不同的码(self):
        all_bad = _frame({"x_vel": _samples((1.0, Quality.INPUT_BAD))})
        out = _by_key(self.d.instance.infer(all_bad))
        self.assertIs(out["vel_max"].quality, Quality.INPUT_BAD)
        self.assertIn("质量码全不可信", out["evidence"].value)

        empty = _frame({"x_vel": []})
        out2 = _by_key(self.d.instance.infer(empty))
        self.assertIs(out2["vel_max"].quality, Quality.NO_INPUT)
        self.assertIn("没有样本", out2["evidence"].value)

    def test_一条都算不出来时每个输出都有锚点(self):
        d = _load()
        declared = {o.key for o in d.declaration.outputs}
        out = _by_key(d.instance.infer(_frame({"x_vel": []})))
        self.assertEqual(set(out), declared,
                         "算不出来要为每个输出各落一个锚点 —— 少发等于说'这段没数据'")


class TestTimestamp(unittest.TestCase):
    def test_T取那笔样本自己的时刻而不是帧右端(self):
        d = _load()
        # 最大值在第 3 笔（T0+2s），帧右端是 T0+60s。
        f = _frame({"x_vel": _samples(1.0, 1.5, 3.9, 1.2)})
        out = _by_key(d.instance.infer(f))
        self.assertEqual(out["vel_max"].t, T0 + timedelta(seconds=2))
        self.assertNotEqual(out["vel_max"].t, f.t_end)

    def test_所有结论的T都落在帧区间内(self):
        d = _load()
        f = _frame({"x_vel": _samples(1.0, 3.9), "z_vel": _samples(0.2, 0.3)})
        res = run_domain(d, f)          # 走骨架的校验路径：越界会被拒收
        self.assertTrue(res.ok)
        self.assertEqual(len(res.findings), len(d.declaration.outputs))


class TestDirectionHint(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def _hint(self, x, y, z, axial="z"):
        f = _frame({"x_vel": _samples(x), "y_vel": _samples(y), "z_vel": _samples(z)},
                   {**FULL_PARAMS, "axial_axis": axial})
        out = _by_key(self.d.instance.infer(f))
        return out["direction_hint"].value, out["axial_ratio"].value

    def test_轴向占比高倾向不对中(self):
        hint, ratio = self._hint(2.0, 1.8, 1.5)
        self.assertIn("不对中", hint)
        self.assertAlmostEqual(ratio, 1.5 / 2.0)

    def test_径向主导轴向弱倾向不平衡(self):
        hint, _ = self._hint(4.0, 3.6, 0.5)
        self.assertIn("不平衡", hint)

    def test_三轴均衡倾向松动(self):
        hint, _ = self._hint(2.0, 1.9, 1.95)
        self.assertIn("松动", hint)

    def test_径向为零不造比值(self):
        hint, ratio = self._hint(0.0, 0.0, 1.0)
        self.assertEqual(ratio, 0.0)
        self.assertIn("无意义", hint)

    def test_轴向轴没有可信样本时落坏码(self):
        f = _frame({"x_vel": _samples(2.0), "z_vel": _samples((1.0, Quality.INPUT_BAD))})
        out = _by_key(self.d.instance.infer(f))
        self.assertIs(out["direction_hint"].quality, Quality.INPUT_BAD)
        self.assertIs(out["iso_zone"].quality, Quality.OK)   # 仍然照出

    def test_只有轴向一路时不硬给倾向(self):
        f = _frame({"z_vel": _samples(2.0)})
        out = _by_key(self.d.instance.infer(f))
        self.assertIs(out["direction_hint"].quality, Quality.INSUFFICIENT_SAMPLES)

    def test_证据里始终写明无频谱(self):
        f = _frame({"x_vel": _samples(4.0), "y_vel": _samples(3.6), "z_vel": _samples(0.5)})
        ev = _by_key(self.d.instance.infer(f))["evidence"].value
        self.assertIn("无频谱", ev)
        self.assertIn("倾向", ev)


class TestDominantAxis(unittest.TestCase):
    def test_取三轴中最大的那一轴(self):
        d = _load()
        f = _frame({"x_vel": _samples(1.0), "y_vel": _samples(3.3), "z_vel": _samples(2.0)})
        out = _by_key(d.instance.infer(f))
        self.assertEqual(out["dominant_axis"].value, "y")
        self.assertAlmostEqual(out["vel_max"].value, 3.3)

    def test_没绑的轴不算作无样本(self):
        d = _load()
        f = _frame({"x_vel": _samples(2.0)})       # y/z 根本没绑
        ev = _by_key(d.instance.infer(f))["evidence"].value
        self.assertNotIn("无样本", ev, "没绑的轴不该被报成'本窗口无样本'（那是绑了但没数据）")


# ═══════════════════ 第②层：基线与偏离 ═══════════════════

import json  # noqa: E402

from aiintegration.types import ArtifactBlob, Dataset, LabeledFrame, ProgressSink  # noqa: E402


def _mkframe(x, y=None, z=None, temp=None, params=None):
    ch = {"x_vel": _samples(x)}
    if y is not None:
        ch["y_vel"] = _samples(y)
    if z is not None:
        ch["z_vel"] = _samples(z)
    if temp is not None:
        ch["temp"] = _samples(temp)
    return _frame(ch, params)


def _train_baseline(domain, xs, ys=None, zs=None, temps=None, label="正常", params=None):
    items = []
    for i, x in enumerate(xs):
        f = _mkframe(x,
                     ys[i] if ys else None,
                     zs[i] if zs else None,
                     temps[i] if temps else None,
                     params)
        items.append(LabeledFrame(frame=f, label=label, sample_id=i + 1))
    ds = Dataset(domain="vibration_lowfreq", binding="dev1", name="基线集",
                 items=tuple(items))
    return domain.train(ds, ProgressSink())


def _as_artifact(trained):
    return ArtifactBlob(id=1, kind=trained.kind, name="基线", blob=trained.blob,
                        algo=trained.algo, accuracy=trained.accuracy,
                        meta=dict(trained.meta), created_at="2026-09-11")


class TestBaselineTraining(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance

    def test_采出来的是基线不是模型(self):
        art = _train_baseline(self.d, [1.0, 1.1, 0.9, 1.05, 0.95, 1.0])
        # ★kind 由域说了算：写死成 model 会让基线顶掉真模型的激活位。
        self.assertEqual(art.kind, "baseline")
        self.assertEqual(art.suffix, ".json")
        # ★基线没有"准确率"这回事 —— 给 None，别拿 1.0 顶（界面会显示成 100%）。
        self.assertIsNone(art.accuracy)

    def test_只用被标成正常的样本(self):
        items = []
        for i in range(6):
            items.append(LabeledFrame(frame=_mkframe(1.0), label="正常", sample_id=i))
        for i in range(3):
            # 故障样本值很大：混进去会把 μ 抬上去，从此再也报不出这个故障
            items.append(LabeledFrame(frame=_mkframe(50.0), label="不平衡", sample_id=100 + i))
        ds = Dataset(domain="vibration_lowfreq", binding="dev1", name="混合集",
                     items=tuple(items))
        art = self.d.train(ds, ProgressSink())
        model = json.loads(art.blob)
        self.assertAlmostEqual(model["channels"]["x_vel"]["mean"], 1.0, places=6)
        self.assertEqual(model["frames"], 6)

    def test_正常样本太少当场失败并说清(self):
        with self.assertRaises(ValueError) as c:
            _train_baseline(self.d, [1.0, 1.0])
        msg = str(c.exception)
        self.assertIn("至少", msg)
        self.assertIn("σ", msg, "要说清为什么少了不行")

    def test_标签对不上时失败信息带出实际分布(self):
        with self.assertRaises(ValueError) as c:
            _train_baseline(self.d, [1.0] * 6, label="良好")
        self.assertIn("良好", str(c.exception), "要能看出实际有哪些标签")

    def test_可用台账改掉哪个标签算正常(self):
        art = _train_baseline(self.d, [1.0] * 6, label="良好",
                              params={**FULL_PARAMS, "normal_label": "良好"})
        self.assertEqual(json.loads(art.blob)["label"], "良好")

    def test_σ有下限免得任何波动都爆表(self):
        art = _train_baseline(self.d, [1.0] * 6)      # 完全不变 ⇒ σ=0
        model = json.loads(art.blob)
        self.assertGreaterEqual(model["channels"]["x_vel"]["std"], 0.01)

    def test_基线里带上采自哪段与几帧(self):
        art = _train_baseline(self.d, [1.0, 1.1, 0.9, 1.05, 0.95, 1.0])
        model = json.loads(art.blob)
        self.assertEqual(model["frames"], 6)
        self.assertTrue(model["t_from"] and model["t_to"])
        # ★"什么时候采的"是它能算资产的前提（C-11 §5 的判据）
        self.assertEqual(art.meta["frames"], "6")
        self.assertIn("t_from", art.meta)

    def test_一路速度都没有时失败(self):
        items = [LabeledFrame(frame=_frame({"x_vel": []}), label="正常", sample_id=i)
                 for i in range(6)]
        ds = Dataset(domain="vibration_lowfreq", binding="dev1", name="空集",
                     items=tuple(items))
        with self.assertRaises(ValueError) as c:
            self.d.train(ds, ProgressSink())
        self.assertIn("一路速度都没能", str(c.exception))


class TestDeviation(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance
        # 基线：x 在 1.0 附近波动，z 在 0.5 附近，温度 40℃
        self.art = _as_artifact(_train_baseline(
            self.d,
            xs=[1.0, 1.1, 0.9, 1.05, 0.95, 1.0],
            zs=[0.5, 0.52, 0.48, 0.51, 0.49, 0.5],
            temps=[40.0, 40.2, 39.8, 40.1, 39.9, 40.0]))

    def _infer(self, x, z=None, temp=None, artifact=True, params=None):
        f = _frame({k: v for k, v in {
            "x_vel": _samples(x),
            "z_vel": _samples(z) if z is not None else None,
            "temp": _samples(temp) if temp is not None else None,
        }.items() if v is not None}, params)
        if artifact:
            f = dataclasses.replace(f, artifacts={"baseline": self.art})
        return _by_key(self.d.infer(f))

    def test_没有基线时那四条落MODEL_NOT_LOADED而ISO照出(self):
        out = self._infer(3.0, artifact=False)
        for k in ("vel_z_max", "ratio_drift", "temp_rise", "anomaly_score"):
            self.assertIs(out[k].quality, Quality.MODEL_NOT_LOADED, k)
            self.assertIsNone(out[k].value)
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertIn("无可用基线", out["evidence"].value)

    def test_正常波动时z分数小(self):
        out = self._infer(1.02, z=0.5, temp=40.0)
        self.assertIs(out["vel_z_max"].quality, Quality.OK)
        self.assertLess(abs(out["vel_z_max"].value), 3.0)
        self.assertLess(out["anomaly_score"].value, 30)

    def test_明显升高时z分数大且异常分跟着上去(self):
        out = self._infer(3.0, z=0.5, temp=40.0)
        self.assertGreater(out["vel_z_max"].value, 3.0)
        self.assertGreater(out["anomaly_score"].value, 30)
        self.assertIn("σ", out["evidence"].value)

    def test_温升算得出且方向对(self):
        out = self._infer(1.0, z=0.5, temp=48.0)
        self.assertAlmostEqual(out["temp_rise"].value, 8.0, places=1)
        self.assertIn("温度较基线高", out["evidence"].value)

    def test_没绑温度就不给温升而不是给0(self):
        out = self._infer(1.0, z=0.5)
        self.assertIs(out["temp_rise"].quality, Quality.NO_INPUT)
        self.assertIsNone(out["temp_rise"].value)

    def test_三轴比例漂移算得出(self):
        # 基线 z/x = 0.5；现在 z 抬到与 x 齐平 ⇒ 漂移 +0.5
        out = self._infer(1.0, z=1.0, temp=40.0)
        self.assertIs(out["ratio_drift"].quality, Quality.OK)
        self.assertAlmostEqual(out["ratio_drift"].value, 0.5, places=2)
        self.assertIn("不对中", out["evidence"].value)

    def test_基线工件读不懂时落坏码而不是掀翻整拍(self):
        bad = ArtifactBlob(id=9, kind="baseline", name="坏的", blob=b"{not json")
        f = dataclasses.replace(_frame({"x_vel": _samples(2.0)}), artifacts={"baseline": bad})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["anomaly_score"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIs(out["iso_zone"].quality, Quality.OK, "①层不该被②层的坏工件带倒")
        self.assertIn("读不懂", out["evidence"].value)

    def test_格式版本不认的基线被拒(self):
        bad = ArtifactBlob(id=9, kind="baseline", name="老格式",
                           blob=json.dumps({"format": "别的@0", "channels": {"x_vel": {}}}
                                           ).encode("utf-8"))
        f = dataclasses.replace(_frame({"x_vel": _samples(2.0)}), artifacts={"baseline": bad})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["vel_z_max"].quality, Quality.MODEL_NOT_LOADED)

    def test_证据里写明基线采自哪段(self):
        out = self._infer(1.0, z=0.5, temp=40.0)
        self.assertIn("基线采自", out["evidence"].value)

    def test_基线里没采过的轴不参与比较(self):
        # 基线只有 x/z；本帧多了 y —— y 没有基线，不该被硬比
        f = _frame({"x_vel": _samples(1.0), "y_vel": _samples(99.0), "z_vel": _samples(0.5)})
        f = dataclasses.replace(f, artifacts={"baseline": self.art})
        out = _by_key(self.d.infer(f))
        self.assertLess(out["vel_z_max"].value, 3.0,
                        "y 轴没有基线，不该拿它算出一个天文数字的 z")


if __name__ == "__main__":
    unittest.main()
