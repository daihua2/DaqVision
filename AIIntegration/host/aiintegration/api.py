"""对外口 —— **控制面 gRPC**（C-8 §3 定）。大对象走 HTTP，不在本模块。

★**只面向 AICloud 后端，浏览器不直连**（AI-9 §2.2）。
  由 AICloud 桥给前端，并**逐个 RPC 自查 ACL** —— 我方不实现平台的账号体系，
  抄一份必漂，漂的样子是「这个人在平台里看不到这台设备，却能在 AI 页面上改它的标注」。

★服务端用 `grpc.method_handlers_generic_handler` **手接**，不用生成的 `_pb2_grpc.py`：
  只需要系统 protoc，不需要 grpc 的 python 插件 —— 少一个构建期依赖。
"""

from __future__ import annotations

import logging
import platform
import sys
from datetime import datetime, timezone

import grpc

from .api_workbench import WorkbenchApiMixin, method_specs
from .apiproto import aiintegration_pb2 as pb
from .bindings import Binding, BindingStore
from .domains import LoadedDomain
from .logstore import LogFilter, LogLevel, LogStore

logger = logging.getLogger(__name__)

SERVICE = "aiintegration.AIIntegrationService"
PROTO_VERSION = "1.6"


def _ts(dt: datetime) -> object:
    from google.protobuf.timestamp_pb2 import Timestamp
    t = Timestamp()
    t.FromDatetime(dt)
    return t


def _dt(ts) -> datetime | None:
    """protobuf Timestamp → datetime；**未设置的返回 None 而不是 1970**。

    ★不设 = 不限（契约如此）。把它当成 1970 就是把"不限"悄悄变成"从纪元起"，
    在 `fromTime` 上看不出区别，在 `toTime` 上会把整个窗口掐掉。
    """
    if ts is None or (ts.seconds == 0 and ts.nanos == 0):
        return None
    return ts.ToDatetime().replace(tzinfo=timezone.utc)


class ApiService(WorkbenchApiMixin):
    """把骨架的各件接到 gRPC 上。**本类不含业务逻辑** —— 只做协议翻译。

    工作台那二十来口在 `api_workbench.py`（同一条纪律，只是文件分开：加一口只改一处）。
    """

    def __init__(self, *, guid: str, version: str, logstore: LogStore,
                 domains: dict[str, LoadedDomain], bindings: BindingStore,
                 load_errors: list[tuple[str, str]] | None = None,
                 on_bindings_changed=None, workbench=None, rediagnose=None,
                 trainer=None) -> None:
        self._guid = guid
        self._version = version
        self._logs = logstore
        self._domains = domains
        self._bindings = bindings
        self._load_errors = load_errors or []
        # ★绑定变更后必须让调度器**重新同步**：新绑定要建点、要起线程。
        #   不回调的话，界面上看着配好了、实际一拍都不走。
        self._on_changed = on_bindings_changed
        # 工作台。★没接就是没接 —— 那几口会当场报错，**不假装成功**。
        self._wb = workbench
        # 回溯判别的执行器（片段 → [(Finding, 显示名)], 备注）。没接则该口如实回"只读模式"。
        self._rediagnose = rediagnose
        # 训练执行器。没接则 StartTraining/CancelTrainJob 如实回"未接"，**不建注定没人跑的任务**。
        self._trainer = trainer

    # ── 身份与域 ──────────────────────────────────────────────────────────
    def GetInfo(self, request, context):
        reply = pb.InfoReply(
            service_version=self._version,
            proto_version=PROTO_VERSION,
            guid=self._guid,
            runtime=f"CPython {platform.python_version()} / {sys.platform}-{platform.machine()}",
            domain_count=len(self._domains),
        )
        # ★装载失败**不藏**：静默跳过会变成"某个域莫名其妙不见了"，那是最难查的一类。
        for f, why in self._load_errors:
            reply.load_errors.add(file=f, reason=why)
        return reply

    def ListDomains(self, request, context):
        reply = pb.DomainsReply()
        for d in self._domains.values():
            info = reply.domains.add(
                key=d.instance.key,
                display=d.instance.display or d.instance.key,
                version=d.instance.version,
                capabilities=sorted(d.caps),
            )
            for i in d.declaration.inputs:
                info.inputs.add(role=i.role, unit=i.unit,
                                required=i.required, description=i.description,
                                kind=i.kind)
            for o in d.declaration.outputs:
                info.outputs.add(key=o.key, display=o.display, value_type=o.value_type,
                                 unit=o.unit, description=o.description)
            # 台账参数自述 —— 贵方**按这张表渲染绑定表单**，不按域名写死字段。
            for pm in d.declaration.params:
                spec = info.params.add(
                    key=pm.key, display=pm.display, value_type=pm.value_type,
                    default=pm.default, required=pm.required,
                    unit=pm.unit, description=pm.description)
                spec.choices.extend(pm.choices)
                spec.choice_displays.extend(pm.choice_displays)
        return reply

    # ── 绑定 ──────────────────────────────────────────────────────────────
    def _to_pb_binding(self, b: Binding) -> pb.Binding:
        out = pb.Binding(domain=b.domain, binding=b.binding,
                         interval_sec=b.interval_sec, window_sec=b.window_sec,
                         enabled=b.enabled, data_origin=b.data_origin)
        for role, gid in b.roles.items():
            out.roles[role] = gid
        for k, v in b.params.items():
            out.params[k] = v
        loaded = self._domains.get(b.domain)
        if loaded is not None:
            # 只算**测点类**必填角色：图片类输入不绑 globalId，由上传触发，不存在"没绑"。
            required = [i.role for i in loaded.declaration.inputs
                        if i.required and i.kind == "point"]
            out.missing_required.extend(b.missing_required(required))
        return out

    def ListBindings(self, request, context):
        reply = pb.ListBindingsReply()
        for b in self._bindings.list(request.domain or None):
            reply.bindings.append(self._to_pb_binding(b))
        return reply

    def PutBinding(self, request, context):
        b = request.binding
        if b.domain not in self._domains:
            # 拒收而不是存下来：存一个指向未装载域的绑定，调度器每拍都会跳过并告警，
            # 而配置者以为配好了。
            return pb.PutBindingReply(
                ok=False, message=f"域 {b.domain!r} 未装载；已装载：{sorted(self._domains)}")
        # 纯图片域（没有测点类输入）的绑定本就没有 roles —— 只有这种域才放行空 roles。
        loaded = self._domains[b.domain]
        no_point_inputs = not any(i.kind == "point" for i in loaded.declaration.inputs)
        try:
            self._bindings.put(Binding(
                domain=b.domain, binding=b.binding, roles=dict(b.roles),
                params=dict(b.params),
                data_origin=b.data_origin,
                interval_sec=b.interval_sec or 60.0,
                window_sec=b.window_sec or 60.0,
                enabled=b.enabled), allow_no_roles=no_point_inputs)
        except ValueError as exc:
            # 校验失败原样回给调用方（globalId=0、一个角色都没绑…），**不吞**。
            return pb.PutBindingReply(ok=False, message=str(exc))
        return pb.PutBindingReply(ok=True, message=self._notify_changed())

    def DeleteBinding(self, request, context):
        ok = self._bindings.delete(request.domain, request.binding)
        # ★只停算，不删结论点。
        if ok:
            self._notify_changed()
        return pb.DeleteBindingReply(ok=ok, message="" if ok else "没有这条绑定")

    def _notify_changed(self) -> str:
        """通知调度器同步。**失败不吞**：回给调用方，否则配置者以为生效了。"""
        if self._on_changed is None:
            return ""
        try:
            self._on_changed()
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("绑定变更后同步调度失败")
            return f"绑定已存，但调度同步失败（下次重启或再次改绑定会重试）: {exc}"

    # ── 日志（形状对齐 hs）────────────────────────────────────────────────
    @staticmethod
    def _to_pb_log(r) -> pb.LogRecord:
        rec = pb.LogRecord(
            SequenceId=r.sequence_id, LogLevel=int(r.level), Category=r.category,
            Message=r.message, MemberName=r.member_name, LineNumber=r.line_number,
            StatuCode=r.status_code)
        rec.TimeStamp.FromDatetime(r.timestamp)
        return rec

    def _filter(self, req, *, with_time: bool) -> LogFilter:
        return LogFilter(
            min_level=LogLevel(int(req.minLevel)),
            category=req.category,
            search=req.search,
            from_time=_dt(req.fromTime) if with_time else None,
            to_time=_dt(req.toTime) if with_time else None,
        )

    def QueryLogs(self, request, context):
        res = self._logs.query(self._filter(request, with_time=True),
                               offset=request.offset, limit=request.limit,
                               newest_first=request.newestFirst)
        out = pb.QueryLogsRes(TotalCount=res.total_count,
                              ReachedOldest=res.reached_oldest,
                              EvictedTotal=self._logs.evicted_count())
        for r in res.logs:
            out.Logs.append(self._to_pb_log(r))
        return out

    def SubscribeLogs(self, request, context):
        sub = self._logs.subscribe(self._filter(request, with_time=False),
                                   backlog=request.backlog)
        last_dropped = 0
        try:
            while context.is_active():
                rec = sub.get(timeout=0.5)
                if rec is not None:
                    yield pb.LogStreamItem(record=self._to_pb_log(rec))
                # ★背压不静默：丢了多少要告诉订阅者（少几行的日志比没有日志更误导）。
                d = sub.dropped_total
                if d != last_dropped:
                    last_dropped = d
                    yield pb.LogStreamItem(droppedTotal=d)
        finally:
            sub.close()


# ── 装配 ──────────────────────────────────────────────────────────────────

def _unary(fn, req_cls, res_cls):
    return grpc.unary_unary_rpc_method_handler(
        fn, request_deserializer=req_cls.FromString,
        response_serializer=lambda m: m.SerializeToString())


def _stream(fn, req_cls, res_cls):
    return grpc.unary_stream_rpc_method_handler(
        fn, request_deserializer=req_cls.FromString,
        response_serializer=lambda m: m.SerializeToString())


def build_handler(svc: ApiService) -> grpc.GenericRpcHandler:
    m = {
        "GetInfo":       _unary(svc.GetInfo, pb.InfoRequest, pb.InfoReply),
        "ListDomains":   _unary(svc.ListDomains, pb.DomainsRequest, pb.DomainsReply),
        "ListBindings":  _unary(svc.ListBindings, pb.ListBindingsRequest, pb.ListBindingsReply),
        "PutBinding":    _unary(svc.PutBinding, pb.PutBindingRequest, pb.PutBindingReply),
        "DeleteBinding": _unary(svc.DeleteBinding, pb.DeleteBindingRequest, pb.DeleteBindingReply),
        "QueryLogs":     _unary(svc.QueryLogs, pb.LogQueryReq, pb.QueryLogsRes),
        "SubscribeLogs": _stream(svc.SubscribeLogs, pb.LogSubscribeReq, pb.LogStreamItem),
    }
    # 工作台那二十来口（契约 1.2）。★只在真接了 Workbench 时才注册：
    #   注册了却没有库，调用方拿到的是内部错误堆栈；不注册拿到的是 UNIMPLEMENTED —— 后者说的是实话。
    if getattr(svc, "_wb", None) is not None:
        for name, (fn, req_cls, res_cls) in method_specs(pb, svc).items():
            m[name] = _unary(fn, req_cls, res_cls)
    return grpc.method_handlers_generic_handler(SERVICE, m)
