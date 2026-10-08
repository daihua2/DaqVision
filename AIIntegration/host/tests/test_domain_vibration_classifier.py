"""低频振动 · 故障分类（13 标量随机森林，`domains/vibration.py` §0.7）。

2026-10-08 用户定：恢复原 v5 的 13 标量分类、手写零依赖；结果只报、不推检测状态。
★这里最要紧的几条是 v5 当年缺的：按样本留出验证（不按帧）、每类最小样本量、accuracy 不是训练集自测。
"""

from __future__ import annotations

import dataclasses
import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.domaindeps import parse_requires
from aiintegration.domains import discover
from aiintegration.quality import Quality
from aiintegration.types import ArtifactBlob, Dataset, Frame, LabeledFrame, ProgressSink, Sample

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
T0 = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
PARAMS = {"algorithms": "classic", "machineGroup": "group2", "supportClass": "rigid",
          "vel_is_rms": "true", "axialAxis": "z", "fault_classify": "true"}

#: 三类的中心。速度都压在 GB/T 6075.3 第 2 组刚性的 A 区（≤1.4 mm/s）—— 好验「分类不推状态」。
CENTERS = {
    "正常":   {"x_vel": 1.0, "z_vel": 0.5, "x_acc": 0.5, "x_disp": 20.0, "x_freq": 50.0, "temp": 40.0},
    "不平衡": {"x_vel": 1.2, "z_vel": 0.5, "x_acc": 0.5, "x_disp": 60.0, "x_freq": 25.0, "temp": 40.0},
    "不对中": {"x_vel": 1.0, "z_vel": 1.0, "x_acc": 0.9, "x_disp": 20.0, "x_freq": 100.0, "temp": 40.0},
}


def _load():
    loaded, failed = discover(DOMAINS_DIR)
    mine = [(str(p), repr(e)) for p, e in failed if p.name == "vibration.py"]
    assert not mine, f"振动域装载失败：{mine}"
    return {d.key: d for d in loaded}["vibration"]


def _samples(*values):
    out = []
    for i, v in enumerate(values):
        q = Quality.OK
        if isinstance(v, tuple):
            v, q = v
        out.append(Sample(t=T0 + timedelta(seconds=i), value=v, quality=q,
                          status_code=1 if q is Quality.OK else -1000))
    return out


def _frame(values, params=None, artifacts=None):
    return Frame(domain="vibration", binding="dev1",
                 t_start=T0 - timedelta(seconds=1), t_end=T0 + timedelta(seconds=60),
                 channels={k: _samples(v) for k, v in values.items()},
                 params=dict(PARAMS if params is None else params), artifacts=dict(artifacts or {}))


def _noisy(center, rng, spread=0.03):
    return {k: v * (1 + rng.uniform(-spread, spread)) for k, v in center.items()}


def _dataset(per_class=(4, 5), centers=CENTERS, algo="classifier", params=None, seed=7):
    """每类 `per_class[0]` 条样本、每条 `per_class[1]` 帧；sample_id 全局唯一。"""
    rng = random.Random(seed)
    items, sid = [], 0
    for label, center in centers.items():
        for _ in range(per_class[0]):
            sid += 1
            items += [LabeledFrame(frame=_frame(_noisy(center, rng), params), label=label, sample_id=sid)
                      for _ in range(per_class[1])]
    return Dataset(domain="vibration", binding="dev1", name="分类集", items=tuple(items), algo=algo)


def _artifact(trained):
    return ArtifactBlob(id=2, kind=trained.kind, name="分类器", blob=trained.blob, algo=trained.algo,
                        accuracy=trained.accuracy, meta=dict(trained.meta), created_at="2026-10-08")


def _by_key(findings):
    return {f.key: f for f in findings}


class TestTraining(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = _load().instance
        cls.trained = cls.d.train(_dataset(), ProgressSink())
        cls.model = json.loads(cls.trained.blob)

    def test_产出是分类器一类工件_格式号与特征表都在(self):
        self.assertEqual(self.trained.kind, "classifier")
        self.assertEqual(self.model["format"], "vibration_classifier/rf@1")
        self.assertEqual(self.model["labels"], ["不对中", "不平衡", "正常"])
        # 特征按 v5 FEATURE_SCHEMA 的顺序，只取训练里出现过的
        self.assertEqual(self.model["features"], ["temp", "x_acc", "x_freq", "x_disp", "x_vel", "z_vel"])
        self.assertEqual(self.model["units"]["x_disp"], "μm")

    def test_可分的数据学得会_准确率是留出集上的(self):
        self.assertGreaterEqual(self.trained.accuracy, 0.9)
        ho = self.model["holdout"]
        self.assertEqual(ho["accuracy"], self.trained.accuracy)
        self.assertEqual(sum(map(sum, ho["confusion"])), ho["frames"])
        self.assertEqual(set(ho["per_class"]), {"正常", "不平衡", "不对中"})
        self.assertIn("按样本分层留出", self.trained.meta["accuracy_basis"])

    def test_留出集是整条样本_各类都有(self):
        ho = self.model["holdout"]
        # 每类 4 条样本 ×25% ⇒ 各留 1 条、每条 5 帧
        self.assertEqual(ho["samples"], 3)
        self.assertEqual(ho["frames"], 15)
        for lb, pc in ho["per_class"].items():
            self.assertEqual(pc["support"], 5, lb)

    def test_同一份数据训两次产物逐字节相同(self):
        again = self.d.train(_dataset(), ProgressSink())
        self.assertEqual(again.blob, self.trained.blob)

    def test_零第三方依赖(self):
        # 与安装脚本同一把尺子：声明了第三方依赖，现场缺包时整个振动域都不铺（定案 2.4）
        self.assertEqual(parse_requires(DOMAINS_DIR / "vibration.py"), ())


class TestTrainingRefuses(unittest.TestCase):
    def setUp(self):
        self.d = _load().instance

    def _fails(self, ds, *needles):
        with self.assertRaises(ValueError) as c:
            self.d.train(ds, ProgressSink())
        for n in needles:
            self.assertIn(n, str(c.exception))

    def test_只有一类不训(self):
        self._fails(_dataset(centers={"正常": CENTERS["正常"]}), "至少要两类", "正常 20 帧")

    def test_某类帧数不够不训且说清各类(self):
        ds = _dataset(per_class=(3, 3))           # 每类 9 帧 < 10
        self._fails(ds, "样本不足", "每类至少 10 帧", "不平衡 9 帧 / 3 条样本")

    def test_某类只有一条样本不训_那一类验不了(self):
        ds = _dataset(per_class=(1, 12))
        self._fails(ds, "样本不足", "2 条样本")

    def test_不认识的算法不猜(self):
        self._fails(_dataset(algo="xgboost"), "不认识的训练算法", "classifier")

    def test_空算法照旧采基线_平台现役就传空(self):
        ds = dataclasses.replace(_dataset(algo=""), items=tuple(
            LabeledFrame(frame=_frame(CENTERS["正常"], {**PARAMS, "algorithms": "baseline"}),
                         label="正常", sample_id=i) for i in range(5)))
        self.assertEqual(self.d.train(ds, ProgressSink()).kind, "baseline")

    def test_停机帧不进训练(self):
        p = {**PARAMS, "stop_threshold": "0.3"}
        ds = _dataset(params=p)
        stopped = tuple(LabeledFrame(frame=_frame({**CENTERS["正常"], "x_vel": 0.1, "z_vel": 0.1}, p),
                                     label="正常", sample_id=900 + i) for i in range(4))
        model = json.loads(self.d.train(dataclasses.replace(ds, items=ds.items + stopped),
                                        ProgressSink()).blob)
        self.assertEqual(model["stopped_excluded"], 4)
        self.assertEqual(model["frames"], 60)

    def test_停机门槛非法不训(self):
        self._fails(_dataset(params={**PARAMS, "stop_threshold": "abc"}), "判不了哪些帧是停机")


class TestNoLeakage(unittest.TestCase):
    """★按帧留出会把同一条样本的帧同时放进训练与验证 —— 考题漏给考生，准确率虚高。

    构造：每条样本的 x_vel 是它独有的一个值、样本内各帧相同；标签按值交错（1 甲 2 乙 3 甲 …）。
    新样本的值落在两个异类邻居之间，学不到规律 ⇒ 按样本留出时准确率应很低；
    按帧留出则验证帧在训练里见过一模一样的值，准确率会接近 1。
    """

    def test_准确率不因同一样本的帧泄漏而虚高(self):
        items = []
        for v in range(1, 13):
            label = "甲" if v % 2 else "乙"
            items += [LabeledFrame(frame=_frame({"x_vel": float(v)}), label=label, sample_id=v)
                      for _ in range(10)]
        ds = Dataset(domain="vibration", binding="dev1", name="泄漏", items=tuple(items), algo="classifier")
        trained = _load().instance.train(ds, ProgressSink())
        self.assertLess(trained.accuracy, 0.5, json.loads(trained.blob)["holdout"])


class TestInference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = _load().instance
        cls.art = _artifact(cls.d.train(_dataset(), ProgressSink()))

    def _infer(self, values, params=None, art="default"):
        arts = {} if art is None else {"classifier": self.art if art == "default" else art}
        return _by_key(self.d.infer(_frame(values, params, arts)))

    def test_判得出类别与票数占比(self):
        out = self._infer(CENTERS["不平衡"])
        self.assertEqual(out["fault_class"].value, "不平衡")
        self.assertIs(out["fault_class"].quality, Quality.OK)
        self.assertGreater(out["fault_vote"].value, 0.5)
        self.assertLessEqual(out["fault_vote"].value, 1.0)
        self.assertIn("最像「不平衡」", out["evidence"].value)
        self.assertIn("不影响检测状态", out["evidence"].value)

    def test_判成故障类也不推检测状态(self):
        on = self._infer(CENTERS["不对中"])
        off = self._infer(CENTERS["不对中"], {**PARAMS, "fault_classify": ""})
        self.assertEqual(on["fault_class"].value, "不对中")
        self.assertEqual(on["status"].value, "normal")
        self.assertEqual((on["status"].value, on["status"].quality), (off["status"].value, off["status"].quality))

    def test_未启用不写这两项(self):
        for raw in ("", "false"):
            out = self._infer(CENTERS["正常"], {**PARAMS, "fault_classify": raw})
            self.assertNotIn("fault_class", out, raw)
            self.assertNotIn("fault_vote", out, raw)

    def test_参数非法落配置不全(self):
        out = self._infer(CENTERS["正常"], {**PARAMS, "fault_classify": "yes"})
        self.assertIs(out["fault_class"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIsNone(out["fault_class"].value)

    def test_启用了但没有分类器_落无可用模型(self):
        out = self._infer(CENTERS["正常"], art=None)
        self.assertIs(out["fault_class"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIs(out["fault_vote"].quality, Quality.MODEL_NOT_LOADED)
        self.assertEqual(out["status"].value, "normal", "没分类器不拦检测状态")

    def test_格式号不认_落无可用模型不硬算(self):
        model = json.loads(self.art.blob)
        model["format"] = "vibration_classifier/rf@9"
        bad = dataclasses.replace(self.art, blob=json.dumps(model).encode())
        out = self._infer(CENTERS["正常"], art=bad)
        self.assertIs(out["fault_class"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIn("只认", out["evidence"].value)

    def test_分类器要的特征本诊断没选_落配置不全(self):
        vals = {k: v for k, v in CENTERS["正常"].items() if k != "x_acc"}
        out = self._infer(vals)
        self.assertIs(out["fault_class"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("x_acc", out["evidence"].value)

    def test_选了但本窗口全是坏值_落样本不足(self):
        base = _frame(CENTERS["正常"], artifacts={"classifier": self.art})
        f = dataclasses.replace(base, channels={**base.channels,
                                                "x_freq": _samples((50.0, Quality.INPUT_BAD))})
        out = _by_key(self.d.infer(f))
        self.assertIs(out["fault_class"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("x_freq", out["evidence"].value)

    def test_停机不写(self):
        out = self._infer({**CENTERS["正常"], "x_vel": 0.1, "z_vel": 0.1}, {**PARAMS, "stop_threshold": "0.3"})
        self.assertEqual(out["status"].value, "stopped")
        self.assertNotIn("fault_class", out)


if __name__ == "__main__":
    unittest.main()
