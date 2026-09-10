"""结论点的 localId 分配与命名 —— 骨架八件里的"点位与命名"。

★**模块永远不碰 id**。模块只在 `declare()` 里说"我出一个叫 `health_score` 的结论"，
  由本模块把 `(域, 绑定, 结论名)` 映射到一个**本 guid 命名空间内稳定**的 `localId`。

为什么必须持久化、必须稳定：

  · hs 侧 `(guid, localId) → globalId` 是**持久映射**，globalId 一经分配就归那个 localId；
  · 我方换一次 localId，就等于**新建一个点**，旧点变成没人写的孤儿 —— 而按对账铁律
    **绝不自动删**，孤儿清不掉。这与"guid 永不重生成"是同一条理由的两个层面。

存储用 **sqlite3（标准库）**：

  · 不破"骨架零重依赖"（不引第三方）；
  · 不重蹈 v5 那个坑 —— 它的 `frames.json` 每追加一帧就把整个数组反序列化 + 重新序列化，
    O(n²)，现场真的写坏过（文件里还带着 JSON 损坏自愈逻辑）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: localId 起始号。留出低位段给将来可能的骨架自用点（自指标之类）。
DEFAULT_BASE_LOCAL_ID = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    domain     TEXT NOT NULL,
    binding    TEXT NOT NULL,
    key        TEXT NOT NULL,
    local_id   INTEGER NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    unit       TEXT NOT NULL DEFAULT '',
    value_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (domain, binding, key)
);
"""


@dataclass(frozen=True, slots=True)
class PointRow:
    domain: str
    binding: str
    key: str
    local_id: int
    name: str
    unit: str
    value_type: str


class PointMap:
    """`(域, 绑定, 结论名)` ⇄ `localId`。**只增不改、绝不回收**。

    不回收的理由与 hs 侧一致：号一旦用过就代表过一段历史数据，复用会让新点查到旧点的历史。
    """

    def __init__(self, db_path: Path, base_local_id: int = DEFAULT_BASE_LOCAL_ID) -> None:
        self._path = Path(db_path)
        self._base = int(base_local_id)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：崩溃一致性，且读写不互相阻塞。
        self._conn.execute("PRAGMA journal_mode=WAL")
        # ★不设 synchronous=0 —— 那是拿"掉电丢最后几条"换吞吐，而这张表是身份类数据，
        #   丢了就意味着重新分配 localId，代价远大于那点吞吐。
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 分配 ──────────────────────────────────────────────────────────────
    def ensure(self, domain: str, binding: str, key: str, *,
               name: str, unit: str, value_type: str) -> PointRow:
        """取已有的；没有就分配一个新的。**同一个三元组恒回同一个 localId。**"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM points WHERE domain=? AND binding=? AND key=?",
                (domain, binding, key),
            ).fetchone()
            if row is not None:
                # 显示名/单位允许改（那是展示层的事），localId 绝不动。
                if row["name"] != name or row["unit"] != unit or row["value_type"] != value_type:
                    if row["value_type"] != value_type:
                        # 值类型变了是**语义变更**，不是改个显示名 —— 吵出来。
                        logger.warning(
                            "结论点 %s/%s/%s 的值类型由 %s 变为 %s（localId=%d 不变）；"
                            "若语义确实变了，应当换一个结论名而不是原地改类型",
                            domain, binding, key, row["value_type"], value_type, row["local_id"],
                        )
                    self._conn.execute(
                        "UPDATE points SET name=?, unit=?, value_type=? "
                        "WHERE domain=? AND binding=? AND key=?",
                        (name, unit, value_type, domain, binding, key),
                    )
                    self._conn.commit()
                return PointRow(domain, binding, key, row["local_id"], name, unit, value_type)

            cur = self._conn.execute("SELECT MAX(local_id) AS m FROM points").fetchone()
            nxt = self._base if cur["m"] is None else max(int(cur["m"]) + 1, self._base)
            self._conn.execute(
                "INSERT INTO points(domain,binding,key,local_id,name,unit,value_type) "
                "VALUES(?,?,?,?,?,?,?)",
                (domain, binding, key, nxt, name, unit, value_type),
            )
            self._conn.commit()
            logger.info("分配结论点 localId=%d ← %s/%s/%s (%s)", nxt, domain, binding, key, name)
            return PointRow(domain, binding, key, nxt, name, unit, value_type)

    # ── 读 ────────────────────────────────────────────────────────────────
    def all(self) -> list[PointRow]:
        """全表。**每轮快照都要发全量** —— hs 的 `SNAPSHOT_END` 是原子提交，
        本轮未出现的旧实体一律删除，漏发一个就是删一个。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM points ORDER BY local_id").fetchall()
        return [
            PointRow(r["domain"], r["binding"], r["key"], r["local_id"],
                     r["name"], r["unit"], r["value_type"])
            for r in rows
        ]

    def local_id_of(self, domain: str, binding: str, key: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT local_id FROM points WHERE domain=? AND binding=? AND key=?",
                (domain, binding, key),
            ).fetchone()
        return None if row is None else int(row["local_id"])

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS c FROM points").fetchone()["c"])


def default_point_name(domain: str, binding: str, key: str) -> str:
    """结论点的显示名模板。

    ★命名要能在点表里一眼看出"这是 AI 出的、哪个域、哪个对象、什么结论"——
    点表是运维天天看的地方，名字含糊的点等于没有。
    """
    return f"AI.{domain}.{binding}.{key}"
