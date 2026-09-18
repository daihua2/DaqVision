"""工作台状态 —— 标注 / 训练集与样本 / 训练任务 / 模型工件 / 片段与报告。

骨架八件里的第 5 件。需求来自 AICloud `C-11`（他们看完 v4/v5/meter/VFD 四套界面后给的
七类页面清单），其中 4~7 四类要我方出端点，落点就是本模块。

---

## 1. 为什么是骨架的活，不是模块的

这些是**平台级资产**：要能备份、能追溯、能授权。模块各存各的，就是第二个 `mock_db.json`
（v5 的 `local_data/` 17 MB JSON 全量读写，追加一条要把整个数组反序列化再写回，
文件里至今带着"JSON 损坏自愈"的补丁 —— 那是 O(n²) 撞上现实的样子）。

⇒ 存储用 **sqlite3（标准库）**：不破"骨架零重依赖"，且分页/批量/事务是它的本行。

## 2. 四条语义，都是从 `C-11` 的要求里逐条落下来的

| # | 语义 | 出处与理由 |
| --- | --- | --- |
| ① | **标注 ≠ 样本** | `C-11 §3.1`。标注是"人对一段数据的判断"；样本是"进了某个训练集的那份拷贝"。两者分开，才谈得上"同一段数据进了三个训练集" |
| ② | **移出 ≠ 删除** | `C-11 §3.2`。移出只解训练集与样本的关系，**原始标注一动不动**。v5 特意分开的，保住 |
| ③ | **标签不枚举** | `C-11 §3.1`。标签是运行期值，从现有数据聚合出来，**没有标签表** —— 有表就一定有人去维护它，也一定漂 |
| ④ | **训练是可查询状态的任务，不是阻塞调用** | `C-11 §3.3`。v5 是同步阻塞 + 一句"请不要关闭页面"。异步之后界面能离开、失败原因留得住 |

## 3. ★样本上的标签是**快照**，不是外键跟随

同一段数据在半年后被人重新判成另一个标签，**已经拿旧标签训出来的那个模型不该被追溯改写** ——
否则"这个模型是用什么训的"这个问题就没有答案了。⇒ `samples.label` 是入集那一刻的拷贝。

## 4. ★基线也走这里（域私有状态的落点，2026-09-11 定）

AICloud `C-10 §6` / `C-11 §5` 给的判据：

> 凡是界面上要能"看见它是什么时候采的、能不能重采"的，它就得是资产，不是中间量。

低频振动的基线（μ/σ、EWMA 起点）**两样都要**：换了工况就得重采，运维必须看得见是哪段数据采的。
⇒ 它是资产，落 `artifacts` 表，`kind='baseline'`。**不为它单开一套存储、也不让模块自己写文件。**

（`artifacts.kind` 同时容得下 `C-11 §4` 说的"图片工件" —— 那本就是同一个 `artifact` 能力位管的东西。）

## 5. 时刻一律存 **UTC 微秒整数**

不存字符串：范围查询要能走索引，而字符串时刻一旦掺进不同格式（带不带 `Z`、带不带毫秒）
就会静默漏掉一段。入口处**只收带时区的 datetime**（裸 datetime 在本机看着对，跨机就是另一个时刻）。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import dataorigin
from .types import MAX_STATE_BYTES

logger = logging.getLogger(__name__)

#: 一页最多给多少。★分页是硬要求（`C-11 §3.2`：v5 的样本表是分页的，逐条调用界面上不可用）。
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100

#: 训练任务的状态机。**只有这五个**，且只能往前走（终态不再变）。
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_READY = "ready"
JOB_FAILED = "failed"
JOB_CANCELED = "canceled"
_JOB_TERMINAL = frozenset({JOB_READY, JOB_FAILED, JOB_CANCELED})

#: 工件种类。`model` = 训出来的模型；`baseline` = 基线（见模块头 §4）；`image` = 图片工件。
#: ★**不是枚举契约的全部** —— 新种类随时加，对端按字符串透传，别写死分支。
KIND_MODEL = "model"
KIND_BASELINE = "baseline"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT    NOT NULL,
    binding     TEXT    NOT NULL,
    t_from_us   INTEGER NOT NULL,
    t_to_us     INTEGER NOT NULL,
    label       TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_ann_scope ON annotations(domain, binding, t_from_us);
CREATE INDEX IF NOT EXISTS ix_ann_label ON annotations(domain, label);

CREATE TABLE IF NOT EXISTS datasets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(domain, name)
);

CREATE TABLE IF NOT EXISTS samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id    INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    -- 来源标注。★标注被删时置空而不是连带删样本：训练集里那份是**拷贝**，
    --   它记录的是"训练时用的是什么"，不该被后来的删除动作改写。
    annotation_id INTEGER,
    binding       TEXT    NOT NULL,
    t_from_us     INTEGER NOT NULL,
    t_to_us       INTEGER NOT NULL,
    label         TEXT    NOT NULL,      -- ★入集那一刻的快照，见模块头 §3
    -- ★同样是**入集那一刻的快照**：来源性质（仿真/现场）取自绑定（契约 1.6）。
    --   日后真机接入、绑定改成 field，**不反写**当初用仿真数据训出来的那批样本。
    data_origin   TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(dataset_id, annotation_id)
);
CREATE INDEX IF NOT EXISTS ix_sample_ds ON samples(dataset_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'model',
    -- 作用对象。空串 = 全域通用（v5 是每传感器一个模型，故常态是具体绑定）。
    binding       TEXT    NOT NULL DEFAULT '',
    name          TEXT    NOT NULL,
    algo          TEXT    NOT NULL DEFAULT '',
    dataset_id    INTEGER,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    feature_count INTEGER NOT NULL DEFAULT 0,
    accuracy      REAL,                  -- ★可空：没测过就是没测过，**不许写 0**
    active        INTEGER NOT NULL DEFAULT 0,
    path          TEXT    NOT NULL DEFAULT '',   -- 相对工件根；大对象走 HTTP 口取
    size          INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT    NOT NULL DEFAULT '',
    meta_json     TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    -- 来历（契约 1.5）。老库由 _migrate 补列。
    origin        TEXT    NOT NULL DEFAULT 'trained',   -- trained | imported
    source        TEXT    NOT NULL DEFAULT '',
    training_data TEXT    NOT NULL DEFAULT '',
    license       TEXT    NOT NULL DEFAULT '',
    -- 训练数据的来源性质（契约 1.6）：训练产出按样本快照**取最严**，外部导入按声明。
    -- ★空 = 未声明，**不是现场**。老库由 _migrate 补列。
    data_origin   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_art_scope ON artifacts(domain, kind, binding);
CREATE INDEX IF NOT EXISTS ix_art_sha ON artifacts(domain, kind, sha256);
-- ★同一 (域, 种类, 对象) 只允许一个激活件。用**部分唯一索引**在库层挡住，
--   不靠应用层"记得先取消上一个" —— 那种约束迟早被并发或异常路径绕过去。
CREATE UNIQUE INDEX IF NOT EXISTS ux_art_active
    ON artifacts(domain, kind, binding) WHERE active = 1;

CREATE TABLE IF NOT EXISTS train_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT    NOT NULL,
    dataset_id    INTEGER NOT NULL,
    binding       TEXT    NOT NULL DEFAULT '',
    algo          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending',
    message       TEXT    NOT NULL DEFAULT '',   -- ★失败原因留在这，别只打日志
    progress      REAL    NOT NULL DEFAULT 0.0,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    artifact_id   INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_job_scope ON train_jobs(domain, id);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT    NOT NULL,
    binding     TEXT    NOT NULL,
    t_from_us   INTEGER NOT NULL,
    t_to_us     INTEGER NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT '',
    point_count INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_seg_scope ON segments(domain, binding, t_from_us);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'basic',
    path        TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_report_seg ON reports(segment_id);

CREATE TABLE IF NOT EXISTS domain_state (
    domain      TEXT    NOT NULL,
    binding     TEXT    NOT NULL,
    state_json  TEXT    NOT NULL,
    since       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    writes      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (domain, binding)
);
"""


class WorkbenchError(ValueError):
    """调用方给错了 —— 原样回给对端，**不吞**（对端要能在界面上说清哪儿错了）。"""


# ─────────────────────────────── 行类型 ───────────────────────────────

def _us(t: datetime, what: str) -> int:
    """带时区的 datetime → UTC 微秒。**裸 datetime 一律拒**（见模块头 §5）。"""
    if not isinstance(t, datetime):
        raise WorkbenchError(f"{what} 必须是 datetime，收到 {type(t).__name__}")
    if t.tzinfo is None or t.tzinfo.utcoffset(t) is None:
        raise WorkbenchError(f"{what} 必须带时区（裸 datetime 会静默错位）: {t!r}")
    return int(t.astimezone(timezone.utc).timestamp() * 1_000_000)


def _dt(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)


@dataclasses.dataclass(frozen=True, slots=True)
class Annotation:
    id: int
    domain: str
    binding: str
    t_from: datetime
    t_to: datetime
    label: str
    note: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Dataset:
    id: int
    domain: str
    name: str
    note: str = ""
    sample_count: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Sample:
    id: int
    dataset_id: int
    annotation_id: int | None
    binding: str
    t_from: datetime
    t_to: datetime
    label: str
    created_at: str = ""
    data_origin: str = ""
    """来源性质（仿真/现场）—— **入集那一刻从绑定取的快照**，见 `dataorigin` 模块头。"""


@dataclasses.dataclass(frozen=True, slots=True)
class Artifact:
    id: int
    domain: str
    kind: str
    binding: str
    name: str
    algo: str
    dataset_id: int | None
    sample_count: int
    feature_count: int
    accuracy: float | None
    active: bool
    path: str
    size: int
    sha256: str
    meta_json: str
    created_at: str = ""
    origin: str = "trained"
    """`trained`（本系统训练面产出）/ `imported`（外部导入）。"""

    source: str = ""
    training_data: str = ""
    license: str = ""
    data_origin: str = ""
    """训练数据的来源性质（`simulated` / `field` / 空=未声明）。见 `dataorigin` 模块头。"""


@dataclasses.dataclass(frozen=True, slots=True)
class TrainJob:
    id: int
    domain: str
    dataset_id: int
    binding: str
    algo: str
    status: str
    message: str
    progress: float
    sample_count: int
    artifact_id: int | None
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Segment:
    id: int
    domain: str
    binding: str
    t_from: datetime
    t_to: datetime
    name: str
    source: str
    point_count: int
    note: str = ""
    created_at: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Report:
    id: int
    segment_id: int
    kind: str
    path: str
    size: int
    created_at: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Page:
    """一页结果。★`total` 是**过滤后的真实总数**，不是本页条数。

    照 hs 日志口那套规矩：给个约数或者给本页条数，界面上的分页器就是错的，而且没人会发现。
    """
    items: list
    total: int
    offset: int
    limit: int


def _page_args(offset: int, limit: int) -> tuple[int, int]:
    if offset < 0:
        raise WorkbenchError(f"offset 不能为负: {offset}")
    if limit <= 0:
        limit = DEFAULT_PAGE_SIZE
    if limit > MAX_PAGE_SIZE:
        # ★不静默截断：调用方以为拿全了，实际少了一截，分页器还显示对的总数。
        raise WorkbenchError(f"limit 最大 {MAX_PAGE_SIZE}，收到 {limit}；请分页取")
    return offset, limit


# ─────────────────────────────── 存储 ───────────────────────────────

@dataclasses.dataclass(frozen=True, slots=True)
class DomainState:
    """一条诊断的跨帧状态。★`since` 是它的要害：**这份状态从哪一刻起攒的**。"""

    domain: str
    binding: str
    state: dict
    since: str
    updated_at: str
    writes: int
    """写了多少拍。给自检/界面看"这条状态到底在不在动"。"""


class Workbench:
    """工作台状态的唯一入口。线程安全（一把锁 + 单连接，与 `PointMap` 同规矩）。"""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        # ★外键默认是关的。不开的话 ON DELETE CASCADE / SET NULL 全是摆设，
        #   删数据集会留下一堆指向空的样本，而且不报错。
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库补列（契约 1.5 的工件来历四列、1.6 的来源性质两列）。

        ★`CREATE TABLE IF NOT EXISTS` 对已存在的表**一个字都不改** —— 新列不会自己长出来，
          读的时候才炸（`no such column`），而且是在现场。AISERVER 上已有一份 1.4 时代的库。
        """
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(artifacts)")}
        added = []
        for col, ddl in (("origin", "TEXT NOT NULL DEFAULT 'trained'"),
                         ("source", "TEXT NOT NULL DEFAULT ''"),
                         ("training_data", "TEXT NOT NULL DEFAULT ''"),
                         ("license", "TEXT NOT NULL DEFAULT ''"),
                         # 1.6：★补成空（未声明），**绝不回填 field** —— 空 ≠ 现场。
                         #   也不按绑定"现在"的取值回填历史：那个方向会把当初用仿真数据
                         #   训的工件洗成现场（见 dataorigin 模块头）。
                         ("data_origin", "TEXT NOT NULL DEFAULT ''")):
            if col not in have:
                self._conn.execute(f"ALTER TABLE artifacts ADD COLUMN {col} {ddl}")
                added.append(col)
        if added:
            # 1.5 之前的工件只可能是训练产出；来历按训练集号回填一句，**不编造细节**。
            self._conn.execute(
                "UPDATE artifacts SET training_data = '训练集 id=' || dataset_id || '（1.5 之前产出，未记录详情）' "
                "WHERE origin = 'trained' AND training_data = '' AND dataset_id IS NOT NULL")
            logger.info("工件表已补列 %s（老库升级）", added)
        have_s = {r["name"] for r in self._conn.execute("PRAGMA table_info(samples)")}
        if "data_origin" not in have_s:
            # ★同上：补成空。1.6 之前入集的样本没有快照，训练时由 `dataorigin.effective`
            #   按绑定当前值**只朝严的方向**兜底（绑定说 simulated 才抬，说 field 仍是未声明）。
            self._conn.execute(
                "ALTER TABLE samples ADD COLUMN data_origin TEXT NOT NULL DEFAULT ''")
            logger.info("样本表已补列 data_origin（老库升级；老样本无快照，一律未声明）")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 标注 ──────────────────────────────────────────────────────────────
    def put_annotation(self, *, domain: str, binding: str, t_from: datetime,
                       t_to: datetime, label: str, note: str = "",
                       annotation_id: int | None = None) -> int:
        """新建或改一条标注。返回 id。**改标签不影响已经入集的样本**（见模块头 §3）。"""
        if not domain or not binding:
            raise WorkbenchError("标注必须说清是哪个域的哪个对象")
        if not label:
            raise WorkbenchError("标注必须有标签 —— 空标签的标注在训练里是噪声，不是'待定'")
        a, b = _us(t_from, "t_from"), _us(t_to, "t_to")
        if b <= a:
            raise WorkbenchError(f"标注时间区间倒挂或为零长: {t_from} → {t_to}")
        with self._lock:
            if annotation_id is None:
                cur = self._conn.execute(
                    "INSERT INTO annotations(domain,binding,t_from_us,t_to_us,label,note)"
                    " VALUES(?,?,?,?,?,?)", (domain, binding, a, b, label, note))
                new_id = int(cur.lastrowid)
            else:
                cur = self._conn.execute(
                    "UPDATE annotations SET domain=?,binding=?,t_from_us=?,t_to_us=?,"
                    "label=?,note=?,updated_at=datetime('now') WHERE id=?",
                    (domain, binding, a, b, label, note, annotation_id))
                if cur.rowcount == 0:
                    raise WorkbenchError(f"没有 id={annotation_id} 这条标注")
                new_id = annotation_id
            self._conn.commit()
        return new_id

    def annotate_range(self, *, domain: str, bindings: Sequence[str], t_from: datetime,
                       t_to: datetime, label: str, note: str = "") -> list[int]:
        """按时间范围**批量**给多个对象打同一个标签（`C-11 §3.1` 的第三种写法）。

        ★一个事务：要么都进去，要么一条都不进。半批成功比全失败难查得多。
        """
        if not bindings:
            raise WorkbenchError("annotate_range 至少要给一个对象")
        a, b = _us(t_from, "t_from"), _us(t_to, "t_to")
        if b <= a:
            raise WorkbenchError(f"时间区间倒挂或为零长: {t_from} → {t_to}")
        if not label:
            raise WorkbenchError("批量标注必须有标签")
        ids: list[int] = []
        with self._lock:
            try:
                for bd in bindings:
                    cur = self._conn.execute(
                        "INSERT INTO annotations(domain,binding,t_from_us,t_to_us,label,note)"
                        " VALUES(?,?,?,?,?,?)", (domain, bd, a, b, label, note))
                    ids.append(int(cur.lastrowid))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return ids

    def list_annotations(self, *, domain: str = "", binding: str = "", label: str = "",
                         t_from: datetime | None = None, t_to: datetime | None = None,
                         offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> Page:
        offset, limit = _page_args(offset, limit)
        where, args = [], []
        if domain:
            where.append("domain=?"); args.append(domain)
        if binding:
            where.append("binding=?"); args.append(binding)
        if label:
            where.append("label=?"); args.append(label)
        # ★区间**相交**即算命中，不是"被完全包含"：按后者查，界面上框一小段就什么都找不到。
        if t_from is not None:
            where.append("t_to_us > ?"); args.append(_us(t_from, "t_from"))
        if t_to is not None:
            where.append("t_from_us < ?"); args.append(_us(t_to, "t_to"))
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) FROM annotations{sql_where}", args).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM annotations{sql_where} ORDER BY t_from_us DESC, id DESC"
                " LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return Page([_to_annotation(r) for r in rows], total, offset, limit)

    def delete_annotations(self, ids: Sequence[int]) -> int:
        """真删标注。★**已入集的样本不受影响** —— 它们是拷贝，`annotation_id` 置空。

        为什么不连带删：样本记录的是"这个模型当初用什么训的"。
        后来把标注删了就把训练历史一起改写，等于让"这个模型是怎么来的"变成无解。
        """
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            self._conn.execute(
                f"UPDATE samples SET annotation_id=NULL WHERE annotation_id IN ({marks})",
                tuple(ids))
            cur = self._conn.execute(
                f"DELETE FROM annotations WHERE id IN ({marks})", tuple(ids))
            self._conn.commit()
        return cur.rowcount

    def list_labels(self, domain: str = "", dataset_id: int | None = None) -> list[tuple[str, int]]:
        """现有标签 + 各自条数。**从数据聚合，没有标签表**（模块头 §2 第 ③ 条）。

        界面据此渲染候选，并允许直接输入新标签 —— 新标签在被用第一次时就存在了。
        """
        with self._lock:
            if dataset_id is not None:
                rows = self._conn.execute(
                    "SELECT label, COUNT(*) c FROM samples WHERE dataset_id=?"
                    " GROUP BY label ORDER BY c DESC, label", (dataset_id,)).fetchall()
            elif domain:
                rows = self._conn.execute(
                    "SELECT label, COUNT(*) c FROM annotations WHERE domain=?"
                    " GROUP BY label ORDER BY c DESC, label", (domain,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT label, COUNT(*) c FROM annotations"
                    " GROUP BY label ORDER BY c DESC, label").fetchall()
        return [(r[0], int(r[1])) for r in rows]

    # ── 训练集 ────────────────────────────────────────────────────────────
    def put_dataset(self, *, domain: str, name: str, note: str = "",
                    dataset_id: int | None = None) -> int:
        if not domain or not name:
            raise WorkbenchError("训练集必须有域与名字")
        with self._lock:
            try:
                if dataset_id is None:
                    cur = self._conn.execute(
                        "INSERT INTO datasets(domain,name,note) VALUES(?,?,?)",
                        (domain, name, note))
                    new_id = int(cur.lastrowid)
                else:
                    cur = self._conn.execute(
                        "UPDATE datasets SET name=?,note=?,updated_at=datetime('now')"
                        " WHERE id=? AND domain=?", (name, note, dataset_id, domain))
                    if cur.rowcount == 0:
                        raise WorkbenchError(f"没有 id={dataset_id} 这个训练集（或不属于域 {domain}）")
                    new_id = dataset_id
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise WorkbenchError(f"域 {domain} 下已经有叫 {name!r} 的训练集") from exc
        return new_id

    def dataset_domain(self, dataset_id: int) -> str:
        """训练集属于哪个域。空串 = 没这个训练集。

        给调用方定位绑定用（样本行只存 `binding` 字符串，而绑定的主键是 **(域, 绑定)**——
        两个域用了同一个对象名时，只按 binding 找会取到别人的那条）。
        """
        with self._lock:
            row = self._conn.execute("SELECT domain FROM datasets WHERE id=?",
                                     (dataset_id,)).fetchone()
        return "" if row is None else row["domain"]

    def list_datasets(self, domain: str = "") -> list[Dataset]:
        sql = ("SELECT d.*, (SELECT COUNT(*) FROM samples s WHERE s.dataset_id=d.id) n"
               " FROM datasets d")
        args: tuple = ()
        if domain:
            sql += " WHERE d.domain=?"; args = (domain,)
        sql += " ORDER BY d.domain, d.name"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_to_dataset(r) for r in rows]

    def delete_dataset(self, dataset_id: int) -> bool:
        """删训练集**连它的样本一起**（样本是这个集合的成员，离了集合没有意义）。

        ★但**不碰标注**：那是人的判断，比任何一个训练集都活得久。
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def copy_dataset(self, dataset_id: int, new_name: str) -> int:
        """整库复制，**连样本一起**（`C-11 §3.2`）。一个事务。"""
        with self._lock:
            src = self._conn.execute("SELECT * FROM datasets WHERE id=?",
                                     (dataset_id,)).fetchone()
            if src is None:
                raise WorkbenchError(f"没有 id={dataset_id} 这个训练集")
            try:
                cur = self._conn.execute(
                    "INSERT INTO datasets(domain,name,note) VALUES(?,?,?)",
                    (src["domain"], new_name, src["note"]))
                new_id = int(cur.lastrowid)
                self._conn.execute(
                    "INSERT INTO samples(dataset_id,annotation_id,binding,t_from_us,t_to_us,"
                    "label,data_origin)"
                    " SELECT ?,annotation_id,binding,t_from_us,t_to_us,label,data_origin"
                    " FROM samples WHERE dataset_id=?", (new_id, dataset_id))
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise WorkbenchError(
                    f"域 {src['domain']} 下已经有叫 {new_name!r} 的训练集") from exc
        return new_id

    # ── 样本 ──────────────────────────────────────────────────────────────
    def add_samples(self, dataset_id: int, annotation_ids: Sequence[int],
                    origins: Mapping[str, str] | None = None) -> int:
        """把标注**拷进**训练集。已经在里面的跳过（幂等），返回新增条数。

        `origins`：绑定 → 来源性质（仿真/现场），由知道绑定的那层给（本层不连绑定库）。
        ★**入集这一刻打快照**，与 `label` 同一条规矩：这条记录说的是"训练时用的是什么"，
          不该被后来改绑定的动作改写。`INSERT OR IGNORE` 顺带保证了**已在集内的样本
          不会被重跑一次"加入"改掉快照**。
        """
        if not annotation_ids:
            return 0
        marks = ",".join("?" * len(annotation_ids))
        # 绑定 → 取值，展开成 CASE：一条 INSERT 里带上快照，不分两步（分两步就得
        # 区分"哪几条是这次新加的"，而 IGNORE 掉的那些一改就破坏了快照语义）。
        pairs = [(b, dataorigin.normalize(v)) for b, v in (origins or {}).items()]
        case_sql = "''"
        case_args: list = []
        if pairs:
            case_sql = ("CASE binding " + " ".join("WHEN ? THEN ?" for _ in pairs)
                        + " ELSE '' END")
            case_args = [x for pair in pairs for x in pair]
        with self._lock:
            if self._conn.execute("SELECT 1 FROM datasets WHERE id=?",
                                  (dataset_id,)).fetchone() is None:
                raise WorkbenchError(f"没有 id={dataset_id} 这个训练集")
            before = self._count_samples(dataset_id)
            # ★`INSERT OR IGNORE` 配 UNIQUE(dataset_id, annotation_id) 做幂等：
            #   界面上重复点一次"加入"，不该多出一份重复样本（那会悄悄改变类别权重）。
            self._conn.execute(
                f"INSERT OR IGNORE INTO samples(dataset_id,annotation_id,binding,t_from_us,"
                f"t_to_us,label,data_origin)"
                f" SELECT ?, id, binding, t_from_us, t_to_us, label, {case_sql}"
                f" FROM annotations WHERE id IN ({marks})",
                (dataset_id, *case_args, *annotation_ids))
            self._touch_dataset(dataset_id)
            self._conn.commit()
            return self._count_samples(dataset_id) - before

    def list_samples(self, dataset_id: int, *, label: str = "", binding: str = "",
                     offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> Page:
        offset, limit = _page_args(offset, limit)
        where, args = ["dataset_id=?"], [dataset_id]
        if label:
            where.append("label=?"); args.append(label)
        if binding:
            where.append("binding=?"); args.append(binding)
        sql_where = " WHERE " + " AND ".join(where)
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) FROM samples{sql_where}", args).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM samples{sql_where} ORDER BY id LIMIT ? OFFSET ?",
                (*args, limit, offset)).fetchall()
        return Page([_to_sample(r) for r in rows], total, offset, limit)

    def remove_samples(self, dataset_id: int, sample_ids: Sequence[int]) -> int:
        """★**移出**：只解这个训练集与样本的关系，**原始标注一动不动**（模块头 §2 第 ② 条）。

        v5 特意把"移出"和"删除"分成两个动作，因为把一条数据从训练集里拿掉，
        与否定人当初的判断，是两件完全不同的事。
        """
        if not sample_ids:
            return 0
        marks = ",".join("?" * len(sample_ids))
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM samples WHERE dataset_id=? AND id IN ({marks})",
                (dataset_id, *sample_ids))
            self._touch_dataset(dataset_id)
            self._conn.commit()
        return cur.rowcount

    def copy_samples(self, sample_ids: Sequence[int], to_dataset: int) -> int:
        """批量复制到另一个训练集（`C-11 §3.2`）。同一条标注在目标集里已有则跳过。"""
        if not sample_ids:
            return 0
        marks = ",".join("?" * len(sample_ids))
        with self._lock:
            if self._conn.execute("SELECT 1 FROM datasets WHERE id=?",
                                  (to_dataset,)).fetchone() is None:
                raise WorkbenchError(f"没有 id={to_dataset} 这个训练集")
            before = self._count_samples(to_dataset)
            self._conn.execute(
                f"INSERT OR IGNORE INTO samples(dataset_id,annotation_id,binding,t_from_us,"
                f"t_to_us,label,data_origin)"
                f" SELECT ?, annotation_id, binding, t_from_us, t_to_us, label, data_origin"
                f" FROM samples WHERE id IN ({marks})", (to_dataset, *sample_ids))
            self._touch_dataset(to_dataset)
            self._conn.commit()
            return self._count_samples(to_dataset) - before

    def _count_samples(self, dataset_id: int) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM samples WHERE dataset_id=?", (dataset_id,)).fetchone()[0])

    def _touch_dataset(self, dataset_id: int) -> None:
        self._conn.execute(
            "UPDATE datasets SET updated_at=datetime('now') WHERE id=?", (dataset_id,))

    # ── 训练任务 ──────────────────────────────────────────────────────────
    def create_job(self, *, domain: str, dataset_id: int, binding: str = "",
                   algo: str = "") -> int:
        """建一个**待跑**的训练任务，立刻返回 id。★不阻塞（模块头 §2 第 ④ 条）。"""
        with self._lock:
            row = self._conn.execute("SELECT domain FROM datasets WHERE id=?",
                                     (dataset_id,)).fetchone()
            if row is None:
                raise WorkbenchError(f"没有 id={dataset_id} 这个训练集")
            if row["domain"] != domain:
                raise WorkbenchError(
                    f"训练集 {dataset_id} 属于域 {row['domain']}，不能拿去训 {domain}")
            n = self._count_samples(dataset_id)
            if n == 0:
                # 空集训练必然产出一个假模型（或抛异常在半路），当场拒比事后查便宜。
                raise WorkbenchError(f"训练集 {dataset_id} 里一个样本都没有")
            cur = self._conn.execute(
                "INSERT INTO train_jobs(domain,dataset_id,binding,algo,status,sample_count)"
                " VALUES(?,?,?,?,?,?)",
                (domain, dataset_id, binding, algo, JOB_PENDING, n))
            self._conn.commit()
        return int(cur.lastrowid)

    def update_job(self, job_id: int, *, status: str | None = None, message: str | None = None,
                   progress: float | None = None, artifact_id: int | None = None) -> None:
        """推进任务状态。★**终态不再变** —— 已失败的任务被后来的写覆盖成"就绪"，
        界面上就再也看不出它失败过。"""
        sets, args = [], []
        with self._lock:
            row = self._conn.execute("SELECT status FROM train_jobs WHERE id=?",
                                     (job_id,)).fetchone()
            if row is None:
                raise WorkbenchError(f"没有 id={job_id} 这个训练任务")
            if row["status"] in _JOB_TERMINAL:
                raise WorkbenchError(
                    f"任务 {job_id} 已是终态 {row['status']}，不能再改"
                    "（终态被覆盖，界面上就看不出它失败过）")
            if status is not None:
                if status not in (JOB_PENDING, JOB_RUNNING, *_JOB_TERMINAL):
                    raise WorkbenchError(f"未知任务状态 {status!r}")
                sets.append("status=?"); args.append(status)
                if status == JOB_RUNNING:
                    sets.append("started_at=datetime('now')")
                elif status in _JOB_TERMINAL:
                    sets.append("finished_at=datetime('now')")
            if message is not None:
                sets.append("message=?"); args.append(message)
            if progress is not None:
                sets.append("progress=?"); args.append(max(0.0, min(1.0, float(progress))))
            if artifact_id is not None:
                sets.append("artifact_id=?"); args.append(artifact_id)
            if not sets:
                return
            self._conn.execute(f"UPDATE train_jobs SET {','.join(sets)} WHERE id=?",
                               (*args, job_id))
            self._conn.commit()

    def get_job(self, job_id: int) -> TrainJob | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM train_jobs WHERE id=?",
                                     (job_id,)).fetchone()
        return None if row is None else _to_job(row)

    def list_jobs(self, *, domain: str = "", status: str = "",
                  offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> Page:
        offset, limit = _page_args(offset, limit)
        where, args = [], []
        if domain:
            where.append("domain=?"); args.append(domain)
        if status:
            where.append("status=?"); args.append(status)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) FROM train_jobs{sql_where}", args).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM train_jobs{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset)).fetchall()
        return Page([_to_job(r) for r in rows], total, offset, limit)

    # ── 模型工件 ──────────────────────────────────────────────────────────
    def add_artifact(self, *, domain: str, name: str, kind: str = KIND_MODEL,
                     binding: str = "", algo: str = "", dataset_id: int | None = None,
                     sample_count: int = 0, feature_count: int = 0,
                     accuracy: float | None = None, path: str = "", size: int = 0,
                     sha256: str = "", meta_json: str = "{}",
                     origin: str = "trained", source: str = "", training_data: str = "",
                     license: str = "", data_origin: str = "") -> int:
        if not domain or not name:
            raise WorkbenchError("工件必须有域与名字")
        if origin == "imported" and not (source.strip() and training_data.strip() and license.strip()):
            # 第二道（第一道在 artifact_import）：来历不明的外部模型不许进库。
            raise WorkbenchError("外部导入的工件必须写明来源、训练数据说明与许可（不清楚就写「未知」）")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO artifacts(domain,kind,binding,name,algo,dataset_id,sample_count,"
                "feature_count,accuracy,path,size,sha256,meta_json,origin,source,training_data,"
                "license,data_origin)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (domain, kind, binding, name, algo, dataset_id, sample_count,
                 feature_count, accuracy, path, size, sha256, meta_json,
                 origin, source, training_data, license,
                 dataorigin.normalize(data_origin)))
            self._conn.commit()
        return int(cur.lastrowid)

    def activate_artifact(self, artifact_id: int) -> None:
        """激活一个工件；同一 (域,种类,对象) 下原来那个自动让位。一个事务。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM artifacts WHERE id=?",
                                     (artifact_id,)).fetchone()
            if row is None:
                raise WorkbenchError(f"没有 id={artifact_id} 这个工件")
            try:
                self._conn.execute(
                    "UPDATE artifacts SET active=0 WHERE domain=? AND kind=? AND binding=?"
                    " AND active=1", (row["domain"], row["kind"], row["binding"]))
                self._conn.execute("UPDATE artifacts SET active=1 WHERE id=?", (artifact_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def deactivate_artifact(self, artifact_id: int) -> bool:
        """停用一个工件。返回"本来是不是激活的"（已经停用的回 `False`，不是错）。

        ★**为什么必须有这一口**：此前只有"启用"没有"停用" —— 启用只让**同作用域**
          (域,种类,对象) 的旧件让位，于是作用域本身建错的那个件（例如训练时 `binding`
          传了空串，落成"全域通用"）**永远没法取消**：同作用域里没有别的件能顶掉它，
          而 `delete_artifact` 又拒删激活中的。2026-09-16 现场真撞上了。

        ★停用之后那个对象就**没有可用工件**了 —— 该域的结论会落
          `MODEL_NOT_LOADED(-1034)`，这是**对的**：没有基线就该说没有基线，
          不是退回"随便挑一个最新的"。调用方要清楚这一点，所以命令行入口要求 `--yes`。
        """
        with self._lock:
            row = self._conn.execute("SELECT active FROM artifacts WHERE id=?",
                                     (artifact_id,)).fetchone()
            if row is None:
                raise WorkbenchError(f"没有 id={artifact_id} 这个工件")
            if not row["active"]:
                return False
            self._conn.execute("UPDATE artifacts SET active=0 WHERE id=?", (artifact_id,))
            self._conn.commit()
        return True

    def active_artifact(self, domain: str, kind: str = KIND_MODEL,
                        binding: str = "") -> Artifact | None:
        """取当前激活的那个。**没有就是没有** —— 调用方据此落 `MODEL_NOT_LOADED`，
        不要退回"随便挑一个最新的"（那会让"我在用哪个模型"这个问题答不上来）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE domain=? AND kind=? AND binding=? AND active=1",
                (domain, kind, binding)).fetchone()
        return None if row is None else _to_artifact(row)

    def list_artifacts(self, *, domain: str = "", kind: str = "", binding: str | None = None,
                       offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> Page:
        offset, limit = _page_args(offset, limit)
        where, args = [], []
        if domain:
            where.append("domain=?"); args.append(domain)
        if kind:
            where.append("kind=?"); args.append(kind)
        if binding is not None:
            where.append("binding=?"); args.append(binding)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) FROM artifacts{sql_where}", args).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM artifacts{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset)).fetchall()
        return Page([_to_artifact(r) for r in rows], total, offset, limit)

    def find_artifact_by_sha256(self, domain: str, kind: str, sha256: str) -> Artifact | None:
        """同域同种类里有没有字节相同的工件。导入去重用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE domain=? AND kind=? AND sha256=? ORDER BY id LIMIT 1",
                (domain, kind, sha256)).fetchone()
        return None if row is None else _to_artifact(row)

    def delete_artifact(self, artifact_id: int) -> bool:
        """删工件记录。★**激活中的不让删** —— 删了之后那个对象就在"用着一个不存在的模型"。"""
        with self._lock:
            row = self._conn.execute("SELECT active FROM artifacts WHERE id=?",
                                     (artifact_id,)).fetchone()
            if row is None:
                return False
            if row["active"]:
                raise WorkbenchError(
                    f"工件 {artifact_id} 正在激活中，先切到别的再删"
                    "（直接删会让那个对象用着一个不存在的模型）")
            self._conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
            self._conn.commit()
        return True

    # ── 片段与报告 ────────────────────────────────────────────────────────
    def put_segment(self, *, domain: str, binding: str, t_from: datetime, t_to: datetime,
                    name: str = "", source: str = "", point_count: int = 0,
                    note: str = "") -> int:
        a, b = _us(t_from, "t_from"), _us(t_to, "t_to")
        if b <= a:
            raise WorkbenchError(f"片段时间区间倒挂或为零长: {t_from} → {t_to}")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO segments(domain,binding,t_from_us,t_to_us,name,source,point_count,note)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (domain, binding, a, b, name, source, point_count, note))
            self._conn.commit()
        return int(cur.lastrowid)

    def list_segments(self, *, domain: str = "", binding: str = "",
                      offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> Page:
        offset, limit = _page_args(offset, limit)
        where, args = [], []
        if domain:
            where.append("domain=?"); args.append(domain)
        if binding:
            where.append("binding=?"); args.append(binding)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) FROM segments{sql_where}", args).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM segments{sql_where} ORDER BY t_from_us DESC, id DESC"
                " LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return Page([_to_segment(r) for r in rows], total, offset, limit)

    def get_segment(self, segment_id: int) -> Segment | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM segments WHERE id=?",
                                     (segment_id,)).fetchone()
        return None if row is None else _to_segment(row)

    def delete_segment(self, segment_id: int) -> bool:
        """删片段连它的报告记录一起（报告是这个片段的产物）。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM segments WHERE id=?", (segment_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def add_report(self, *, segment_id: int, kind: str = "basic", path: str = "",
                   size: int = 0) -> int:
        with self._lock:
            if self._conn.execute("SELECT 1 FROM segments WHERE id=?",
                                  (segment_id,)).fetchone() is None:
                raise WorkbenchError(f"没有 id={segment_id} 这个片段")
            cur = self._conn.execute(
                "INSERT INTO reports(segment_id,kind,path,size) VALUES(?,?,?,?)",
                (segment_id, kind, path, size))
            self._conn.commit()
        return int(cur.lastrowid)

    def list_reports(self, segment_id: int) -> list[Report]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reports WHERE segment_id=? ORDER BY id DESC",
                (segment_id,)).fetchall()
        return [_to_report(r) for r in rows]

    # ── 跨帧状态 ──────────────────────────────────────────────────────────
    #
    # ★这一格与上面几张表**性质不同**：标注/训练集/工件是**资产**（人看得见、要能重采，
    #   见 README §9.3），跨帧状态是**运行状态** —— 没人会去"查看"它，它只是让下一拍
    #   接着上一拍算。所以它不进 `artifacts`，也不进 `stats()` 的资产计数。
    #
    # ★但它仍然要**落库**而不是搁在进程内存里：搁内存 = 进程一重启就从头攒，
    #   而界面上看不出"这条趋势是从什么时候开始的"（README §11.3 否掉「让模块自己攒」
    #   的正是这一条）。于是多一列 `since`：这份状态是从哪一刻起攒的，答得出来。

    def get_domain_state(self, domain: str, binding: str) -> "DomainState | None":
        """读一条跨帧状态。没有就是 `None`（第一拍）。

        ★**存坏了也返回 `None`**（并记错），不抛：一条状态读不出来不该让这条诊断
          从此再也跑不了一拍。丢状态是退化成"从头攒"，停止诊断是彻底没结论。
        """
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM domain_state WHERE domain=? AND binding=?",
                (domain, binding)).fetchone()
        if r is None:
            return None
        try:
            state = json.loads(r["state_json"])
        except ValueError as exc:
            logger.error("%s/%s 的跨帧状态解不出来（已当作没有，将从头攒）：%s",
                         domain, binding, exc)
            return None
        if not isinstance(state, dict):
            logger.error("%s/%s 的跨帧状态不是 JSON 对象而是 %s（已当作没有）",
                         domain, binding, type(state).__name__)
            return None
        return DomainState(domain=domain, binding=binding, state=state,
                           since=r["since"], updated_at=r["updated_at"],
                           writes=int(r["writes"]))

    def put_domain_state(self, domain: str, binding: str, state: Mapping[str, Any]) -> int:
        """写一条跨帧状态，返回序列化后的字节数。

        ★`since` **只在第一次写时定下，之后原样留着** —— 它要回答"这份状态从哪一刻起攒的"，
          每次覆盖都刷新就等于永远回答"刚刚"，那一格也就白留了。
          要重新计时只有一条路：`clear_domain_state()`（删掉整条，下次再写就是新的 `since`）。

        不合规一律**抛 `WorkbenchError`**，由调用方决定怎么办（骨架的做法是保留旧状态并记错）。
        """
        if not domain or not binding:
            raise WorkbenchError("跨帧状态必须说清是哪个域的哪个对象")
        if not isinstance(state, dict):
            raise WorkbenchError(
                f"跨帧状态必须是 JSON 对象（dict），收到 {type(state).__name__}")
        bad = [k for k in state if not isinstance(k, str)]
        if bad:
            raise WorkbenchError(f"跨帧状态的键必须都是字符串，这些不是：{bad!r}")
        try:
            # ★`allow_nan=False`：NaN/Infinity **不是合法 JSON**，Python 却默认写成裸的
            #   `NaN`，别的读者（前端、jq、别的语言）一概解不出来 —— 与 Finding 里挡
            #   NaN 是同一条理由，只不过这里是写出去的那一端。
            blob = json.dumps(state, ensure_ascii=False, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise WorkbenchError(f"跨帧状态存不成 JSON：{exc}") from exc
        size = len(blob.encode("utf-8"))
        if size > MAX_STATE_BYTES:
            raise WorkbenchError(
                f"跨帧状态 {size} 字节，超过上限 {MAX_STATE_BYTES} 字节 —— "
                "这份状态每拍写一次库，越攒越大会拖慢每一拍；请只留必要的摘要，别把整条轨迹堆在里面")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            # ON CONFLICT 只更新值与计数，**不动 since**。
            self._conn.execute(
                "INSERT INTO domain_state(domain,binding,state_json,since,updated_at,writes)"
                " VALUES(?,?,?,?,?,1)"
                " ON CONFLICT(domain,binding) DO UPDATE SET"
                "   state_json=excluded.state_json, updated_at=excluded.updated_at,"
                "   writes=domain_state.writes+1",
                (domain, binding, blob, now, now))
            self._conn.commit()
        return size

    def clear_domain_state(self, domain: str, binding: str) -> bool:
        """删掉一条跨帧状态。返回是否真删掉了。

        ★删绑定时**必须**调它。`BindingStore.delete` 有意不删结论点（那是历史，见它的文档），
          但状态不是历史，是"算到哪儿了"：留着的话，日后重建**同名**绑定会**悄悄接上
          上一条早已作废的轨迹** —— 界面上是一条从没断过的趋势，而中间那段根本没在算。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM domain_state WHERE domain=? AND binding=?", (domain, binding))
            self._conn.commit()
        return cur.rowcount > 0

    # ── 自检用 ────────────────────────────────────────────────────────────
    def stats(self, domain: str = "") -> dict[str, int]:
        """各表条数。给 `GetInfo`/自检用，也给界面上那几个统计数字。"""
        out: dict[str, int] = {}
        with self._lock:
            for table in ("annotations", "datasets", "samples", "artifacts",
                          "train_jobs", "segments", "reports"):
                if domain and table in ("annotations", "datasets", "artifacts",
                                        "train_jobs", "segments"):
                    n = self._conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE domain=?", (domain,)).fetchone()[0]
                else:
                    n = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                out[table] = int(n)
        return out


# ─────────────────────────── 行 → 数据类 ───────────────────────────

def _to_annotation(r: sqlite3.Row) -> Annotation:
    return Annotation(id=int(r["id"]), domain=r["domain"], binding=r["binding"],
                      t_from=_dt(r["t_from_us"]), t_to=_dt(r["t_to_us"]),
                      label=r["label"], note=r["note"],
                      created_at=r["created_at"], updated_at=r["updated_at"])


def _to_dataset(r: sqlite3.Row) -> Dataset:
    keys = r.keys()
    return Dataset(id=int(r["id"]), domain=r["domain"], name=r["name"], note=r["note"],
                   sample_count=int(r["n"]) if "n" in keys else 0,
                   created_at=r["created_at"], updated_at=r["updated_at"])


def _to_sample(r: sqlite3.Row) -> Sample:
    ann = r["annotation_id"]
    return Sample(id=int(r["id"]), dataset_id=int(r["dataset_id"]),
                  annotation_id=None if ann is None else int(ann),
                  binding=r["binding"], t_from=_dt(r["t_from_us"]), t_to=_dt(r["t_to_us"]),
                  label=r["label"], created_at=r["created_at"],
                  data_origin=r["data_origin"] or "")


def _to_artifact(r: sqlite3.Row) -> Artifact:
    ds = r["dataset_id"]
    return Artifact(id=int(r["id"]), domain=r["domain"], kind=r["kind"], binding=r["binding"],
                    name=r["name"], algo=r["algo"],
                    dataset_id=None if ds is None else int(ds),
                    sample_count=int(r["sample_count"]), feature_count=int(r["feature_count"]),
                    accuracy=None if r["accuracy"] is None else float(r["accuracy"]),
                    active=bool(r["active"]), path=r["path"], size=int(r["size"]),
                    sha256=r["sha256"], meta_json=r["meta_json"], created_at=r["created_at"],
                    origin=r["origin"], source=r["source"], training_data=r["training_data"],
                    license=r["license"], data_origin=r["data_origin"] or "")


def _to_job(r: sqlite3.Row) -> TrainJob:
    art = r["artifact_id"]
    return TrainJob(id=int(r["id"]), domain=r["domain"], dataset_id=int(r["dataset_id"]),
                    binding=r["binding"], algo=r["algo"], status=r["status"],
                    message=r["message"], progress=float(r["progress"]),
                    sample_count=int(r["sample_count"]),
                    artifact_id=None if art is None else int(art),
                    created_at=r["created_at"], started_at=r["started_at"] or "",
                    finished_at=r["finished_at"] or "")


def _to_segment(r: sqlite3.Row) -> Segment:
    return Segment(id=int(r["id"]), domain=r["domain"], binding=r["binding"],
                   t_from=_dt(r["t_from_us"]), t_to=_dt(r["t_to_us"]), name=r["name"],
                   source=r["source"], point_count=int(r["point_count"]), note=r["note"],
                   created_at=r["created_at"])


def _to_report(r: sqlite3.Row) -> Report:
    return Report(id=int(r["id"]), segment_id=int(r["segment_id"]), kind=r["kind"],
                  path=r["path"], size=int(r["size"]), created_at=r["created_at"])
