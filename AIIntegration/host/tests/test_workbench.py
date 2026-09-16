"""工作台状态的回归。

钉的是 `C-11` 里 AICloud 逐条点名要保住的语义，以及我方自己那几条不肯让步的：

  ① **标注 ≠ 样本**；
  ② **移出 ≠ 删除**（移出只解关系，原始标注一动不动）；
  ③ **标签不枚举**（从数据聚合，没有标签表）；
  ④ **训练是任务不是阻塞调用**，且**终态不可覆盖**；
  ⑤ 分页的 `total` 是**过滤后的真实总数**；
  ⑥ 时刻只收带时区的（裸 datetime 跨机就是另一个时刻）；
  ⑦ 激活工件**同一对象只能有一个**，且激活中的不让删。
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiintegration.workbench import (
    JOB_FAILED, JOB_READY, JOB_RUNNING, KIND_BASELINE, KIND_MODEL, MAX_PAGE_SIZE,
    Workbench, WorkbenchError,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=UTC)


def rng(i, mins=10):
    a = T0 + timedelta(hours=i)
    return a, a + timedelta(minutes=mins)


class WbBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wb = Workbench(Path(self._tmp.name) / "wb.db")

    def tearDown(self):
        self.wb.close(); self._tmp.cleanup()

    def ann(self, i=0, *, binding="dev1", label="正常", domain="vib"):
        a, b = rng(i)
        return self.wb.put_annotation(domain=domain, binding=binding,
                                      t_from=a, t_to=b, label=label)


class TestAnnotations(WbBase):
    def test_存取往返(self):
        aid = self.ann(0)
        page = self.wb.list_annotations(domain="vib")
        self.assertEqual(page.total, 1)
        one = page.items[0]
        self.assertEqual((one.id, one.binding, one.label), (aid, "dev1", "正常"))
        self.assertEqual(one.t_from, T0)
        self.assertEqual(one.t_from.tzinfo, UTC)

    def test_裸datetime被拒(self):
        with self.assertRaises(WorkbenchError) as c:
            self.wb.put_annotation(domain="vib", binding="d", label="x",
                                   t_from=datetime(2026, 9, 11, 8), t_to=T0)
        self.assertIn("时区", str(c.exception))

    def test_区间倒挂或零长被拒(self):
        for a, b in ((T0 + timedelta(hours=1), T0), (T0, T0)):
            with self.assertRaises(WorkbenchError):
                self.wb.put_annotation(domain="vib", binding="d", label="x", t_from=a, t_to=b)

    def test_空标签被拒(self):
        # 空标签的标注在训练里是噪声，不是"待定"。
        with self.assertRaises(WorkbenchError):
            self.wb.put_annotation(domain="vib", binding="d", label="",
                                   t_from=T0, t_to=T0 + timedelta(minutes=1))

    def test_按时间查是相交不是被包含(self):
        """★框一小段就该找到跨过它的那条标注，否则界面上"这段有没有标过"永远答错。"""
        self.ann(0)                                    # 08:00 ~ 08:10
        mid_a = T0 + timedelta(minutes=3)
        mid_b = T0 + timedelta(minutes=5)
        self.assertEqual(self.wb.list_annotations(t_from=mid_a, t_to=mid_b).total, 1)
        # 完全在外面的不该命中
        far = T0 + timedelta(hours=5)
        self.assertEqual(
            self.wb.list_annotations(t_from=far, t_to=far + timedelta(minutes=1)).total, 0)

    def test_批量按范围打标签是一个事务(self):
        ids = self.wb.annotate_range(domain="vib", bindings=["d1", "d2", "d3"],
                                     t_from=T0, t_to=T0 + timedelta(minutes=5), label="不平衡")
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.wb.list_annotations(domain="vib", label="不平衡").total, 3)

    def test_批量范围区间非法时一条都不进(self):
        with self.assertRaises(WorkbenchError):
            self.wb.annotate_range(domain="vib", bindings=["d1", "d2"],
                                   t_from=T0, t_to=T0, label="x")
        self.assertEqual(self.wb.list_annotations().total, 0)

    def test_改标签不动别的字段(self):
        aid = self.ann(0)
        a, b = rng(0)
        self.wb.put_annotation(domain="vib", binding="dev1", t_from=a, t_to=b,
                               label="不对中", note="复核过", annotation_id=aid)
        one = self.wb.list_annotations().items[0]
        self.assertEqual((one.id, one.label, one.note), (aid, "不对中", "复核过"))

    def test_改不存在的标注要报错不是静默新建(self):
        with self.assertRaises(WorkbenchError):
            self.wb.put_annotation(domain="vib", binding="d", t_from=T0,
                                   t_to=T0 + timedelta(minutes=1), label="x",
                                   annotation_id=9999)


class TestLabels(WbBase):
    def test_标签从数据聚合而不是查表(self):
        self.ann(0, label="正常"); self.ann(1, label="正常"); self.ann(2, label="不平衡")
        self.assertEqual(self.wb.list_labels("vib"), [("正常", 2), ("不平衡", 1)])

    def test_新标签用一次就存在不必先注册(self):
        # ★"允许新增标签"在这套设计里不需要任何"新增"动作 —— 没有标签表可注册。
        self.ann(0, label="从没见过的标签")
        self.assertIn("从没见过的标签", [l for l, _ in self.wb.list_labels("vib")])


class TestDatasets(WbBase):
    def test_重名被拒(self):
        self.wb.put_dataset(domain="vib", name="基准集")
        with self.assertRaises(WorkbenchError) as c:
            self.wb.put_dataset(domain="vib", name="基准集")
        self.assertIn("基准集", str(c.exception))

    def test_不同域可以同名(self):
        self.wb.put_dataset(domain="vib", name="基准集")
        self.wb.put_dataset(domain="vfd", name="基准集")
        self.assertEqual(len(self.wb.list_datasets()), 2)

    def test_列出带样本数(self):
        ds = self.wb.put_dataset(domain="vib", name="A")
        self.wb.add_samples(ds, [self.ann(0), self.ann(1)])
        self.assertEqual(self.wb.list_datasets("vib")[0].sample_count, 2)

    def test_整库复制连样本一起(self):
        ds = self.wb.put_dataset(domain="vib", name="A")
        self.wb.add_samples(ds, [self.ann(0), self.ann(1), self.ann(2)])
        new = self.wb.copy_dataset(ds, "A 的副本")
        self.assertEqual(self.wb.list_samples(new).total, 3)
        # 两边独立：改副本不影响原集
        sid = self.wb.list_samples(new).items[0].id
        self.wb.remove_samples(new, [sid])
        self.assertEqual(self.wb.list_samples(ds).total, 3)
        self.assertEqual(self.wb.list_samples(new).total, 2)

    def test_复制成重名时一条样本都不该留下(self):
        ds = self.wb.put_dataset(domain="vib", name="A")
        self.wb.add_samples(ds, [self.ann(0)])
        self.wb.put_dataset(domain="vib", name="B")
        with self.assertRaises(WorkbenchError):
            self.wb.copy_dataset(ds, "B")
        # ★事务回滚：不能留下一个"名字没建成、样本却进去了"的半拉状态
        self.assertEqual(len(self.wb.list_datasets("vib")), 2)
        self.assertEqual(self.wb.stats()["samples"], 1)

    def test_删训练集连样本但不动标注(self):
        ds = self.wb.put_dataset(domain="vib", name="A")
        a1, a2 = self.ann(0), self.ann(1)
        self.wb.add_samples(ds, [a1, a2])
        self.assertTrue(self.wb.delete_dataset(ds))
        self.assertEqual(self.wb.stats()["samples"], 0)
        # ★标注是人的判断，比任何一个训练集活得久。
        self.assertEqual(self.wb.list_annotations().total, 2)


class TestSamples(WbBase):
    def setUp(self):
        super().setUp()
        self.ds = self.wb.put_dataset(domain="vib", name="A")

    def test_入集是拷贝且幂等(self):
        a1 = self.ann(0)
        self.assertEqual(self.wb.add_samples(self.ds, [a1]), 1)
        # ★重复点"加入"不该多一份 —— 重复样本会悄悄改变类别权重
        self.assertEqual(self.wb.add_samples(self.ds, [a1]), 0)
        self.assertEqual(self.wb.list_samples(self.ds).total, 1)

    def test_样本标签是入集那一刻的快照(self):
        """★改标注的标签，**不追溯改写**已经入集的样本。

        否则"这个模型当初是用什么训的"就没有答案了。
        """
        a1 = self.ann(0, label="正常")
        self.wb.add_samples(self.ds, [a1])
        a, b = rng(0)
        self.wb.put_annotation(domain="vib", binding="dev1", t_from=a, t_to=b,
                               label="其实是不平衡", annotation_id=a1)
        self.assertEqual(self.wb.list_samples(self.ds).items[0].label, "正常")
        self.assertEqual(self.wb.list_annotations().items[0].label, "其实是不平衡")

    def test_移出只解关系不动标注(self):
        a1 = self.ann(0)
        self.wb.add_samples(self.ds, [a1])
        sid = self.wb.list_samples(self.ds).items[0].id
        self.assertEqual(self.wb.remove_samples(self.ds, [sid]), 1)
        self.assertEqual(self.wb.list_samples(self.ds).total, 0)
        # ★这条是 v5 特意分开两个动作的理由：把数据从训练集拿掉 ≠ 否定人的判断
        self.assertEqual(self.wb.list_annotations().total, 1)

    def test_删标注不连带删样本只把来源置空(self):
        a1 = self.ann(0)
        self.wb.add_samples(self.ds, [a1])
        self.assertEqual(self.wb.delete_annotations([a1]), 1)
        s = self.wb.list_samples(self.ds).items[0]
        self.assertIsNone(s.annotation_id, "来源该置空")
        self.assertEqual(s.label, "正常", "训练历史不该被后来的删除改写")

    def test_批量复制到另一个训练集(self):
        self.wb.add_samples(self.ds, [self.ann(0), self.ann(1)])
        other = self.wb.put_dataset(domain="vib", name="B")
        ids = [s.id for s in self.wb.list_samples(self.ds).items]
        self.assertEqual(self.wb.copy_samples(ids, other), 2)
        self.assertEqual(self.wb.list_samples(other).total, 2)
        # 再复制一次不该翻倍
        self.assertEqual(self.wb.copy_samples(ids, other), 0)

    def test_往不存在的训练集加样本要报错(self):
        with self.assertRaises(WorkbenchError):
            self.wb.add_samples(9999, [self.ann(0)])


class TestPaging(WbBase):
    def test_total是过滤后的真实总数不是本页条数(self):
        for i in range(25):
            self.ann(i, label="正常" if i % 2 == 0 else "不平衡")
        page = self.wb.list_annotations(domain="vib", label="正常", offset=0, limit=5)
        self.assertEqual(len(page.items), 5)
        self.assertEqual(page.total, 13, "total 要是过滤后的真实总数，不然分页器是错的")

    def test_翻页不重不漏(self):
        for i in range(7):
            self.ann(i)
        seen = []
        for off in (0, 3, 6):
            seen += [a.id for a in self.wb.list_annotations(offset=off, limit=3).items]
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)

    def test_超过上限要报错而不是静默截断(self):
        # 静默截断 = 调用方以为拿全了，实际少一截，而 total 还显示对的数。
        with self.assertRaises(WorkbenchError):
            self.wb.list_annotations(limit=MAX_PAGE_SIZE + 1)

    def test_负偏移被拒(self):
        with self.assertRaises(WorkbenchError):
            self.wb.list_annotations(offset=-1)


class TestTrainJobs(WbBase):
    def setUp(self):
        super().setUp()
        self.ds = self.wb.put_dataset(domain="vib", name="A")
        self.wb.add_samples(self.ds, [self.ann(0), self.ann(1)])

    def test_建任务立刻返回且是待跑态(self):
        jid = self.wb.create_job(domain="vib", dataset_id=self.ds, algo="决策树")
        job = self.wb.get_job(jid)
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.sample_count, 2)

    def test_空训练集不许开训(self):
        empty = self.wb.put_dataset(domain="vib", name="空的")
        with self.assertRaises(WorkbenchError) as c:
            self.wb.create_job(domain="vib", dataset_id=empty)
        self.assertIn("一个样本都没有", str(c.exception))

    def test_跨域拿训练集被拒(self):
        with self.assertRaises(WorkbenchError) as c:
            self.wb.create_job(domain="vfd", dataset_id=self.ds)
        self.assertIn("vib", str(c.exception))

    def test_失败原因留在任务上而不是只打日志(self):
        jid = self.wb.create_job(domain="vib", dataset_id=self.ds)
        self.wb.update_job(jid, status=JOB_RUNNING)
        self.wb.update_job(jid, status=JOB_FAILED, message="特征维度对不上：期望 13 收到 4")
        job = self.wb.get_job(jid)
        self.assertEqual(job.status, JOB_FAILED)
        self.assertIn("13", job.message)
        self.assertTrue(job.finished_at)

    def test_终态不可覆盖(self):
        """★已失败的任务被后来的写改成"就绪"，界面上就再也看不出它失败过。"""
        jid = self.wb.create_job(domain="vib", dataset_id=self.ds)
        self.wb.update_job(jid, status=JOB_FAILED, message="炸了")
        with self.assertRaises(WorkbenchError) as c:
            self.wb.update_job(jid, status=JOB_READY)
        self.assertIn("终态", str(c.exception))
        self.assertEqual(self.wb.get_job(jid).status, JOB_FAILED)

    def test_未知状态被拒(self):
        jid = self.wb.create_job(domain="vib", dataset_id=self.ds)
        with self.assertRaises(WorkbenchError):
            self.wb.update_job(jid, status="差不多好了")

    def test_按状态筛与真实总数(self):
        a = self.wb.create_job(domain="vib", dataset_id=self.ds)
        self.wb.create_job(domain="vib", dataset_id=self.ds)
        self.wb.update_job(a, status=JOB_FAILED)
        self.assertEqual(self.wb.list_jobs(domain="vib").total, 2)
        self.assertEqual(self.wb.list_jobs(domain="vib", status=JOB_FAILED).total, 1)


class TestArtifacts(WbBase):
    def test_同一对象只能有一个激活件(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        b = self.wb.add_artifact(domain="vib", name="m2", binding="dev1")
        self.wb.activate_artifact(a)
        self.assertEqual(self.wb.active_artifact("vib", binding="dev1").id, a)
        self.wb.activate_artifact(b)
        self.assertEqual(self.wb.active_artifact("vib", binding="dev1").id, b)
        actives = [x for x in self.wb.list_artifacts(domain="vib").items if x.active]
        self.assertEqual(len(actives), 1, "库层的部分唯一索引该挡住第二个激活件")

    def test_不同对象各自有各自的激活件(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        b = self.wb.add_artifact(domain="vib", name="m2", binding="dev2")
        self.wb.activate_artifact(a); self.wb.activate_artifact(b)
        self.assertEqual(self.wb.active_artifact("vib", binding="dev1").id, a)
        self.assertEqual(self.wb.active_artifact("vib", binding="dev2").id, b)

    def test_没有激活件时如实回空(self):
        self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        # ★不许退回"随便挑一个最新的"：那会让"我在用哪个模型"答不上来。
        self.assertIsNone(self.wb.active_artifact("vib", binding="dev1"))

    def test_激活中的不让删(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        self.wb.activate_artifact(a)
        with self.assertRaises(WorkbenchError) as c:
            self.wb.delete_artifact(a)
        self.assertIn("激活", str(c.exception))

    def test_没测过的准确率是空不是0(self):
        a = self.wb.add_artifact(domain="vib", name="m1")
        self.assertIsNone(self.wb.list_artifacts(domain="vib").items[0].accuracy)
        b = self.wb.add_artifact(domain="vib", name="m2", accuracy=0.0)
        got = {x.id: x.accuracy for x in self.wb.list_artifacts(domain="vib").items}
        self.assertIsNone(got[a], "没测过 ≠ 测出来是 0")
        self.assertEqual(got[b], 0.0)

    def test_基线与模型互不挤占激活位(self):
        """★基线走同一张表（域私有状态的落点），但 kind 不同 ⇒ 各占各的激活位。"""
        m = self.wb.add_artifact(domain="vib", name="m1", binding="dev1", kind=KIND_MODEL)
        b = self.wb.add_artifact(domain="vib", name="基线-20260911", binding="dev1",
                                 kind=KIND_BASELINE)
        self.wb.activate_artifact(m); self.wb.activate_artifact(b)
        self.assertEqual(self.wb.active_artifact("vib", KIND_MODEL, "dev1").id, m)
        self.assertEqual(self.wb.active_artifact("vib", KIND_BASELINE, "dev1").id, b)


class TestSegments(WbBase):
    def test_片段与报告往返且删片段连报告(self):
        a, b = rng(0, mins=30)
        sid = self.wb.put_segment(domain="vib", binding="dev1", t_from=a, t_to=b,
                                  name="停机前 30 分钟", source="人工归档", point_count=1800)
        self.wb.add_report(segment_id=sid, kind="basic", path="reports/1.pdf", size=1234)
        self.wb.add_report(segment_id=sid, kind="deep", path="reports/2.pdf", size=5678)
        self.assertEqual(len(self.wb.list_reports(sid)), 2)
        self.assertTrue(self.wb.delete_segment(sid))
        self.assertEqual(self.wb.stats()["reports"], 0, "外键级联要真的开着")

    def test_给不存在的片段加报告要报错(self):
        with self.assertRaises(WorkbenchError):
            self.wb.add_report(segment_id=9999)

    def test_片段区间倒挂被拒(self):
        with self.assertRaises(WorkbenchError):
            self.wb.put_segment(domain="vib", binding="d", t_from=T0, t_to=T0)


class TestPersistence(WbBase):
    def test_重开库数据还在(self):
        path = Path(self._tmp.name) / "wb.db"
        ds = self.wb.put_dataset(domain="vib", name="A")
        self.wb.add_samples(ds, [self.ann(0)])
        self.wb.close()
        wb2 = Workbench(path)
        try:
            self.assertEqual(wb2.list_datasets("vib")[0].sample_count, 1)
        finally:
            wb2.close()
        self.wb = Workbench(path)   # 给 tearDown 收尾


if __name__ == "__main__":
    unittest.main()


class TestDataOriginSnapshot(WbBase):
    """来源性质在**入集那一刻**打快照（契约 1.6，AICloud `C-36 §4`）。

    与 `label` 快照同一条道理，但后果更重：label 错了是"模型用了旧判断"，
    这一格错了是"仿真数据训出来的基线在界面上看着像现场基线"，而且**从数值上看不出来**。
    """

    def setUp(self):
        super().setUp()
        self.ds = self.wb.put_dataset(domain="vib", name="A")

    def test_入集时按绑定打快照(self):
        a = self.ann(0, binding="sim1")
        self.wb.add_samples(self.ds, [a], {"sim1": "simulated"})
        self.assertEqual(self.wb.list_samples(self.ds).items[0].data_origin, "simulated")

    def test_没给来源的绑定留空而不是现场(self):
        a = self.ann(0, binding="dev1")
        self.wb.add_samples(self.ds, [a], {"sim1": "simulated"})   # dev1 不在表里
        self.assertEqual(self.wb.list_samples(self.ds).items[0].data_origin, "")

    def test_一次入集多个绑定各取各的(self):
        a1, a2 = self.ann(0, binding="sim1"), self.ann(1, binding="dev1")
        self.wb.add_samples(self.ds, [a1, a2],
                            {"sim1": "simulated", "dev1": "field"})
        got = {s.binding: s.data_origin for s in self.wb.list_samples(self.ds).items}
        self.assertEqual(got, {"sim1": "simulated", "dev1": "field"})

    def test_再次加入同一标注不改写快照(self):
        # ★幂等那条路不能变成"改写快照"的后门：绑定后来改了，重点一次"加入"
        #   就把当初的记录洗掉，那这份快照等于没有。
        a = self.ann(0, binding="sim1")
        self.wb.add_samples(self.ds, [a], {"sim1": "simulated"})
        self.assertEqual(self.wb.add_samples(self.ds, [a], {"sim1": "field"}), 0)
        self.assertEqual(self.wb.list_samples(self.ds).items[0].data_origin, "simulated")

    def test_复制训练集连快照一起复制(self):
        a = self.ann(0, binding="sim1")
        self.wb.add_samples(self.ds, [a], {"sim1": "simulated"})
        new_id = self.wb.copy_dataset(self.ds, "A-副本")
        self.assertEqual(self.wb.list_samples(new_id).items[0].data_origin, "simulated")

    def test_工件那一格存得住取得回(self):
        aid = self.wb.add_artifact(domain="vib", name="基线", kind=KIND_BASELINE,
                                   data_origin="simulated")
        got = {x.id: x.data_origin for x in self.wb.list_artifacts(domain="vib").items}
        self.assertEqual(got[aid], "simulated")

    def test_老工件那一格是空不是现场(self):
        aid = self.wb.add_artifact(domain="vib", name="老的")
        got = {x.id: x.data_origin for x in self.wb.list_artifacts(domain="vib").items}
        self.assertEqual(got[aid], "", "1.6 之前的老工件是未声明，**不是现场**")

    def test_训练集的域查得出来(self):
        self.assertEqual(self.wb.dataset_domain(self.ds), "vib")
        self.assertEqual(self.wb.dataset_domain(99999), "")


class TestDataOriginMigration(unittest.TestCase):
    """★1.5 时代的老库（AISERVER 上正跑着的就是这一份）开库即补列。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表一个字都不改 —— 不补列，就是读的时候才炸
    （`no such column`），而且是在现场。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "wb.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_老库补列且老行一律未声明(self):
        import sqlite3
        conn = sqlite3.connect(str(self.path))
        conn.executescript("""
        CREATE TABLE datasets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, name TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(domain, name));
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id INTEGER NOT NULL,
            annotation_id INTEGER, binding TEXT NOT NULL, t_from_us INTEGER NOT NULL,
            t_to_us INTEGER NOT NULL, label TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(dataset_id, annotation_id));
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'model', binding TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL, algo TEXT NOT NULL DEFAULT '', dataset_id INTEGER,
            sample_count INTEGER NOT NULL DEFAULT 0, feature_count INTEGER NOT NULL DEFAULT 0,
            accuracy REAL, active INTEGER NOT NULL DEFAULT 0, path TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            origin TEXT NOT NULL DEFAULT 'trained', source TEXT NOT NULL DEFAULT '',
            training_data TEXT NOT NULL DEFAULT '', license TEXT NOT NULL DEFAULT '');
        INSERT INTO datasets(id,domain,name) VALUES(1,'vib','老集');
        INSERT INTO samples(dataset_id,annotation_id,binding,t_from_us,t_to_us,label)
            VALUES(1,1,'sim1',0,1000,'正常');
        INSERT INTO artifacts(domain,name,dataset_id) VALUES('vib','老基线',1);
        """)
        conn.commit(); conn.close()

        wb = Workbench(self.path)
        try:
            (a,) = wb.list_artifacts(domain="vib").items
            self.assertEqual(a.data_origin, "", "老工件必须是未声明，**不许**被回填成现场")
            (s,) = wb.list_samples(1).items
            self.assertEqual(s.data_origin, "", "老样本没有快照就是没有")
            # 补列之后新写的照常带得上
            aid = wb.add_artifact(domain="vib", name="新的", data_origin="simulated")
            got = {x.id: x.data_origin for x in wb.list_artifacts(domain="vib").items}
            self.assertEqual(got[aid], "simulated")
        finally:
            wb.close()


class TestBindingDataOrigin(unittest.TestCase):
    """绑定上那一格：存得住、老库补得上、空不是现场。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "b.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_存得住取得回(self):
        from aiintegration.bindings import Binding, BindingStore
        st = BindingStore(self.path)
        try:
            st.put(Binding("vib", "sim1", {"x_vel": 100}, data_origin="simulated"))
            self.assertEqual(st.get("vib", "sim1").data_origin, "simulated")
            # 改成现场（真机接入那一刻）——绑定这一格是**可以**改的，改的是"以后"。
            st.put(Binding("vib", "sim1", {"x_vel": 100}, data_origin="field"))
            self.assertEqual(st.get("vib", "sim1").data_origin, "field")
        finally:
            st.close()

    def test_没声明就是空不是现场(self):
        from aiintegration.bindings import Binding, BindingStore
        st = BindingStore(self.path)
        try:
            st.put(Binding("vib", "d1", {"x_vel": 100}))
            self.assertEqual(st.get("vib", "d1").data_origin, "")
        finally:
            st.close()

    def test_老库开库即补列(self):
        import sqlite3
        from aiintegration.bindings import BindingStore
        conn = sqlite3.connect(str(self.path))
        conn.executescript("""
        CREATE TABLE bindings (
            domain TEXT NOT NULL, binding TEXT NOT NULL, roles_json TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}', interval_sec REAL NOT NULL,
            window_sec REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (domain, binding));
        INSERT INTO bindings(domain,binding,roles_json,interval_sec,window_sec)
            VALUES('vib','老绑定','{"x_vel": 100}',60,60);
        """)
        conn.commit(); conn.close()
        st = BindingStore(self.path)
        try:
            b = st.get("vib", "老绑定")
            self.assertEqual(b.data_origin, "", "老绑定必须是未声明，不许当现场")
            self.assertEqual(b.roles, {"x_vel": 100})
        finally:
            st.close()


class TestDeactivate(WbBase):
    """停用（契约 1.7）—— 补的是一个**现场撞出来的**缺口：能启用、不能停用。"""

    def test_停用后就没有激活件了(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        self.wb.activate_artifact(a)
        self.assertEqual(self.wb.active_artifact("vib", binding="dev1").id, a)
        self.assertTrue(self.wb.deactivate_artifact(a))
        self.assertIsNone(self.wb.active_artifact("vib", binding="dev1"),
                          "停用之后该对象就该是「没有可用工件」，调用方据此落 -1034")

    def test_重复停用不是错但要分得清(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        self.wb.activate_artifact(a)
        self.assertTrue(self.wb.deactivate_artifact(a), "本来是激活的 → True")
        self.assertFalse(self.wb.deactivate_artifact(a), "本来就没激活 → False，不是抛错")

    def test_没这个工件要响亮地失败(self):
        with self.assertRaises(WorkbenchError):
            self.wb.deactivate_artifact(99999)

    def test_作用域建错的全域通用件能被取消(self):
        """★这条就是 2026-09-16 现场那一幕，是本次加这一口的全部理由。

        训练时 binding 传了空串 ⇒ 工件落成"全域通用"并激活。启用别的件顶不掉它
        （不同作用域），删又删不得（激活中）—— 在 1.7 之前它就永远卡在那里，
        而该域任何新绑定都会静默回退捡到它。
        """
        wrong = self.wb.add_artifact(domain="vib", name="误创-全域通用",
                                     kind=KIND_BASELINE, binding="")
        right = self.wb.add_artifact(domain="vib", name="对的-绑定专属",
                                     kind=KIND_BASELINE, binding="dev1")
        self.wb.activate_artifact(wrong)
        self.wb.activate_artifact(right)
        # ★先证明"顶不掉"：两个都还激活着，因为作用域不同
        actives = {x.id for x in self.wb.list_artifacts(domain="vib").items if x.active}
        self.assertEqual(actives, {wrong, right}, "作用域不同，启用一个顶不掉另一个")
        # ★也证明"删不得"
        with self.assertRaises(WorkbenchError):
            self.wb.delete_artifact(wrong)
        # ⇒ 只有停用这一条路
        self.assertTrue(self.wb.deactivate_artifact(wrong))
        self.assertIsNone(self.wb.active_artifact("vib", KIND_BASELINE, ""),
                          "全域通用那一档不该再有激活件 —— 否则新绑定仍会回退捡到它")
        self.assertEqual(self.wb.active_artifact("vib", KIND_BASELINE, "dev1").id, right,
                         "绑定专属那一档不受影响")
        # 停用之后就删得掉了
        self.assertTrue(self.wb.delete_artifact(wrong))
