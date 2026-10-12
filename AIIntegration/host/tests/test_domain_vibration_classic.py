"""低频采集AI振动诊断（`vibration`）经典算法部分的回归（`algorithms=classic`）。原 `vibration_iso` 的用例，合并后照跑。

★用**真的装载器**（`discover`）从真的 `domains/` 目录装，不在用例里手搓一个类。

钉的东西按重要性排：
1. **不猜缺省**：参数缺一项，对应那几条落 `CONFIG_INCOMPLETE`，其余照出；
2. **机组类别只有第 1、2 组**，「不适用」不出分级；参数键与取值逐字照平台台账（`C-64 §2.3`）；
3. **ISO 边界判在正确的一侧**；
4. **坏值不当成 0**；
5. **T 取样本自己的时刻**；
6. **检测状态**：A/B 正常、C 警告、D 危险、停机；分级给不出时同落坏码（`C-65 §2`）；
7. 低速设备（工业机器与泵）照常按速度出分级，但警示标准要求另看位移、可能判轻；方向性只作提示；一条绑定只管一个传感器的三轴（`C-64 §2.1`）。
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.runner import run_domain
from aiintegration.types import Frame, Sample

#: 只启用经典时不写的那一半（与 `vibration.BASELINE_KEYS` 同），加上未启用的故障分类（`CLASSIFY_KEYS`）。
BASELINE_KEYS = ("vel_z_max", "acc_z_max", "disp_z_max", "freq_z_max",
                 "ratio_drift", "temp_rise", "anomaly_score",
                 "fault_class", "fault_vote")

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
T0 = datetime(2026, 9, 17, 8, 0, 0, tzinfo=timezone.utc)

FULL_PARAMS = {
    "algorithms": "classic",
    "machineGroup": "group2",
    "supportClass": "rigid",      # ⇒ 边界 1.4 / 2.8 / 4.5
    "vel_is_rms": "true",
    "axialAxis": "z",
    "ratedSpeedRpm": "1480",
}
JUDGMENT_PARAMS = {"machineGroup", "supportClass", "vel_is_rms", "axialAxis"}


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration.py"]
    assert not mine, f"经典算法振动诊断装载失败：{mine}"
    by_key = {d.key: d for d in loaded}
    assert "vibration" in by_key, f"没装上，只装到 {sorted(by_key)}"
    return by_key["vibration"]


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
    return Frame(domain="vibration", binding="dev1",
                 t_start=T0 - timedelta(seconds=1), t_end=T0 + timedelta(seconds=60),
                 channels=channels, params=dict(FULL_PARAMS if params is None else params))


def _by_key(findings):
    return {f.key: f for f in findings}


class TestDeclaration(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_判据类参数一律没有缺省(self):
        """★机器分组、支承方式、泵类别在声明上不必填（泵与工业机器各需一半），
        但**都没有缺省**；必填性由推理时按所走判据检查（见 TestParamsMissing）。"""
        specs = {p.key: p for p in self.d.declaration.params}
        for key in JUDGMENT_PARAMS | {"pumpCategory", "ratedPowerKw"}:
            self.assertIn(key, specs)
            self.assertEqual(specs[key].default, "", f"{key} 不该有缺省")
        self.assertTrue(specs["vel_is_rms"].required)
        self.assertTrue(specs["axialAxis"].required)

    def test_机器分组只有第1第2组与不适用_取值照平台台账(self):
        spec = {p.key: p for p in self.d.declaration.params}["machineGroup"]
        self.assertEqual(spec.choices, ("group1", "group2", "notApplicable"))
        self.assertEqual(len(spec.choice_displays), 3)
        self.assertIn("GB/T 6075.3", spec.display)

    def test_泵类别第1第2类与不适用_取值照平台台账(self):
        spec = {p.key: p for p in self.d.declaration.params}["pumpCategory"]
        self.assertEqual(spec.choices, ("category1", "category2", "notApplicable"))
        self.assertIn("GB/T 6075.7", spec.display)

    def test_参数归属(self):
        levels = {p.key: p.level for p in self.d.declaration.params}
        for k in ("machineGroup", "supportClass", "pumpCategory", "ratedPowerKw", "ratedSpeedRpm"):
            self.assertEqual(levels[k], "machine", k)
        self.assertEqual(levels["axialAxis"], "sensor", "轴向由振动传感器给（C-64 §2.3）")
        self.assertEqual(levels["vel_is_rms"], "position")

    def test_每个输入项都有显示名(self):
        for i in self.d.declaration.inputs:
            self.assertTrue(i.display, f"{i.role} 缺显示名")

    def test_只有X轴必选(self):
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
        z, _c, _m = self._zone(7.0, {**FULL_PARAMS, "machineGroup": "group1",
                                     "supportClass": "flexible"})
        self.assertEqual(z.value, "B")

    def test_旧取值与旧版第3第4组都不认(self):
        """★1.x 的取值 `1`/`2` 不再认：平台原样拷贝台账值，认旧值等于给两套写法开口子。"""
        for g in ("1", "2", "3", "group3"):
            z, _c, _m = self._zone(3.0, {**FULL_PARAMS, "machineGroup": g})
            self.assertIs(z.quality, Quality.CONFIG_INCOMPLETE, g)

    def test_旧键名不认(self):
        params = {"algorithms": "classic", "iso_group": "2", "mount_type": "rigid", "vel_is_rms": "true",
                  "axial_axis": "z"}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0)}, params)))
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIs(out["direction_hint"].quality, Quality.CONFIG_INCOMPLETE)

    def test_平台所填不适用已不采用_缺功率则不出分级并说明(self):
        # 用户 2026-10-12 定「不适用应由你判定」：平台的「不适用」不再采用；没有功率就定不了分组
        f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "machineGroup": "notApplicable"})
        out = _by_key(self.d.instance.infer(f))
        for k in ("iso_zone", "iso_zone_code", "iso_margin"):
            self.assertIs(out[k].quality, Quality.CONFIG_INCOMPLETE, k)
            self.assertIsNone(out[k].value)
        self.assertIs(out["vel_max"].quality, Quality.OK, "数值照给")
        self.assertIn("已不采用", out["evidence"].value)
        self.assertIn("额定功率未传", out["evidence"].value)

    def test_判据摘要写明国标(self):
        ev = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0)})))["evidence"].value
        self.assertIn("GB/T 6075.3-2011（按所填第 2 组 / 刚性支承）", ev)
        self.assertNotIn("ISO 20816", ev, "判级依据已改用国标")


class TestStatus(unittest.TestCase):
    """`C-65 §2`：检测状态五档词表。★A、B 都是正常（国标 B 区即「可长期运行」）。"""

    def setUp(self):
        self.d = _load()

    def _status(self, vel, params=None):
        return _by_key(self.d.instance.infer(_frame({"x_vel": _samples(vel)}, params)))["status"]

    def test_烈度区对档位(self):
        for vel, want in ((1.0, "normal"), (2.0, "normal"), (3.0, "warning"), (9.0, "danger")):
            s = self._status(vel)
            self.assertIs(s.quality, Quality.OK, vel)
            self.assertEqual(s.value, want, vel)

    def test_分级给不出时状态同落配置不全_不报正常(self):
        s = self._status(1.0, {**FULL_PARAMS, "vel_is_rms": "false"})
        self.assertIs(s.quality, Quality.CONFIG_INCOMPLETE)
        self.assertIsNone(s.value)

    def test_停机写停机(self):
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(0.1)},
                                                   {**FULL_PARAMS, "stop_threshold": "0.3"})))
        self.assertEqual(out["status"].value, "stopped")

    def test_没数据时状态也落锚点(self):
        s = _by_key(self.d.instance.infer(_frame({"x_vel": []})))["status"]
        self.assertIs(s.quality, Quality.NO_INPUT)


class TestPump(unittest.TestCase):
    """GB/T 6075.7-2015：按泵类别与额定功率（200 kW 分档）取限值，不看支承；低速另看位移（我方测不到，警示）。"""

    PUMP = {"algorithms": "classic", "pumpCategory": "category2", "ratedPowerKw": "90", "vel_is_rms": "true", "axialAxis": "z"}

    def setUp(self):
        self.d = _load()

    def _out(self, vel, **over):
        return _by_key(self.d.instance.infer(_frame({"x_vel": _samples(vel)}, {**self.PUMP, **over})))

    def test_第2类小功率限值(self):
        # 第Ⅱ类 ≤200 kW ⇒ 3.2 / 5.1 / 8.5
        for vel, zone in ((3.2, "A"), (3.3, "B"), (5.1, "B"), (5.2, "C"), (8.5, "C"), (8.6, "D")):
            self.assertEqual(self._out(vel)["iso_zone"].value, zone, vel)

    def test_功率按200kW分档且200归小档(self):
        self.assertEqual(self._out(3.3, ratedPowerKw="200")["iso_zone"].value, "B")
        # >200 kW ⇒ 4.2 / 6.1 / 9.5，3.3 落 A
        self.assertEqual(self._out(3.3, ratedPowerKw="201")["iso_zone"].value, "A")

    def test_四档限值逐格(self):
        # 每档取 A/B、B/C、C/D 三个边界各自「刚过线」的值，四张子表任一格写错都会红
        table = {("category1", "90"): (2.5, 4.0, 6.6), ("category1", "300"): (3.5, 5.0, 7.6),
                 ("category2", "90"): (3.2, 5.1, 8.5), ("category2", "300"): (4.2, 6.1, 9.5)}
        for (cat, kw), bounds in table.items():
            for b, (at, over) in zip(bounds, (("A", "B"), ("B", "C"), ("C", "D"))):
                self.assertEqual(self._out(b, pumpCategory=cat, ratedPowerKw=kw)["iso_zone"].value,
                                 at, (cat, kw, b))
                self.assertEqual(self._out(round(b + 0.05, 2), pumpCategory=cat,
                                           ratedPowerKw=kw)["iso_zone"].value, over, (cat, kw, b))

    def test_泵不看支承方式(self):
        a = self._out(5.0, supportClass="rigid")["iso_zone"].value
        b = self._out(5.0, supportClass="flexible")["iso_zone"].value
        self.assertEqual(a, b)

    def test_低速泵照常分级但警示可能判轻(self):
        # 用户 2026-10-12 定：GB/T 6075.7 §3.1 要求 <600 r/min 另测峰峰值位移，我方测不到 ⇒ 照出但警示（原「泵不加低速注释」作废）
        out = self._out(3.0, ratedSpeedRpm="300")
        ev = out["evidence"].value
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertIn("GB/T 6075.7-2015（第Ⅱ类", ev)
        self.assertIn("低速泵", ev)
        self.assertIn("可能判轻", ev)
        self.assertNotIn("可能判轻", self._out(3.0, ratedSpeedRpm="1480")["evidence"].value)

    def test_泵1kW及以下无适用标准_按附录C下端判(self):
        # 无 6075.7 可依；用户 2026-10-13 定：自定门槛未填的按 GB/T 41850.1 附录 C 下端 0.71 / 1.8 / 4.5
        out = self._out(3.0, ratedPowerKw="1")
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertEqual(out["iso_zone"].value, "C")
        self.assertIn("⚠无适用的现场判级标准", out["evidence"].value)
        self.assertIn("GB/T 41850.1-2024 附录 C", out["evidence"].value)
        self.assertNotIn("GB/T 6075.7-2015（第", out["evidence"].value)
        self.assertAlmostEqual(out["vel_max"].value, 3.0)
        out = self._out(3.0, ratedPowerKw="1.1")
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertNotIn("41850.1", out["evidence"].value, ">1 kW 按 6075.7")

    def test_泵缺额定功率不出分级(self):
        out = self._out(3.0, ratedPowerKw="")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("额定功率", out["evidence"].value)

    def test_泵类别与机器分组都填是矛盾(self):
        out = self._out(3.0, machineGroup="group2", supportClass="rigid")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("自相矛盾", out["evidence"].value)

    def test_泵类别选不适用时按工业机器判(self):
        params = {**FULL_PARAMS, "pumpCategory": "notApplicable"}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0)}, params)))
        self.assertEqual(out["iso_zone"].value, "C")
        self.assertIn("GB/T 6075.3-2011", out["evidence"].value)


class TestLowSpeed(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def _ev(self, rpm):
        params = {**FULL_PARAMS, "ratedSpeedRpm": rpm}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(2.0)}, params)))
        return out

    def test_低速设备照常分级但警示可能判轻(self):
        out = self._ev("450")
        self.assertIs(out["iso_zone"].quality, Quality.OK, "照常出分级")
        self.assertIn("可能判轻", out["evidence"].value)

    def test_非低速不加注(self):
        self.assertNotIn("可能判轻", self._ev("1480")["evidence"].value)

    def test_600整不算低速(self):
        self.assertNotIn("可能判轻", self._ev("600")["evidence"].value)

    def test_转速不在国标范围不出分级(self):
        # GB/T 6075.3 第 1 章：120~15 000 r/min
        for rpm in ("100", "16000"):
            out = self._ev(rpm)
            self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE, rpm)
            self.assertIn("⚠无适用的现场判级标准", out["evidence"].value, rpm)
        for rpm in ("120", "15000"):
            self.assertIs(self._ev(rpm)["iso_zone"].quality, Quality.OK, rpm)

    def test_未填额定转速如实说明(self):
        self.assertIn("额定转速未填", self._ev("")["evidence"].value)


class TestScopeAndRadial(unittest.TestCase):
    """用户 2026-10-12 定：按标准判级；不在标准范围的不出烈度区、只给数值并警示；判区只取径向（GB/T 6075.3 §5.2.4）。"""

    def setUp(self):
        self.d = _load()

    def _out(self, channels, **over):
        return _by_key(self.d.instance.infer(_frame(channels, {**FULL_PARAMS, **over})))

    def test_功率15kW及以下与超50MW无适用标准(self):
        out = self._out({"x_vel": _samples(2.0)}, ratedPowerKw="50001", machineGroup="")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIsNone(out["iso_zone"].value)
        self.assertIn("⚠无适用的现场判级标准", out["evidence"].value)
        self.assertAlmostEqual(out["vel_max"].value, 2.0)
        # ≤15 kW：仍无 6075.3 可依，但自定门槛未填时按 GB/T 41850.1 附录 C 下端判（用户 2026-10-13 定）
        for kw in ("15", "5"):
            out = self._out({"x_vel": _samples(2.0)}, ratedPowerKw=kw, machineGroup="")
            self.assertIs(out["iso_zone"].quality, Quality.OK, kw)
            self.assertEqual(out["iso_zone"].value, "C", kw)
            self.assertIn("⚠无适用的现场判级标准", out["evidence"].value, kw)
            self.assertIn("GB/T 41850.1-2024 附录 C", out["evidence"].value, kw)
            self.assertAlmostEqual(out["vel_max"].value, 2.0)
        for kw in ("15.1", "300", "50000"):
            self.assertIs(self._out({"x_vel": _samples(2.0)}, ratedPowerKw=kw, machineGroup="")["iso_zone"].quality,
                          Quality.OK, kw)

    def test_平台所填不适用不采用_按功率推分组照判(self):
        out = self._out({"x_vel": _samples(2.0)}, machineGroup="notApplicable", ratedPowerKw="200")
        self.assertEqual(out["iso_zone"].value, "B")
        self.assertIn("已不采用", out["evidence"].value)


    def test_轴向最大时只按径向判区(self):
        # 第 2 组刚性 1.4/2.8/4.5：径向 x=2.0 ⇒ B；轴向 z=5.0 若参与就是 D
        out = self._out({"x_vel": _samples(2.0), "z_vel": _samples(5.0)})
        self.assertEqual(out["iso_zone"].value, "B")
        self.assertAlmostEqual(out["iso_margin"].value, 0.8)
        self.assertAlmostEqual(out["vel_max"].value, 5.0, msg="速度最大值照旧是三轴最大")
        self.assertEqual(out["dominant_axis"].value, "z")
        self.assertIn("不参与判区", out["evidence"].value)

    def test_径向两轴取大(self):
        out = self._out({"x_vel": _samples(1.0), "y_vel": _samples(3.0), "z_vel": _samples(0.5)})
        self.assertEqual(out["iso_zone"].value, "C")

    def test_只有轴向读数不出分级(self):
        out = self._out({"z_vel": _samples(2.0)})
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("只认径向", out["evidence"].value)

    def test_轴向未指明按三轴最大判并警示(self):
        out = self._out({"x_vel": _samples(2.0), "z_vel": _samples(5.0)}, axialAxis="")
        self.assertEqual(out["iso_zone"].value, "D")
        self.assertIn("轴向未指明", out["evidence"].value)


class TestApplicabilityByUs(unittest.TestCase):
    """用户 2026-10-12 定「不适用应由你判定」：平台只传事实（设备类型、功率、转速、支承、泵类别），
    适用与否、分组都由本模块按 GB/T 6075.3 / 6075.7 判。"""

    BASE = {"algorithms": "classic", "vel_is_rms": "true", "axialAxis": "z", "supportClass": "rigid",
            "ratedSpeedRpm": "1480"}

    def setUp(self):
        self.d = _load()

    def _out(self, vel=3.0, **over):
        return _by_key(self.d.instance.infer(_frame({"x_vel": _samples(vel)}, {**self.BASE, **over})))

    def _no_std(self, out, word):
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIsNone(out["iso_zone"].value)
        self.assertIn("⚠无适用的现场判级标准", out["evidence"].value)
        self.assertIn(word, out["evidence"].value)
        self.assertIs(out["vel_max"].quality, Quality.OK, "数值照给")

    def test_分组由功率推出(self):
        # 3.0 mm/s：第 1 组刚性 2.3/4.5/7.1 ⇒ B；第 2 组刚性 1.4/2.8/4.5 ⇒ C
        self.assertEqual(self._out(ratedPowerKw="400", machineType="motor")["iso_zone"].value, "B")
        self.assertEqual(self._out(ratedPowerKw="300", machineType="motor")["iso_zone"].value, "C")
        self.assertIn("按额定 400 kW 定为第 1 组", self._out(ratedPowerKw="400", machineType="motor")["evidence"].value)

    def test_非电机所填分组与功率不符是矛盾(self):
        out = self._out(ratedPowerKw="200", machineType="compressor", machineGroup="group1")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("自相矛盾", out["evidence"].value)

    def test_电机可按轴中心高分组_所填优先(self):
        out = self._out(ratedPowerKw="200", machineType="motor", machineGroup="group1")
        self.assertEqual(out["iso_zone"].value, "B", "按所填第 1 组")
        self.assertIn("轴中心高", out["evidence"].value)

    def test_另有标准的类型不出分级(self):
        for mtype, word in (("reciprocating", "GB/T 6075.6"), ("screwCompressor", "明文排除"),
                            ("hydroUnit", "GB/T 6075.5"), ("windTurbine", "ISO 10816-21")):
            self._no_std(self._out(ratedPowerKw="200", machineType=mtype), word)

    def test_燃机超3MW另依6075点4(self):
        self._no_std(self._out(ratedPowerKw="5000", machineType="gasTurbine"), "GB/T 6075.4")
        self.assertIs(self._out(ratedPowerKw="2000", machineType="gasTurbine")["iso_zone"].quality, Quality.OK)

    def test_300kW及以下柔性支承风机应依ISO14694(self):
        self._no_std(self._out(ratedPowerKw="200", machineType="fan", supportClass="flexible"), "ISO 14694")
        self.assertIs(self._out(ratedPowerKw="200", machineType="fan")["iso_zone"].quality, Quality.OK,
                      "刚性支承的风机 GB/T 6075.3 照判")
        self.assertIs(self._out(ratedPowerKw="400", machineType="fan", supportClass="flexible")["iso_zone"].quality,
                      Quality.OK, ">300 kW 的风机 GB/T 6075.3 照判")

    def test_设备类型未传照判并警示(self):
        out = self._out(ratedPowerKw="200")
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertIn("设备类型未传", out["evidence"].value)
        self.assertNotIn("设备类型未传", self._out(ratedPowerKw="200", machineType="motor")["evidence"].value)

    def test_设备类型取值不认不猜(self):
        out = self._out(ratedPowerKw="200", machineType="spaceship")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("取值不认", out["evidence"].value)

    def test_类型为泵须有泵类别_类别与类型冲突是矛盾(self):
        out = self._out(ratedPowerKw="90", machineType="pump")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("泵类别", out["evidence"].value)
        self.assertEqual(self._out(ratedPowerKw="90", machineType="pump", pumpCategory="category2")["iso_zone"].value,
                         "A", "第Ⅱ类 ≤200 kW：3.0 < 3.2")
        out = self._out(ratedPowerKw="90", machineType="motor", pumpCategory="category2")
        self.assertIn("自相矛盾", out["evidence"].value)

    def test_缺功率也缺分组则不出分级(self):
        out = self._out(machineType="motor")
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("额定功率未传", out["evidence"].value)

    def test_设备类型声明在台账层级且无缺省(self):
        spec = {p.key: p for p in self.d.declaration.params}["machineType"]
        self.assertEqual(spec.level, "machine")
        self.assertEqual(spec.default, "")
        self.assertIn("fan", spec.choices)


class TestParamsMissing(unittest.TestCase):
    def setUp(self):
        self.d = _load()

    def test_口径不是有效值就不给分级(self):
        for bad, why in (("false", "手册明写为峰值"), ("", "未填"), ("TRUE-ish", "取值不认")):
            f = _frame({"x_vel": _samples(3.0)}, {**FULL_PARAMS, "vel_is_rms": bad})
            out = _by_key(self.d.instance.infer(f))
            self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE, bad)
            self.assertIn(why, out["evidence"].value, bad)
            self.assertAlmostEqual(out["vel_max"].value, 3.0)

    def test_手册未注明口径按惯例视为有效值(self):
        # 用户 2026-10-12 定：速度是 RMS、位移是峰峰值为惯例 —— 「是」这一项必须把「手册未注明」包进来，
        # 否则填表的人会因手册没写而选「否」，整条诊断就不出烈度分级。
        spec = {p.key: p for p in self.d.declaration.params}["vel_is_rms"]
        yes = dict(zip(spec.choices, spec.choice_displays))["true"]
        self.assertIn("手册未注明", yes)
        self.assertIn("惯例", spec.description)

    def test_工业机器缺机器分组或支承方式不出分级(self):
        for missing in ("machineGroup", "supportClass"):
            params = {k: v for k, v in FULL_PARAMS.items() if k != missing}
            out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0)}, params)))
            self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE, missing)
            self.assertIsNone(out["iso_zone"].value)

    def test_缺轴向只影响方向性两条ISO照出(self):
        params = {k: v for k, v in FULL_PARAMS.items() if k != "axialAxis"}
        out = _by_key(self.d.instance.infer(_frame({"x_vel": _samples(3.0), "z_vel": _samples(0.5)}, params)))
        self.assertIs(out["direction_hint"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIs(out["iso_zone"].quality, Quality.OK)
        self.assertEqual(out["iso_zone"].value, "C")
        self.assertEqual(out["status"].value, "warning")


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
        declared = {o.key for o in self.d.declaration.outputs} - set(BASELINE_KEYS)
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
        self.assertEqual(len(res.findings), len(self.d.declaration.outputs) - len(BASELINE_KEYS))


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

    def test_轴向轴没有可信样本(self):
        f = _frame({"x_vel": _samples(2.0), "z_vel": _samples((1.0, Quality.INPUT_BAD))})
        out = _by_key(self.d.instance.infer(f))
        self.assertIs(out["direction_hint"].quality, Quality.INPUT_BAD)
        self.assertEqual(out["iso_zone"].value, "B", "烈度分级照出")

    def test_只有轴向轴没有径向可比(self):
        out = _by_key(self.d.instance.infer(_frame({"x_vel": [], "z_vel": _samples(1.0)})))
        self.assertIs(out["direction_hint"].quality, Quality.INSUFFICIENT_SAMPLES)

    def test_输出显示名为提示(self):
        spec = {o.key: o for o in self.d.declaration.outputs}["direction_hint"]
        self.assertIn("提示", spec.display)


class TestStopState(unittest.TestCase):
    """定案 1.6：停机门槛。停机时文字点写「停机」，数值点这一拍不写；不填不判。"""

    def setUp(self):
        self.d = _load()

    def _infer(self, thr, **vals):
        params = dict(FULL_PARAMS, stop_threshold=thr)
        return _by_key(self.d.instance.infer(_frame({k: _samples(v) for k, v in vals.items()}, params)))

    def test_低于门槛判停机_文字点写停机_数值点不写(self):
        out = self._infer("0.3", x_vel=0.1, y_vel=0.2, z_vel=0.05)
        self.assertEqual(out["run_state"].value, "停机")
        self.assertEqual(out["status"].value, "stopped")
        self.assertEqual(out["iso_zone"].value, "停机")
        self.assertEqual(out["iso_zone_code"].value, 0)
        self.assertEqual(out["direction_hint"].value, "停机")
        for k in ("run_state", "status", "iso_zone", "iso_zone_code", "direction_hint",
                  "vel_max", "evidence"):
            self.assertIs(out[k].quality, Quality.OK, k)
        self.assertNotIn("iso_margin", out)
        self.assertNotIn("axial_ratio", out)
        self.assertIn("停机", out["evidence"].value)

    def test_停机时速度实测值照出(self):
        out = self._infer("0.3", x_vel=0.1, y_vel=0.2)
        self.assertEqual(out["vel_max"].value, 0.2)
        self.assertEqual(out["dominant_axis"].value, "y")

    def test_等于门槛算运行(self):
        out = self._infer("0.3", x_vel=0.3, z_vel=0.1)
        self.assertEqual(out["run_state"].value, "运行")
        self.assertEqual(out["iso_zone"].value, "A")
        self.assertIn("iso_margin", out)

    def test_任一通道过门槛即运行(self):
        out = self._infer("0.3", x_vel=0.1, y_vel=2.0, z_vel=0.1)
        self.assertEqual(out["run_state"].value, "运行")
        self.assertEqual(out["iso_zone"].value, "B")

    def test_不填门槛不判且照常出全部结论(self):
        out = self._infer("", x_vel=0.01, z_vel=0.01)
        self.assertEqual(out["run_state"].value, "未判")
        self.assertIs(out["run_state"].quality, Quality.OK)
        self.assertEqual(set(out), {o.key for o in self.d.declaration.outputs} - set(BASELINE_KEYS))

    def test_门槛非法只让运行状态落配置不全(self):
        for bad in ("abc", "0", "-1", "nan", "inf"):
            out = self._infer(bad, x_vel=0.01, z_vel=0.01)
            self.assertIs(out["run_state"].quality, Quality.CONFIG_INCOMPLETE, bad)
            self.assertIsNone(out["run_state"].value)
            self.assertEqual(out["iso_zone"].value, "A", bad)
            self.assertEqual(out["status"].value, "normal", bad)

    def test_门槛可选且没有缺省(self):
        spec = {p.key: p for p in self.d.declaration.params}["stop_threshold"]
        self.assertFalse(spec.required)
        self.assertEqual(spec.default, "")
        self.assertEqual(spec.level, "position")

    def test_走骨架校验停机结论全部收下(self):
        params = dict(FULL_PARAMS, stop_threshold="0.3")
        res = run_domain(self.d, _frame({"x_vel": _samples(0.1)}, params))
        self.assertTrue(res.ok)
        self.assertEqual(res.error, "")
        self.assertEqual({f.key for f in res.findings},
                         {"vel_max", "dominant_axis", "run_state", "status", "iso_zone",
                          "iso_zone_code", "direction_hint", "evidence"})


if __name__ == "__main__":
    unittest.main()


class TestCustomLimits(unittest.TestCase):
    """用户 2026-10-12 定「除了有国际标准的，阈值应该能配置」：无适用标准时用自定预警 / 报警 / 危险线，有国标不采用。"""

    # 2 kW 伺服电机：不在 GB/T 6075.3 范围（≤15 kW）
    SERVO = {"algorithms": "classic", "vel_is_rms": "true", "axialAxis": "z", "supportClass": "rigid",
             "machineType": "motor", "ratedPowerKw": "2", "ratedSpeedRpm": "3000"}
    LIMITS = {"limitAttention": "2.0", "limitWarning": "3.5", "limitDanger": "5.0"}

    def setUp(self):
        self.d = _load()

    def _out(self, vel, params):
        return _by_key(self.d.instance.infer(_frame({"x_vel": _samples(vel)}, params)))

    def test_无标准时按自定门槛判_四段对正常注意警告危险(self):
        for vel, zone, status in ((2.0, "A", "normal"), (3.0, "B", "attention"),
                                  (4.0, "C", "warning"), (5.1, "D", "danger")):
            out = self._out(vel, {**self.SERVO, **self.LIMITS})
            self.assertEqual(out["iso_zone"].value, zone, vel)
            self.assertIs(out["iso_zone"].quality, Quality.OK, vel)
            self.assertEqual(out["status"].value, status, vel)
        ev = self._out(3.0, {**self.SERVO, **self.LIMITS})["evidence"].value
        self.assertIn("依用户自定门槛", ev)
        self.assertIn("非标准", ev)
        self.assertIn("⚠无适用的现场判级标准", ev, "仍要说清为什么没有标准")

    def test_有国标时自定门槛不采用(self):
        params = {**self.SERVO, **self.LIMITS, "ratedPowerKw": "200"}   # 第 2 组刚性 1.4/2.8/4.5
        out = self._out(3.0, params)
        self.assertEqual(out["iso_zone"].value, "C", "按国标，不按自定（自定下是 B）")
        self.assertEqual(out["status"].value, "warning")
        self.assertIn("自定门槛不采用", out["evidence"].value)

    def test_自定门槛不全或不递增不采用(self):
        for bad in ({"limitAttention": "2.0", "limitWarning": "3.5"},
                    {"limitAttention": "3.5", "limitWarning": "2.0", "limitDanger": "5.0"},
                    {"limitAttention": "x", "limitWarning": "3.5", "limitDanger": "5.0"}):
            out = self._out(3.0, {**self.SERVO, **bad})
            self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE, bad)
            self.assertIn("三条都填且递增", out["evidence"].value, bad)

    def test_另有专门标准而未实现的_无自定门槛提示可以填(self):
        # 往复式机器另依 GB/T 6075.6：表 C.1 注 1 只许用于「没有特定标准」的机器，不套附录 C 缺省
        for extra in ({"machineType": "reciprocating"}, {"machineType": "fan", "supportClass": "flexible"},
                      {"machineType": "fan"}):
            out = self._out(3.0, {**self.SERVO, **extra})
            self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE, extra)
            self.assertIn("可在诊断上填自定预警线", out["evidence"].value, extra)
            self.assertNotIn("41850.1", out["evidence"].value, extra)

    def test_无标准无自定门槛_小型机器按附录C下端判_B区为正常(self):
        # 用户 2026-10-13 定：≤15 kW 且无专门标准，按 GB/T 41850.1 表 C.1 下端 0.71 / 1.8 / 4.5
        for vel, zone, status in ((0.71, "A", "normal"), (1.5, "B", "normal"),
                                  (3.0, "C", "warning"), (4.6, "D", "danger")):
            out = self._out(vel, self.SERVO)
            self.assertEqual(out["iso_zone"].value, zone, vel)
            self.assertIs(out["iso_zone"].quality, Quality.OK, vel)
            self.assertEqual(out["status"].value, status, vel)
        ev = self._out(1.5, self.SERVO)["evidence"].value
        self.assertIn("GB/T 41850.1-2024 附录 C 表 C.1", ev)
        self.assertIn("非专门标准", ev)
        self.assertIn("⚠无适用的现场判级标准", ev, "仍要说清为什么没有专门标准")
        self.assertNotIn("依用户自定门槛", ev)

    def test_附录C缺省只到15kW_泵只到1kW(self):
        self.assertIs(self._out(1.5, {**self.SERVO, "ratedPowerKw": "15"})["iso_zone"].quality, Quality.OK)
        pump = {**self.SERVO, "machineType": "pump", "pumpCategory": "category1", "ratedPowerKw": "0.75"}
        out = self._out(1.5, pump)
        self.assertEqual(out["iso_zone"].value, "B")
        self.assertIn("41850.1", out["evidence"].value)
        # 15 kW 以上、转速超出 6075.3 范围：无标准但不是小型机器，表 C.1 没给取哪一端 ⇒ 不缺省
        out = self._out(1.5, {**self.SERVO, "ratedPowerKw": "30", "ratedSpeedRpm": "60"})
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertNotIn("41850.1", out["evidence"].value)

    def test_附录C缺省_设备类型未传要警示(self):
        params = {k: v for k, v in self.SERVO.items() if k != "machineType"}
        out = self._out(1.5, params)
        self.assertEqual(out["iso_zone"].value, "B")
        self.assertIn("未能核对是否另有专门标准", out["evidence"].value)

    def test_自定门槛优先于附录C缺省(self):
        out = self._out(3.0, {**self.SERVO, **self.LIMITS})
        self.assertEqual(out["iso_zone"].value, "B", "按自定 2.0/3.5/5.0，不按附录 C（附录 C 下是 C）")
        self.assertEqual(out["status"].value, "attention")

    def test_自定门槛填错不退回附录C缺省(self):
        out = self._out(1.5, {**self.SERVO, "limitAttention": "2.0", "limitWarning": "3.5"})
        self.assertIs(out["iso_zone"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("三条都填且递增", out["evidence"].value)
        self.assertNotIn("41850.1", out["evidence"].value)

    def test_附录C缺省值与说明出处(self):
        import sys   # noqa: PLC0415
        v = sys.modules[type(self.d.instance).__module__]
        self.assertEqual(v.ANNEX_C_LOWER, (0.71, 1.8, 4.5))
        desc = {p.key: p for p in self.d.declaration.params}["limitAttention"].description
        for s in ("GB/T 41850.1-2024", "ISO 20816-1:2016", "表 C.1", "0.71 / 1.8 / 4.5",
                  "注 1", "注 2", "功率不大于 15 kW", "资料性", "B 区为正常"):
            self.assertIn(s, desc, s)

    def test_自定门槛声明随诊断填且无缺省(self):
        specs = {p.key: p for p in self.d.declaration.params}
        for k in ("limitAttention", "limitWarning", "limitDanger"):
            self.assertEqual(specs[k].level, "position", k)
            self.assertEqual(specs[k].default, "", k)
            self.assertFalse(specs[k].required, k)
