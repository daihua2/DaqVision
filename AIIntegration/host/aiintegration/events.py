"""事件驱动入口 —— **来一张图，算一次**。

骨架原有的只有调度（`scheduler.py`）：按节拍去实时库取测点。图片类输入不是这个形状：
它没有 globalId、不在实时库里、什么时候来由现场决定。本模块是它的入口。

---

## 1. 三条不肯让步的

1. **时刻由调用方给，不给就拒。** 结论的 T 是**拍照时刻**，不是上传时刻、更不是算完的时刻（VQT 铁律）。
   巡检 App 拍完可能半小时后才有网 —— 拿上传时刻当 T，就是把"半小时前的违规"记成"现在的违规"，
   而且看不出来。所以入口上**没有缺省**：缺了、不带时区，一律 400。
2. **必须先有绑定。** 台账参数（阈值等）挂在绑定上，结论点也按绑定建。
   没有绑定就收图，结论写到哪、用什么阈值算，都只能猜 —— 不猜，404。
3. **结论照写回实时库（若写路径就绪），但只读运行时如实说"没写"。**
   调用方拿到的 `written=false` 附原因，不会以为平台上已经有这条记录。

## 2. 并发：同一时刻只算有限几张，满了明说"忙"

图片推理吃 CPU（m 模型开发机上约 250ms/张），而同一台机器上还跑着有节拍的测点推理 ——
那边卡了会掉数据。⇒ 用一个有界信号量限并发，等不到就回 503"繁忙，稍后重试"，
**不无限排队**：无限排队的样子是"请求一直挂着，最后一起超时"，比明说忙更难处置。

## 3. 本模块**不含**图片解码与算法

它只做：校验 → 成帧（`Frame.blobs`）→ 交给域 → 回流。
图片解不开、模型不对，是域的事，由域落质量码（分界文档 §1：模块只懂算法，骨架不懂图片）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .bindings import BindingStore
from .domains import LoadedDomain
from .pointmap import PointMap, default_point_name
from .runner import RunResult, run_domain
from .types import Frame, InputBlob

logger = logging.getLogger(__name__)

#: 单份上传上限。与现网 helmet_service 一致（20 MB）。
MAX_BLOB_BYTES = 20 * 1024 * 1024

#: 抢不到推理槽时最多等多久（秒）。等不到回 503，不无限排队。
BUSY_WAIT_SEC = 30.0


class EventError(Exception):
    """调用方给错了 / 当下处理不了。`status` 是给 HTTP 层用的状态码，`message` 原样回给调用方。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class EventResult:
    run: RunResult
    frame: Frame
    written: bool
    """结论是否已写回实时库。"""

    write_note: str
    """没写或写失败时的原因（人话）。写成功为空。"""


def image_roles(loaded: LoadedDomain) -> list[str]:
    """该域声明的**图片类**输入角色。"""
    return [i.role for i in loaded.declaration.inputs if i.kind == "image"]


class EventRunner:
    def __init__(self, *, domains: dict[str, LoadedDomain], bindings: BindingStore,
                 points: PointMap, artifacts=None, client=None, can_write: bool = False,
                 max_concurrent: int = 1) -> None:
        self._domains = domains
        self._bindings = bindings
        self._points = points
        self._artifacts = artifacts
        self._client = client
        self._can_write = can_write
        self._sem = threading.BoundedSemaphore(max(1, int(max_concurrent)))

    def submit(self, *, domain: str, binding: str, data: bytes, content_type: str,
               captured_at, role: str | None = None, source: str = "") -> EventResult:
        loaded = self._domains.get(domain)
        if loaded is None:
            raise EventError(404, f"域 {domain!r} 未装载；已装载：{sorted(self._domains)}")

        roles = image_roles(loaded)
        if not roles:
            raise EventError(409, f"域 {domain} 没有图片类输入，不接受上传（它按节拍取测点）")
        if role is None:
            if len(roles) != 1:
                raise EventError(400, f"域 {domain} 有多路图片输入 {roles}，须指明 role")
            role = roles[0]
        elif role not in roles:
            raise EventError(400, f"域 {domain} 没有名为 {role!r} 的图片输入；有：{roles}")

        b = self._bindings.get(domain, binding)
        if b is None:
            raise EventError(404, f"没有绑定 {domain}/{binding} —— 先在绑定页建好"
                                  "（台账参数与结论点都挂在绑定上，没有绑定就只能猜）")
        if not b.enabled:
            raise EventError(409, f"绑定 {domain}/{binding} 已停用")

        if not data:
            raise EventError(400, "上传内容为空")
        if len(data) > MAX_BLOB_BYTES:
            raise EventError(413, f"上传 {len(data)} 字节，超过上限 {MAX_BLOB_BYTES} 字节")
        if captured_at is None:
            raise EventError(400, "缺拍照时刻 —— 结论的时刻是拍照时刻，不是上传时刻，不给就不算")
        try:
            blob = InputBlob(t=captured_at, content_type=content_type or "", data=bytes(data),
                             source=source)
        except (TypeError, ValueError) as exc:
            raise EventError(400, str(exc)) from exc

        if not self._sem.acquire(timeout=BUSY_WAIT_SEC):
            raise EventError(503, f"推理繁忙：{BUSY_WAIT_SEC:.0f} 秒内没排上，请稍后重试")
        try:
            arts = self._artifacts.for_binding(domain, binding) if self._artifacts else {}
            frame = Frame(domain=domain, binding=binding, t_start=blob.t, t_end=blob.t,
                          channels={}, blobs={role: blob}, params=dict(b.params),
                          artifacts=arts)
            run = run_domain(loaded, frame)
        finally:
            self._sem.release()

        written, note = self._write(loaded, b, run)
        return EventResult(run=run, frame=frame, written=written, write_note=note)

    def _write(self, loaded: LoadedDomain, b, run: RunResult) -> tuple[bool, str]:
        if not self._can_write or self._client is None:
            return False, "写路径未配置（只读运行）：结论只回给调用方，没有写入实时库"
        if not run.findings:
            return False, "本次没有结论可写"
        items = []
        for f in run.findings:
            lid = self._points.local_id_of(b.domain, b.binding, f.key)
            if lid is None:
                spec = next(o for o in loaded.declaration.outputs if o.key == f.key)
                lid = self._points.ensure(
                    b.domain, b.binding, f.key,
                    name=default_point_name(b.domain, b.binding, f.key),
                    unit=spec.unit, value_type=spec.value_type).local_id
                logger.info("为新结论 %s/%s/%s 补建了点 localId=%d", b.domain, b.binding, f.key, lid)
            items.append((lid, f))
        try:
            self._client.post_vqt(items)
        except Exception as exc:  # noqa: BLE001 —— 写失败不许吞掉推理结果
            logger.warning("事件结论写实时库失败（%s/%s）：%s", b.domain, b.binding, exc)
            return False, f"写实时库失败：{type(exc).__name__}: {exc}"
        return True, ""
