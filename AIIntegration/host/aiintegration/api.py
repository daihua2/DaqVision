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
from .pointmap import default_point_name
from .quality import Quality
from .types import ROLE_STATUS
from . import structbind

logger = logging.getLogger(__name__)

SERVICE = "aiintegration.AIIntegrationService"
PROTO_VERSION = "1.18"


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


def _undeclared(b: Binding, loaded: LoadedDomain) -> str:
    """绑定里有、模块声明里没有的角色与参数。全对得上回空串。

    ★为什么拒而不是收下不用（AICloud `C-64 §3` 点破）：多带的东西被静默收下，就一直挂着没人知道 ——
      现役 `vibration_iso` 绑定里挂着一路模块从不读的温度，`vibration_baseline` 绑定里挂着三个
      它从不读的判级参数，都是从旧绑定迁移时带过来的。更坏的一种是**键名写错**（`axialaxis`）：
      收下了，模块按"没填"落码，配置者以为填了。
    """
    roles = {i.role for i in loaded.declaration.inputs}
    params = {p.key for p in loaded.declaration.params}
    extra_roles = sorted(set(b.roles) - roles)
    extra_params = sorted(set(b.params) - params)
    if not extra_roles and not extra_params:
        return ""
    parts = []
    if extra_roles:
        parts.append(f"角色 {extra_roles} 不在模块声明里（已声明 {sorted(roles)}）")
    if extra_params:
        parts.append(f"参数 {extra_params} 不在模块声明里（已声明 {sorted(params)}）")
    return f"绑定 {b.domain}/{b.binding} 未保存：" + "；".join(parts)


class ApiService(WorkbenchApiMixin):
    """把骨架的各件接到 gRPC 上。**本类不含业务逻辑** —— 只做协议翻译。

    工作台那二十来口在 `api_workbench.py`（同一条纪律，只是文件分开：加一口只改一处）。
    """

    def __init__(self, *, guid: str, version: str, logstore: LogStore,
                 domains: dict[str, LoadedDomain], bindings: BindingStore,
                 load_errors: list[tuple[str, str]] | None = None,
                 on_bindings_changed=None, workbench=None, rediagnose=None,
                 trainer=None, points=None, hs_client=None, structs=None,
                 registration=None) -> None:
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
        # 点表。没接则 `Binding.points` 回空 —— 那是**如实**的（我方确实答不出点号），
        # 不是给一个 0 冒充。贵方据此知道"这一格现在没有"，而不是"点号是 0"。
        self._points = points
        # 按字段绑定的核对与字段描述（1.11）要读实时库。★没接就**拒绝字段绑定**并说清，
        #   不跳过核对把它存下来 —— 那等于"不换算、只拒绝"那条规矩在这条路上失效。
        self._hs = hs_client
        self._structs = structs
        # 结论点登记（授权数据点数，`registration` 模块头）：建前拦、建后报。没接（无写路径）= 两样都不做。
        self._registration = registration

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
        # 质量码字典（1.9，AICloud C-45 §3.6）。★骨架级全局表，不随域变 ——
        #   每个域回一遍只会重复八份一样的表，还给了它们各自改口径的机会。
        for q in Quality:
            reply.quality_codes.add(code=q.value, display=q.display(),
                                    is_fault=q.is_fault(), hint=q.hint(),
                                    # ★1.10：两套编号的对照。看结论那一侧拿到的是数值码，
                                    #   没有这一格，「按 is_fault 区分不是设备故障」在那个界面上落不了地。
                                    status_code=q.to_status_code())
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
                                kind=i.kind, display=i.display,
                                group=i.group, group_display=i.group_display,
                                quantity=i.quantity, record=i.record, axis=i.axis)
            info.requires_artifacts.extend(d.declaration.requires_artifacts)
            for o in d.declaration.outputs:
                ospec = info.outputs.add(
                    key=o.key, display=o.display, value_type=o.value_type,
                    unit=o.unit, description=o.description,
                    stop_behavior=o.stop_behavior, role=o.role, headline=o.headline)
                ospec.choices.extend(o.choices)
                ospec.choice_displays.extend(o.choice_displays)
            # 台账参数自述 —— 贵方**按这张表渲染绑定表单**，不按域名写死字段。
            for pm in d.declaration.params:
                spec = info.params.add(
                    key=pm.key, display=pm.display, value_type=pm.value_type,
                    default=pm.default, required=pm.required,
                    unit=pm.unit, description=pm.description, level=pm.level,
                    has_default=pm.has_default, blank_meaning=pm.blank_meaning,
                    min=pm.min, max=pm.max, step=pm.step)
                spec.choices.extend(pm.choices)
                spec.choice_displays.extend(pm.choice_displays)
        return reply

    # ── 绑定 ──────────────────────────────────────────────────────────────
    def _to_pb_binding(self, b: Binding) -> pb.Binding:
        out = pb.Binding(domain=b.domain, binding=b.binding,
                         interval_sec=b.interval_sec, window_sec=b.window_sec,
                         enabled=b.enabled, data_origin=b.data_origin,
                         display_name=b.display_name, status_on_delay_sec=b.status_on_delay_sec)
        for role, gid in b.roles.items():
            out.roles[role] = gid
        for role, fname in b.fields.items():
            out.role_fields[role] = fname
        for k, v in b.params.items():
            out.params[k] = v
        # ★1.10：本条绑定产出哪些结论点 + 它们的 localId（贵方 C-47 §3）。
        #   没有它，看结论那一侧只有点号与质量码，按 stop_behavior 置灰就只能前端写死点名单。
        #   ★点还没建时 local_id=0 —— **0 不是可用点号**，契约里已写明不可拿它去查。
        if self._points is not None:
            loaded = self._domains.get(b.domain)
            outs = loaded.declaration.outputs if loaded is not None else ()
            for o in outs:
                lid = self._points.local_id_of(b.domain, b.binding, o.key) or 0
                out.points.add(key=o.key, local_id=lid,
                               name=default_point_name(b.domain, b.binding, o.key),
                               unit=o.unit, value_type=o.value_type,
                               unregistered=bool(lid) and self._registration is not None
                               and self._registration.is_unregistered(lid))
        loaded = self._domains.get(b.domain)
        if loaded is not None:
            # 只算**测点类**必填角色：图片类输入不绑 globalId，由上传触发，不存在"没绑"。
            required = [i.role for i in loaded.declaration.inputs
                        if i.required and i.kind == "point"]
            out.missing_required.extend(b.missing_required(required))
            # ★1.14：**本条绑定**要哪几类工件（按它的参数算；只启用经典的振动诊断不要基线）。
            out.requires_artifacts.extend(loaded.required_artifacts(b.params))
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
        nb = Binding(
            domain=b.domain, binding=b.binding, roles=dict(b.roles),
            params=dict(b.params),
            data_origin=b.data_origin,
            interval_sec=b.interval_sec or 60.0,
            window_sec=b.window_sec or 60.0,
            enabled=b.enabled, fields=dict(b.role_fields),
            display_name=b.display_name, status_on_delay_sec=b.status_on_delay_sec)
        why = _undeclared(nb, loaded)
        if why:
            return pb.PutBindingReply(ok=False, message=why)
        if nb.status_on_delay_sec and not any(o.role == ROLE_STATUS for o in loaded.declaration.outputs):
            # 同 `_undeclared`：收下不用，配置者以为去抖配上了。
            return pb.PutBindingReply(
                ok=False, message=f"绑定 {nb.domain}/{nb.binding} 未保存：模块没有检测状态，"
                                  f"去抖时长 {nb.status_on_delay_sec} 秒无处可用（请留空）")
        if nb.fields:
            why = self._check_fields(nb, loaded)
            if why:
                return pb.PutBindingReply(ok=False, message=why)
        why = self._admit_points(nb, loaded)
        if why:
            return pb.PutBindingReply(ok=False, message=why)
        try:
            self._bindings.put(nb, allow_no_roles=no_point_inputs)
        except ValueError as exc:
            # 校验失败原样回给调用方（globalId=0、一个角色都没绑…），**不吞**。
            return pb.PutBindingReply(ok=False, message=str(exc))
        return pb.PutBindingReply(ok=True, message=self._notify_changed())

    def _admit_points(self, b: Binding, loaded: LoadedDomain) -> str:
        """建前拦（`registration` 甲）：这次保存会让新结论点进快照、而实时库授权数据点数不够 ⇒ 不存。

        ★只看「这次才进快照」的点：在用的早已在快照里；停用的绑定不建点（启用那次再问）。
          所以满额时改参数、停用都照样能存。
        """
        if self._registration is None or self._points is None or not b.enabled:
            return ""
        fresh, revived = self._points.to_add(
            b.domain, b.binding, [o.key for o in loaded.declaration.outputs])
        why = self._registration.admit(new_count=fresh, local_ids=revived,
                                       what=f"保存绑定 {b.domain}/{b.binding}")
        return f"绑定 {b.domain}/{b.binding} 未保存：{why}" if why else ""

    def _check_fields(self, b: Binding, loaded: LoadedDomain) -> str:
        """按字段绑定的核对（`structbind`）。过了回空串，不过回原因。"""
        if self._hs is None or self._structs is None:
            return "本服务未接实时库读口，无法核对结构值字段 —— 按字段绑定一律拒绝（不跳过核对存下来）"
        try:
            structbind.check_binding(b, loaded.declaration.inputs,
                                     structbind.cached_lookup(self._hs, self._structs))
        except structbind.StructBindError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 —— 连不上实时库等：如实回，不当成核对通过
            logger.warning("按字段绑定核对时读实时库失败：%s", exc)
            return f"读实时库失败，无法核对结构值字段（{type(exc).__name__}: {exc}）—— 绑定未保存，请稍后重试"
        return ""

    def DescribeStructPoint(self, request, context):
        if self._hs is None or self._structs is None:
            return pb.DescribeStructPointRes(ok=False, message="本服务未接实时库读口")
        gid = int(request.point_id)
        if gid <= 0:
            return pb.DescribeStructPointRes(
                ok=False, message=f"point_id={gid} 不是可用的 globalId（0 = hs 尚未分配，绝不能拿 0 去查）")
        try:
            ps = structbind.find_point_struct(self._hs, self._structs, gid)
        except structbind.StructBindError as exc:
            return pb.DescribeStructPointRes(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            return pb.DescribeStructPointRes(
                ok=False, message=f"读实时库失败：{type(exc).__name__}: {exc}")
        d = ps.struct
        res = pb.DescribeStructPointRes(ok=True, struct_name=d.name, struct_id=d.id,
                                        struct_version=d.version, seen_at=_ts(ps.seen_at))
        for f in d.fields:
            res.fields.add(name=f.name, display_name=f.displayName,
                           type=structbind.type_name(f), unit=f.unit,
                           quantity=f.attrs.get(structbind.ATTR_QUANTITY, ""),
                           axis=f.attrs.get(structbind.ATTR_AXIS, ""),
                           bindable=structbind.is_bindable(f), description=f.description)
        return res

    def DeleteBinding(self, request, context):
        ok = self._bindings.delete(request.domain, request.binding)
        # ★只停算，不删结论点。
        if ok:
            # ★但**跨帧状态要删**。点是历史（算过的东西，用户唯一能回看的），
            #   状态是"算到哪儿了" —— 留着的话，日后重建同名绑定会悄悄接上
            #   一条早已作废的轨迹，界面上看成一条从没断过的趋势。
            if self._wb is not None:
                try:
                    if self._wb.clear_domain_state(request.domain, request.binding):
                        logger.info("绑定 %s/%s 删除，连带清掉它的跨帧状态",
                                    request.domain, request.binding)
                except Exception:  # noqa: BLE001 —— 清不掉状态不该让删绑定整个失败
                    logger.exception("清 %s/%s 的跨帧状态失败（绑定已删）",
                                     request.domain, request.binding)
            self._notify_changed()
        return pb.DeleteBindingReply(ok=ok, message="" if ok else "没有这条绑定")

    def RetirePoints(self, request, context):
        """停用已删绑定留下的结论点（1.12，C-59 §5）：移出快照，号不回收，不删历史。"""
        if self._points is None:
            return pb.RetirePointsRes(ok=False, message="本服务未接点表")
        if not request.domain or not request.binding:
            return pb.RetirePointsRes(ok=False, message="domain 与 binding 都必填")
        # ★还有绑定就拒 —— 停了一个还在算的绑定，下一拍 ensure 又会把点启用回来，
        #   来回翻的样子是点在平台点表上时有时无。要停先删绑定（启用与否都算"还有"）。
        if self._bindings.get(request.domain, request.binding) is not None:
            return pb.RetirePointsRes(
                ok=False, message=f"{request.domain}/{request.binding} 还有绑定，请先 DeleteBinding")
        ids = self._points.retire(request.domain, request.binding)
        if not ids:
            return pb.RetirePointsRes(ok=False, message="名下没有在用的结论点（没建过或已停用）")
        # 借绑定变更那条路重推全量快照 —— 停用的点正是靠"本轮快照里缺席"退出点表。
        return pb.RetirePointsRes(ok=True, message=self._notify_changed("点已停用"), local_ids=ids)

    def _notify_changed(self, done: str = "绑定已存") -> str:
        """通知调度器同步。**失败不吞**：回给调用方，否则配置者以为生效了。"""
        if self._on_changed is None:
            return ""
        try:
            self._on_changed()
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("绑定变更后同步调度失败")
            return f"{done}，但调度同步失败（下次重启或再次改绑定会重试）: {exc}"

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
        "RetirePoints": _unary(svc.RetirePoints, pb.RetirePointsReq, pb.RetirePointsRes),
        "DescribeStructPoint": _unary(svc.DescribeStructPoint, pb.DescribeStructPointReq,
                                      pb.DescribeStructPointRes),
        "QueryLogs":     _unary(svc.QueryLogs, pb.LogQueryReq, pb.QueryLogsRes),
        "SubscribeLogs": _stream(svc.SubscribeLogs, pb.LogSubscribeReq, pb.LogStreamItem),
    }
    # 工作台那二十来口（契约 1.2）。★只在真接了 Workbench 时才注册：
    #   注册了却没有库，调用方拿到的是内部错误堆栈；不注册拿到的是 UNIMPLEMENTED —— 后者说的是实话。
    if getattr(svc, "_wb", None) is not None:
        for name, (fn, req_cls, res_cls) in method_specs(pb, svc).items():
            m[name] = _unary(fn, req_cls, res_cls)
    return grpc.method_handlers_generic_handler(SERVICE, m)
