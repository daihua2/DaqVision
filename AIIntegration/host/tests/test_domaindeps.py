"""安装期「缺依赖的域不铺」的回归。

写这个文件是因为 2026-09-12 的实账：发布件带着仓里所有域，`install.sh` 的「只补不删」
把视觉域也铺到了现场，而发布件不带 `cv2` ⇒ 现场 `GetInfo.load_errors` 常驻一条，
AICloud 界面上是一条永远红的装载错误。手工移走文件只治标，**下次部署它照样回来**。

钉五条：
  · 读 `REQUIRES` **不 import 域模块**（import 了就当场炸在缺的那个包上）；
  · 只认**模块顶层**的 `REQUIRES`，函数/类里的同名赋值不算；
  · 依赖齐 → 铺；缺一个就 → 不铺，且把缺哪个说出来；
  · 没有 `REQUIRES` = 零第三方依赖 → 照铺；
  · **只补不删**：跳过不铺，绝不删掉现场已有的同名文件。
"""

import io
import tempfile
import unittest
from pathlib import Path

from aiintegration.domaindeps import (
    install, missing_modules, parse_requires, plan,
)

ABSENT = "定_不_存_在_的_包_xyz"          # 故意用非 ASCII：顺便钉住不是按 ASCII 名判的


def _tmpdir(case: unittest.TestCase) -> Path:
    """临时目录。★不用 `TestCase.enterContext` —— 那是 **3.11+**，而现场是 3.10.12。"""
    d = tempfile.TemporaryDirectory()
    case.addCleanup(d.cleanup)
    return Path(d.name)


class TestParseRequires(unittest.TestCase):

    def _write(self, text: str) -> Path:
        d = _tmpdir(self)
        p = d / "d.py"
        p.write_text(text, encoding="utf-8")
        return p

    def test_tuple(self):
        self.assertEqual(parse_requires(self._write('REQUIRES = ("cv2", "numpy")\n')),
                         ("cv2", "numpy"))

    def test_list_and_str(self):
        self.assertEqual(parse_requires(self._write('REQUIRES = ["a"]\n')), ("a",))
        self.assertEqual(parse_requires(self._write('REQUIRES = "solo"\n')), ("solo",))

    def test_absent_means_no_third_party_dep(self):
        self.assertEqual(parse_requires(self._write("x = 1\n")), ())

    def test_only_module_level(self):
        """函数体里的同名赋值不算 —— 否则会把不相干的局部变量当依赖。"""
        src = 'def f():\n    REQUIRES = ("cv2",)\n    return REQUIRES\n'
        self.assertEqual(parse_requires(self._write(src)), ())

    def test_does_not_import_the_module(self):
        """★关键：模块顶层 import 一个不存在的包 + 顶层就抛异常，仍要读得出 REQUIRES。"""
        src = (f'REQUIRES = ("{ABSENT}",)\n'
               f'import {ABSENT}\n'
               'raise RuntimeError("这行绝不该被执行")\n')
        self.assertEqual(parse_requires(self._write(src)), (ABSENT,))

    def test_broken_syntax_is_not_a_death_sentence(self):
        """语法坏了是另一类问题，交给运行时装载器报 load_errors，这里不替它判。"""
        self.assertEqual(parse_requires(self._write("def (:\n")), ())

    def test_missing_file(self):
        d = _tmpdir(self)
        self.assertEqual(parse_requires(d / "没有这个文件.py"), ())


class TestMissingModules(unittest.TestCase):

    def test_stdlib_present(self):
        self.assertEqual(missing_modules(("json", "sys", "pathlib")), [])

    def test_absent_reported(self):
        self.assertEqual(missing_modules((ABSENT,)), [ABSENT])

    def test_mixed_and_dedup(self):
        self.assertEqual(missing_modules(("json", ABSENT, ABSENT)), [ABSENT])

    def test_empty(self):
        self.assertEqual(missing_modules(()), [])


class TestInstall(unittest.TestCase):

    def setUp(self):
        self.src = _tmpdir(self)
        self.dst = _tmpdir(self)
        (self.src / "plain.py").write_text("# 零第三方依赖\n", encoding="utf-8")
        (self.src / "needs.py").write_text(f'REQUIRES = ("{ABSENT}",)\n', encoding="utf-8")
        (self.src / "ok_dep.py").write_text('REQUIRES = ("json",)\n', encoding="utf-8")
        (self.src / "__init__.py").write_text("\n", encoding="utf-8")

    def test_lays_satisfied_skips_missing(self):
        buf = io.StringIO()
        laid, skipped = install(self.src, self.dst, out=buf)
        self.assertEqual((laid, skipped), (2, 1))
        got = sorted(p.name for p in self.dst.glob("*.py"))
        self.assertEqual(got, ["ok_dep.py", "plain.py"])     # needs.py 没铺
        self.assertNotIn("__init__.py", got)                 # 下划线开头的不是域

    def test_says_which_dep_is_missing(self):
        """只说"跳过"不够 —— 现场要能一眼看出缺哪个包。"""
        buf = io.StringIO()
        install(self.src, self.dst, out=buf)
        text = buf.getvalue()
        self.assertIn("needs.py", text)
        self.assertIn(ABSENT, text)

    def test_only_add_never_delete(self):
        """★只补不删：现场已有的同名域，跳过时绝不删它。"""
        stale = self.dst / "needs.py"
        stale.write_text("# 现场手工放的旧版\n", encoding="utf-8")
        install(self.src, self.dst, out=io.StringIO())
        self.assertTrue(stale.is_file())
        self.assertIn("现场手工放的旧版", stale.read_text(encoding="utf-8"))

    def test_plan_is_sorted_and_reproducible(self):
        names = [p.name for p, _, _ in plan(self.src)]
        self.assertEqual(names, sorted(names))

    def test_missing_src_dir(self):
        self.assertEqual(plan(Path("/一个/不存在的/目录")), [])


class TestRealVisionDomainDeclaresDeps(unittest.TestCase):
    """钉住仓里那个真域：视觉域必须自述依赖，否则又会被铺到没装 cv2 的现场。"""

    def test_vision_helmet_requires(self):
        here = Path(__file__).resolve().parents[2] / "domains" / "vision_helmet.py"
        if not here.is_file():
            self.skipTest("仓布局变了，跳过")
        self.assertEqual(set(parse_requires(here)), {"cv2", "numpy", "onnxruntime"})

    def test_vibration_lowfreq_has_no_third_party_dep(self):
        here = Path(__file__).resolve().parents[2] / "domains" / "vibration_lowfreq.py"
        if not here.is_file():
            self.skipTest("仓布局变了，跳过")
        self.assertEqual(parse_requires(here), ())


if __name__ == "__main__":
    unittest.main()
