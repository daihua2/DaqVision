"""绑定 —— 把域声明的**输入角色**对到**实际测点**上。

模块只说"我要一路叫 `x_acc` 的加速度"；**哪个设备的哪个点**是运维配的，不是模块写死的。
这一层就是那张对照表。

★角色对到的是 **globalId**：骨架从 hs 取数走的是**明文回环全量连接**，那条连接上一律用
  globalId（见 `hsclient` 模块头「两个 id 空间」）。写结论走另一条受限连接、用 localId，
  两者**不是同一个号，也不该互相换算着用**。

★**不做单位换算**。`InputSpec.unit` 只是绑定时给人核对的提示 ——
  换算错了比不换算更危险：不换算是"图形不对，一眼看得出"，换错了是"数值看着合理但全错"。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: 缺省节拍与窗口。标量域（如 v5 的 13 点）一分钟一帧足够；域可以在绑定里覆盖。
DEFAULT_INTERVAL_SEC = 60.0
DEFAULT_WINDOW_SEC = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    domain       TEXT NOT NULL,
    binding      TEXT NOT NULL,
    roles_json   TEXT NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    interval_sec REAL NOT NULL,
    window_sec   REAL NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (domain, binding)
);
"""


@dataclass(frozen=True, slots=True)
class Binding:
    domain: str
    binding: str
    """被诊断对象的标识。骨架不解释它 —— 对人有意义就行（设备号/测点号/工位号）。"""

    roles: dict[str, int]
    """`InputSpec.role` → **globalId**。"""

    params: dict[str, str] = field(default_factory=dict)
    """被诊断对象的**台账参数**（`ParamSpec.key` → 取值字符串）。

    ★**骨架只搬运不解释**（照 hs 的 `GatewayIdentity.attrs`）：键名与取值由域自述，
      骨架不枚举、不校验语义 —— 否则"新增一个域只写一个 .py"当场不成立。
      校验归模块；**模块读不到就落坏质量码，不许替它猜**。
    """

    interval_sec: float = DEFAULT_INTERVAL_SEC
    window_sec: float = DEFAULT_WINDOW_SEC
    enabled: bool = True

    def missing_required(self, required_roles: list[str]) -> list[str]:
        """声明里必填、而绑定里没给的那些角色。"""
        return [r for r in required_roles if r not in self.roles]


class BindingStore:
    """绑定表。与 `PointMap` 同库同规矩：sqlite3（标准库）、WAL、不图快牺牲一致性。"""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库补列。

        ★`CREATE TABLE IF NOT EXISTS` 对**已存在**的表一个字都不改 —— 新加的列不会自己长出来，
          而读的时候才炸（`no such column`），且是在现场、在半夜。这里显式补。
        """
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(bindings)")}
        if "params_json" not in have:
            self._conn.execute(
                "ALTER TABLE bindings ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}'")
            logger.info("绑定表已补列 params_json（老库升级）")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def put(self, b: Binding, *, allow_no_roles: bool = False) -> None:
        """存绑定。

        `allow_no_roles`：该域**根本没有测点类输入**（纯图片、事件驱动）时由调用方置真。
        ★这一层不知道域的声明，所以由知道的那层（api）决定 —— 不在这里放宽成"空也行"，
          否则测点域少绑一个角色都没人拦。
        """
        if not b.roles and not allow_no_roles:
            # 一个角色都没绑的绑定不是"待完善"，是"取不到任何数据" —— 存下来只会让
            # 调度器每个周期空转一次并报一次坏值。宁可现在就拒。
            raise ValueError(f"绑定 {b.domain}/{b.binding} 一个角色都没有")
        for role, gid in b.roles.items():
            if not isinstance(gid, int) or gid <= 0:
                raise ValueError(
                    f"绑定 {b.domain}/{b.binding} 的角色 {role} 指向非法 globalId {gid!r}"
                    "（0 = hs 尚未分配映射；**绝不能拿 0 去查**，那会静默查到别人的点）"
                )
        for k, v in b.params.items():
            # 只管形状（键非空、值是字符串），**语义一律不碰** —— 那是域的事。
            if not k or not isinstance(k, str):
                raise ValueError(f"绑定 {b.domain}/{b.binding} 的台账参数键非法 {k!r}")
            if not isinstance(v, str):
                raise ValueError(
                    f"绑定 {b.domain}/{b.binding} 的台账参数 {k} 取值必须是字符串，"
                    f"收到 {type(v).__name__}（契约里 params 是 map<string,string>）")
        with self._lock:
            self._conn.execute(
                "INSERT INTO bindings(domain,binding,roles_json,params_json,interval_sec,window_sec,enabled) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(domain,binding) DO UPDATE SET "
                "roles_json=excluded.roles_json, params_json=excluded.params_json, "
                "interval_sec=excluded.interval_sec, "
                "window_sec=excluded.window_sec, enabled=excluded.enabled, "
                "updated_at=datetime('now')",
                (b.domain, b.binding, json.dumps(b.roles, ensure_ascii=False),
                 json.dumps(b.params, ensure_ascii=False),
                 float(b.interval_sec), float(b.window_sec), 1 if b.enabled else 0),
            )
            self._conn.commit()

    def get(self, domain: str, binding: str) -> Binding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bindings WHERE domain=? AND binding=?",
                (domain, binding)).fetchone()
        return None if row is None else _to_binding(row)

    def list(self, domain: str | None = None, *, only_enabled: bool = False) -> list[Binding]:
        sql = "SELECT * FROM bindings"
        args: list = []
        where = []
        if domain:
            where.append("domain=?")
            args.append(domain)
        if only_enabled:
            where.append("enabled=1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY domain, binding"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_to_binding(r) for r in rows]

    def delete(self, domain: str, binding: str) -> bool:
        """删绑定 —— **只停止取数与推理，不删已经写进实时库的结论点**。

        ★两件事分开：绑定是"要不要继续算"，结论点是"算过的历史"。
          删绑定顺手删点，就把历史一起抹了，而那是用户唯一能回看的东西。
          点的去留另有出口（且要走确认），不在这里顺带做。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bindings WHERE domain=? AND binding=?", (domain, binding))
            self._conn.commit()
        return cur.rowcount > 0


def _to_binding(row: sqlite3.Row) -> Binding:
    return Binding(
        domain=row["domain"],
        binding=row["binding"],
        roles={k: int(v) for k, v in json.loads(row["roles_json"]).items()},
        params={k: str(v) for k, v in json.loads(row["params_json"] or "{}").items()},
        interval_sec=float(row["interval_sec"]),
        window_sec=float(row["window_sec"]),
        enabled=bool(row["enabled"]),
    )
