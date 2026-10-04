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

**上级实体**（`C-57`，2026-10-02 用户定「甲 + 族根 15/17」）：来源自己登记自己的结构 ——
  服务 → **域** → **绑定** → 点，对齐网关的 网关 → 连接 → 通道 → 点。域与绑定各是 hs 里的一个实体，
  也要一个本 guid 空间内稳定的 localId，理由同点：换号 = hs 那边新建一个、旧的成孤儿。
  ⇒ 与点**共用一个号段**（取两张表的最大号 +1），只增不回收；绑定实体按 (域, 绑定) 区分 ——
  同名绑定挂在两个域下就是两个实体（一条通道只属一条连接）。
  ★平台按这套分层（族根 15 = 域、17 = 绑定）挂层名标签（`C-62 §3`）：调层级、加一层，都**先函告**。

**停用**（`C-59 §5`，2026-10-03 用户定「清掉、显式停用」）：删绑定只停算、不删点（那是历史），
  于是旧绑定的点会被每轮快照永远推下去，平台点表上一直列着、看不出是停用的。停用 = 点行标上
  `retired_at`，**不再进快照**；号照旧占着、绝不回收（同上：号代表过一段历史）。
  ★从快照里消失之后，hs 只把它记进**待确认删除清单**、存储一个字节不动（`PendingDeletion`），
    真删要人在 hs 那边确认 —— 停用本身不删任何历史。
  ★真删（hs `ConfirmDeletions` 判 `DELETE`）也**不动号**：同一三元组重建，两端拿回原 localId / 原 gid，
    历史从零（`H-281`）。但「删前盘上**有过数据**、同号再写」这条路 hs 没有用例钉过 ——
    要重建这种点，**先函告 hs**（`H-281 §5`）。
  ★上级实体不另标：它们由点行归纳（`hsclient.build_container_entities`），名下没有在用的点就不推。
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
    retired_at TEXT NOT NULL DEFAULT '',   -- 非空 = 已停用：不进快照，号不回收
    PRIMARY KEY (domain, binding, key)
);
-- 上级实体。binding = '' 是域实体，否则是该域下的绑定实体。
CREATE TABLE IF NOT EXISTS containers (
    domain     TEXT NOT NULL,
    binding    TEXT NOT NULL,
    local_id   INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (domain, binding)
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
    binding_entity_id: int = 0
    """所属绑定实体的 localId（点的 `RelationId`）。0 = 不挂上级（只在手工构造的行上出现）。"""
    domain_entity_id: int = 0
    """所属域实体的 localId（绑定实体的 `ContainerId`）。"""


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
        self._migrate()
        self._conn.commit()
        self._backfill_containers()

    def _migrate(self) -> None:
        """老库补列（`CREATE TABLE IF NOT EXISTS` 不会给已有的表长新列，同 `BindingStore._migrate`）。"""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(points)")}
        if "retired_at" not in have:
            self._conn.execute(
                "ALTER TABLE points ADD COLUMN retired_at TEXT NOT NULL DEFAULT ''")
            logger.info("点表已补列 retired_at（老库升级；已有点一律在用）")

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
                if row["retired_at"]:
                    # ★停用过的三元组又被要了（同名绑定重建）—— 同一个 (域, 绑定, 结论名) 就是
                    #   同一个意思，复用原号，不另分；吵一句，免得"停用"被悄悄撤销没人知道。
                    self._conn.execute(
                        "UPDATE points SET retired_at='' WHERE domain=? AND binding=? AND key=?",
                        (domain, binding, key))
                    self._conn.commit()
                    logger.warning("结论点 %s/%s/%s（localId=%d）停用于 %s，现被重新启用",
                                   domain, binding, key, row["local_id"], row["retired_at"])
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
                return PointRow(domain, binding, key, row["local_id"], name, unit, value_type,
                                *self._containers_of(domain, binding))

            nxt = self._next_id()
            self._conn.execute(
                "INSERT INTO points(domain,binding,key,local_id,name,unit,value_type) "
                "VALUES(?,?,?,?,?,?,?)",
                (domain, binding, key, nxt, name, unit, value_type),
            )
            # 点先分号、上级后分 —— 空库第一个点仍是基数号。同一事务提交，不会出现"有点无上级"。
            parents = self._ensure_containers(domain, binding)
            self._conn.commit()
            logger.info("分配结论点 localId=%d ← %s/%s/%s (%s)", nxt, domain, binding, key, name)
            return PointRow(domain, binding, key, nxt, name, unit, value_type, *parents)

    # ── 上级实体（调用方持锁）────────────────────────────────────────────
    def _next_id(self) -> int:
        """点与上级实体共用的下一个号。**两张表一起取最大** —— 只看一张就会撞号。"""
        cur = self._conn.execute(
            "SELECT MAX(m) AS m FROM (SELECT MAX(local_id) AS m FROM points "
            "UNION ALL SELECT MAX(local_id) FROM containers)").fetchone()
        return self._base if cur["m"] is None else max(int(cur["m"]) + 1, self._base)

    def _container_id(self, domain: str, binding: str) -> int:
        row = self._conn.execute(
            "SELECT local_id FROM containers WHERE domain=? AND binding=?",
            (domain, binding)).fetchone()
        if row is not None:
            return int(row["local_id"])
        nxt = self._next_id()
        self._conn.execute("INSERT INTO containers(domain,binding,local_id) VALUES(?,?,?)",
                           (domain, binding, nxt))
        logger.info("分配上级实体 localId=%d ← %s", nxt, container_name(domain, binding))
        return nxt

    def _ensure_containers(self, domain: str, binding: str) -> tuple[int, int]:
        """→ (绑定实体号, 域实体号)。不提交，由调用方与点一起提交。"""
        dom = self._container_id(domain, "")
        return self._container_id(domain, binding), dom

    def _containers_of(self, domain: str, binding: str) -> tuple[int, int]:
        """已有点的上级号。开库时已补登（`_backfill_containers`），这里只读。"""
        rows = {r["binding"]: int(r["local_id"]) for r in self._conn.execute(
            "SELECT binding, local_id FROM containers WHERE domain=? AND binding IN (?, '')",
            (domain, binding))}
        return rows.get(binding, 0), rows.get("", 0)

    def _backfill_containers(self) -> None:
        """给**还没有上级**的已有点补登域 / 绑定实体（上级实体之前建的库，现场 27 个点即是）。

        ★已有点的号**一个都不动** —— 补登只新分上级的号，从现有最大号往后排。
        ★按 (域, 绑定) 排序补，结果与开库次数无关；补过的再开库不会再分。
        """
        with self._lock:
            pairs = self._conn.execute(
                "SELECT DISTINCT domain, binding FROM points ORDER BY domain, binding").fetchall()
            for p in pairs:
                self._ensure_containers(p["domain"], p["binding"])
            self._conn.commit()

    # ── 停用 ──────────────────────────────────────────────────────────────
    def retire(self, domain: str, binding: str) -> list[int]:
        """停用 (域, 绑定) 名下**全部在用**的点 → 这次停用的 localId（已停用的不重复算）。

        ★只标记、不删行：号绝不回收。是否允许停用（例如绑定还在不在）由调用方判。
        """
        with self._lock:
            ids = [int(r["local_id"]) for r in self._conn.execute(
                "SELECT local_id FROM points WHERE domain=? AND binding=? AND retired_at='' "
                "ORDER BY local_id", (domain, binding))]
            if ids:
                self._conn.execute(
                    "UPDATE points SET retired_at=datetime('now') "
                    "WHERE domain=? AND binding=? AND retired_at=''", (domain, binding))
                self._conn.commit()
                logger.warning("停用结论点 %d 个 ← %s（localId %d~%d），不再进快照",
                               len(ids), container_name(domain, binding), ids[0], ids[-1])
            return ids

    # ── 读 ────────────────────────────────────────────────────────────────
    def all(self) -> list[PointRow]:
        """**在用的**全表。**每轮快照都要发全量** —— hs 的 `SNAPSHOT_END` 是原子提交，
        本轮未出现的旧实体一律删除，漏发一个就是删一个。停用的点正是借这一条退出快照。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.*, COALESCE(b.local_id, 0) AS bid, COALESCE(d.local_id, 0) AS did "
                "FROM points p "
                "LEFT JOIN containers b ON b.domain = p.domain AND b.binding = p.binding "
                "LEFT JOIN containers d ON d.domain = p.domain AND d.binding = '' "
                "WHERE p.retired_at = '' "
                "ORDER BY p.local_id").fetchall()
        return [
            PointRow(r["domain"], r["binding"], r["key"], r["local_id"],
                     r["name"], r["unit"], r["value_type"], r["bid"], r["did"])
            for r in rows
        ]

    def local_id_of(self, domain: str, binding: str, key: str) -> int | None:
        """在用点的号；没有或已停用 → None。★停用点不给号：给了就会往一个不在快照里的点写值。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT local_id FROM points WHERE domain=? AND binding=? AND key=? "
                "AND retired_at=''",
                (domain, binding, key),
            ).fetchone()
        return None if row is None else int(row["local_id"])

    def count(self) -> int:
        """在用点数。"""
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS c FROM points WHERE retired_at=''").fetchone()["c"])


def default_point_name(domain: str, binding: str, key: str) -> str:
    """结论点的显示名模板。

    ★命名要能在点表里一眼看出"这是 AI 出的、哪个域、哪个对象、什么结论"——
    点表是运维天天看的地方，名字含糊的点等于没有。
    """
    return f"AI.{domain}.{binding}.{key}"


def container_name(domain: str, binding: str = "") -> str:
    """上级实体的显示名，与点名同一套前缀：`AI.<域>` / `AI.<域>.<绑定>`。"""
    return f"AI.{domain}.{binding}" if binding else f"AI.{domain}"
