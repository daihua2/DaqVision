"""`OutputSpec.stop_behavior` 必须与域**停机时的真实行为**逐条对得上（契约 1.9）。

★这条用例是本轮最要紧的一条，理由在 AICloud `C-45 §3.4`：

  我方 `AI-49` 要求界面"凭 `run_state` 把停机时的数值结论置灰"，
  `AI-51 §3` 又立规矩"不要按域名写死" —— 而当时 `OutputSpec` 里没有这个信息，
  前端只能把点名单抄进去。**两封函自相矛盾**，由对方点破。

  加了 `stop_behavior` 之后，新的失败方式是**它与代码不一致**：
  声明说"停机时不写"，代码却写了（或反过来）。那种错同样**不报错** ——
  界面照声明置灰，而点上其实有新值，或反之把有效值灰掉。
  ⇒ 所以不能只断言"字段有值"，必须**真跑一帧停机的、逐条比对**。

钉的：
  ① 声明 `not_written` 的，停机那一帧**确实没出现在结论里**；
  ② 声明 `literal_stopped` 的，确实写了，且写的值**在 choices 里**；
  ③ 声明 `written` 的，停机时照样写；
  ④ 两个振动域的 `run_state` 取值域自述与实际写出的值一致。
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.types import (
    STOP_LITERAL, STOP_NOT_WRITTEN, STOP_WRITTEN, ArtifactBlob, Frame, Sample,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)
DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"

#: 停机门槛远高于下面喂的速度值 ⇒ 必然判停机。
STOPPED_PARAMS = {
    "stop_threshold": "100",
    "axial_axis": "x",
    "iso_group": "1",
    "mount_type": "rigid",
    "rated_power_kw": "100",
    "rated_speed_rpm": "1500",
    "vel_is_rms": "yes",
}


def _load(key):
    loaded, failed = discover(DOMAINS_DIR)
    bad = [(p, e) for p, e in failed if p.stem == key]
    assert not bad, f"{key} 装载失败：{bad}"
    return {d.key: d for d in loaded}[key]


def _frame(domain, roles, params):
    return Frame(domain=domain, binding="dev1",
                 t_start=T0 - timedelta(seconds=60), t_end=T0,
                 channels={r: [Sample(t=T0, value=0.1, quality=Quality.OK)] for r in roles},
                 params=dict(params))


class StopBehaviorBase(unittest.TestCase):
    key = ""
    extra_artifacts: dict = {}

    def declared(self):
        return {o.key: o for o in _load(self.key).declaration.outputs}

    def stopped_findings(self):
        d = _load(self.key)
        roles = [i.role for i in d.declaration.inputs if i.kind == "point"]
        frame = _frame(self.key, roles, STOPPED_PARAMS)
        if self.extra_artifacts:
            frame = Frame(domain=frame.domain, binding=frame.binding,
                          t_start=frame.t_start, t_end=frame.t_end,
                          channels=frame.channels, params=frame.params,
                          artifacts=self.extra_artifacts)
        out = d.instance.infer(frame)
        return {f.key: f for f in out}

    def check(self):
        decl = self.declared()
        got = self.stopped_findings()
        self.assertEqual(got.get("run_state").value, "停机",
                         "这一帧没判成停机，后面的比对就没有意义")
        for key, spec in decl.items():
            with self.subTest(output=key):
                if spec.stop_behavior == STOP_NOT_WRITTEN:
                    self.assertNotIn(
                        key, got,
                        f"{key} 声明停机时不写，实际却写了 —— "
                        "界面会按声明把它灰掉，而点上其实有新值")
                else:
                    self.assertIn(
                        key, got,
                        f"{key} 声明停机时要写（{spec.stop_behavior}），实际没写 —— "
                        "界面不会置灰，会把停机前的旧值当成当前值")
                if spec.stop_behavior == STOP_LITERAL:
                    self.assertTrue(spec.choices, f"{key} 是 literal_stopped 却没有 choices")
                    self.assertIn(
                        str(got[key].value), [str(c) for c in spec.choices],
                        f"{key} 停机时写的值不在它自己声明的 choices 里")


class TestVibrationIso(StopBehaviorBase):
    key = "vibration_iso"

    def test_停机时各结论点与stop_behavior逐条对得上(self):
        self.check()

    def test_三档都用到了(self):
        """★若哪天有人把所有输出一律标成 written，上面那条会全绿 —— 这条挡住它。"""
        kinds = {o.stop_behavior for o in self.declared().values()}
        self.assertEqual(kinds, {STOP_WRITTEN, STOP_LITERAL, STOP_NOT_WRITTEN})

    def test_烈度区数值停机时写0且0在取值域里(self):
        spec = self.declared()["iso_zone_code"]
        self.assertEqual(self.stopped_findings()["iso_zone_code"].value, 0)
        self.assertIn("0", spec.choices, "iso_zone_code 加了取值 0 却没写进 choices")

    def test_run_state取值域自述与实际一致(self):
        spec = self.declared()["run_state"]
        self.assertEqual(set(spec.choices), {"运行", "停机", "未判"})


class TestVibrationBaseline(StopBehaviorBase):
    key = "vibration_baseline"

    def test_停机时各结论点与stop_behavior逐条对得上(self):
        self.check()

    def test_四条数值结论停机时都不写(self):
        decl = self.declared()
        for k in ("vel_z_max", "ratio_drift", "temp_rise", "anomaly_score"):
            self.assertEqual(decl[k].stop_behavior, STOP_NOT_WRITTEN, k)

    def test_run_state取值域自述与实际一致(self):
        self.assertEqual(set(self.declared()["run_state"].choices),
                         {"运行", "停机", "未判"})


class TestGrouping(unittest.TestCase):
    """第二测点必须自述成一组，否则界面拦不住"配了 X 漏了 Z"（C-45 §3.1）。"""

    def test_两个振动域的第二测点都成组(self):
        for key in ("vibration_iso", "vibration_baseline"):
            with self.subTest(domain=key):
                ins = _load(key).declaration.inputs
                # 角色名形如 x_vel / x2_vel / temp / temp2 —— 第二测点的带 "2"
                second = [i for i in ins if "2" in i.role]
                self.assertTrue(second, "没找到第二测点的角色")
                self.assertGreaterEqual(len(second), 3, f"第二测点只认出 {len(second)} 个角色")
                for i in second:
                    self.assertEqual(i.group, "point2", f"{i.role} 没标进第二测点组")
                    self.assertEqual(i.group_display, "第二测点")
                    self.assertFalse(i.required, f"{i.role} 成组可选却标了必填")
                # 测点 1 不该被卷进组里
                for i in ins:
                    if "2" not in i.role:
                        self.assertEqual(i.group, "", f"{i.role} 不该属于第二测点组")


class TestStopThresholdSelfDescription(unittest.TestCase):
    """`stop_threshold` 的三件自述（C-45 §3.2/§3.3）—— 两个域必须一致。"""

    def test_无缺省留空不评且下限为正(self):
        for key in ("vibration_iso", "vibration_baseline"):
            with self.subTest(domain=key):
                p = {x.key: x for x in _load(key).declaration.params}["stop_threshold"]
                self.assertFalse(p.has_default,
                                 "★界面会据此替它预置一个值 —— 那会静默停止诊断")
                self.assertEqual(p.default, "")
                self.assertEqual(p.blank_meaning, "not_evaluated")
                self.assertEqual(p.min, "0", "没有下限，界面拦不住 -1，要到采基线才失败")


class TestOutputSpecGuards(unittest.TestCase):
    """类型层的两道闸 —— 声明自相矛盾时**当场炸**，而不是等界面表现出怪样子。"""

    def test_literal_stopped必须带取值域(self):
        from aiintegration.types import OutputSpec
        with self.assertRaises(ValueError):
            OutputSpec(key="x", display="x", value_type="string",
                       stop_behavior=STOP_LITERAL)

    def test_不认识的stop_behavior当场拒(self):
        from aiintegration.types import OutputSpec
        with self.assertRaises(ValueError):
            OutputSpec(key="x", display="x", value_type="string", stop_behavior="随便写的")

    def test_choice_displays错位当场拒(self):
        from aiintegration.types import OutputSpec
        with self.assertRaises(ValueError):
            OutputSpec(key="x", display="x", value_type="string",
                       choices=("a", "b"), choice_displays=("甲",))

    def test_给了缺省却说没有缺省当场拒(self):
        from aiintegration.types import ParamSpec
        with self.assertRaises(ValueError):
            ParamSpec(key="x", display="x", value_type="string", default="abc")


if __name__ == "__main__":
    unittest.main()
