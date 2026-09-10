"""域插件宿主的回归 —— 钉的是"丢一个 .py 就多一个域，且坏模块不拖垮别人"。"""

import tempfile
import unittest
from pathlib import Path

from aiintegration.domains import CAPABILITIES, discover

GOOD = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec, Finding
from aiintegration.quality import Quality

class VibLow(Domain):
    key = "vibration_lowfreq"
    display = "低频振动诊断"
    version = "1.0.0"

    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x_acc", unit="g"),),
            outputs=(OutputSpec(key="health_score", display="健康分", value_type="float"),),
        )

    def infer(self, frame):
        return [Finding(key="health_score", value=90.0,
                        quality=Quality.OK, t=frame.t_end)]
'''

WITH_TRAIN = GOOD.replace('key = "vibration_lowfreq"', 'key = "with_train"').replace(
    "    def infer(self, frame):",
    "    def train(self, dataset, report):\n        return None\n\n    def infer(self, frame):",
)

BAD_KEY = GOOD.replace('key = "vibration_lowfreq"', 'key = "Bad Key!"')
BROKEN = "raise RuntimeError('模块自己炸了')\n"
UNKNOWN_CAP = GOOD.replace('key = "vibration_lowfreq"', 'key = "unknown_cap"').replace(
    "    def infer(self, frame):",
    "    def capabilities(self):\n        return {'infer', 'teleport'}\n\n    def infer(self, frame):",
)


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, body: str) -> Path:
        p = self.dir / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_丢一个py就多一个域(self):
        self.write("viblow.py", GOOD)
        loaded, failed = discover(self.dir)
        self.assertEqual([d.key for d in loaded], ["vibration_lowfreq"])
        self.assertEqual(failed, [])

    def test_能力位按实现了哪些方法推断(self):
        self.write("viblow.py", GOOD)
        self.write("wt.py", WITH_TRAIN)
        loaded, _ = discover(self.dir)
        caps = {d.key: d.caps for d in loaded}
        self.assertEqual(caps["vibration_lowfreq"], frozenset({"infer"}))
        self.assertEqual(caps["with_train"], frozenset({"infer", "train"}))

    def test_describe给前端的形状里有能力位(self):
        self.write("viblow.py", GOOD)
        loaded, _ = discover(self.dir)
        d = loaded[0].describe()
        self.assertEqual(d["key"], "vibration_lowfreq")
        self.assertEqual(d["capabilities"], ["infer"])
        # 前端按 capabilities 渲染，不按 key 分支 —— 所以这个字段必须在。
        self.assertIn("capabilities", d)

    def test_一个模块坏了不拖垮别的(self):
        self.write("viblow.py", GOOD)
        self.write("boom.py", BROKEN)
        loaded, failed = discover(self.dir)
        self.assertEqual([d.key for d in loaded], ["vibration_lowfreq"])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0].name, "boom.py")

    def test_非法key被拒(self):
        self.write("bad.py", BAD_KEY)
        loaded, failed = discover(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("key 不合法", str(failed[0][1]))

    def test_未知能力位被拒而不是静默忽略(self):
        # 未知位到了前端就是"渲染不出来的能力"，而前端不会报错。
        self.write("uc.py", UNKNOWN_CAP)
        loaded, failed = discover(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("未知能力位", str(failed[0][1]))

    def test_key重复被拒(self):
        self.write("a.py", GOOD)
        self.write("b.py", GOOD)
        loaded, failed = discover(self.dir)
        self.assertEqual(len(loaded), 1)
        self.assertIn("重复", str(failed[0][1]))

    def test_下划线开头的文件被跳过(self):
        self.write("_helper.py", BROKEN)
        loaded, failed = discover(self.dir)
        self.assertEqual((loaded, failed), ([], []))

    def test_目录不存在不报错只是没有域(self):
        loaded, failed = discover(self.dir / "nope")
        self.assertEqual((loaded, failed), ([], []))

    def test_已知能力位有文字定义(self):
        # 名字是契约：每个位都要有一句定义，改义要发函。
        for name, doc in CAPABILITIES.items():
            self.assertTrue(doc.strip(), f"能力位 {name} 缺定义")


if __name__ == "__main__":
    unittest.main()
