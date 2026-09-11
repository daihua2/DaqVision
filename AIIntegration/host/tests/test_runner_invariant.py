"""骨架硬规则：**没有任何可信输入，就不许出质量 OK 的结论**。

来由：2026-09-11 对原 v5 真跑实测 —— 阶次规则诊断拿"没有数据"判出了「正常 0.71」，
支持理由里还写着"1X 占比很高"（总幅值为 0 时代码把 1X 占比写死成 100%）。
这类错单看结论完全像真的。新写的域自己守住了，但骨架此前不查，于是落到骨架。

钉的：
  ① 没样本 → 违规的 OK 结论改落 NO_INPUT、值清空；
  ② 有样本但全不可信 → 改落 INPUT_BAD（与"没样本"分开，处置方向不同）；
  ③ 有一个可信样本、或有图片输入 → 放行（本规则只挡"完全没数据"）；
  ④ 本来就是坏质量的结论不动；
  ⑤ 违规要吵（ERROR 日志 + RunResult.error），不许静默修正；
  ⑥ 现有真域在无数据时本就守规，不触发。
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domains import Domain, LoadedDomain, discover
from aiintegration.quality import Quality
from aiintegration.runner import run_domain
from aiintegration.types import (
    Declaration, Finding, Frame, InputBlob, InputSpec, OutputSpec, Sample,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=UTC)
DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"


class AlwaysNormal(Domain):
    """模拟原 v5 那种写法：不管有没有数据，都判"正常"。"""
    key = "always_normal"
    display = "总说正常"
    version = "1.0.0"

    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x", unit="mm/s"),),
            outputs=(OutputSpec(key="verdict", display="结论", value_type="string"),
                     OutputSpec(key="score", display="分", value_type="float"),
                     OutputSpec(key="evidence", display="摘要", value_type="string")))

    def infer(self, frame):
        t = frame.t_end
        return [Finding(key="verdict", value="正常", quality=Quality.OK, t=t),
                Finding(key="score", value=0.71, quality=Quality.OK, t=t),
                Finding(key="evidence", value="本来就标了没数据", quality=Quality.NO_INPUT, t=t)]


def _loaded(inst: Domain) -> LoadedDomain:
    return LoadedDomain(inst, inst.declare(), frozenset(inst.capabilities()), Path("用例内造"))


def _frame(channels=None, blobs=None):
    return Frame(domain="always_normal", binding="dev1", t_start=T0 - timedelta(seconds=60),
                 t_end=T0, channels=channels if channels is not None else {}, blobs=blobs or {})


def _sample(q):
    return Sample(t=T0 - timedelta(seconds=1), value=1.0, quality=q,
                  status_code=1 if q is Quality.OK else -1000)


class TestNoTrustedInput(unittest.TestCase):
    def setUp(self):
        self.dom = _loaded(AlwaysNormal())

    def run_it(self, frame):
        return {f.key: f for f in run_domain(self.dom, frame).findings}, run_domain(self.dom, frame)

    def test_没样本时OK结论改落NO_INPUT且值清空(self):
        for frame in (_frame({"x": []}), _frame({})):
            with self.assertLogs("aiintegration.runner", level="ERROR"):
                res = run_domain(self.dom, frame)
            got = {f.key: f for f in res.findings}
            for k in ("verdict", "score"):
                self.assertIs(got[k].quality, Quality.NO_INPUT, k)
                self.assertIsNone(got[k].value, f"{k}：坏质量下不许留着编出来的值")

    def test_只有坏样本时改落INPUT_BAD(self):
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.dom, _frame({"x": [_sample(Quality.INPUT_BAD)] * 3}))
        got = {f.key: f for f in res.findings}
        self.assertIs(got["verdict"].quality, Quality.INPUT_BAD)
        self.assertIsNone(got["score"].value)

    def test_有一个可信样本就放行(self):
        res = run_domain(self.dom, _frame({"x": [_sample(Quality.INPUT_BAD), _sample(Quality.OK)]}))
        got = {f.key: f for f in res.findings}
        self.assertIs(got["verdict"].quality, Quality.OK)
        self.assertEqual(got["verdict"].value, "正常")
        self.assertEqual(res.error, "")

    def test_有图片输入就放行(self):
        blob = InputBlob(t=T0, content_type="image/png", data=b"\x89PNG")
        res = run_domain(self.dom, _frame({}, {"image": blob}))
        self.assertIs({f.key: f for f in res.findings}["score"].quality, Quality.OK)
        self.assertEqual(res.error, "")

    def test_本就是坏质量的结论不动(self):
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.dom, _frame({"x": []}))
        ev = {f.key: f for f in res.findings}["evidence"]
        self.assertIs(ev.quality, Quality.NO_INPUT)
        self.assertEqual(ev.value, "本来就标了没数据")

    def test_违规要吵且写进结果(self):
        with self.assertLogs("aiintegration.runner", level="ERROR") as logs:
            res = run_domain(self.dom, _frame({"x": []}))
        self.assertTrue(res.ok, "模块本身跑完了，ok 仍为真；违规写在 error 里")
        self.assertIn("没有任何可信输入", res.error)
        self.assertIn("verdict", res.error)
        self.assertIn("没有任何可信输入", "\n".join(logs.output))

    def test_结论条数不变(self):
        with self.assertLogs("aiintegration.runner", level="ERROR"):
            res = run_domain(self.dom, _frame({"x": []}))
        self.assertEqual(len(res.findings), 3, "改落坏质量，不是删掉 —— 删掉等于说'这段没数据'")


class TestRealDomainsAlreadyComply(unittest.TestCase):
    def test_低频振动在无数据时本就守规(self):
        loaded, failed = discover(DOMAINS_DIR)
        mine = [(p, e) for p, e in failed if p.name == "vibration_lowfreq.py"]
        assert not mine, mine
        vib = {d.key: d for d in loaded}["vibration_lowfreq"]
        frame = Frame(domain="vibration_lowfreq", binding="dev1", t_start=T0 - timedelta(seconds=60),
                      t_end=T0, channels={"x_vel": []},
                      params={"iso_group": "2", "mount_type": "rigid", "vel_is_rms": "true",
                              "axial_axis": "z"})
        res = run_domain(vib, frame)
        self.assertEqual(res.error, "", "现有域自己就落了坏质量，不该触发骨架修正")
        self.assertFalse(any(f.quality.is_good() for f in res.findings))


if __name__ == "__main__":
    unittest.main()
