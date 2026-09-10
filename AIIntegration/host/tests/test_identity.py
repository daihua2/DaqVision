"""身份的回归 —— 钉的是"永不重生成"。

每一条对应一种"会悄悄换掉身份"的走法。换身份的后果：先前写入的点变成无主数据，
而按对账铁律**绝不自动删**，残留清不掉。
"""

import tempfile
import unittest
import uuid
from pathlib import Path

from aiintegration.identity import GuidError, SystemGuid


class TestSystemGuid(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.a = base / "app" / "system.guid"
        self.b = base / "etc" / "system.guid"

    def tearDown(self):
        self._tmp.cleanup()

    def sg(self):
        return SystemGuid([self.a, self.b])

    def test_首启生成并两处都写(self):
        value = self.sg().load_or_create()
        self.assertTrue(self.a.exists() and self.b.exists())
        self.assertEqual(self.a.read_text().strip(), value)
        self.assertEqual(self.b.read_text().strip(), value)
        uuid.UUID(value)  # 合法 UUID

    def test_再次启动读回同一个绝不重生成(self):
        first = self.sg().load_or_create()
        for _ in range(3):
            self.assertEqual(self.sg().load_or_create(), first)

    def test_应用目录那份被删掉也不换身份而是从冗余处补回(self):
        # 这正是"冗余落盘"存在的理由：重铺应用目录不该换身份。
        first = self.sg().load_or_create()
        self.a.unlink()
        self.assertEqual(self.sg().load_or_create(), first)
        self.assertTrue(self.a.exists())

    def test_两处不一致时拒绝启动而不是挑一个(self):
        # 挑错了就等于换身份。宁可起不来。
        self.sg().load_or_create()
        self.b.write_text(str(uuid.uuid4()) + "\n", encoding="utf-8")
        with self.assertRaises(GuidError) as ctx:
            self.sg().load_or_create()
        self.assertIn("不一致", str(ctx.exception))

    def test_空文件不被当成没有(self):
        # 把"空文件"当成"没有"，就会在一次写坏之后重新生成身份。
        self.a.parent.mkdir(parents=True, exist_ok=True)
        self.a.write_text("", encoding="utf-8")
        with self.assertRaises(GuidError):
            self.sg().load_or_create()

    def test_内容畸形直接拒绝而不是拿去当身份用(self):
        self.a.parent.mkdir(parents=True, exist_ok=True)
        self.a.write_text("not-a-uuid\n", encoding="utf-8")
        with self.assertRaises(GuidError):
            self.sg().load_or_create()

    def test_没有配置落点直接报错(self):
        with self.assertRaises(GuidError):
            SystemGuid([])


if __name__ == "__main__":
    unittest.main()
