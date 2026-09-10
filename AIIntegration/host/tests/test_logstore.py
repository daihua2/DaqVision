"""日志接口的回归 —— 钉的是"与 hs 逐条对齐的那四条语义"。

每一条都对应 hs 那边用问题换来的一个判据；实现一放松，用例必须红。
"""

import queue
import unittest
from datetime import datetime, timedelta, timezone

from aiintegration.logstore import (
    DEFAULT_SUB_QUEUE, LogFilter, LogLevel, LogStore, _fold_ascii,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def fill(store, n, level=LogLevel.INFO, category="hs", prefix="msg"):
    for i in range(n):
        store.append(level, category, f"{prefix}{i}", timestamp=T0 + timedelta(seconds=i))


class TestSearchSemantics(unittest.TestCase):
    """★第 2 条：ASCII 大小写不敏感，中文原样比较。"""

    def test_ASCII大小写不敏感(self):
        s = LogStore()
        s.append(LogLevel.INFO, "c", "Connection Refused")
        r = s.query(LogFilter(search="connection refused"))
        self.assertEqual(r.total_count, 1)

    def test_中文关键词照常可搜(self):
        s = LogStore()
        s.append(LogLevel.INFO, "c", "连不上实时库")
        self.assertEqual(s.query(LogFilter(search="连不上")).total_count, 1)

    def test_非ASCII不做Unicode折叠(self):
        """★这条是与 `str.lower()` 的**真**判别式。

        中文验不出来 —— 它本来就没有大小写，两种实现结果相同（我方第一版用例
        就栽在这里：写了个自以为在验、其实恒等的断言，靠变异验证才发现）。
        要用**确实有 Unicode 大小写映射的非 ASCII 字符**才分得开：
        全角拉丁 `Ａ` 与西里尔 `О` 在 `str.lower()` 下会被折叠，按 hs 的判据则不折叠。
        """
        self.assertEqual(_fold_ascii("ＡＢＣ"), "ＡＢＣ")   # 不折叠
        self.assertEqual("ＡＢＣ".lower(), "ａｂｃ")          # str.lower() 会折叠
        self.assertEqual(_fold_ascii("ОШИБКА"), "ОШИБКА")

        s = LogStore()
        s.append(LogLevel.INFO, "c", "ОШИБКА 连不上")
        # 按 hs 判据：小写西里尔搜不到大写西里尔。
        self.assertEqual(s.query(LogFilter(search="ошибка")).total_count, 0)
        # 但原样能搜到。
        self.assertEqual(s.query(LogFilter(search="ОШИБКА")).total_count, 1)


class TestSameMatcher(unittest.TestCase):
    """★第 1 条：实时流与历史翻阅必须同一判据。"""

    def test_订阅与查询对同一条记录的判定一致(self):
        s = LogStore()
        flt = LogFilter(min_level=LogLevel.WARN, search="Boom")
        with s.subscribe(flt) as sub:
            s.append(LogLevel.INFO, "c", "boom low level")   # 级别不够
            s.append(LogLevel.ERROR, "c", "BOOM here")       # 命中
            s.append(LogLevel.ERROR, "c", "quiet")           # 关键词不中
            got = [sub.get(timeout=0.2) for _ in range(1)]
        q = s.query(flt)
        self.assertEqual(q.total_count, 1)
        self.assertEqual(q.logs[0].message, "BOOM here")
        self.assertEqual([r.message for r in got], ["BOOM here"])

    def test_补发的历史与后续实时用同一过滤(self):
        s = LogStore()
        s.append(LogLevel.ERROR, "c", "old hit")
        s.append(LogLevel.INFO, "c", "old miss")
        flt = LogFilter(min_level=LogLevel.WARN)
        with s.subscribe(flt, backlog=10) as sub:
            self.assertEqual(sub.get(timeout=0.2).message, "old hit")
            self.assertIsNone(sub.get(timeout=0.05))  # old miss 不该补发


class TestTotalCountIsReal(unittest.TestCase):
    """★第 3 条：total_count 是过滤后的真实总数，不是本页条数。"""

    def test_分页时总数仍是全量匹配数(self):
        s = LogStore()
        fill(s, 50)
        r = s.query(LogFilter(), offset=0, limit=10)
        self.assertEqual(len(r.logs), 10)
        self.assertEqual(r.total_count, 50)

    def test_过滤后的总数只算命中的(self):
        s = LogStore()
        fill(s, 10, level=LogLevel.INFO)
        fill(s, 3, level=LogLevel.ERROR, prefix="err")
        self.assertEqual(s.query(LogFilter(min_level=LogLevel.ERROR)).total_count, 3)


class TestBackpressureIsLoud(unittest.TestCase):
    """★第 4 条：丢了要报数，不许安静地少几行。"""

    def test_队列满时累计dropped而不是静默丢(self):
        s = LogStore()
        sub = s.subscribe(LogFilter(), queue_size=3)
        for i in range(10):
            s.append(LogLevel.INFO, "c", f"m{i}")
        self.assertEqual(sub.dropped_total, 7)
        sub.close()

    def test_取消订阅后不再扇出(self):
        s = LogStore()
        sub = s.subscribe(LogFilter())
        sub.close()
        self.assertEqual(s.subscriber_count(), 0)
        s.append(LogLevel.INFO, "c", "after close")
        self.assertEqual(sub.dropped_total, 0)


class TestReachedOldestIsHonest(unittest.TestCase):
    """★那条与 hs 的诚实差异：环挤掉过东西，就不许说"翻到底了"。"""

    def test_没挤掉过时翻到底就是真到底(self):
        s = LogStore(capacity=100)
        fill(s, 5)
        r = s.query(LogFilter(), newest_first=False, limit=100)
        self.assertTrue(r.reached_oldest)
        self.assertEqual(s.evicted_count(), 0)

    def test_环挤掉过就不许报到底(self):
        s = LogStore(capacity=5)
        fill(s, 20)
        r = s.query(LogFilter(), newest_first=False, limit=100)
        self.assertGreater(s.evicted_count(), 0)
        self.assertFalse(r.reached_oldest)  # 更早的在磁盘文件里，本接口查不到


class TestLevelAlignment(unittest.TestCase):
    def test_级别数值与hs逐一对齐(self):
        # 对齐了转发时才不必再映射一次；改动这里等于改契约。
        self.assertEqual(
            [int(x) for x in (LogLevel.TRACE, LogLevel.DEBUG, LogLevel.INFO,
                              LogLevel.WARN, LogLevel.ERROR, LogLevel.FATAL, LogLevel.OFF)],
            [0, 1, 2, 3, 4, 5, 6],
        )

    def test_category精确相等而不是子串(self):
        s = LogStore()
        s.append(LogLevel.INFO, "hsclient", "x")
        self.assertEqual(s.query(LogFilter(category="hsclient")).total_count, 1)
        self.assertEqual(s.query(LogFilter(category="hs")).total_count, 0)


class TestLoggingBridge(unittest.TestCase):
    def test_标准库logging的记录会进store(self):
        import logging

        from aiintegration.logstore import LogStoreHandler

        s = LogStore()
        lg = logging.getLogger("aiintegration.test.bridge")
        lg.setLevel(logging.DEBUG)
        lg.addHandler(LogStoreHandler(s))
        try:
            lg.warning("桥接了一条 %s", "记录")
        finally:
            lg.handlers.clear()
        r = s.query(LogFilter(min_level=LogLevel.WARN))
        self.assertEqual(r.total_count, 1)
        self.assertEqual(r.logs[0].message, "桥接了一条 记录")
        self.assertEqual(r.logs[0].category, "aiintegration.test.bridge")


if __name__ == "__main__":
    unittest.main()
