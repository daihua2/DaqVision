"""工作台那二十来口的回归 —— 起**真的 gRPC server**、用真 channel 打。

★为什么不满足于 `test_workbench.py`（库层已经 44 条全绿）：库层对了、翻译层丢一个字段，
  对端拿到的就是错的，而两边的单测都绿。`AI-11` 那次"运行期新加的绑定既不建点也不起线程"
  就是这么漏的 —— **单测全绿时它一直在**，靠起真服务打真口才逮到。

钉的重点：
  · `total` 是过滤后的真实总数（翻译层最容易顺手填成 `len(items)`）；
  · 校验失败 **`ok=false` + 原因原文**，不吞成"操作失败"；
  · 准确率**没测过时 `has_accuracy=false`**，不是 0；
  · 移出 ≠ 删除，这条要在**线上**也成立；
  · 没接 Workbench 时那些口回 **UNIMPLEMENTED**，不是内部错误。
"""

import tempfile
import unittest
from concurrent import futures
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grpc

from aiintegration.api import SERVICE, ApiService, build_handler
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import BindingStore
from aiintegration.domains import discover
from aiintegration.logstore import LogStore
from aiintegration.workbench import KIND_BASELINE, KIND_MODEL, Workbench

UTC = timezone.utc
T0 = datetime(2026, 9, 11, 8, 0, 0, tzinfo=UTC)

DOM = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec

class D(Domain):
    key = "vib"
    display = "振动"
    version = "1.0.0"
    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x", unit="mm/s"),),
            outputs=(OutputSpec(key="score", display="分", value_type="float"),),
        )
    def infer(self, frame):
        return []
'''


class WbApiBase(unittest.TestCase):
    with_workbench = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        dom_dir = root / "domains"; dom_dir.mkdir()
        (dom_dir / "vib.py").write_text(DOM, encoding="utf-8")
        loaded, _ = discover(dom_dir)
        self.wb = Workbench(root / "wb.db") if self.with_workbench else None
        self.bindings = BindingStore(root / "b.db")
        self.svc = ApiService(
            guid="g", version="0.1.0", logstore=LogStore(capacity=50),
            domains={d.key: d for d in loaded}, bindings=self.bindings,
            workbench=self.wb)
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4),
                                  handlers=(build_handler(self.svc),))
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.ch = grpc.insecure_channel(f"127.0.0.1:{port}",
                                        options=[("grpc.enable_http_proxy", 0)])

    def tearDown(self):
        self.ch.close(); self.server.stop(0)
        self.bindings.close()
        if self.wb is not None:
            self.wb.close()
        self._tmp.cleanup()

    def call(self, method, req, res_cls, timeout=5):
        return self.ch.unary_unary(
            f"/{SERVICE}/{method}", request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=res_cls.FromString)(req, timeout=timeout)

    # 便捷造数
    def mk_ann(self, i=0, *, label="正常", binding="dev1"):
        req = pb.PutAnnotationReq(domain="vib", binding=binding, label=label)
        req.t_from.FromDatetime(T0 + timedelta(hours=i))
        req.t_to.FromDatetime(T0 + timedelta(hours=i, minutes=10))
        r = self.call("PutAnnotation", req, pb.MutateRes)
        self.assertTrue(r.ok, r.message)
        return r.id


class TestAnnotationsWire(WbApiBase):
    def test_建改查往返(self):
        aid = self.mk_ann(0)
        res = self.call("ListAnnotations", pb.ListAnnotationsReq(domain="vib"),
                        pb.ListAnnotationsRes)
        self.assertEqual(res.total, 1)
        one = res.items[0]
        self.assertEqual((one.id, one.binding, one.label), (aid, "dev1", "正常"))
        self.assertEqual(one.t_from.ToDatetime().replace(tzinfo=UTC), T0)

    def test_校验失败回原因原文不吞(self):
        req = pb.PutAnnotationReq(domain="vib", binding="d", label="")   # 空标签
        req.t_from.FromDatetime(T0)
        req.t_to.FromDatetime(T0 + timedelta(minutes=1))
        r = self.call("PutAnnotation", req, pb.MutateRes)
        self.assertFalse(r.ok)
        self.assertIn("标签", r.message)      # 界面要能直接显示这句

    def test_写入口漏给时刻是漏填不是不限(self):
        r = self.call("PutAnnotation",
                      pb.PutAnnotationReq(domain="vib", binding="d", label="x"),
                      pb.MutateRes)
        self.assertFalse(r.ok)
        self.assertIn("必填", r.message)

    def test_total是过滤后的真实总数(self):
        for i in range(9):
            self.mk_ann(i, label="正常" if i % 3 == 0 else "不平衡")
        res = self.call("ListAnnotations",
                        pb.ListAnnotationsReq(domain="vib", label="不平衡", limit=2),
                        pb.ListAnnotationsRes)
        self.assertEqual(len(res.items), 2)
        self.assertEqual(res.total, 6, "翻译层最容易把 total 顺手填成 len(items)")

    def test_超上限报错而不是回一页空的(self):
        with self.assertRaises(grpc.RpcError) as c:
            self.call("ListAnnotations", pb.ListAnnotationsReq(limit=99999),
                      pb.ListAnnotationsRes)
        self.assertEqual(c.exception.code(), grpc.StatusCode.INVALID_ARGUMENT)

    def test_批量按范围打标签(self):
        req = pb.AnnotateRangeReq(domain="vib", bindings=["d1", "d2", "d3"], label="不对中")
        req.t_from.FromDatetime(T0)
        req.t_to.FromDatetime(T0 + timedelta(minutes=5))
        r = self.call("AnnotateRange", req, pb.AnnotateRangeRes)
        self.assertTrue(r.ok, r.message)
        self.assertEqual(len(r.ids), 3)

    def test_批量删回受影响条数(self):
        a1 = self.mk_ann(0)
        r = self.call("DeleteAnnotations", pb.DeleteAnnotationsReq(ids=[a1, 9999]),
                      pb.MutateRes)
        self.assertTrue(r.ok)
        self.assertEqual(r.id, 1)
        self.assertIn("其余不存在", r.message, "删 2 个成功 1 个，界面要看得出来")

    def test_标签从数据聚合(self):
        self.mk_ann(0, label="正常"); self.mk_ann(1, label="正常"); self.mk_ann(2, label="松动")
        res = self.call("ListLabels", pb.ListLabelsReq(domain="vib"), pb.ListLabelsRes)
        self.assertEqual([(i.label, i.count) for i in res.items], [("正常", 2), ("松动", 1)])


class TestDatasetsWire(WbApiBase):
    def test_建集加样本再复制(self):
        ds = self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"),
                       pb.MutateRes).id
        ids = [self.mk_ann(0), self.mk_ann(1)]
        r = self.call("AddSamples", pb.AddSamplesReq(dataset_id=ds, annotation_ids=ids),
                      pb.MutateRes)
        self.assertEqual(r.id, 2)
        lst = self.call("ListDatasets", pb.ListDatasetsReq(domain="vib"), pb.ListDatasetsRes)
        self.assertEqual(lst.items[0].sample_count, 2)

        cp = self.call("CopyDataset", pb.CopyDatasetReq(id=ds, new_name="A 副本"), pb.MutateRes)
        self.assertTrue(cp.ok, cp.message)
        self.assertEqual(
            self.call("ListSamples", pb.ListSamplesReq(dataset_id=cp.id),
                      pb.ListSamplesRes).total, 2)

    def test_重名被拒且说清撞了哪个名字(self):
        self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"), pb.MutateRes)
        r = self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"), pb.MutateRes)
        self.assertFalse(r.ok)
        self.assertIn("A", r.message)

    def test_重复加样本回明新增几条(self):
        ds = self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"),
                       pb.MutateRes).id
        a1 = self.mk_ann(0)
        self.call("AddSamples", pb.AddSamplesReq(dataset_id=ds, annotation_ids=[a1]),
                  pb.MutateRes)
        r = self.call("AddSamples", pb.AddSamplesReq(dataset_id=ds, annotation_ids=[a1]),
                      pb.MutateRes)
        self.assertEqual(r.id, 0)
        self.assertIn("已在集内", r.message)

    def test_移出不动标注在线上也成立(self):
        ds = self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"),
                       pb.MutateRes).id
        a1 = self.mk_ann(0)
        self.call("AddSamples", pb.AddSamplesReq(dataset_id=ds, annotation_ids=[a1]),
                  pb.MutateRes)
        sid = self.call("ListSamples", pb.ListSamplesReq(dataset_id=ds),
                        pb.ListSamplesRes).items[0].id
        r = self.call("RemoveSamples", pb.RemoveSamplesReq(dataset_id=ds, sample_ids=[sid]),
                      pb.MutateRes)
        self.assertTrue(r.ok)
        self.assertIn("原始标注未动", r.message)
        self.assertEqual(
            self.call("ListAnnotations", pb.ListAnnotationsReq(), pb.ListAnnotationsRes).total, 1)

    def test_来源标注被删后样本仍在且annotation_id为0(self):
        ds = self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"),
                       pb.MutateRes).id
        a1 = self.mk_ann(0)
        self.call("AddSamples", pb.AddSamplesReq(dataset_id=ds, annotation_ids=[a1]),
                  pb.MutateRes)
        self.call("DeleteAnnotations", pb.DeleteAnnotationsReq(ids=[a1]), pb.MutateRes)
        s = self.call("ListSamples", pb.ListSamplesReq(dataset_id=ds),
                      pb.ListSamplesRes).items[0]
        self.assertEqual(s.annotation_id, 0)
        self.assertEqual(s.label, "正常", "训练历史不该被后来的删除改写")


class TestArtifactsWire(WbApiBase):
    def test_没测过的准确率是未测不是0(self):
        self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        self.wb.add_artifact(domain="vib", name="m2", binding="dev2", accuracy=0.0)
        got = {a.name: a for a in self.call(
            "ListArtifacts", pb.ListArtifactsReq(domain="vib"), pb.ListArtifactsRes).items}
        self.assertFalse(got["m1"].has_accuracy, "没测过要用 has_accuracy 说明，别画成 0%")
        self.assertTrue(got["m2"].has_accuracy)
        self.assertEqual(got["m2"].accuracy, 0.0)

    def test_激活切换与不许删激活件(self):
        a = self.wb.add_artifact(domain="vib", name="m1", binding="dev1")
        b = self.wb.add_artifact(domain="vib", name="m2", binding="dev1")
        self.assertTrue(self.call("ActivateArtifact", pb.IdReq(id=a), pb.MutateRes).ok)
        r = self.call("DeleteArtifact", pb.IdReq(id=a), pb.MutateRes)
        self.assertFalse(r.ok)
        self.assertIn("激活", r.message)
        self.call("ActivateArtifact", pb.IdReq(id=b), pb.MutateRes)
        self.assertTrue(self.call("DeleteArtifact", pb.IdReq(id=a), pb.MutateRes).ok)

    def test_空binding与不过滤要分得开(self):
        """★`binding=''` 是"全域通用"那一档，不是"不过滤" —— 靠 `binding_set` 区分。"""
        self.wb.add_artifact(domain="vib", name="全域", binding="")
        self.wb.add_artifact(domain="vib", name="dev1 的", binding="dev1")
        all_ = self.call("ListArtifacts", pb.ListArtifactsReq(domain="vib"), pb.ListArtifactsRes)
        self.assertEqual(all_.total, 2)
        only_global = self.call(
            "ListArtifacts", pb.ListArtifactsReq(domain="vib", binding="", binding_set=True),
            pb.ListArtifactsRes)
        self.assertEqual(only_global.total, 1)
        self.assertEqual(only_global.items[0].name, "全域")

    def test_基线与模型都在这张表里(self):
        self.wb.add_artifact(domain="vib", name="基线-20260911", kind=KIND_BASELINE)
        self.wb.add_artifact(domain="vib", name="m1", kind=KIND_MODEL)
        res = self.call("ListArtifacts", pb.ListArtifactsReq(domain="vib", kind=KIND_BASELINE),
                        pb.ListArtifactsRes)
        self.assertEqual(res.total, 1)
        self.assertEqual(res.items[0].kind, KIND_BASELINE)


class TestJobsAndSegmentsWire(WbApiBase):
    def test_任务查得到且失败原因带得出来(self):
        ds = self.call("PutDataset", pb.PutDatasetReq(domain="vib", name="A"),
                       pb.MutateRes).id
        self.call("AddSamples", pb.AddSamplesReq(dataset_id=ds, annotation_ids=[self.mk_ann(0)]),
                  pb.MutateRes)
        jid = self.wb.create_job(domain="vib", dataset_id=ds, algo="决策树")
        self.wb.update_job(jid, status="failed", message="特征维度对不上")
        job = self.call("GetTrainJob", pb.IdReq(id=jid), pb.TrainJob)
        self.assertEqual(job.status, "failed")
        self.assertIn("维度", job.message)

    def test_查不存在的任务回NOT_FOUND(self):
        with self.assertRaises(grpc.RpcError) as c:
            self.call("GetTrainJob", pb.IdReq(id=9999), pb.TrainJob)
        self.assertEqual(c.exception.code(), grpc.StatusCode.NOT_FOUND)

    def test_片段与报告往返(self):
        req = pb.PutSegmentReq(domain="vib", binding="dev1", name="停机前", point_count=600)
        req.t_from.FromDatetime(T0)
        req.t_to.FromDatetime(T0 + timedelta(minutes=10))
        sid = self.call("PutSegment", req, pb.MutateRes).id
        self.wb.add_report(segment_id=sid, kind="basic", path="reports/1.pdf", size=99)
        res = self.call("ListReports", pb.IdReq(id=sid), pb.ListReportsRes)
        self.assertEqual(res.items[0].path, "reports/1.pdf")
        segs = self.call("ListSegments", pb.ListSegmentsReq(domain="vib"), pb.ListSegmentsRes)
        self.assertEqual(segs.total, 1)
        self.assertEqual(segs.items[0].name, "停机前")

    def test_没接执行器时回溯判别如实说而不是假装成功(self):
        req = pb.PutSegmentReq(domain="vib", binding="dev1")
        req.t_from.FromDatetime(T0)
        req.t_to.FromDatetime(T0 + timedelta(minutes=1))
        sid = self.call("PutSegment", req, pb.MutateRes).id
        r = self.call("RediagnoseSegment", pb.IdReq(id=sid), pb.RediagnoseRes)
        self.assertFalse(r.ok)
        self.assertIn("只读", r.message)


class TestRediagnoseWire(WbApiBase):
    """接了执行器时，结论按 V/Q/T 三样如实翻译。"""

    def setUp(self):
        super().setUp()
        from aiintegration.quality import Quality
        from aiintegration.types import Finding
        self._Q, self._F = Quality, Finding

        def fake(seg):
            return ([
                (Finding(key="iso_zone", value="C", quality=Quality.OK, t=seg.t_to), "ISO 烈度区"),
                (Finding(key="axial_ratio", value=None,
                         quality=Quality.CONFIG_INCOMPLETE, t=seg.t_to), "轴向/径向比"),
            ], "用当前模型重跑；未写回实时库")
        self.svc._rediagnose = fake

    def test_值与质量码都翻译对且坏质量下不给值(self):
        req = pb.PutSegmentReq(domain="vib", binding="dev1")
        req.t_from.FromDatetime(T0)
        req.t_to.FromDatetime(T0 + timedelta(minutes=10))
        sid = self.call("PutSegment", req, pb.MutateRes).id
        r = self.call("RediagnoseSegment", pb.IdReq(id=sid), pb.RediagnoseRes)
        self.assertTrue(r.ok, r.message)
        self.assertIn("未写回", r.message)
        got = {f.key: f for f in r.findings}
        self.assertEqual(got["iso_zone"].text, "C")
        self.assertEqual(got["iso_zone"].status_code, 1)
        bad = got["axial_ratio"]
        self.assertEqual(bad.status_code, -1001, "台账不全是 -1001，不是 -1007")
        self.assertEqual(bad.WhichOneof("value"), None, "坏质量下一个值都不许给")


class TestNoWorkbench(WbApiBase):
    with_workbench = False

    def test_没接工作台的实例那几口回UNIMPLEMENTED(self):
        """★不注册比注册一个会抛内部错误的更诚实：UNIMPLEMENTED 说的是实话。"""
        with self.assertRaises(grpc.RpcError) as c:
            self.call("ListDatasets", pb.ListDatasetsReq(), pb.ListDatasetsRes)
        self.assertEqual(c.exception.code(), grpc.StatusCode.UNIMPLEMENTED)

    def test_原有那几口照常(self):
        r = self.call("GetInfo", pb.InfoRequest(), pb.InfoReply)
        self.assertEqual(r.proto_version, "1.5")


if __name__ == "__main__":
    unittest.main()
