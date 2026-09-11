"""类型层的回归 —— 钉的是"'只填 V'在代码层不可能"。

★这些用例的价值不在"证明它能用"，而在**每一条都对应一个真实事故或一条铁律**：
  把它们改成宽松的实现，用例必须变红。
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

from aiintegration.quality import COLLAPSED_TO_BAD, Quality, STATUS_OK, STATUS_QUALITY_BAD
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec, Sample,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


class TestFindingEnforcesVQT(unittest.TestCase):
    """V/Q/T 缺一不可。"""

    def test_三者齐全才构造得出来(self):
        f = Finding(key="health_score", value=88.5, quality=Quality.OK, t=T0)
        self.assertEqual(f.value, 88.5)
        self.assertIs(f.quality, Quality.OK)
        self.assertEqual(f.t, T0)

    def test_缺质量或时刻直接构造不出来(self):
        # 没有默认值 ⇒ 少给一个就是 TypeError。这正是"只填 V 在代码层不可能"。
        with self.assertRaises(TypeError):
            Finding(key="health_score", value=88.5)          # 缺 Q 和 T
        with self.assertRaises(TypeError):
            Finding(key="health_score", value=88.5, quality=Quality.OK)  # 缺 T

    def test_质量不接受裸字符串(self):
        # 裸字符串正是"质量码被随手编出来"的方式。
        with self.assertRaises(TypeError):
            Finding(key="k", value=1.0, quality="ok", t=T0)

    def test_裸datetime被拒(self):
        # 裸 datetime 在本机看着对，跨时区/跨机就错位，且不报错。
        with self.assertRaises(ValueError):
            Finding(key="k", value=1.0, quality=Quality.OK, t=datetime(2026, 9, 10, 12, 0))

    def test_带时区的非UTC会被归一到UTC(self):
        tz8 = timezone(timedelta(hours=8))
        f = Finding(key="k", value=1.0, quality=Quality.OK,
                    t=datetime(2026, 9, 10, 20, 0, tzinfo=tz8))
        self.assertEqual(f.t, T0)


class TestFindingRejectsV4Bug(unittest.TestCase):
    """★v4 那条教训的正面拦截：NaN/Inf 不许伪装成合法值。"""

    def test_OK质量下NaN被拒而不是被改写成0(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    Finding(key="rms", value=bad, quality=Quality.OK, t=T0)
                self.assertIn("NaN/Inf", str(ctx.exception))

    def test_OK质量下不许没有值(self):
        with self.assertRaises(ValueError):
            Finding(key="rms", value=None, quality=Quality.OK, t=T0)

    def test_坏质量允许没有值(self):
        f = Finding(key="rms", value=None, quality=Quality.NO_INPUT, t=T0)
        self.assertIsNone(f.value)

    def test_坏质量下的NaN被抹成None不让它流下去(self):
        # 下游画 NaN 会变成断点或 0，两种都在说谎。
        f = Finding(key="rms", value=float("nan"), quality=Quality.COMPUTE_ERROR, t=T0)
        self.assertIsNone(f.value)


class TestQualityMapping(unittest.TestCase):
    def test_OK映射到1(self):
        self.assertEqual(Quality.OK.to_status_code(), STATUS_OK)

    def test_每一档都有映射(self):
        for q in Quality:
            self.assertIsInstance(q.to_status_code(), int)

    def test_对不上的折叠成QualityBad而不是挪用别的码(self):
        # 挪用既有码（如 -1028 已有"算术族补满栅格"的既定含义）不会报错，
        # 只会让对端按错的意思处置 —— 故一律折叠 -1000。
        self.assertIn(Quality.INSUFFICIENT_SAMPLES, COLLAPSED_TO_BAD)
        for q in COLLAPSED_TO_BAD:
            self.assertEqual(q.to_status_code(), STATUS_QUALITY_BAD)

    def test_配置不全用ConfigError而不是OutOfService(self):
        """★不是洁癖：`-1007` 在 hs 读路径里已经是"采集中断的延续"。

        给同一个码，界面就分不开"去补台账"与"去查设备"，而这两件事的处置正相反。
        由 AICloud `C-10 §2` 逮到我方 AI-12 那一版的自相矛盾（要求对端分开显示，却给同一个码）。
        """
        self.assertEqual(Quality.CONFIG_INCOMPLETE.to_status_code(), -1001)
        self.assertNotEqual(Quality.CONFIG_INCOMPLETE.to_status_code(),
                            Quality.MODEL_NOT_LOADED.to_status_code())

    def test_没有两档共用同一个非折叠码(self):
        """折叠进 QualityBad 的那几档是**刻意**共用；其余每一档必须各占一个码。

        ★共用一个码 = 对端无法按码分流，只能去读文本 —— 而文本是写给人的，
          拿它当程序判据，措辞一改对端就分错类，且不报错。
        """
        distinct = [q for q in Quality if q not in COLLAPSED_TO_BAD]
        codes = [q.to_status_code() for q in distinct]
        self.assertEqual(len(codes), len(set(codes)),
                         f"有两档撞码了：{[(q.name, q.to_status_code()) for q in distinct]}")

    def test_只有OK算好(self):
        self.assertTrue(Quality.OK.is_good())
        for q in Quality:
            if q is not Quality.OK:
                self.assertFalse(q.is_good())


class TestDeclaration(unittest.TestCase):
    def test_输出为空的域没有意义(self):
        with self.assertRaises(ValueError):
            Declaration(inputs=(), outputs=())

    def test_重复的角色名或结论名被拒(self):
        o = OutputSpec(key="a", display="A", value_type="float")
        with self.assertRaises(ValueError):
            Declaration(inputs=(), outputs=(o, o))
        i = InputSpec(role="x")
        with self.assertRaises(ValueError):
            Declaration(inputs=(i, i), outputs=(o,))

    def test_值类型闭集(self):
        with self.assertRaises(ValueError):
            OutputSpec(key="a", display="A", value_type="双精度")


class TestFrame(unittest.TestCase):
    def test_时间区间倒挂被拒(self):
        with self.assertRaises(ValueError):
            Frame(domain="d", binding="b", t_start=T0,
                  t_end=T0 - timedelta(seconds=1), channels={})

    def test_采样点也要带时区(self):
        with self.assertRaises(ValueError):
            Sample(t=datetime(2026, 9, 10, 12, 0), value=1.0, quality=Quality.OK)


if __name__ == "__main__":
    unittest.main()
