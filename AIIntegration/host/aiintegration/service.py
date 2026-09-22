"""服务入口 —— 把骨架八件装配起来跑。

    身份 → 日志 → 装载域 → 点表/绑定 → 连 hs → 建点推快照 → 调度 → 对外口

★**只有本进程持有身份并对外通信**（对 hs 的 gRPC、对 AICloud 的两个口都在这里）。
  域跑在各自的 worker 里、只吃数据出结论 —— 理由见 `hsclient` 模块头（多进程共用一份身份
  会互相删点且循环）。

★**没有写路径也要能起来**：起不来就什么都看不见；起来了至少域清单、日志、绑定管理能用，
  且健康口会明说"结论不回流"。**降级要可见，不是静默**。
"""

from __future__ import annotations

import logging
import signal
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from concurrent import futures

import grpc

from . import api, httpapi
from .artifactcache import ActiveArtifacts
from .bindings import BindingStore
from .config import Config, ConfigError
from .domains import discover
from .artifact_import import ArtifactImporter
from .events import EventRunner
from .fetch import Fetcher
from .hsclient import HsClient, HsConfig, SourceIdentityInfo
from .identity import SystemGuid
from .logstore import LogStore, LogStoreHandler
from .pointmap import PointMap
from .runner import run_domain
from .scheduler import Scheduler
from .trainer import Trainer
from .types import Frame
from .workbench import Workbench

logger = logging.getLogger(__name__)

VERSION = "0.1.0"


def _build_stamp() -> str:
    """包内**最新一个源码文件的时刻**，形如 `20260916T152600Z`。

    ★为什么要这一格（AICloud `C-46`，我方 `AI-53 §3` 许下的）：
      `InfoReply.service_version` 此前是写死的 `"0.1.0"`，从 1.0 至今没动过 ——
      **它回答不了"跑的是哪个 build"**。于是「契约投了、实现没投」这种事
      在接口上**完全看不出来**：对端拨 `GetInfo` 一切正常，而跑的是两天前的码。
      这次能被发现，纯粹是因为我方碰巧升了 `proto_version`；
      而**大多数改动只动实现、不动契约**，那时就没有任何信号。

    ★取源码 mtime 而不是编译期常量，是为了**不依赖出包脚本**：
      现场那台正是被直接 `scp` 覆盖的（没走 `make-release.sh`），
      编译期写入的版本号在那条路径上根本不会更新，而 mtime 会。

    算不出来就回空串（**绝不因为取不到版本号而拦住启动**）。
    """
    try:
        newest = 0.0
        for f in Path(__file__).resolve().parent.rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            newest = max(newest, f.stat().st_mtime)
        if newest <= 0:
            return ""
        return datetime.fromtimestamp(newest, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:  # noqa: BLE001 —— 版本号取不到不是拦住服务的理由
        return ""


def service_version() -> str:
    """对外自述的服务版本：`0.1.0+src20260916T152600Z`。

    ★后半段是**源码时刻**，一眼看得出新旧 —— 对端不必去 `ls` 我方的机器。
    """
    stamp = _build_stamp()
    return f"{VERSION}+src{stamp}" if stamp else VERSION


def source_identity_info(cfg: Config, *, domain_count: int) -> SourceIdentityInfo | None:
    """自报身份的内容（hs op 7）。开关关着回 None —— 那就是"不发"。

    取值照 `AI-61 §4`：`name` 按配置（缺省 `AI 集成服务(<主机名>)`）；`des` 随实际取、不写死；
    `attrs` 用 AICloud `C-1` 的键名。★`commit` 这一键**不给**：我方版本串里只有源码时刻、
    没有提交号，编一个等于报假；对端按"缺键"处理比拿到一个错的强。
    """
    if not cfg.source_identity:
        return None
    return SourceIdentityInfo(
        name=cfg.source_name,
        des=f"AI 算法集成服务 · 契约 {api.PROTO_VERSION} · {domain_count} 个域",
        attrs=(("hostName", socket.gethostname()),
               ("version", service_version()),
               ("app", "AIIntegration")),
    )


def _setup_logging(store: LogStore) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 控制台（systemd 收走进 journal）
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root.addHandler(console)
    # ★同时喂给 LogStore：SubscribeLogs / QueryLogs 两口读的就是它。
    #   做成 handler 而不是让各处自己调 append —— 那样一定会漏，
    #   而漏掉的恰恰是出问题时最想看的那几行。
    root.addHandler(LogStoreHandler(store))


def rediagnose_segment(seg, *, domains, bindings, fetcher, artifacts):
    """对一个归档片段再判别一次（`C-11 §3.4`）。返回 `([(Finding, 显示名)], 备注)`。

    ★**不写回实时库**：回溯判别问的是"当时若用现在的模型会怎样"，
      写回去就把历史改写成了从没发生过的样子。

    ★**也不许动线上那份跨帧状态**（同一条理由的另一半）：拿几个月前的片段推一遍，
      就把"算到哪儿了"覆盖成那时候的，而线上这条诊断还在跑 —— 往后每一拍都接在
      错的地方，且从数值上看不出来。

      这件事**不靠"记得别传"**：本函数**根本没有 states 这个参数**，
      也拿不到工作台库 —— 想传也传不进来。此前它是 `Service.run()` 里的一个闭包，
      闭包里 `workbench` 是现成的，只隔着一句注释；而那条路要真 hs 才走得到，
      单测够不着 ⇒ 变异验证里"把 states 传进去"一条用例都不红。
      提到模块级、砍掉那个入口，这条保证才是结构上的，不是口头的。
    """
    loaded_dom = domains.get(seg.domain)
    if loaded_dom is None:
        raise RuntimeError(f"域 {seg.domain} 未装载，无法回溯判别")
    b = bindings.get(seg.domain, seg.binding)
    if b is None:
        raise RuntimeError(
            f"{seg.domain}/{seg.binding} 没有绑定 —— 不知道该取哪些点，无法回溯判别")
    frame = fetcher.fetch(b, seg.t_to,
                          artifacts=artifacts.for_binding(seg.domain, seg.binding))
    # 片段的窗口就是片段本身的时间范围，不是绑定上配的那个 window_sec。
    frame = Frame(domain=frame.domain, binding=frame.binding,
                  t_start=seg.t_from, t_end=seg.t_to,
                  channels=frame.channels, params=frame.params,
                  artifacts=frame.artifacts)
    res = run_domain(loaded_dom, frame)
    names = {o.key: o.display for o in loaded_dom.declaration.outputs}
    note = ("用当前配置与模型重跑；**未写回实时库**"
            if res.ok else f"模块没跑完（{res.error}），下面是坏值锚点；未写回实时库")
    return [(f, names.get(f.key, f.key)) for f in res.findings], note


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.logstore = LogStore(capacity=cfg.log_capacity)
        self._stop = threading.Event()

    def stop(self) -> None:
        """叫停。嵌进别的进程时用这个，别去碰内部的 Event。"""
        self._stop.set()

    def run(self) -> int:
        cfg = self.cfg
        _setup_logging(self.logstore)

        logger.warning("AIIntegration %s 启动", service_version())
        for line in cfg.describe():
            logger.info("配置生效值: %s", line)

        # ⓪ 监听地址先校验（★在生成身份、建库、连 hs **之前**：fail fast）。
        #   绑到错地址的服务"起得来、健康口正常、日志正常"，而 AICloud 永远拨不到它 ——
        #   现场只看得到"连不上"。与其起一个找不到的服务，不如当场说清哪个变量错了。
        try:
            cfg.validate_listen()
        except ConfigError as e:
            logger.error("监听地址不合规：%s —— 服务不启动（改对再起）", e)
            return 2

        # ① 身份：读得到就用，两处都没有才生成；生成后立刻两处都写、永不重生成。
        guid = SystemGuid(cfg.guid_paths).load_or_create()
        logger.warning("系统 guid = %s", guid)

        # ② 域：丢一个 .py 就多一个域。失败的**不藏**，进 GetInfo.load_errors。
        loaded, failed = discover(cfg.domains_dir)
        domains = {d.key: d for d in loaded}
        load_errors = [(p.name, str(e)) for p, e in failed]
        if not domains:
            logger.warning("一个域都没装上（目录 %s）—— 服务照常起，但不会产出任何结论",
                           cfg.domains_dir)

        # ③ 本地存储
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        points = PointMap(cfg.data_dir / "points.db")
        bindings = BindingStore(cfg.data_dir / "bindings.db")
        # 工作台：标注/训练集/样本/工件/任务/片段。**平台级资产**，与点表同级同库规矩。
        workbench = Workbench(cfg.data_dir / "workbench.db")

        # ④ hs 连接与调度
        can_write = cfg.can_write()
        if not can_write:
            # ★降级可见：每次启动都吵一句，且健康口也说。静默降级 = 现场以为在跑其实没结果。
            #   ★地址本身写错时**点名说是地址错**，否则现场会去查证书、查网络、查对端 ——
            #     那正是「只看得到连不上、看不出配置错」的老坑（AICloud C-20 §4 同类）。
            why = cfg.write_addr_problem()
            if why:
                logger.warning(
                    "写路径地址不合规：%s —— **结论不会回流实时库**。只读运行。", why)
            else:
                logger.warning(
                    "写路径未就绪（AII_HS_WRITE=%r，证书目录 %s）—— "
                    "**结论不会回流实时库**，平台侧看不到 AI 结果。只读运行。",
                    cfg.hs_write_addr, cfg.cert_dir)
        client = HsClient(HsConfig(
            read_addr=cfg.hs_read_addr, write_addr=cfg.hs_write_addr,
            ca_file=cfg.ca_file, cert_file=cfg.cert_file, key_file=cfg.key_file,
            source_identity=source_identity_info(cfg, domain_count=len(domains))))
        # 当前启用工件的提供者：推理时交给模块（模块不碰存储）。
        active_arts = ActiveArtifacts(workbench, cfg.data_dir / "artifacts")
        sched = Scheduler(client=client, fetcher=Fetcher(client), domains=domains,
                          bindings=bindings, points=points, artifacts=active_arts,
                          states=workbench)
        # 训练执行器：串行一条，排队顺序 = 建任务顺序（见 trainer 模块头 §2）。
        # ★没有写路径也照起 —— 训练只读实时库、只写本地工件，与结论回流无关。
        trainer = Trainer(workbench=workbench, bindings=bindings, domains=domains,
                          fetcher=Fetcher(client),
                          artifacts_dir=cfg.data_dir / "artifacts")
        trainer.start()

        if can_write:
            try:
                n = sched.ensure_points()
                logger.warning("结论点已就绪并推送快照：%d 个", n)
            except Exception as exc:  # noqa: BLE001
                # 起不来的原因要说清楚，但**不因此拒绝启动** —— 实时库晚点起来是常态，
                # 调度线程会按退避重试。
                client.log_degraded("启动时推送结论点快照", exc)
            sched.start()
        else:
            logger.warning("跳过调度启动（无写路径）")

        # ⑤ 对外两口
        def rediagnose(seg):
            return rediagnose_segment(seg, domains=domains, bindings=bindings,
                                      fetcher=Fetcher(client), artifacts=active_arts)

        svc = api.ApiService(guid=guid, version=service_version(), logstore=self.logstore,
                             domains=domains, bindings=bindings, load_errors=load_errors,
                             # 绑定一变就重新同步：建点、推快照、起线程。
                             on_bindings_changed=(sched.sync if can_write else None),
                             workbench=workbench, rediagnose=rediagnose,
                             trainer=trainer, points=points)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=8),
                             handlers=(api.build_handler(svc),))
        if server.add_insecure_port(cfg.api_listen) == 0:
            logger.error("gRPC 口绑不上 %s —— 端口被占？服务退出", cfg.api_listen)
            return 1
        server.start()
        logger.warning("控制面 gRPC 监听 %s（只面向 AICloud 后端，浏览器不直连）", cfg.api_listen)

        # 事件驱动入口（图片类输入）：来一张算一次，与按节拍取测点的调度并列。
        events = EventRunner(domains=domains, bindings=bindings, points=points,
                             artifacts=active_arts, client=client, can_write=can_write,
                             states=workbench)
        http = httpapi.make_server(
            cfg.http_listen, guid=guid, version=service_version(), domains=list(domains),
            artifacts_dir=cfg.data_dir / "artifacts",
            reports_dir=cfg.data_dir / "reports", can_write=can_write, events=events,
            scheduler=sched,      # /health 的 snapshot 那一格要问它（AICloud C-27 §3.2）
            # 外部工件导入：只在本进程里写工作台库（"唯一写者"前提），命令行工具只是 HTTP 客户端。
            importer=ArtifactImporter(workbench=workbench, domains=domains,
                                      artifacts_dir=cfg.data_dir / "artifacts"))
        httpapi.serve_in_thread(http)
        logger.warning("大对象 HTTP 监听 %s", cfg.http_listen)

        # ⑥ 等停
        # ★`signal.signal` **只在主线程装得上**：在别的线程调它会抛 ValueError。
        #   不接住的话，两个口都已经起好了，却被这一句掀翻 —— 现象是"服务起来了又没起来"，
        #   日志里最后一行还是"监听 …"，极难看出真因。（本条由整机用例真起进程时逮到。）
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self._stop.set())
        except ValueError:
            # 非主线程（被嵌进别的进程、或整机自检里起在线程里）—— 由宿主负责叫停。
            logger.info("非主线程，未装信号处理；停机由宿主调 stop() 触发")
        self._stop.wait()

        logger.warning("收到停止信号，正在停…")
        sched.stop()
        trainer.stop()
        http.shutdown()
        http.server_close()
        server.stop(3).wait()
        client.close()
        points.close()
        bindings.close()
        workbench.close()
        logger.warning("已停止")
        return 0


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        # 日志还没装上（装日志要先有配置），直接写 stderr —— systemd 照样收进 journal。
        print(f"配置不合规：{e} —— 服务不启动（改对再起）", file=sys.stderr)
        return 2
    return Service(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
