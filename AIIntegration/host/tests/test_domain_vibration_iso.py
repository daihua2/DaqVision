"""经典算法振动诊断（vibration_iso）的回归。

★用**真的装载器**（`discover`）从真的 `domains/` 目录装，不在用例里手搓一个类。

钉的东西按重要性排：
1. **不猜缺省**：参数缺一项，对应那几条落 `CONFIG_INCOMPLETE`，其余照出；
2. **机组类别只有第 1、2 组**（ISO 20816-3:2022），「不适用」不出分级；
3. **ISO 边界判在正确的一侧**；
4. **坏值不当成 0**；
5. **T 取样本自己的时刻**；
6. 低速设备注明仅供参考；方向性只作提示；第二测点参与判定。
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.runner import run_domain
from aiintegration.types import Frame, Sample

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
T0 = datetime(2026, 9, 17, 8, 0, 0, tzinfo=timezone.utc)

FULL_PARAMS = {
    "iso_group": "2",
    "mount_type": "rigid",        # ⇒ 边界 1.4 / 2.8 / 4.5
    "vel_is_rms": "true",
    "axial_axis": "z",
    "rated_speed_rpm": "1480",
}
JUDGMENT_PARAMS = {"iso_group", "mount_type", "vel_is_rms", "axial_axis"}


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration_iso.py"]
    assert not mine, f"经典算法振动诊断装载失败：{mine}"
    by_key = {d.key: d for d in loaded}
    assert "vibration_iso" in by_key, f"没装上，只装到 {sorted(by_key)}"
    return by_key["vibration_iso"]


def _samples(*values, start=T0):
    out = []
    for i, v in enumerate(values):
        q = Quality.OK
        if isinstance(v, tuple):
            v, q = v
        out.append(Sample(t=start + timedelta(seconds=i), value=v, quality=q,
                          status_code=1 if q is Quality.OK else -1000))
    return out


def _frame(channels, params=None):
    return Frame(domain="vibration_iso", binding="dev1",
                 t_start=T0 - timedelta(seconds=1), t_end=T0 + timedelta(seconds=60),
                 channels=channels, params=dict(FULL_PARAMS if params is None else params))


def _by_key(findings):
    return {f.key: f for f in findings}


class TestDeclaration(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_只有推理能力不需要训练(self):
        self.assertEqual(self.d.caps, frozenset({"infer"}))

    def test_判据类参数必填且没有缺省(self):
        specs = {p.key: p for p in self.d.declaration.params}
        self.assertTrue(JUDGMENT_PARAMS <= set(specs))
        for key in JUDGMENT_PARAMS:
            self.assertTrue(specs[key].required, key)
            self.assertEqual(specs[key].default, "", f"{key} 不该有缺省")

    def test_机组类别只有第1第2组与不适用(self):
        spec = {p.key: p for p in self.d.declaration.params}["iso_group"]
        self.assertEqual(spec.choices, ("1", "2", "na"))
        self.assertEqual(len(spec.choice_displays), 3)

    def test_参数归属(self):
        levels = {p.key: p.level for p in self.d.declaration.params}
        self.assertEqual(levels["iso_group"], "machine")
        self.assertEqual(levels["mount_type"], "machine")
        self.assertEqual(levels["rated_speed_rpm"], "machine")
        self.assertEqual(levels["vel_is_rms"], "position")
        self.assertEqual(levels["axial_axis"], "position")

    def test_每个输入项都有显示名(self):
        for i in self.d.declaration.inputs:
            self.assertTrue(i.display, f"{i.role} 缺显示名")

    def test_只有测点1的X轴必选(self):
        req = [i.role for i in self.d.declaration.inputs if i.required]
        self.assertEqual(req, ["x_vel"])


class TestIsoClassification(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def _zone(self, vel, params=None):
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(vel)}, params)))
        return out["iso_zone"], out["iso_zone_code"], out["iso_margin"]

    def test_边界值本身归上一档(self):
        for vel, zone in ((1.4, "A"), (1.4001, "B"), (2.8, "B"), (2.8001, "C"),
                          (4.5, "C"), (4.5001, "D")):
            self.assertEqual(self._zone(vel)[0].value, zone, vel)

    def test_区码与区名同步(self):
        for vel, zone, code in ((1.0, "A", 1), (2.0, "B", 2), (3.0, "C", 3), (9.0, "D", 4)):
            z, c, _ = self._zone(vel)
            self.assertEqual((z.value, c.value), (zone, code))

    def test_余量在D区为负(self):
        self.assertAlmostEqual(self._zone(6.5)[2].value, 4.5 - 6.5, places=6)

    def test_第1组柔性限值(self):
        # 第 1 组柔性 ⇒ 3.5/7.1/11.0，7.0 落 B
        z, _c, _m = self._zone(7.0, {**FULL_PARAMS, "iso_group": "1", "mount_type": "flexible"})
        self.assertEqual(z.value, "B")

    def test_旧版第3第4组不再认(self):
        for g in ("3", "4"):
            z, _c, _m = self._zone(3.0, {**FULL_PARAMS, "iso_group": g})
            self.assertIs(z.quality, Quality.CONFIG_INCOMPLETE, f"第 {g} 组已被 ISO 20816-3:2022 删去")

    def test_不适用则不出分级且说明原因(self):
        f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "iso_group": "na"})
        out = _by_key(self.d.instance.infer(f))
        for k in ("iso_zone", "iso_zone_code", "iso_margin"):
            self.assertIs(out[k].quality, Quality.CONFIG_INCOMPLETE, k)
            self.assertIsNone(out[k].value)
        self.assertIs(out["vel_max"].quality, Quality.OK, "数值照给")
        self.assertIn("不适用", out["evidence"].value)

    def test_判据摘要写明标准版本(self):
        ev = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0)})))["evidence"].value
        self.assertIn("ISO 20816-3:2022", ev)


class TestLowSpeed(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def _ev(self, rpm):
        params = {**FULL_PARAMS, "rated_speed_rpm": rpm}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(2.0)}, params)))
        return out

    def test_低速设备照常分级但注明仅供参考(self):
        out = self._ev("450")
        self.assertIs(out["iso_zone"].quality, Quality.OK, "照常出分级")
        self.assertIn("仅供参考", out["evidence"].value)

    def test_非低速不加注(self):
        self.assertNotIn("仅供参考", self._ev("1480")["evidence"].value)

    def test_600整不算低速(self):
        self.assertNotIn("仅供参考", self._ev("600")["evidence"].value)

    def test_未填额定转速如实说明(self):
        self.assertIn("额定转速未填", self._ev("")["evidence"].value)


class TestParamsMissing(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_口径未确认为有效值就不给分级(self):
        for bad in ("false", "", "TRUE-ish"):
            f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "vel_is_rms": bad})
            out = _by_key(self.d.instance.infer(f))
            self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE, bad)
            self.assertAlmostEqual(out["vel_max"].value, 3.0)

    def test_缺轴向只影响方向性两条ISO照出(self):
        params = {k: v for k, v in FULL_PARAMS.items() if k != "axial_axis"}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0), "z_vel": _samples(0.5)}, params)))
        self.assertIs(out["direction_hint"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertEqual(out["iso_zone"].value, "C")


class TestBadInput(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_坏值不参与判定而不是当成0(self):
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(2.0, (9.9, Quality.INPUT_BAD))})))
        self.assertAlmostEqual(out["vel_max"].value, 2.0)
        self.assertEqual(out["iso_zone"].value, "B")

    def test_全是坏值与没有数据给不同的码(self):
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples((1.0, Quality.INPUT_BAD))})))
        self.assertIs(out["vel_max"].quality, Quality.INPUT_BAD)
        out2 = _by_key(self.d.instance.infer(_frame({"x_vel": []})))
        self.assertIs(out2["vel_max"].quality, Quality.NO_INPUT)

    def test_一条都算不出来时每个输出都有锚点(self):
        declared = {o.key for o in self.d.declaration.outputs}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": []})))
        self.assertEqual(set(out), declared)

    def test_T取那笔样本自己的时刻(self):
        f = _frame({"x_vel": _samples(1.0, 1.5, 3.9, 1.2)})
        out = _by_key(self.d.instance.infer(f))
        self.assertEqual(out["vel_max"].t, T0 + timedelta(seconds=2))

    def test_走骨架校验路径结论齐全(self):
        f = _frame({"x_vel": _samples(1.0, 3.9), "z_vel": _samples(0.2, 0.3)})
        res = run_domain(self.d, f)
        self.assertTrue(res.ok)
        self.assertEqual(len(res.findings), len(self.d.declaration.outputs))


class TestDirectionHint(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def _hint(self, x, y, z):
        f = _frame({"x_vel": _samples(x), "y_vel": _samples(y), "z_vel": _samples(z)})
        out = _by_key(self.d.instance.infer(f))
        return out["direction_hint"].value, out["axial_ratio"].value

    def test_轴向占比高提示不对中(self):
        hint, ratio = self._hint(2.0, 1.8, 1.5)
        self.assertIn("不对中", hint)
        self.assertIn("提示", hint)
        self.assertAlmostEqual(ratio, 1.5 / 2.0)

    def test_径向主导提示不平衡(self):
        self.assertIn("不平衡", self._hint(4.0, 3.6, 0.5)[0])

    def test_三轴均衡提示松动(self):
        self.assertIn("松动", self._hint(2.0, 1.9, 1.95)[0])

    def test_径向为零不造比值(self):
        hint, ratio = self._hint(0.0, 0.0, 1.0)
        self.assertEqual(ratio, 0.0)
        self.assertIn("无意义", hint)

    def test_输出显示名为提示(self):
        spec = {o.key: o for o in self.d.declaration.outputs}["direction_hint"]
        self.assertIn("提示", spec.display)


class TestSecondPoint(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_第二测点的最大值参与判级(self):
        f = _frame({"x_vel": _samples(1.0), "x2_vel": _samples(5.0)})
        out = _by_key(self.d.instance.infer(f))
        self.assertAlmostEqual(out["vel_max"].value, 5.0)
        self.assertEqual(out["dominant_axis"].value, "x2")
        self.assertEqual(out["iso_zone"].value, "D")

    def test_方向性取最大值所在测点(self):
        # 测点1 径向主导（不平衡）；测点2 更大且轴向偏高（不对中）⇒ 取测点2
        f = _frame({"x_vel": _samples(1.0), "y_vel": _samples(0.9), "z_vel": _samples(0.1),
                    "x2_vel": _samples(3.0), "y2_vel": _samples(1.0), "z2_vel": _samples(2.5)})
        out = _by_key(self.d.instance.infer(f))
        self.assertIn("不对中", out["direction_hint"].value)
        self.assertAlmostEqual(out["axial_ratio"].value, 2.5 / 3.0, places=4)

    def test_最大值测点算不出方向时退到另一个测点(self):
        f = _frame({"x_vel": _samples(1.0), "y_vel": _samples(0.9), "z_vel": _samples(0.1),
                    "x2_vel": _samples(5.0)})           # 测点2 没有轴向轴
        out = _by_key(self.d.instance.infer(f))
        self.assertIs(out["direction_hint"].quality, Quality.OK)
        self.assertIn("不平衡", out["direction_hint"].value)


if __name__ == "__main__":
    unittest.main()
