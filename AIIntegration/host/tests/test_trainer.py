"""训练执行的回归。

钉的重点，全是"看起来成功了、其实不是那么回事"的那几种：

  ① **不支持训练的域当场拒**，不建一条注定失败的任务；
  ② **取不到数的样本不静默** —— 丢了几条、为什么丢，必须留在任务与工件上
     （"用 200 条训出来的"与"以为用 200 条、实际只用了 3 条"是两个模型）；
  ③ **一条都组装不出来 ⇒ 失败**，不是"训出一个空模型然后就绪"；
  ④ **新工件不自动启用**（自动启用 = 训一次就换一次现场模型）；
  ⑤ **取消要如实**：排队中的当场取消；正在跑的只是"请求取消"，不谎报；
  ⑥ 模块抛异常 ⇒ 任务 `failed` 且**原因留在任务上**，执行器自己不倒。
"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import Domain, LoadedDomain
from aiintegration.quality import Quality
from aiintegration.trainer import Trainer
from aiintegration.types import (
    Declaration, Frame, InputSpec, OutputSpec, ProgressSink, Sample, TrainedArtifact,
)
from aiintegration.workbench import JOB_FAILED, JOB_READY, Workbench, WorkbenchError

UTC = timezone.utc
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=UTC)


class _Base(Domain):
    key = "vib"
    display = "振动"
    version = "1.0.0"

    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x_vel", unit="mm/s"),),
            outputs=(OutputSpec(key="score", display="分", value_type="float"),))

    def infer(self, frame):
        return []


class TrainableDomain(_Base):
    """会训练的域。记下它拿到了什么，用例据此断言骨架交过去的东西对不对。"""

    def __init__(self):
        self.seen = None
        self.raise_what = None
        self.return_what = None
        self.slow = 0.0

    def train(self, dataset, report):
        self.seen = dataset
        report.report(0.5, "练到一半")
        if self.slow:
            deadline = time.monotonic() + self.slow
            while time.monotonic() < deadline:
                if report.canceled:      # ★肯看取消标志的模块能提前收手
                    raise RuntimeError("被取消（模块自己收的手）")
                time.sleep(0.02)
        if self.raise_what:
            raise self.raise_what
        if self.return_what is not None:
            return self.return_what
        return TrainedArtifact(blob=b"MODEL-BYTES", algo="决策树",
                               accuracy=0.93, feature_count=3,
                               meta={"note": "用例造的"})


class NoTrainDomain(_Base):
    key = "vfd"


def _loaded(inst: Domain) -> LoadedDomain:
    caps = set(inst.capabilities())
    return LoadedDomain(inst, inst.declare(), frozenset(caps), Path("用例内造"))


class FakeFetcher:
    """按 (binding) 决定给不给数据。★"取不到数"是常态，必须能造出来。"""

    def __init__(self, empty_bindings=()):
        self.empty = set(empty_bindings)
        self.calls = []

    def fetch(self, b: Binding, end_time):
        self.calls.append((b.binding, b.window_sec, end_time))
        if b.binding in self.empty:
            return Frame(domain=b.domain, binding=b.binding,
                         t_start=end_time - timedelta(seconds=b.window_sec),
                         t_end=end_time, channels={"x_vel": []}, params=dict(b.params))
        return Frame(domain=b.domain, binding=b.binding,
                     t_start=end_time - timedelta(seconds=b.window_sec),
                     t_end=end_time,
                     channels={"x_vel": [Sample(t=end_time, value=1.0, quality=Quality.OK,
                                                status_code=1)]},
                     params=dict(b.params))


class TrainerBase(unittest.TestCase):
    empty_bindings = ()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.wb = Workbench(root / "wb.db")
        self.bindings = BindingStore(root / "b.db")
        self.dom = TrainableDomain()
        self.domains = {"vib": _loaded(self.dom), "vfd": _loaded(NoTrainDomain())}
        self.fetcher = FakeFetcher(self.empty_bindings)
        self.artifacts = root / "artifacts"
        self.tr = Trainer(workbench=self.wb, bindings=self.bindings,
                          domains=self.domains, fetcher=self.fetcher,
                          artifacts_dir=self.artifacts)

    def tearDown(self):
        self.tr.stop(timeout=5)
        self.bindings.close(); self.wb.close(); self._tmp.cleanup()

    def make_dataset(self, bindings=("dev1", "dev1", "dev2"), labels=None):
        ds = self.wb.put_dataset(domain="vib", name="A")
        labels = labels or ["正常"] * len(bindings)
        anns = []
        for i, (bd, lb) in enumerate(zip(bindings, labels)):
            if self.bindings.get("vib", bd) is None:
                self.bindings.put(Binding("vib", bd, {"x_vel": 100 + i}))
            anns.append(self.wb.put_annotation(
                domain="vib", binding=bd, label=lb,
                t_from=T0 + timedelta(hours=i), t_to=T0 + timedelta(hours=i, minutes=5)))
        self.wb.add_samples(ds, anns)
        return ds

    def run_one(self, job_id, timeout=10):
        """跑起执行器，等这个任务到终态。"""
        self.tr.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.wb.get_job(job_id)
            if job.status in (JOB_READY, JOB_FAILED, "canceled"):
                return job
            time.sleep(0.05)
        self.fail(f"任务 {job_id} 超时未到终态，当前 {self.wb.get_job(job_id).status}")


class TestSubmit(TrainerBase):
    def test_不支持训练的域当场拒(self):
        ds = self.wb.put_dataset(domain="vfd", name="B")
        self.wb.add_samples(ds, [self.wb.put_annotation(
            domain="vfd", binding="d", label="x",
            t_from=T0, t_to=T0 + timedelta(minutes=1))])
        with self.assertRaises(WorkbenchError) as c:
            self.tr.submit(domain="vfd", dataset_id=ds)
        self.assertIn("能力位", str(c.exception))
        # ★不建一条注定失败的任务
        self.assertEqual(self.wb.list_jobs(domain="vfd").total, 0)

    def test_未装载的域当场拒(self):
        with self.assertRaises(WorkbenchError):
            self.tr.submit(domain="没这个域", dataset_id=1)

    def test_建任务立刻返回不阻塞(self):
        ds = self.make_dataset()
        t = time.monotonic()
        jid = self.tr.submit(domain="vib", dataset_id=ds, algo="决策树")
        self.assertLess(time.monotonic() - t, 1.0, "submit 必须立刻返回")
        self.assertEqual(self.wb.get_job(jid).status, "pending")


class TestHappyPath(TrainerBase):
    def test_训完落工件且不自动启用(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds, algo="决策树")
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_READY, job.message)
        self.assertTrue(job.artifact_id)

        art = self.wb.list_artifacts(domain="vib").items[0]
        self.assertEqual(art.algo, "决策树")
        self.assertEqual(art.accuracy, 0.93)
        self.assertEqual(art.sample_count, 3)
        # ★不自动启用：启用是人的决定
        self.assertFalse(art.active)
        self.assertIsNone(self.wb.active_artifact("vib"))
        self.assertIn("未自动启用", job.message)

    def test_工件真落盘且sha256与大小对得上(self):
        import hashlib
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds, algo="决策树")
        self.run_one(jid)
        art = self.wb.list_artifacts(domain="vib").items[0]
        f = self.artifacts / art.path
        self.assertTrue(f.is_file(), f"工件没落盘：{art.path}")
        blob = f.read_bytes()
        self.assertEqual(blob, b"MODEL-BYTES")
        self.assertEqual(art.size, len(blob))
        self.assertEqual(art.sha256, hashlib.sha256(blob).hexdigest())
        self.assertFalse(list(f.parent.glob("*.part")), "临时文件该被改名，不该留下")

    def test_模块拿到的是带标签的帧且窗口是样本自己的(self):
        ds = self.make_dataset(bindings=("dev1", "dev2"), labels=["正常", "不平衡"])
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        self.run_one(jid)
        seen = self.dom.seen
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen.label_counts(), {"正常": 1, "不平衡": 1})
        # ★窗口取样本自己的时间范围（5 分钟），不是绑定上配的 window_sec（缺省 60s）
        self.assertEqual({w for _b, w, _t in self.fetcher.calls}, {300.0})

    def test_标签分布写进任务与工件(self):
        ds = self.make_dataset(bindings=("dev1", "dev2"), labels=["正常", "不平衡"])
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertIn("标签分布", job.message)
        art = self.wb.list_artifacts(domain="vib").items[0]
        meta = json.loads(art.meta_json)
        self.assertEqual(json.loads(meta["label_counts"]), {"正常": 1, "不平衡": 1})
        self.assertEqual(meta["note"], "用例造的", "模块自己挂的 meta 要原样留着")

    def test_进度被推进(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertEqual(job.progress, 1.0)


class TestSkipsAreLoud(TrainerBase):
    """★取不到数是常态，但不许静默。"""

    empty_bindings = ("dev2",)

    def test_丢了几条为什么丢都留在任务上(self):
        ds = self.make_dataset(bindings=("dev1", "dev2", "dev2"))
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_READY, job.message)
        self.assertIn("2/3", job.message, "丢了几条要如实说")
        self.assertIn("已无数据", job.message, "为什么丢也要说")
        self.assertIn("用 1 条样本训练", job.message)

    def test_工件上也留一份(self):
        # 任务记录可能被清理，而"这个模型是用什么训的"要长久答得上。
        ds = self.make_dataset(bindings=("dev1", "dev2"))
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        self.run_one(jid)
        meta = json.loads(self.wb.list_artifacts(domain="vib").items[0].meta_json)
        self.assertEqual(meta["skipped_count"], "1")
        self.assertTrue(meta["skipped_sample_ids"])

    def test_没有绑定的样本也算丢并说清原因(self):
        ds = self.wb.put_dataset(domain="vib", name="B")
        aid = self.wb.put_annotation(domain="vib", binding="从没绑过的", label="正常",
                                     t_from=T0, t_to=T0 + timedelta(minutes=5))
        self.wb.add_samples(ds, [aid])
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_FAILED)
        self.assertIn("没有绑定", job.message)

    def test_一条都组装不出来是失败不是就绪(self):
        ds = self.make_dataset(bindings=("dev2", "dev2"))
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        # ★不是"训出一个空模型然后就绪"
        self.assertEqual(job.status, JOB_FAILED, job.message)
        self.assertIn("一条样本都没有", job.message)
        self.assertEqual(self.wb.list_artifacts(domain="vib").total, 0)


class TestFailures(TrainerBase):
    def test_模块抛异常时任务失败且原因留着(self):
        ds = self.make_dataset()
        self.dom.raise_what = ValueError("特征维度对不上：期望 13 收到 4")
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_FAILED)
        self.assertIn("13", job.message)
        self.assertIn("ValueError", job.message)

    def test_执行器不因一个任务炸掉而停摆(self):
        ds = self.make_dataset()
        self.dom.raise_what = RuntimeError("炸")
        first = self.tr.submit(domain="vib", dataset_id=ds)
        self.run_one(first)
        # 下一个照常跑
        self.dom.raise_what = None
        second = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(second)
        self.assertEqual(job.status, JOB_READY, job.message)

    def test_模块返回错类型被拒(self):
        ds = self.make_dataset()
        self.dom.return_what = {"blob": b"x"}
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_FAILED)
        self.assertIn("TrainedArtifact", job.message)

    def test_空工件在类型层就被挡住(self):
        with self.assertRaises(ValueError) as c:
            TrainedArtifact(blob=b"", algo="x")
        self.assertIn("blob", str(c.exception))

    def test_准确率越界被挡(self):
        for bad in (1.5, -0.1, float("nan")):
            with self.assertRaises(ValueError):
                TrainedArtifact(blob=b"x", algo="a", accuracy=bad)


class TestCancel(TrainerBase):
    def test_排队中的当场取消(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)   # 没 start，一直排队
        msg = self.tr.cancel(jid)
        self.assertIn("还没开始跑", msg)
        self.assertEqual(self.wb.get_job(jid).status, "canceled")

    def test_取消已终态的如实说不适用(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        self.run_one(jid)
        msg = self.tr.cancel(jid)
        self.assertIn("不适用", msg)

    def test_已训完但执行器还没清当前任务时也说不适用(self):
        """★竞态：执行器先写终态、后在 finally 清 `_current`。窗口里取消不许谎报"已请求取消"。

        2026-09-18 在三环境全量跑里偶发红（新增用例改变时序撞上），这里把那个窗口构造出来钉住。
        """
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_READY)
        self.tr._current = (jid, ProgressSink())      # 模拟"终态已写、_current 未清"
        msg = self.tr.cancel(jid)
        self.assertIn("不适用", msg)
        self.assertFalse(self.tr._current[1].canceled, "已终态的任务不该被置取消标志")

    def test_正在跑的只说请求取消不谎报(self):
        """★关键：模块不看取消标志就只能跑完，回执必须如实。"""
        ds = self.make_dataset()
        self.dom.slow = 3.0
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        self.tr.start()
        deadline = time.monotonic() + 5
        while self.wb.get_job(jid).status != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        msg = self.tr.cancel(jid)
        self.assertIn("已请求取消", msg)
        self.assertNotIn("已取消（", msg)
        # 这个域是肯看取消标志的，所以它会收手
        job = self.run_one(jid)
        self.assertIn(job.status, ("canceled", JOB_FAILED), job.message)

    def test_取消不存在的任务报错(self):
        with self.assertRaises(WorkbenchError):
            self.tr.cancel(9999)


class TestRestart(TrainerBase):
    """★进程在训练中途死掉（崩溃 / 断电 / systemd restart）之后，那条任务怎么办。

    没有这一条的话：任务永远停在 `running`，没人跑它、也没人把它标失败 ——
    界面上一直显示"训练中"。这正是"看起来在跑、其实没有"那一类。
    （本条是自查时发现的：`_next_pending` 只取 pending，启动时对遗留的 running 一句话都不说。）
    """

    def test_重启后遗留的running任务被标失败并说清原因(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        # 模拟"上一个进程跑到一半死了"：状态停在 running，没有任何执行器持有它
        self.wb.update_job(jid, status="running", message="正在组装数据集")

        fresh = Trainer(workbench=self.wb, bindings=self.bindings, domains=self.domains,
                        fetcher=self.fetcher, artifacts_dir=self.artifacts)
        try:
            fresh.start()
            job = self.wb.get_job(jid)
            self.assertEqual(job.status, JOB_FAILED,
                             "遗留的 running 任务不该永远挂在'训练中'")
            self.assertIn("重启", job.message, "要说清是服务重启导致未完成，不是算法失败")
        finally:
            fresh.stop(timeout=5)

    def test_重启后遗留的pending任务照常跑(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)     # 排着队，进程死了
        fresh = Trainer(workbench=self.wb, bindings=self.bindings, domains=self.domains,
                        fetcher=self.fetcher, artifacts_dir=self.artifacts)
        self.tr = fresh                                       # 让 run_one/tearDown 用新的
        job = self.run_one(jid)
        self.assertEqual(job.status, JOB_READY, job.message)

    def test_已终态的任务重启时不被碰(self):
        ds = self.make_dataset()
        jid = self.tr.submit(domain="vib", dataset_id=ds)
        self.wb.update_job(jid, status=JOB_FAILED, message="原本的失败原因")
        fresh = Trainer(workbench=self.wb, bindings=self.bindings, domains=self.domains,
                        fetcher=self.fetcher, artifacts_dir=self.artifacts)
        try:
            fresh.start()
            self.assertEqual(self.wb.get_job(jid).message, "原本的失败原因",
                             "终态不可覆盖 —— 重启处置只管 running")
        finally:
            fresh.stop(timeout=5)


class TestSerial(TrainerBase):
    def test_排队顺序是建任务顺序且一次只跑一个(self):
        ds = self.make_dataset()
        self.dom.slow = 0.4
        ids = [self.tr.submit(domain="vib", dataset_id=ds) for _ in range(3)]
        self.tr.start()
        for jid in ids:
            self.run_one(jid, timeout=20)
        finished = [self.wb.get_job(i) for i in ids]
        self.assertTrue(all(j.status == JOB_READY for j in finished),
                        [j.message for j in finished])
        # 工件按任务顺序产出（串行的直接证据）
        arts = sorted(self.wb.list_artifacts(domain="vib").items, key=lambda a: a.id)
        self.assertEqual([a.path.split("/")[-1].split("-")[0] for a in arts],
                         [str(i) for i in ids])


if __name__ == "__main__":
    unittest.main()


class TestDataOrigin(TrainerBase):
    """工件上的来源性质：**按参与训练的样本取最严**（契约 1.6 / AICloud `C-36 §4.2.2`）。"""

    def _train(self, bindings, origins, labels=None):
        ds = self.wb.put_dataset(domain="vib", name="A")
        anns = []
        for i, bd in enumerate(bindings):
            if self.bindings.get("vib", bd) is None:
                self.bindings.put(Binding("vib", bd, {"x_vel": 100 + i},
                                          data_origin=origins.get(bd, "")))
            anns.append(self.wb.put_annotation(
                domain="vib", binding=bd, label=(labels or ["正常"] * len(bindings))[i],
                t_from=T0 + timedelta(hours=i), t_to=T0 + timedelta(hours=i, minutes=5)))
        self.wb.add_samples(ds, anns, origins)
        job = self.run_one(self.tr.submit(domain="vib", dataset_id=ds))
        self.assertEqual(job.status, JOB_READY, job.message)
        art = {a.id: a for a in self.wb.list_artifacts(domain="vib").items}[job.artifact_id]
        return art

    def test_全现场训出来的工件记现场(self):
        art = self._train(["d1", "d2"], {"d1": "field", "d2": "field"})
        self.assertEqual(art.data_origin, "field")

    def test_混进一段仿真就记仿真(self):
        art = self._train(["d1", "sim1"], {"d1": "field", "sim1": "simulated"})
        self.assertEqual(art.data_origin, "simulated")
        # ★混了多少要能看见：压成一个词之后，比例只剩训练数据说明这一处。
        self.assertIn("不一致", art.training_data)
        self.assertIn("仿真 1 条", art.training_data)

    def test_谁都没声明就是未声明而不是现场(self):
        art = self._train(["d1", "d2"], {})
        self.assertEqual(art.data_origin, "")

    def test_现场混未声明退回未声明(self):
        art = self._train(["d1", "d2"], {"d1": "field"})
        self.assertEqual(art.data_origin, "")

    def test_训完改绑定不反写已有工件(self):
        # ★真机接入后把绑定改成 field —— 当初用仿真数据训出来的那个工件必须岿然不动。
        art = self._train(["sim1"], {"sim1": "simulated"})
        self.assertEqual(art.data_origin, "simulated")
        b = self.bindings.get("vib", "sim1")
        self.bindings.put(Binding("vib", b.binding, b.roles, data_origin="field"))
        again = {a.id: a for a in self.wb.list_artifacts(domain="vib").items}[art.id]
        self.assertEqual(again.data_origin, "simulated", "改绑定把历史工件洗成现场了")

    def test_快照缺失时按绑定兜底但只朝严的方向(self):
        # 1.6 之前入集的样本没有快照（这里用不传 origins 造出来）。
        ds = self.wb.put_dataset(domain="vib", name="老集")
        self.bindings.put(Binding("vib", "sim1", {"x_vel": 100}, data_origin="simulated"))
        a = self.wb.put_annotation(domain="vib", binding="sim1", label="正常",
                                   t_from=T0, t_to=T0 + timedelta(minutes=5))
        self.wb.add_samples(ds, [a])            # ← 没给 origins，快照为空
        self.assertEqual(self.wb.list_samples(ds).items[0].data_origin, "")
        job = self.run_one(self.tr.submit(domain="vib", dataset_id=ds))
        art = {x.id: x for x in self.wb.list_artifacts(domain="vib").items}[job.artifact_id]
        self.assertEqual(art.data_origin, "simulated", "绑定说仿真，兜底该抬成仿真")

    def test_快照缺失而绑定说现场时仍是未声明(self):
        # ★兜底**绝不可能**产出一个假的现场 —— 这条红了，整条兜底就不能要。
        ds = self.wb.put_dataset(domain="vib", name="老集2")
        self.bindings.put(Binding("vib", "d1", {"x_vel": 100}, data_origin="field"))
        a = self.wb.put_annotation(domain="vib", binding="d1", label="正常",
                                   t_from=T0, t_to=T0 + timedelta(minutes=5))
        self.wb.add_samples(ds, [a])
        job = self.run_one(self.tr.submit(domain="vib", dataset_id=ds))
        art = {x.id: x for x in self.wb.list_artifacts(domain="vib").items}[job.artifact_id]
        self.assertEqual(art.data_origin, "", "快照缺失时兜出了现场")

    def test_取不到数的样本不参与取最严(self):
        # 那条仿真样本取不到数、没进模型 ⇒ 它影响不了这个工件能不能说现场的话。
        self.fetcher.empty = {"sim1"}
        art = self._train(["d1", "sim1"], {"d1": "field", "sim1": "simulated"})
        self.assertEqual(art.data_origin, "field")
