"""训练执行 —— 把工作台里的**任务**真的跑起来。

`workbench.py` 那半（任务表、五态机、工件表）先落的；本模块是缺的那半：
谁去取待跑的任务、谁去组装数据集、谁去调模块的 `train()`、工件落到哪。

---

## 1. ★为什么是**任务**而不是一个阻塞调用

AICloud `C-11 §3.3` 点名的：v5 是同步阻塞 + 一句"通常需要几十秒，请不要关闭页面"。
那句话本身就是设计缺陷的自白 —— 它把"这活儿要跑很久"变成了用户的责任。

⇒ 这里：`StartTraining` 建一条 `pending` 记录**立刻返回**；一个后台线程按序取来跑。
  界面随便关，回来 `GetTrainJob` 一查就知道跑到哪了、成没成、为什么没成。

## 2. ★串行一条，不并发

分界文档 §2 第 6 条：**单进程多域共享算力，只能由宿主统一编排**。
两个训练同时跑，在边缘盒上的表现是**两个都变慢，而且推理跟着卡** ——
而推理是有节拍的，卡了就直接掉数据。⇒ 一次只跑一个，其余排队。

排队顺序 = 建任务的顺序（`id` 升序）。**不做优先级** —— 优先级要有人维护，
而它一旦存在，"我的任务为什么还没跑"就变成一个查不清的问题。

## 3. ★组装数据集时"取不到数"是常态，但**不许静默**

样本记的是一段时间范围；等到训练时，hs 里那段可能已经被滚存删掉了。

  · 取不到的样本**不进** `Dataset`（拿空帧去训等于喂噪声）；
  · 但**丢了几条、为什么丢，写进任务的 `message`**，且训完在工件的 `meta` 里也留一份。

  "用 200 条训出来的"与"以为用 200 条、实际只用了 3 条"是**两个模型**，
  而后者在界面上看起来和前者一模一样 —— 这正是要防的那类静默。

## 4. 工件落盘：**骨架存，模块不碰文件系统**

模块交回一坨 `bytes`，本模块按 `<域>/<任务id>-<算法>.<后缀>` 落进工件根，
算 sha256、记大小，写进 `artifacts` 表。下载走 HTTP 那个口。

★**新训出来的工件不自动激活**。激活是人的决定 —— 自动激活等于"训一次就换一次现场模型"，
  而训练常常是拿新标注试试看。界面上给"启用"按钮（`ActivateArtifact`）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

from .bindings import Binding, BindingStore
from .domains import LoadedDomain
from .fetch import Fetcher
from .types import Dataset, LabeledFrame, ProgressSink, TrainedArtifact
from .workbench import (
    JOB_CANCELED, JOB_FAILED, JOB_PENDING, JOB_READY, JOB_RUNNING,
    Workbench, WorkbenchError,
)

logger = logging.getLogger(__name__)

#: 没有待跑任务时的轮询间隔。★不做通知唤醒：一秒的延迟对一个要跑几十秒的活儿无所谓，
#: 而"建任务"与"跑任务"解耦（不必同进程、不必同一把锁）换来的简单是实打实的。
POLL_SEC = 1.0

#: 单个训练集最多取多少条样本成帧。★不是性能上限，是**防跑飞**：
#: 一个手滑复制出十万条的训练集，会把取数打成 DDoS。超了当场拒，说清楚。
MAX_DATASET_ITEMS = 20000


class TrainerError(RuntimeError):
    pass


class Trainer:
    """训练任务的执行器。**一次只跑一个**（见模块头 §2）。"""

    def __init__(self, *, workbench: Workbench, bindings: BindingStore,
                 domains: dict[str, LoadedDomain], fetcher: Fetcher,
                 artifacts_dir: Path) -> None:
        self._wb = workbench
        self._bindings = bindings
        self._domains = domains
        self._fetcher = fetcher
        self._artifacts_dir = Path(artifacts_dir)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 正在跑的那一个：(job_id, ProgressSink)。取消靠给 sink 置位。
        self._current: tuple[int, ProgressSink] | None = None
        self._lock = threading.Lock()

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        # ★先收拾上一个进程留下的烂摊子，再开始取新任务。顺序不能反：
        #   先起线程的话，恢复与取任务会抢同一批行。
        self._recover_stale()
        self._thread = threading.Thread(target=self._loop, name="trainer", daemon=True)
        self._thread.start()
        logger.info("训练执行器已启动（串行一条，排队顺序 = 建任务顺序）")

    def _recover_stale(self) -> int:
        """把上一个进程**跑到一半就死掉**的任务标成失败。返回处置条数。

        ★为什么必须有：执行器只取 `pending`。进程在训练中途死掉（崩溃 / 断电 /
          `systemctl restart`）之后，那条任务会**永远停在 `running`**——没人跑它、也没人把它标失败，
          界面上一直显示"训练中"。这正是"看起来在跑、其实没有"那一类。

        ★为什么标**失败**而不是改回 `pending` 重跑：半截训练可能已经吃掉了大量取数配额，
          也可能正是这个训练把进程搞崩的（内存打爆之类）——自动重跑就是自动再崩一次。
          重不重跑是人的决定；我方只把"它没跑完、为什么没跑完"如实写上。

        ★前提：**本进程是这个库唯一的执行器**（骨架单进程，见 `service.py` 模块头）。
          若将来多个进程共用一个工作台库，这里会把别人正在跑的任务误杀 —— 那时要换成租约。
        """
        n = 0
        while True:
            page = self._wb.list_jobs(status=JOB_RUNNING, limit=1000)
            if not page.items:
                break
            for job in page.items:
                try:
                    self._wb.update_job(
                        job.id, status=JOB_FAILED,
                        message=(f"服务重启时该任务仍在训练中，未完成（重启前最后状态：{job.message or '无'}）"
                                 "——未自动重跑，请确认原因后重新开训"))
                    n += 1
                except WorkbenchError:
                    pass                    # 并发下刚好到了终态：不覆盖
            if len(page.items) < 1000:
                break
        if n:
            # 每次启动都说，不限流：这是"有训练被中断过"的唯一痕迹。
            logger.warning("启动时发现 %d 条上次未跑完的训练任务，已标失败（未自动重跑）", n)
        return n

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._lock:
            if self._current is not None:
                # 停机时也给正在跑的那个置位：肯看的模块能提前收手。
                self._current[1].canceled = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── 对外：建任务 / 取消 ───────────────────────────────────────────────
    def submit(self, *, domain: str, dataset_id: int, binding: str = "",
               algo: str = "") -> int:
        """建一条待跑任务，**立刻返回** id。校验不过就抛 `WorkbenchError`（原样回给对端）。"""
        loaded = self._domains.get(domain)
        if loaded is None:
            raise WorkbenchError(f"域 {domain!r} 未装载；已装载：{sorted(self._domains)}")
        if "train" not in loaded.caps:
            # ★当场拒，而不是建一条注定失败的任务：能力位就是为了让界面**根本不出**这个按钮，
            #   真被调到了说明前端没按能力位渲染，要让它知道。
            raise WorkbenchError(
                f"域 {domain} 不支持训练（能力位 {sorted(loaded.caps)} 里没有 train）"
                "—— 界面应按能力位渲染，不该出现训练入口")
        return self._wb.create_job(domain=domain, dataset_id=dataset_id,
                                   binding=binding, algo=algo)

    def cancel(self, job_id: int) -> str:
        """取消一个任务。回一句人话说明**实际发生了什么** —— 三种情形结果不同。"""
        job = self._wb.get_job(job_id)
        if job is None:
            raise WorkbenchError(f"没有 id={job_id} 这个训练任务")
        if job.status == JOB_PENDING:
            self._wb.update_job(job_id, status=JOB_CANCELED, message="排队中被取消")
            return "已取消（还没开始跑）"
        with self._lock:
            running = self._current is not None and self._current[0] == job_id
            if running:
                self._current[1].canceled = True
        if running:
            # ★**不谎报"已取消"**：模块不看 `canceled` 就只能等它自己跑完。
            return "已请求取消；该域的算法若不检查取消标志，会跑完当前这次训练才停"
        return f"任务当前状态是 {job.status}，取消不适用"

    # ── 主循环 ────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._next_pending()
            if job is None:
                self._stop.wait(POLL_SEC)
                continue
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001 —— 一个任务炸了不许掀翻执行器
                logger.exception("训练任务 %s 执行器内部出错", job.id)
                self._fail(job.id, f"执行器内部错误: {exc!r}")

    def _next_pending(self):
        page = self._wb.list_jobs(status=JOB_PENDING, limit=1)
        if not page.items:
            return None
        # list_jobs 按 id 降序（界面要最新的在前），这里要**最早的**那条。
        page = self._wb.list_jobs(status=JOB_PENDING, limit=1000)
        return min(page.items, key=lambda j: j.id)

    def _fail(self, job_id: int, message: str) -> None:
        try:
            self._wb.update_job(job_id, status=JOB_FAILED, message=message)
        except WorkbenchError:
            # 已是终态（比如刚被取消）——不覆盖。终态不可覆盖是库层的规矩。
            logger.info("任务 %s 已是终态，失败信息未覆盖：%s", job_id, message)

    def _run_job(self, job) -> None:
        loaded = self._domains.get(job.domain)
        if loaded is None:
            return self._fail(job.id, f"域 {job.domain} 未装载（建任务后被移除？）")

        sink = ProgressSink(lambda p, m: self._on_progress(job.id, p, m))
        with self._lock:
            self._current = (job.id, sink)
        try:
            self._wb.update_job(job.id, status=JOB_RUNNING, message="正在组装数据集")
        except WorkbenchError:
            return                      # 刚被取消，别再往前走
        started = time.monotonic()

        try:
            dataset, skipped = self._assemble(job, sink)
            if sink.canceled:
                return self._cancel_now(job.id, "组装数据集阶段被取消")
            if not dataset.items:
                return self._fail(
                    job.id,
                    "组装出来一条样本都没有：" + _explain_skips(skipped, total=job.sample_count))

            note = f"用 {len(dataset)} 条样本训练（标签分布 {dataset.label_counts()}）"
            if skipped:
                # ★丢了多少必须留在任务上。见模块头 §3。
                note += "；" + _explain_skips(skipped, total=job.sample_count)
            self._wb.update_job(job.id, progress=0.1, message=note)

            artifact = loaded.instance.train(dataset, sink)
            if sink.canceled:
                return self._cancel_now(job.id, "训练过程中被取消（模块已收手）")
            if not isinstance(artifact, TrainedArtifact):
                return self._fail(
                    job.id,
                    f"域 {job.domain} 的 train() 返回了 {type(artifact).__name__}，"
                    "应为 TrainedArtifact")

            art_id = self._store(job, dataset, artifact, skipped)
            secs = time.monotonic() - started
            self._wb.update_job(
                job.id, status=JOB_READY, progress=1.0, artifact_id=art_id,
                message=f"{note}；用时 {secs:.1f}s；工件已存但**未自动启用**，请在界面上启用")
            logger.warning("训练完成：任务 %s 域 %s 工件 %s（%.1fs，%d 条样本）",
                           job.id, job.domain, art_id, secs, len(dataset))
        except Exception as exc:  # noqa: BLE001 —— 模块的异常不许掀翻执行器
            logger.exception("训练任务 %s 失败", job.id)
            self._fail(job.id, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._current = None

    def _cancel_now(self, job_id: int, why: str) -> None:
        try:
            self._wb.update_job(job_id, status=JOB_CANCELED, message=why)
        except WorkbenchError:
            pass

    def _on_progress(self, job_id: int, p: float, m: str) -> None:
        try:
            # 进度是**训练本身**那一段（0.1~0.95），前后留给组装与落盘。
            self._wb.update_job(job_id, progress=0.1 + 0.85 * p, message=m or None)
        except WorkbenchError:
            pass                        # 终态之后模块还在报进度：忽略，别炸它

    # ── 组装数据集 ────────────────────────────────────────────────────────
    def _assemble(self, job, sink: ProgressSink):
        page = self._wb.list_samples(job.dataset_id, limit=1000)
        if page.total > MAX_DATASET_ITEMS:
            raise TrainerError(
                f"训练集有 {page.total} 条样本，超过上限 {MAX_DATASET_ITEMS} —— "
                "取数会把实时库打垮。请拆分训练集")
        samples = list(page.items)
        while len(samples) < page.total:
            more = self._wb.list_samples(job.dataset_id, offset=len(samples), limit=1000)
            if not more.items:
                break
            samples += more.items

        ds_row = next((d for d in self._wb.list_datasets(job.domain)
                       if d.id == job.dataset_id), None)
        ds_name = ds_row.name if ds_row else str(job.dataset_id)

        items: list[LabeledFrame] = []
        skipped: list[tuple[int, str]] = []
        bindings_cache: dict[str, Binding | None] = {}

        for i, s in enumerate(samples):
            if sink.canceled:
                break
            if s.binding not in bindings_cache:
                bindings_cache[s.binding] = self._bindings.get(job.domain, s.binding)
            b = bindings_cache[s.binding]
            if b is None:
                skipped.append((s.id, f"{s.binding} 没有绑定，不知道取哪些点"))
                continue
            # 样本自己的时间范围就是窗口，**不用绑定上配的 window_sec**：
            # 那个是在线节拍用的，与人当初框的那一段无关。
            window = max(1.0, (s.t_to - s.t_from).total_seconds())
            probe = Binding(domain=b.domain, binding=b.binding, roles=b.roles,
                            params=b.params, interval_sec=b.interval_sec,
                            window_sec=window, enabled=b.enabled)
            try:
                frame = self._fetcher.fetch(probe, s.t_to)
            except Exception as exc:  # noqa: BLE001
                skipped.append((s.id, f"取数失败: {type(exc).__name__}"))
                continue
            if not any(frame.channels.get(r) for r in b.roles):
                # 一路样本都没有 —— hs 里那段多半已被滚存删掉。
                skipped.append((s.id, "该时段在实时库里已无数据"))
                continue
            items.append(LabeledFrame(frame=frame, label=s.label, sample_id=s.id))
            if i % 20 == 0:
                self._wb.update_job(job.id, progress=0.1 * (i + 1) / max(1, len(samples)))

        return Dataset(domain=job.domain, binding=job.binding, name=ds_name,
                       items=tuple(items), skipped=tuple(skipped)), skipped

    # ── 落盘 ──────────────────────────────────────────────────────────────
    def _store(self, job, dataset: Dataset, art: TrainedArtifact,
               skipped: list[tuple[int, str]]) -> int:
        rel = f"{job.domain}/{art.kind}/{job.id}-{_safe(art.algo)}{art.suffix}"
        target = self._artifacts_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = bytes(art.blob)
        # 先写临时再改名：半个工件躺在那里，下载下来是坏的，而记录里看着一切正常。
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(blob)
        tmp.replace(target)

        meta = dict(art.meta)
        meta.setdefault("label_counts", json.dumps(dataset.label_counts(), ensure_ascii=False))
        if skipped:
            # ★工件上也留一份：任务记录可能被清理，而"这个模型是用什么训的"要长久答得上。
            meta.setdefault("skipped_count", str(len(skipped)))
            meta.setdefault("skipped_sample_ids",
                            ",".join(str(sid) for sid, _ in skipped[:50]))
        return self._wb.add_artifact(
            # ★`kind` 由域说了算（模型 / 基线 / …）。骨架写死会让基线顶掉模型的激活位。
            domain=job.domain, name=f"{dataset.name}-{job.id}", kind=art.kind,
            binding=job.binding, algo=art.algo, dataset_id=job.dataset_id,
            sample_count=len(dataset), feature_count=art.feature_count,
            accuracy=art.accuracy, path=rel, size=len(blob),
            sha256=hashlib.sha256(blob).hexdigest(),
            meta_json=json.dumps(meta, ensure_ascii=False))


def _safe(name: str) -> str:
    """算法名进文件名：只留安全字符。★中文算法名（"决策树"）要能用，别只允许 ASCII。"""
    out = "".join(c for c in name if c.isalnum() or c in "-_")
    return out or "model"


def _explain_skips(skipped: list[tuple[int, str]], *, total: int) -> str:
    """把"丢了哪些"说成人话。★按原因归并，但**总数如实**。"""
    if not skipped:
        return ""
    by_reason: dict[str, int] = {}
    for _sid, why in skipped:
        by_reason[why] = by_reason.get(why, 0) + 1
    parts = "；".join(f"{why}（{n} 条）" for why, n in
                      sorted(by_reason.items(), key=lambda kv: -kv[1]))
    return f"★{len(skipped)}/{total} 条样本没能取到数据，未参与训练：{parts}"
