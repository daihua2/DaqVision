"""跨帧状态（U5 / 视频定案 V8）：骨架为每条诊断存一份状态，推理时传入、算完带回。

为什么这一格在骨架而不在模块里（README §11.3 的裁定）：模块自己在实例里攒的东西
**进程一重启就归零**，而界面上看不出"这条趋势是从什么时候开始攒的"。

钉的：
  ① 声明了 stateful 才给状态；没声明的域恒拿空字典，也存不进去；
  ② 第一拍是空字典（不是 None）；下一拍拿得到上一拍带回的那份；跨重启接得上；
  ③ `state=None` = 不动（保留上一拍），`state={}` = 明确清空 —— 两者不同；
  ④ 模块抛异常这一拍：状态原样不动（什么都没算出来，凭什么改"算到哪儿了"）；
  ⑤ 存不下去（非 JSON / NaN / 超上限）：保留旧状态 + RunResult.state_error，
     但结论照写、ok 仍为 True；
  ⑥ `since` 只在第一次写时定下，之后原样留着（否则永远回答"刚刚"）；
  ⑦ 违规帧（没可信输入却出 OK 结论）不许把这一拍攒进状态；
  ⑧ 存坏了读出来当"没有"而不是抛 —— 丢状态是退化，停诊断是彻底没结论；
  ⑨ 落库的是真 JSON，别的读者解得开（不是 repr、不含裸 NaN）。

回溯判别不许动状态也钉在这里；删绑定连带清状态钉在 test_api.py。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import Domain, LoadedDomain
from aiintegration.quality import Quality
from aiintegration.runner import run_domain
from aiintegration.types import (
    MAX_STATE_BYTES, Declaration, Finding, Frame, InferOut, InputSpec, OutputSpec, Sample,
)
from aiintegration.workbench import Workbench, WorkbenchError

UTC = timezone.utc
T0 = datetime(2026, 9, 18, 8, 0, 0, tzinfo=UTC)


class Counter(Domain):
    """每拍把 count 加一并写进状态 —— 最小的"跨帧"行为。"""
    key = "counter"
    display = "计数"
    version = "1.0.0"
    stateful = True
    #: 用例逐条改这个来指定"这一拍返回什么状态"。
    next_state = "increment"

    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x", unit="mm/s"),),
            outputs=(OutputSpec(key="n", display="第几拍", value_type="float"),),
            stateful=self.stateful)

    def infer(self, frame):
        n = int(frame.state.get("count", 0)) + 1
        f = [Finding(key="n", value=float(n), quality=Quality.OK, t=frame.t_end)]
        if self.next_state == "increment":
            return InferOut(f, state={"count": n})
        if self.next_state == "keep":
            return InferOut(f, state=None)
        if self.next_state == "clear":
            return InferOut(f, state={})
        if self.next_state == "plain_list":
            return f
        return InferOut(f, state=self.next_state)


def _loaded(inst: Domain) -> LoadedDomain:
    return LoadedDomain(inst, inst.declare(), frozenset(inst.capabilities()), Path("用例内造"))


def _frame(binding="dev1", good=True):
    q = Quality.OK if good else Quality.INPUT_BAD
    return Frame(domain="counter", binding=binding, t_start=T0 - timedelta(seconds=60),
                 t_end=T0, channels={"x": [Sample(t=T0, value=1.0, quality=q)]})


class DomainStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "wb.db"
        self.wb = Workbench(self.db)
        self.dom = Counter()
        self.loaded = _loaded(self.dom)

    def tearDown(self):
        self.wb.close()
        self._tmp.cleanup()

    # ── ① 声明了才给 ──────────────────────────────────────────────────────
    def test_没声明stateful的域拿不到也存不进状态(self):
        self.dom.stateful = False
        loaded = _loaded(self.dom)
        self.wb.put_domain_state("counter", "dev1", {"count": 7})
        seen = {}

        def infer(frame):
            seen["state"] = dict(frame.state)
            return InferOut([Finding(key="n", value=1.0, quality=Quality.OK, t=frame.t_end)],
                            state={"count": 999})

        self.dom.infer = infer
        with self.assertLogs("aiintegration.runner", level="ERROR") as log:
            res = run_domain(loaded, _frame(), states=self.wb)
        self.assertTrue(res.ok)
        self.assertEqual(seen["state"], {}, "没声明 stateful 却拿到了状态")
        self.assertIn("没有声明 stateful", "\n".join(log.output))
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state, {"count": 7},
                         "库里那份被一个没声明的域覆盖了")

    def test_调用方硬塞的状态也会被清掉(self):
        """"声明了才给"不能是一句空话，否则域作者会依赖上一个没声明的偶然。"""
        self.dom.stateful = False
        loaded = _loaded(self.dom)
        seen = {}

        def infer(frame):
            seen["state"] = dict(frame.state)
            return [Finding(key="n", value=1.0, quality=Quality.OK, t=frame.t_end)]

        self.dom.infer = infer
        frame = Frame(domain="counter", binding="dev1", t_start=T0 - timedelta(seconds=60),
                      t_end=T0, channels={"x": [Sample(t=T0, value=1.0, quality=Quality.OK)]},
                      state={"偷塞的": 1})
        run_domain(loaded, frame, states=self.wb)
        self.assertEqual(seen["state"], {})

    # ── ② 第一拍 / 接上一拍 ───────────────────────────────────────────────
    def test_第一拍是空字典而不是None(self):
        seen = {}
        real = self.dom.infer
        self.dom.infer = lambda f: (seen.update(state=f.state) or real(f))
        run_domain(self.loaded, _frame(), states=self.wb)
        self.assertEqual(seen["state"], {})
        self.assertIsNotNone(seen["state"], "第一拍给 None 会逼每个模块写判空")

    def test_下一拍接得上上一拍(self):
        for expect in (1.0, 2.0, 3.0):
            res = run_domain(self.loaded, _frame(), states=self.wb)
            self.assertEqual(res.findings[0].value, expect)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 3)

    def test_跨进程重启接得上(self):
        """状态落库的意义就在这 —— 换一个 Workbench 实例（= 重启）还接得上。"""
        run_domain(self.loaded, _frame(), states=self.wb)
        run_domain(self.loaded, _frame(), states=self.wb)
        self.wb.close()
        self.wb = Workbench(self.db)
        res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertEqual(res.findings[0].value, 3.0, "重启后又从头攒了")

    def test_不同绑定各攒各的(self):
        run_domain(self.loaded, _frame(binding="dev1"), states=self.wb)
        run_domain(self.loaded, _frame(binding="dev1"), states=self.wb)
        res = run_domain(self.loaded, _frame(binding="dev2"), states=self.wb)
        self.assertEqual(res.findings[0].value, 1.0, "dev2 串到了 dev1 的状态")

    def test_没接存放处就每拍从头攒(self):
        for _ in range(3):
            res = run_domain(self.loaded, _frame(), states=None)
            self.assertEqual(res.findings[0].value, 1.0)

    def test_回溯判别既不读也不写线上那份状态(self):
        """`service.rediagnose` 那条路**故意不传 states** —— 这条用例钉的就是"不传"的后果。

        它问的是"当时若用现在的模型会怎样"，既不写回实时库，也不许动线上
        "算到哪儿了"：拿几个月前的片段推一遍就把状态覆盖成那时候的，
        而线上这条诊断还在跑，往后每一拍都接在错的地方，且看不出来。
        """
        run_domain(self.loaded, _frame(), states=self.wb)
        run_domain(self.loaded, _frame(), states=self.wb)          # count=2
        seen = {}
        real = self.dom.infer
        self.dom.infer = lambda f: (seen.update(state=dict(f.state)) or real(f))

        res = run_domain(self.loaded, _frame(), states=None)        # ← 回溯判别那条路

        self.assertEqual(seen["state"], {}, "回溯判别读到了线上的状态")
        self.assertEqual(res.findings[0].value, 1.0)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 2,
                         "回溯判别把线上'算到哪儿了'覆盖掉了")

    def test_回溯判别整条路都不碰线上状态(self):
        """上一条钉的是 `run_domain` 的语义，这一条钉的是**回溯判别那条路真的没传**。

        `rediagnose_segment` 提到模块级就是为了这条：它**没有 states 参数**，
        单测拿假 fetcher 就能整条走一遍。此前它是 `Service.run()` 里的闭包，
        要真 hs 才走得到 —— 变异验证里"把 states 传进去"一条用例都不红。
        """
        from aiintegration.service import rediagnose_segment

        run_domain(self.loaded, _frame(), states=self.wb)
        run_domain(self.loaded, _frame(), states=self.wb)          # count=2

        class _Seg:
            domain, binding = "counter", "dev1"
            t_from, t_to = T0 - timedelta(seconds=60), T0

        class _Fetcher:
            def fetch(self, b, end_time, artifacts=None):
                return _frame()

        class _Arts:
            #: ★变异验证的抓手：给它挂上工作台库，模拟"有人把状态存放处递进这条路"。
            #  正常路径下 `rediagnose_segment` 拿不到它 —— 它没有 states 这个参数。
            wb = self.wb

            def for_binding(self, domain, binding):
                return {}

        class _Bindings:
            def get(self, domain, binding):
                return object()

        pairs, note = rediagnose_segment(
            _Seg(), domains={"counter": self.loaded}, bindings=_Bindings(),
            fetcher=_Fetcher(), artifacts=_Arts())

        self.assertEqual(pairs[0][0].value, 1.0, "回溯判别读到了线上的状态")
        self.assertIn("未写回实时库", note)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 2,
                         "回溯判别把线上'算到哪儿了'覆盖掉了")

    # ── ③ None 与 {} 不是一回事 ───────────────────────────────────────────
    def test_state为None是不动(self):
        run_domain(self.loaded, _frame(), states=self.wb)          # count=1
        self.dom.next_state = "keep"
        run_domain(self.loaded, _frame(), states=self.wb)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 1)

    def test_state为空字典是清空(self):
        run_domain(self.loaded, _frame(), states=self.wb)
        self.dom.next_state = "clear"
        run_domain(self.loaded, _frame(), states=self.wb)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state, {})
        self.dom.next_state = "increment"
        res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertEqual(res.findings[0].value, 1.0, "清空后没有从头攒")

    def test_照旧返回列表的域一切照常(self):
        """签名没变：不关心状态的域 `return [Finding…]`，一行不用改。"""
        self.dom.next_state = "plain_list"
        res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertTrue(res.ok)
        self.assertEqual(res.findings[0].value, 1.0)
        self.assertIsNone(self.wb.get_domain_state("counter", "dev1"))

    # ── ④ 算不出来就不动状态 ──────────────────────────────────────────────
    def test_模块抛异常时状态原样不动(self):
        run_domain(self.loaded, _frame(), states=self.wb)          # count=1

        def boom(frame):
            raise RuntimeError("炸")

        self.dom.infer = boom
        res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertFalse(res.ok)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 1,
                         "这一拍什么都没算出来，却改了'算到哪儿了'")

    # ── ⑤ 存不下去：保留旧的、吵、但结论照写 ──────────────────────────────
    def test_存不成JSON时保留旧状态且结论照写(self):
        run_domain(self.loaded, _frame(), states=self.wb)
        self.dom.next_state = {"bad": object()}
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertTrue(res.ok, "存不下状态不该把一次正常推理判成失败")
        self.assertTrue(res.findings, "结论被状态问题连累丢了")
        self.assertTrue(res.state_error)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 1)

    def test_NaN存不进去(self):
        """NaN/Infinity 不是合法 JSON，Python 却默认写成裸 NaN，别的读者一概解不出。"""
        self.dom.next_state = {"v": float("nan")}
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertTrue(res.state_error)
        self.assertIsNone(self.wb.get_domain_state("counter", "dev1"))

    def test_超上限存不进去(self):
        self.dom.next_state = {"blob": "x" * (MAX_STATE_BYTES + 10)}
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertTrue(res.ok)
        self.assertIn("超过上限", res.state_error)
        self.assertIsNone(self.wb.get_domain_state("counter", "dev1"))

    def test_键不是字符串存不进去(self):
        self.dom.next_state = {1: "一"}
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertTrue(res.state_error)

    def test_InferOut当场挡住非dict状态(self):
        with self.assertRaises(TypeError):
            InferOut([], state=[1, 2, 3])
        with self.assertRaises(TypeError):
            InferOut("不是列表")

    # ── ⑥ since 不刷新 ────────────────────────────────────────────────────
    def test_since只在第一次写时定下(self):
        """★这条**不能**靠"两次写挨得近所以 since 相同"来验 —— since 只精确到秒，
           同一秒内写两次，就算实现里每次都刷新，用例也照样绿。
           所以先把 since 摆到一个确定的老时刻，再看它动没动。
        """
        self.wb.put_domain_state("counter", "dev1", {"count": 1})
        old = "2020-01-01T00:00:00+00:00"
        with self.wb._lock:
            self.wb._conn.execute("UPDATE domain_state SET since=?", (old,))
            self.wb._conn.commit()
        self.wb.put_domain_state("counter", "dev1", {"count": 2})
        again = self.wb.get_domain_state("counter", "dev1")
        self.assertEqual(again.since, old,
                         "since 每次覆盖都刷新，就等于永远回答'刚刚'，那一格白留了")
        self.assertEqual(again.state["count"], 2, "值没更新")
        self.assertEqual(again.writes, 2)

    def test_清空后再写是从头计的(self):
        self.wb.put_domain_state("counter", "dev1", {"count": 1})
        self.assertTrue(self.wb.clear_domain_state("counter", "dev1"))
        self.assertFalse(self.wb.clear_domain_state("counter", "dev1"))
        self.wb.put_domain_state("counter", "dev1", {"count": 1})
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").writes, 1)

    # ── ⑦ 违规帧不进状态 ──────────────────────────────────────────────────
    def test_没可信输入却出OK结论时不许攒进状态(self):
        run_domain(self.loaded, _frame(), states=self.wb)          # count=1
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.loaded, _frame(good=False), states=self.wb)
        self.assertTrue(res.error, "违规没被记下来")
        self.assertTrue(res.state_error)
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 1,
                         "已证实有缺陷的那一拍被攒进了会跨重启留着的状态")

    # ── ⑧⑨ 存坏了 / 真 JSON ──────────────────────────────────────────────
    def test_落库的是别人也解得开的JSON(self):
        self.wb.put_domain_state("counter", "dev1", {"中文": [1, 2], "b": True})
        row = self.wb._conn.execute(
            "SELECT state_json FROM domain_state WHERE domain='counter'").fetchone()
        self.assertEqual(json.loads(row["state_json"]), {"中文": [1, 2], "b": True})

    def test_存坏了读出来当没有而不是抛(self):
        """一条状态读不出来，不该让这条诊断从此再也跑不了一拍。"""
        self.wb.put_domain_state("counter", "dev1", {"count": 1})
        with self.wb._lock:
            self.wb._conn.execute("UPDATE domain_state SET state_json='{坏'")
            self.wb._conn.commit()
        with self.assertLogs("aiintegration.workbench", level="ERROR"):
            self.assertIsNone(self.wb.get_domain_state("counter", "dev1"))
        res = run_domain(self.loaded, _frame(), states=self.wb)
        self.assertEqual(res.findings[0].value, 1.0, "状态坏了就该从头攒，而不是停诊断")

    def test_存的不是JSON对象时读出来当没有(self):
        with self.wb._lock:
            self.wb._conn.execute(
                "INSERT INTO domain_state(domain,binding,state_json,since,updated_at)"
                " VALUES('counter','dev9','[1,2]','t','t')")
            self.wb._conn.commit()
        with self.assertLogs("aiintegration.workbench", level="ERROR"):
            self.assertIsNone(self.wb.get_domain_state("counter", "dev9"))

    def test_库层直接拒非法状态(self):
        for bad in ({"v": float("inf")}, {"v": object()}, {2: "x"}):
            with self.assertRaises(WorkbenchError):
                self.wb.put_domain_state("counter", "dev1", bad)
        with self.assertRaises(WorkbenchError):
            self.wb.put_domain_state("", "dev1", {})
        with self.assertRaises(WorkbenchError):
            self.wb.put_domain_state("counter", "dev1", [1, 2])

    def test_老库自己长出状态表(self):
        """AISERVER 上已有老库 —— 建表语句要能在既有库上补出这张表。"""
        self.assertIsNone(self.wb.get_domain_state("counter", "从没写过"))
        self.wb.close()
        self.wb = Workbench(self.db)
        self.wb.put_domain_state("counter", "dev1", {"count": 1})
        self.assertEqual(self.wb.get_domain_state("counter", "dev1").state["count"], 1)


if __name__ == "__main__":
    unittest.main()
