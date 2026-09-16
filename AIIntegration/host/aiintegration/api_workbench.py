"""工作台那几口的协议翻译（契约 1.2）。

★与 `api.py` 同一条纪律：**本模块不含业务逻辑**，只做 protobuf ⇄ `workbench` 的翻译。
  判断、校验、事务全在 `workbench.py` 里 —— 校验写在协议层，换个入口（HTTP、自检脚本）就绕过去了。

★**`WorkbenchError` 原样回给调用方**，不吞、不改写成"操作失败"。
  对端要能把那句话直接显示在界面上（"域 vib 下已经有叫 '基准集' 的训练集"比"操作失败"有用得多）。

★时间戳：**不设 = 不限**。把未设的 Timestamp 当成 1970 会把"不限"悄悄变成"从纪元起"，
  在 `t_from` 上看不出来，在 `t_to` 上会把整个窗口掐掉（与 `api.py` 里 `_dt` 同一条）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import grpc

from .apiproto import aiintegration_pb2 as pb
from .workbench import WorkbenchError

logger = logging.getLogger(__name__)


def _dt(ts):
    """protobuf Timestamp → datetime；未设置的回 None（= 不限），不是 1970。"""
    if ts is None or (ts.seconds == 0 and ts.nanos == 0):
        return None
    return ts.ToDatetime().replace(tzinfo=timezone.utc)


def _set_ts(field, dt: datetime) -> None:
    field.FromDatetime(dt)


def _guard(fn):
    """把 `WorkbenchError` 变成 `ok=false + 原因原文`，别的异常照旧往上抛。

    ★只接住"调用方给错了"这一类。真出了内部错误（磁盘满、库损坏）**不许**伪装成
      一句温和的 `ok=false` —— 那会让对端以为是自己参数错，去改参数，而真相在别处。
    """
    def wrap(self, request, context):
        try:
            return fn(self, request, context)
        except WorkbenchError as exc:
            return pb.MutateRes(ok=False, message=str(exc))
    return wrap


class WorkbenchApiMixin:
    """挂到 `ApiService` 上。

    要求宿主提供 `self._wb`（Workbench）、`self._rediagnose`、`self._trainer`。
    后两个可以是 `None` —— 那时对应的口**如实回"未接"**，不假装成功。
    """

    # ── 标注 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _to_pb_annotation(a) -> pb.Annotation:
        out = pb.Annotation(id=a.id, domain=a.domain, binding=a.binding, label=a.label,
                            note=a.note, created_at=a.created_at, updated_at=a.updated_at)
        _set_ts(out.t_from, a.t_from)
        _set_ts(out.t_to, a.t_to)
        return out

    def ListAnnotations(self, request, context):
        try:
            page = self._wb.list_annotations(
                domain=request.domain, binding=request.binding, label=request.label,
                t_from=_dt(request.t_from), t_to=_dt(request.t_to),
                offset=request.offset, limit=request.limit)
        except WorkbenchError as exc:
            # 列表口没有 ok 字段，只能用 gRPC 状态码说话 —— 但**要说**，不能回一页空的：
            # 空页与"参数错"在界面上看着一样，而处置完全不同。
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        res = pb.ListAnnotationsRes(total=page.total)
        for a in page.items:
            res.items.append(self._to_pb_annotation(a))
        return res

    @_guard
    def PutAnnotation(self, request, context):
        new_id = self._wb.put_annotation(
            domain=request.domain, binding=request.binding,
            t_from=_require_ts(request.t_from, "t_from"),
            t_to=_require_ts(request.t_to, "t_to"),
            label=request.label, note=request.note,
            annotation_id=request.id or None)
        return pb.MutateRes(ok=True, id=new_id)

    def AnnotateRange(self, request, context):
        try:
            ids = self._wb.annotate_range(
                domain=request.domain, bindings=list(request.bindings),
                t_from=_require_ts(request.t_from, "t_from"),
                t_to=_require_ts(request.t_to, "t_to"),
                label=request.label, note=request.note)
        except WorkbenchError as exc:
            return pb.AnnotateRangeRes(ok=False, message=str(exc))
        return pb.AnnotateRangeRes(ok=True, ids=ids)

    @_guard
    def DeleteAnnotations(self, request, context):
        n = self._wb.delete_annotations(list(request.ids))
        # ★回受影响条数，不只回 ok：批量删了 3 条里的 2 条（有一个 id 不存在）
        #   与"3 条全删了"在界面上必须看得出区别。
        return pb.MutateRes(ok=True, id=n,
                            message="" if n == len(request.ids)
                            else f"给了 {len(request.ids)} 个 id，实际删掉 {n} 条（其余不存在）")

    def ListLabels(self, request, context):
        rows = self._wb.list_labels(request.domain, request.dataset_id or None)
        res = pb.ListLabelsRes()
        for label, count in rows:
            res.items.add(label=label, count=count)
        return res

    # ── 训练集 ────────────────────────────────────────────────────────────
    def ListDatasets(self, request, context):
        res = pb.ListDatasetsRes()
        for d in self._wb.list_datasets(request.domain):
            res.items.add(id=d.id, domain=d.domain, name=d.name, note=d.note,
                          sample_count=d.sample_count,
                          created_at=d.created_at, updated_at=d.updated_at)
        return res

    @_guard
    def PutDataset(self, request, context):
        new_id = self._wb.put_dataset(domain=request.domain, name=request.name,
                                      note=request.note, dataset_id=request.id or None)
        return pb.MutateRes(ok=True, id=new_id)

    @_guard
    def DeleteDataset(self, request, context):
        ok = self._wb.delete_dataset(request.id)
        return pb.MutateRes(ok=ok, message="" if ok else "没有这个训练集")

    @_guard
    def CopyDataset(self, request, context):
        new_id = self._wb.copy_dataset(request.id, request.new_name)
        return pb.MutateRes(ok=True, id=new_id)

    # ── 样本 ──────────────────────────────────────────────────────────────
    def ListSamples(self, request, context):
        try:
            page = self._wb.list_samples(request.dataset_id, label=request.label,
                                         binding=request.binding,
                                         offset=request.offset, limit=request.limit)
        except WorkbenchError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        res = pb.ListSamplesRes(total=page.total)
        for s in page.items:
            item = res.items.add(id=s.id, dataset_id=s.dataset_id,
                                 annotation_id=s.annotation_id or 0,
                                 binding=s.binding, label=s.label, created_at=s.created_at)
            _set_ts(item.t_from, s.t_from)
            _set_ts(item.t_to, s.t_to)
        return res

    @_guard
    def AddSamples(self, request, context):
        # ★来源性质在**入集这一刻**从绑定取一次快照（契约 1.6）：库层不连绑定表，
        #   所以由这一层查好给它。日后改绑定不反写已入集的样本 —— 那正是快照的意义。
        domain = self._wb.dataset_domain(request.dataset_id)
        origins = {b.binding: b.data_origin
                   for b in self._bindings.list(domain or None) if b.data_origin}
        n = self._wb.add_samples(request.dataset_id, list(request.annotation_ids), origins)
        asked = len(request.annotation_ids)
        return pb.MutateRes(ok=True, id=n,
                            message="" if n == asked
                            else f"给了 {asked} 条，新增 {n} 条（其余已在集内或标注不存在）")

    @_guard
    def RemoveSamples(self, request, context):
        n = self._wb.remove_samples(request.dataset_id, list(request.sample_ids))
        return pb.MutateRes(ok=True, id=n, message="已移出（原始标注未动）")

    @_guard
    def CopySamples(self, request, context):
        n = self._wb.copy_samples(list(request.sample_ids), request.to_dataset)
        asked = len(request.sample_ids)
        return pb.MutateRes(ok=True, id=n,
                            message="" if n == asked
                            else f"给了 {asked} 条，新增 {n} 条（其余在目标集里已有）")

    # ── 工件 ──────────────────────────────────────────────────────────────
    def ListArtifacts(self, request, context):
        try:
            page = self._wb.list_artifacts(
                domain=request.domain, kind=request.kind,
                binding=request.binding if request.binding_set else None,
                offset=request.offset, limit=request.limit)
        except WorkbenchError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        res = pb.ListArtifactsRes(total=page.total)
        for a in page.items:
            res.items.add(
                id=a.id, domain=a.domain, kind=a.kind, binding=a.binding, name=a.name,
                algo=a.algo, dataset_id=a.dataset_id or 0, sample_count=a.sample_count,
                feature_count=a.feature_count,
                # ★没测过的准确率**不给数**，用 has_accuracy 说明 —— 给 0 会被画成"准确率 0%"。
                accuracy=a.accuracy if a.accuracy is not None else 0.0,
                has_accuracy=a.accuracy is not None,
                active=a.active, path=a.path, size=a.size, sha256=a.sha256,
                meta_json=a.meta_json, created_at=a.created_at,
                origin=a.origin, source=a.source,
                training_data=a.training_data, license=a.license,
                data_origin=a.data_origin)
        return res

    @_guard
    def ActivateArtifact(self, request, context):
        self._wb.activate_artifact(request.id)
        return pb.MutateRes(ok=True, id=request.id)

    @_guard
    def DeactivateArtifact(self, request, context):
        was = self._wb.deactivate_artifact(request.id)
        # ★如实回"本来是不是激活的"：重复停用不是错，但要让调用方分得清这两种情形。
        return pb.MutateRes(ok=True, id=1 if was else 0,
                            message="" if was else "该工件本来就不是激活状态")

    @_guard
    def DeleteArtifact(self, request, context):
        ok = self._wb.delete_artifact(request.id)
        return pb.MutateRes(ok=ok, message="" if ok else "没有这个工件")

    # ── 训练任务 ──────────────────────────────────────────────────────────
    @staticmethod
    def _to_pb_job(j) -> pb.TrainJob:
        return pb.TrainJob(
            id=j.id, domain=j.domain, dataset_id=j.dataset_id, binding=j.binding,
            algo=j.algo, status=j.status, message=j.message, progress=j.progress,
            sample_count=j.sample_count, artifact_id=j.artifact_id or 0,
            created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at)

    @_guard
    def StartTraining(self, request, context):
        """建一条待跑任务，**立刻返回**。★不阻塞 —— 界面随便关。"""
        if self._trainer is None:
            return pb.MutateRes(
                ok=False, message="本实例未接训练执行器（只读模式），无法开训")
        jid = self._trainer.submit(domain=request.domain, dataset_id=request.dataset_id,
                                   binding=request.binding, algo=request.algo)
        return pb.MutateRes(ok=True, id=jid, message="已排队；用 GetTrainJob 查进度")

    @_guard
    def CancelTrainJob(self, request, context):
        if self._trainer is None:
            return pb.MutateRes(ok=False, message="本实例未接训练执行器（只读模式）")
        # ★回执原样透出执行器那句话：三种情形（排队中/正在跑/已终态）结果不同，
        #   笼统回一句"已取消"会让界面说谎。
        return pb.MutateRes(ok=True, id=request.id,
                            message=self._trainer.cancel(request.id))

    def GetTrainJob(self, request, context):
        job = self._wb.get_job(request.id)
        if job is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"没有 id={request.id} 这个训练任务")
        return self._to_pb_job(job)

    def ListTrainJobs(self, request, context):
        try:
            page = self._wb.list_jobs(domain=request.domain, status=request.status,
                                      offset=request.offset, limit=request.limit)
        except WorkbenchError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        res = pb.ListTrainJobsRes(total=page.total)
        for j in page.items:
            res.items.append(self._to_pb_job(j))
        return res

    # ── 片段与报告 ────────────────────────────────────────────────────────
    def ListSegments(self, request, context):
        try:
            page = self._wb.list_segments(domain=request.domain, binding=request.binding,
                                          offset=request.offset, limit=request.limit)
        except WorkbenchError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        res = pb.ListSegmentsRes(total=page.total)
        for s in page.items:
            item = res.items.add(id=s.id, domain=s.domain, binding=s.binding, name=s.name,
                                 source=s.source, point_count=s.point_count, note=s.note,
                                 created_at=s.created_at)
            _set_ts(item.t_from, s.t_from)
            _set_ts(item.t_to, s.t_to)
        return res

    @_guard
    def PutSegment(self, request, context):
        sid = self._wb.put_segment(
            domain=request.domain, binding=request.binding,
            t_from=_require_ts(request.t_from, "t_from"),
            t_to=_require_ts(request.t_to, "t_to"),
            name=request.name, source=request.source,
            point_count=request.point_count, note=request.note)
        return pb.MutateRes(ok=True, id=sid)

    @_guard
    def DeleteSegment(self, request, context):
        ok = self._wb.delete_segment(request.id)
        return pb.MutateRes(ok=ok, message="" if ok else "没有这个片段")

    def ListReports(self, request, context):
        res = pb.ListReportsRes()
        for r in self._wb.list_reports(request.id):
            res.items.add(id=r.id, segment_id=r.segment_id, kind=r.kind,
                          path=r.path, size=r.size, created_at=r.created_at)
        return res

    def RediagnoseSegment(self, request, context):
        """对片段再判别一次。**不写回实时库**（回溯判别不该篡改历史）。"""
        if self._rediagnose is None:
            return pb.RediagnoseRes(
                ok=False, message="本实例未接推理执行器（只读模式），无法回溯判别")
        seg = self._wb.get_segment(request.id)
        if seg is None:
            return pb.RediagnoseRes(ok=False, message=f"没有 id={request.id} 这个片段")
        try:
            findings, note = self._rediagnose(seg)
        except Exception as exc:  # noqa: BLE001 —— 执行器的异常不许掀翻这条 RPC
            logger.exception("回溯判别失败：片段 %s", request.id)
            return pb.RediagnoseRes(ok=False, message=f"回溯判别失败: {exc!r}")

        res = pb.RediagnoseRes(ok=True, message=note)
        for f, display in findings:
            item = res.findings.add(key=f.key, display=display,
                                    status_code=f.quality.to_status_code())
            _set_ts(item.t, f.t)
            # ★坏质量下**一个值都不给**（`Finding` 那层已经保证 value is None）。
            if f.value is None:
                continue
            if isinstance(f.value, bool):
                item.flag = f.value
            elif isinstance(f.value, (int, float)):
                item.num = float(f.value)
            else:
                item.text = str(f.value)
        return res


def _require_ts(ts, what: str) -> datetime:
    """写入口的时刻**必须给** —— 这里"不设"不是"不限"，是漏填。"""
    v = _dt(ts)
    if v is None:
        raise WorkbenchError(f"{what} 必填（未设的时间戳在写入口是漏填，不是'不限'）")
    return v


#: 挂进 `build_handler` 的方法表。放这里而不是 `api.py`，是为了让"加一口"只改一处。
def method_specs(pb_mod, svc):
    return {
        "ListAnnotations":   (svc.ListAnnotations, pb_mod.ListAnnotationsReq, pb_mod.ListAnnotationsRes),
        "PutAnnotation":     (svc.PutAnnotation, pb_mod.PutAnnotationReq, pb_mod.MutateRes),
        "AnnotateRange":     (svc.AnnotateRange, pb_mod.AnnotateRangeReq, pb_mod.AnnotateRangeRes),
        "DeleteAnnotations": (svc.DeleteAnnotations, pb_mod.DeleteAnnotationsReq, pb_mod.MutateRes),
        "ListLabels":        (svc.ListLabels, pb_mod.ListLabelsReq, pb_mod.ListLabelsRes),
        "ListDatasets":      (svc.ListDatasets, pb_mod.ListDatasetsReq, pb_mod.ListDatasetsRes),
        "PutDataset":        (svc.PutDataset, pb_mod.PutDatasetReq, pb_mod.MutateRes),
        "DeleteDataset":     (svc.DeleteDataset, pb_mod.IdReq, pb_mod.MutateRes),
        "CopyDataset":       (svc.CopyDataset, pb_mod.CopyDatasetReq, pb_mod.MutateRes),
        "ListSamples":       (svc.ListSamples, pb_mod.ListSamplesReq, pb_mod.ListSamplesRes),
        "AddSamples":        (svc.AddSamples, pb_mod.AddSamplesReq, pb_mod.MutateRes),
        "RemoveSamples":     (svc.RemoveSamples, pb_mod.RemoveSamplesReq, pb_mod.MutateRes),
        "CopySamples":       (svc.CopySamples, pb_mod.CopySamplesReq, pb_mod.MutateRes),
        "ListArtifacts":     (svc.ListArtifacts, pb_mod.ListArtifactsReq, pb_mod.ListArtifactsRes),
        "ActivateArtifact":  (svc.ActivateArtifact, pb_mod.IdReq, pb_mod.MutateRes),
        "DeactivateArtifact": (svc.DeactivateArtifact, pb_mod.IdReq, pb_mod.MutateRes),
        "DeleteArtifact":    (svc.DeleteArtifact, pb_mod.IdReq, pb_mod.MutateRes),
        "ListTrainJobs":     (svc.ListTrainJobs, pb_mod.ListTrainJobsReq, pb_mod.ListTrainJobsRes),
        "StartTraining":     (svc.StartTraining, pb_mod.StartTrainingReq, pb_mod.MutateRes),
        "CancelTrainJob":    (svc.CancelTrainJob, pb_mod.IdReq, pb_mod.MutateRes),
        "GetTrainJob":       (svc.GetTrainJob, pb_mod.IdReq, pb_mod.TrainJob),
        "ListSegments":      (svc.ListSegments, pb_mod.ListSegmentsReq, pb_mod.ListSegmentsRes),
        "PutSegment":        (svc.PutSegment, pb_mod.PutSegmentReq, pb_mod.MutateRes),
        "DeleteSegment":     (svc.DeleteSegment, pb_mod.IdReq, pb_mod.MutateRes),
        "ListReports":       (svc.ListReports, pb_mod.IdReq, pb_mod.ListReportsRes),
        "RediagnoseSegment": (svc.RediagnoseSegment, pb_mod.IdReq, pb_mod.RediagnoseRes),
    }
