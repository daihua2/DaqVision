"""来源性质（仿真/现场）的回归 —— 契约 1.6，AICloud `C-36 §4`。

这一格只回答一件事：**这条结论能不能用来说现场设备的话。**
所以钉的全是"会把不该当现场的东西当成现场"的那几种：

  ① **空 ≠ 现场** —— 老绑定、老样本、老工件读出来是空，不许在任何一层变成 `field`；
  ② **取最严** —— 混了一段仿真，工件那一格就是 `simulated`；
  ③ **快照不被反写** —— 真机接入后把绑定改成 `field`，当初用仿真数据训的工件不许跟着变；
  ④ **兜底只朝严的方向** —— 快照缺失时按绑定当前值兜底，但**绝不可能**兜出一个假的 `field`；
  ⑤ 不是封闭枚举 —— 骨架不认得的新取值原样透传，不吞、不纠错。
"""

import unittest

from aiintegration import dataorigin as do


class TestNormalize(unittest.TestCase):
    def test_空与空白一律是未声明(self):
        for v in (None, "", "   ", "\t"):
            self.assertEqual(do.normalize(v), do.UNDECLARED)

    def test_不认得的取值原样留着(self):
        # 不是封闭枚举：新取值随时加，骨架不该替对端纠错。
        self.assertEqual(do.normalize("  lab_rig "), "lab_rig")


class TestIsField(unittest.TestCase):
    def test_只有明写field才算现场(self):
        self.assertTrue(do.is_field("field"))
        for v in (None, "", "simulated", "Field", "现场", "lab_rig"):
            self.assertFalse(do.is_field(v), f"{v!r} 不该被当成现场")


class TestFold(unittest.TestCase):
    def test_全是现场才是现场(self):
        self.assertEqual(do.fold(["field", "field", "field"]), do.FIELD)

    def test_混进一段仿真就是仿真(self):
        # ★C-36 §4.2.2 点名的那条：十段里有一段是仿真，这个工件就不能说现场的话。
        self.assertEqual(do.fold(["field"] * 9 + ["simulated"]), do.SIMULATED)

    def test_仿真压过未声明(self):
        self.assertEqual(do.fold(["", "simulated", ""]), do.SIMULATED)

    def test_现场混未声明退回未声明而不是现场(self):
        # ★最要紧的一条：不知道就说不知道，**不许**因为"大部分是现场"就报现场。
        self.assertEqual(do.fold(["field", "", "field"]), do.UNDECLARED)

    def test_一条样本都没有是未声明(self):
        self.assertEqual(do.fold([]), do.UNDECLARED)

    def test_单一的陌生取值原样透传(self):
        self.assertEqual(do.fold(["lab_rig", "lab_rig"]), "lab_rig")

    def test_陌生取值混了现场就退回未声明(self):
        self.assertEqual(do.fold(["lab_rig", "field"]), do.UNDECLARED)


class TestEffective(unittest.TestCase):
    def test_有快照就用快照_绑定现在说什么都不算(self):
        # ★真机接入后绑定改成 field，当初入集的仿真样本仍是仿真。
        self.assertEqual(do.effective("simulated", "field"), do.SIMULATED)
        self.assertEqual(do.effective("field", "simulated"), do.FIELD)

    def test_快照为空时绑定说仿真则按仿真兜底(self):
        self.assertEqual(do.effective("", "simulated"), do.SIMULATED)

    def test_快照为空时绝不兜出现场(self):
        # ★这条是整个兜底能成立的前提：它只可能把"不知道"抬成"仿真"，
        #   不可能把"不知道"说成"现场"。
        self.assertEqual(do.effective("", "field"), do.UNDECLARED)
        self.assertEqual(do.effective("", ""), do.UNDECLARED)
        self.assertEqual(do.effective(None, None), do.UNDECLARED)

    def test_兜底不会产出field(self):
        # 穷举一遍：任何 (快照, 绑定) 组合，只要快照不是 field，结果就不可能是 field。
        vals = (None, "", "simulated", "field", "lab_rig")
        for snap in vals:
            for now in vals:
                got = do.effective(snap, now)
                if do.normalize(snap) != do.FIELD:
                    self.assertNotEqual(got, do.FIELD,
                                        f"快照={snap!r} 绑定={now!r} 兜出了现场")


if __name__ == "__main__":
    unittest.main()
