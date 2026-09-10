"""日志接口 —— **照 historystore 那套做**，形状与语义逐条对齐。

为什么照搬而不是自己设计一套：AICloud 前端已经在消费 hs 的日志流与翻阅口
（`SubscribeLogs` / `QueryLogs`），运维也已经习惯那个列表。同一个平台里两套日志语义，
使用者要记两遍，而差异恰恰都在细节上（大小写敏不敏感、总数是不是真的、丢没丢）。

对齐的四条（都是 hs 那边用问题换来的，不是纸面设计）：

1. **实时流与历史翻阅共用同一个过滤判据**。hs 原话：「实时流与历史翻阅是同一个列表里上下
   相邻的行，**不许两套语义**」。⇒ 本模块里 `_matches()` 是**唯一**的匹配实现，两条路都调它。
2. **search 大小写不敏感，但折叠只作用于 ASCII `A-Z/a-z`**，中文等非 ASCII 原样比较。
3. **`total_count` 是过滤后的真实匹配总数**，不是调用方自累计伪造的。
   （hs 专门加这个字段，就是为了修正 daqgate 用自累计冒充总数的失真。）
4. **背压不静默**：订阅者队列满了要丢，但**丢多少要告诉它**（`dropped_total`），
   而不是安静地少几行 —— 少几行的日志比没有日志更误导。

★与 hs 的一处**诚实差异**：hs 的历史翻阅读的是磁盘日志文件，故 `reached_oldest` 指
  "已含当前文件中最早可用记录"。本模块的翻阅读的是**内存环**，故 `reached_oldest` 指
  "已含内存环里最早的一条"，**更早的只在磁盘文件里、本接口查不到**。
  不把它说成一回事 —— 那会让人以为翻到底了就是全部。
"""

from __future__ import annotations

import enum
import itertools
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

#: 内存环容量。够前端翻几页、够现场看一段，再多就该去翻磁盘文件。
DEFAULT_CAPACITY = 20_000
#: 单个订阅者的队列深度。满了就丢并计数（见模块头第 4 条）。
DEFAULT_SUB_QUEUE = 2_000


class LogLevel(enum.IntEnum):
    """与 hs 的 `LogLevel` **数值逐一对齐**，免得转发时还要映射一次。"""

    TRACE = 0
    DEBUG = 1
    INFO = 2
    WARN = 3
    ERROR = 4
    FATAL = 5
    OFF = 6


@dataclass(frozen=True, slots=True)
class LogRecord:
    """一条日志。字段取 hs `LogRecord` 里对我方有意义的那些，名字保持一致。"""

    sequence_id: int
    timestamp: datetime
    level: LogLevel
    category: str
    message: str
    member_name: str = ""
    line_number: int = 0
    status_code: int = 0

    def to_dict(self) -> dict:
        return {
            "sequenceId": self.sequence_id,
            "timeStamp": self.timestamp.isoformat(),
            "logLevel": int(self.level),
            "category": self.category,
            "message": self.message,
            "memberName": self.member_name,
            "lineNumber": self.line_number,
            "statuCode": self.status_code,  # 拼写与 hs 契约一致（对方就是这么拼的）
        }


def _fold_ascii(s: str) -> str:
    """只折叠 ASCII 大小写；非 ASCII 原样。与 hs 1.9.199 起的判据逐字一致。

    ★不要用 `str.lower()`：它会对非 ASCII 也做 Unicode 折叠，于是同一个关键词
    在我方与 hs 的搜索结果不同 —— 而两边的行就摆在同一个列表里上下相邻。
    """
    out = []
    for ch in s:
        o = ord(ch)
        out.append(chr(o + 32) if 65 <= o <= 90 else ch)
    return "".join(out)


@dataclass(frozen=True, slots=True)
class LogFilter:
    """过滤条件。实时订阅与历史翻阅**共用同一个**，这是模块头第 1 条的落实。"""

    min_level: LogLevel = LogLevel.TRACE
    category: str = ""     # 空 = 不限；非空 = **精确相等**（同 hs）
    search: str = ""       # 空 = 不限；非空 = message 子串，ASCII 大小写不敏感
    from_time: datetime | None = None   # 含下界
    to_time: datetime | None = None     # 含上界

    def matches(self, rec: LogRecord) -> bool:
        if rec.level < self.min_level:
            return False
        if self.category and rec.category != self.category:
            return False
        if self.search and _fold_ascii(self.search) not in _fold_ascii(rec.message):
            return False
        if self.from_time is not None and rec.timestamp < self.from_time:
            return False
        if self.to_time is not None and rec.timestamp > self.to_time:
            return False
        return True


@dataclass
class _Subscriber:
    flt: LogFilter
    q: "queue.Queue[LogRecord]"
    dropped_total: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True, slots=True)
class QueryResult:
    """对齐 hs `QueryLogsRes`。"""

    logs: tuple[LogRecord, ...]
    total_count: int
    """★**过滤后的真实匹配总数**，不是本页条数、更不是调用方自累计的数。"""
    reached_oldest: bool
    """本页已含**内存环里**最早的一条。更早的只在磁盘文件里 —— 见模块头那条诚实差异。"""


class LogStore:
    """内存环 + 订阅扇出。骨架的所有日志都经它，故它自己**绝不写日志**（会递归）。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = int(capacity)
        self._records: list[LogRecord] = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._subs: list[_Subscriber] = []
        self._evicted = 0  # 被环挤掉的条数，用于 reached_oldest 的判定

    # ── 写 ────────────────────────────────────────────────────────────────
    def append(self, level: LogLevel, category: str, message: str, *,
               member_name: str = "", line_number: int = 0,
               status_code: int = 0, timestamp: datetime | None = None) -> LogRecord:
        ts = timestamp or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        rec = LogRecord(
            sequence_id=next(self._seq), timestamp=ts, level=level,
            category=category, message=message, member_name=member_name,
            line_number=line_number, status_code=status_code,
        )
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._capacity:
                drop = len(self._records) - self._capacity
                del self._records[:drop]
                self._evicted += drop
            subs = list(self._subs)
        for sub in subs:
            if not sub.flt.matches(rec):
                continue
            try:
                sub.q.put_nowait(rec)
            except queue.Full:
                # ★不静默：丢了多少要让订阅者知道（模块头第 4 条）。
                with sub.lock:
                    sub.dropped_total += 1
        return rec

    # ── 历史翻阅 ──────────────────────────────────────────────────────────
    def query(self, flt: LogFilter, *, offset: int = 0, limit: int = 200,
              newest_first: bool = True) -> QueryResult:
        with self._lock:
            snapshot = list(self._records)
            evicted = self._evicted
        matched = [r for r in snapshot if flt.matches(r)]
        total = len(matched)                       # ★真实总数
        ordered = list(reversed(matched)) if newest_first else matched
        limit = limit if limit > 0 else 200
        page = ordered[offset: offset + limit]

        # 本页是否触到了内存环里最早的一条。
        oldest_in_ring = snapshot[0].sequence_id if snapshot else None
        reached = bool(page) and oldest_in_ring is not None and \
            any(r.sequence_id == oldest_in_ring for r in page)
        # 环从没挤掉过东西 ⇒ 最早那条就是服务启动以来的第一条，"到底"是真的到底。
        # 挤掉过 ⇒ 到底只是"内存里到底"，磁盘文件里还有 —— 这个区别不能抹掉。
        return QueryResult(tuple(page), total, reached and evicted == 0)

    def evicted_count(self) -> int:
        """被内存环挤掉的总条数。>0 说明历史翻阅已经不完整，要去看磁盘文件。"""
        with self._lock:
            return self._evicted

    # ── 实时订阅 ──────────────────────────────────────────────────────────
    def subscribe(self, flt: LogFilter, *, backlog: int = 0,
                  queue_size: int = DEFAULT_SUB_QUEUE) -> "LogSubscription":
        sub = _Subscriber(flt=flt, q=queue.Queue(maxsize=queue_size))
        with self._lock:
            if backlog > 0:
                # 先补发最近 N 条历史（同 hs 的 LogSubscribeReq.backlog）。
                # ★用的是同一个 flt —— 补发与后续实时流必须同一判据，否则同一个列表里
                #   上半截和下半截的过滤规则不一样。
                recent = [r for r in self._records if flt.matches(r)][-backlog:]
                for r in recent:
                    try:
                        sub.q.put_nowait(r)
                    except queue.Full:
                        sub.dropped_total += 1
            self._subs.append(sub)
        return LogSubscription(self, sub)

    def _unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class LogSubscription:
    """一次订阅。迭代它拿实时日志；`dropped_total` 随时可读（背压提示）。"""

    def __init__(self, store: LogStore, sub: _Subscriber) -> None:
        self._store = store
        self._sub = sub
        self._closed = False

    @property
    def dropped_total(self) -> int:
        with self._sub.lock:
            return self._sub.dropped_total

    def get(self, timeout: float | None = None) -> LogRecord | None:
        try:
            return self._sub.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def __iter__(self) -> Iterator[LogRecord]:
        while not self._closed:
            rec = self.get(timeout=0.5)
            if rec is not None:
                yield rec

    def close(self) -> None:
        self._closed = True
        self._store._unsubscribe(self._sub)

    def __enter__(self) -> "LogSubscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── 把标准库 logging 接进来 ──────────────────────────────────────────────
class LogStoreHandler:
    """`logging.Handler`，把骨架所有日志喂进 `LogStore`。

    ★做成独立 handler 而不是让各处自己调 `store.append()`：那样一定会漏，
    而漏掉的恰恰是出问题时最想看的那几行。
    """

    _LEVEL_MAP = {
        10: LogLevel.DEBUG, 20: LogLevel.INFO, 30: LogLevel.WARN,
        40: LogLevel.ERROR, 50: LogLevel.FATAL,
    }

    def __new__(cls, store: LogStore):  # 延迟 import，保持本模块可被单独测试
        import logging

        class _Handler(logging.Handler):
            def emit(self, record: "logging.LogRecord") -> None:
                try:
                    level = LogStoreHandler._LEVEL_MAP.get(record.levelno, LogLevel.INFO)
                    store.append(
                        level=level,
                        category=record.name,
                        message=record.getMessage(),
                        member_name=record.funcName or "",
                        line_number=record.lineno or 0,
                        timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc),
                    )
                except Exception:  # noqa: BLE001 —— 日志系统自己绝不能把服务弄崩
                    pass

        return _Handler()
