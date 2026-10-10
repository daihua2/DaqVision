"""取值色调 `OutputSpec.choice_tones`（契约 1.19，AICloud `C-91`）。

钉的几条：
  · 与 `choices` 同序、长度必须相同；词只能取自 `TONES` 或空串 —— 错位或自造词，平台上错色且不报错；
  · 骨架经 `ListDomains` 原样透传；
  · 振动诊断的烈度区按 A 绿 / B 蓝 / C 橙 / D 红 / 停机默认色声明，且只有它上色。
"""

import tempfile
import unittest
from pathlib import Path

from aiintegration.api import ApiService
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import BindingStore
from aiintegration.domains import discover
from aiintegration.logstore import LogStore
from aiintegration.types import TONES, OutputSpec

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"


class TestSpec(unittest.TestCase):
    def mk(self, tones, choices=("A", "B")):
        return OutputSpec(key="z", display="区", value_type="string", choices=choices,
                          choice_tones=tones)

    def test_不上色是缺省(self):
        self.assertEqual(OutputSpec(key="z", display="区", value_type="string").choice_tones, ())

    def test_同序且长度相同才收(self):
        self.assertEqual(self.mk(("success", "info")).choice_tones, ("success", "info"))
        with self.assertRaises(ValueError):
            self.mk(("success",))

    def test_词表外的词拒收_空串同default收(self):
        self.mk(("", "default"))
        with self.assertRaises(ValueError):
            self.mk(("success", "blue"))

    def test_词表与C91一致(self):
        self.assertEqual(TONES, ("success", "info", "warning", "error", "default"))


class TestVibrationAndApi(unittest.TestCase):
    def setUp(self):
        loaded, failed = discover(DOMAINS_DIR)
        self.domains = {d.key: d for d in loaded}
        self.assertIn("vibration", self.domains, failed)
        self._tmp = tempfile.TemporaryDirectory()
        self.bindings = BindingStore(Path(self._tmp.name) / "b.db")

    def tearDown(self):
        self.bindings.close(); self._tmp.cleanup()

    def test_烈度区按C91上色_其余不上色(self):
        outs = {o.key: o for o in self.domains["vibration"].declaration.outputs}
        z = outs["iso_zone"]
        self.assertEqual(dict(zip(z.choices, z.choice_tones)),
                         {"A": "success", "B": "info", "C": "warning", "D": "error", "停机": "default"})
        others = [k for k, o in outs.items() if o.choice_tones and k != "iso_zone"]
        self.assertEqual(others, [], "C-91 只请烈度区上色")

    def test_ListDomains原样透传(self):
        svc = ApiService(guid="g", version="0", logstore=LogStore(capacity=10),
                         domains={"vibration": self.domains["vibration"]}, bindings=self.bindings)
        d = svc.ListDomains(pb.DomainsRequest(), None).domains[0]
        got = {o.key: list(o.choice_tones) for o in d.outputs}
        self.assertEqual(got["iso_zone"], ["success", "info", "warning", "error", "default"])
        self.assertEqual(got["dominant_axis"], [])


if __name__ == "__main__":
    unittest.main()
