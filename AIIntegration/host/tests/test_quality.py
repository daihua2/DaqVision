"""入向质量码判定的回归。

写这个文件是因为 2026-09-12 查出：`from_status_code` 只认 `Ok(1)`，
而**实时库点值的好码是 `QualityGood(5000)`** —— 于是每一笔真实输入都被判成坏。
此前本模块的入向映射**一条用例都没有**，测试夹具又恰好用 `code=1` 造数据，
两头一致地错，测试全绿也照样漏。⇒ 这里的用例一律**对着码表现取的值**钉，不写字面量魔数。

钉四条：
  · 点值好码 `QualityGood(5000)` 必须算可信（曾经判坏的那一格）；
  · `Ok(1)` 仍算可信（别处「原值 1=Ok」的现网用法）；
  · `QualityPending(5004)` 算可信（每个点末拍恒为它，值是真实采样）；
  · Bad 族（全部负码）一律不可信；Uncertain 族不算可信（不替上游发明语义）。
"""

import unittest

from aiintegration.fetch import vqt_to_sample
from aiintegration.hsproto import daqcontract_pb2 as daq
from aiintegration.quality import (
    STATUS_OK, STATUS_QUALITY_BAD, STATUS_QUALITY_GOOD, STATUS_QUALITY_PENDING,
    TRUSTED_INPUT_CODES, Quality,
)

CODE = dict(daq.StatusCode.items())  # ★现取，不抄


class TestConstantsMatchContract(unittest.TestCase):
    """常量必须等于契约里的值 —— 写错一个数，下面所有判定都跟着错。"""

    def test_constants(self):
        self.assertEqual(STATUS_OK, CODE["Ok"])
        self.assertEqual(STATUS_QUALITY_GOOD, CODE["QualityGood"])
        self.assertEqual(STATUS_QUALITY_PENDING, CODE["QualityPending"])
        self.assertEqual(STATUS_QUALITY_BAD, CODE["QualityBad"])

    def test_good_is_not_ok(self):
        """★这两个不是一回事 —— 混为一谈正是那个缺陷的成因。"""
        self.assertNotEqual(CODE["Ok"], CODE["QualityGood"])


class TestTrusted(unittest.TestCase):

    def test_quality_good_is_trusted(self):
        """曾经判坏的那一格：实时库点值的好码。"""
        self.assertIs(Quality.from_status_code(CODE["QualityGood"]), Quality.OK)

    def test_ok_is_trusted(self):
        self.assertIs(Quality.from_status_code(CODE["Ok"]), Quality.OK)

    def test_pending_is_trusted(self):
        """末拍恒为 Pending；判它坏 = 每帧丢最后一笔真实采样。"""
        self.assertIs(Quality.from_status_code(CODE["QualityPending"]), Quality.OK)

    def test_trusted_set_is_exactly_these_three(self):
        self.assertEqual(TRUSTED_INPUT_CODES,
                         frozenset({CODE["Ok"], CODE["QualityGood"], CODE["QualityPending"]}))


class TestNotTrusted(unittest.TestCase):

    def test_every_negative_code_is_bad(self):
        """Bad 族全列（76 个负码）一个都不许漏进可信。"""
        neg = [v for v in CODE.values() if v < 0]
        self.assertGreater(len(neg), 50, "码表没取到，用例本身失效了")
        for v in neg:
            with self.subTest(code=v):
                self.assertIs(Quality.from_status_code(v), Quality.INPUT_BAD)

    def test_uncertain_family_not_trusted(self):
        """不替上游发明「可不可信」：Uncertain 族一律不算可信。"""
        for name in ("QualityInitial", "QualityNo", "QualityLocalOverride",
                     "QualitySubstituted", "Uncertain", "QualityUncertain",
                     "QualityLastUsable", "QualityEI"):
            with self.subTest(name=name):
                self.assertIs(Quality.from_status_code(CODE[name]), Quality.INPUT_BAD)

    def test_zero_is_bad(self):
        self.assertIs(Quality.from_status_code(CODE["None"]), Quality.INPUT_BAD)


class TestEndToEndVQT(unittest.TestCase):
    """端到端：一笔**和现场同形**的 VQT（值在 R4、码是 5000）必须落成可信样本。

    两个坑合在一条用例里：预设 `R8` 会取到 0.0；只认 `1` 会判成坏。
    """

    def _vqt(self, *, code, r4=None):
        v = daq.VQT(TagId=810)
        v.Quality.Code = code
        v.TimStampUtc.GetCurrentTime()
        if r4 is None:
            v.NullValue = True
        else:
            v.R4 = r4
        return v

    def test_r4_good_sample(self):
        s = vqt_to_sample(self._vqt(code=CODE["QualityGood"], r4=0.4633))
        self.assertIs(s.quality, Quality.OK)
        self.assertAlmostEqual(s.value, 0.4633, places=4)   # ★值不是 0.0
        self.assertEqual(s.status_code, CODE["QualityGood"])

    def test_r4_pending_sample(self):
        s = vqt_to_sample(self._vqt(code=CODE["QualityPending"], r4=26.4759))
        self.assertIs(s.quality, Quality.OK)
        self.assertAlmostEqual(s.value, 26.4759, places=4)

    def test_dead_point_sample(self):
        """原选 sim 组那 12 天的形态：NullValue + QualityBad。"""
        s = vqt_to_sample(self._vqt(code=CODE["QualityBad"]))
        self.assertIs(s.quality, Quality.INPUT_BAD)
        self.assertIsNone(s.value)
        self.assertEqual(s.status_code, CODE["QualityBad"])


if __name__ == "__main__":
    unittest.main()
