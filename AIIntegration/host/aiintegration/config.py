"""配置 —— 全部走环境变量，**没有配置文件**。

★为什么不给配置文件：这套东西的配置项少（十来个），而配置文件会立刻带来
"文件在哪、谁改的、和 systemd 里的环境变量哪个赢"三个问题 ——
现场排查时最费时间的恰恰是这类。环境变量只有一个来源：systemd 的 drop-in。

★启动时**把生效值全打出来**（`describe()`）。缺省值悄悄生效是排查噩梦：
  日志里看不见的配置，等于不存在。
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path("/home/Project/AIIntegration")


class ConfigError(ValueError):
    """配置本身不成立 —— 起来也没意义，不如当场说清楚。"""


def check_addr(addr: str, *, what: str) -> tuple[str, int]:
    """拆 `host:port` 并校验端口；不合规抛 `ConfigError`。返回 `(host, port)`。

    ★**端口 0 一律拒**。两端的理由不同，结论一样：

    · **监听端** `:0` 是「内核挑一个空闲端口」的语义。我方这两个口是**要被 AICloud
      拨号找到的**（地址登记在对方平台上），绑到随机端口 = 服务在、健康口正常、日志也正常，
      **而对方永远连不上**；现场只看得到"连不上"，排查方向会被带去"服务没起 / 网络不通"。
      ★实测（2026-09-12）：`grpc.add_insecure_port("127.0.0.1:0")` 回的是**真实端口号**
      （如 37511，非 0），所以"返回 0 才算绑定失败"那条判据**挡不住它**；
      `ThreadingHTTPServer(("127.0.0.1", 0))` 同样静默随机化。
    · **拨号端** `:0` 永远连不上。

    （同一类的另一半由 AICloud `C-20 §4` 报出：他们把 `127.0.0.1:0` 存进了库。）

    ★**只认数字端口**，不查 `/etc/services` 的服务名：本进程的地址来自运维写的环境变量，
      不是人在界面上敲的，没有"写 `https` 更顺手"的场景；认服务名只会让错字多一条活路。
    """
    raw = (addr or "").strip()
    if not raw:
        raise ConfigError(f"{what} 是空的；要填 host:port（如 127.0.0.1:50070）")
    if raw.startswith("["):                       # [::1]:50070 这种写法
        host, sep, port_s = raw.rpartition("]:")
        host, port_s = host + "]", port_s if sep else ""
    else:
        host, sep, port_s = raw.rpartition(":")
        if not sep:
            raise ConfigError(f"{what}={raw!r} 少了端口；要填 host:port（如 127.0.0.1:50070）")
    if not host:
        raise ConfigError(f"{what}={raw!r} 少了主机；要填 host:port，监听全部网卡写 0.0.0.0")
    if not port_s.isdigit():                      # 负号、空白、服务名一并落这里
        raise ConfigError(
            f"{what}={raw!r} 的端口 {port_s!r} 不是数字；只认 1-65535 的数字端口")
    port = int(port_s)
    if port == 0:
        raise ConfigError(
            f"{what}={raw!r} 的端口是 0。0 是「由内核挑一个空闲端口」的写法："
            f"绑得上、日志和健康口都正常，**而对方永远拨不到这个服务**。要填确定的端口号")
    if not 1 <= port <= 65535:
        raise ConfigError(f"{what}={raw!r} 的端口 {port} 超范围；要在 1-65535 之间")
    return host, port


def addr_problem(addr: str, *, what: str) -> str | None:
    """同上，但**不抛**：合规回 `None`，不合规回一句人能读的原因。

    给「坏了也要继续跑」的那一侧用（写路径坏掉应降级只读，不该掀翻整个服务）。
    """
    try:
        check_addr(addr, what=what)
    except ConfigError as e:
        return str(e)
    return None


def parse_switch(v: str, *, what: str) -> bool:
    """开关只认 `on` / `off`（不分大小写）。

    ★别的写法一律拒绝启动，不猜：`yes`、`1`、`true`、`enable` 各有人写，
      猜错一个方向就是"以为开了其实没开"或反过来 —— 而这个开关管的是对外自报身份，
      开早了违反与 AICloud 约定的上线顺序（`C-50 §4`）。
    """
    s = v.strip().lower()
    if s == "on":
        return True
    if s == "off":
        return False
    raise ConfigError(f"{what}={v!r} 不合规：只认 on / off")


def _env(name: str, default: str) -> tuple[str, bool]:
    """返回 (值, 是否来自环境)。第二项用来在启动日志里标 (env)/(default)。"""
    v = os.environ.get(name)
    return (v, True) if v not in (None, "") else (default, False)


@dataclass(frozen=True)
class Config:
    root: Path
    domains_dir: Path
    data_dir: Path
    cert_dir: Path

    guid_paths: list[Path]

    hs_read_addr: str
    hs_write_addr: str

    api_listen: str
    http_listen: str

    log_capacity: int

    # 自报身份（hs op 7，`SOURCE_IDENTITY`）。★缺省**关**：何时打开由往来函定
    # （`C-50 §4` AICloud 函告 + `AI-61 §3` 演练通过），不由代码自己判断。见 hsclient 模块头。
    source_identity: bool = False
    source_name: str = ""
    _sources: dict = field(default_factory=dict, compare=False)

    @staticmethod
    def from_env() -> "Config":
        src: dict[str, str] = {}

        def pick(name: str, default: str) -> str:
            v, from_env = _env(name, default)
            src[name] = "env" if from_env else "default"
            return v

        root = Path(pick("AII_ROOT", str(DEFAULT_ROOT)))
        return Config(
            root=root,
            domains_dir=Path(pick("AII_DOMAINS_DIR", str(root / "domains"))),
            data_dir=Path(pick("AII_DATA_DIR", str(root / "data"))),
            cert_dir=Path(pick("AII_CERT_DIR", str(root / "cert"))),
            # ★冗余落盘：一处在应用目录内，一处在**应用目录之外**（重铺目录冲不掉身份）。
            guid_paths=[
                Path(pick("AII_GUID_PRIMARY", str(root / "system.guid"))),
                Path(pick("AII_GUID_BACKUP", "/etc/aiintegration/system.guid")),
            ],
            # 读：明文回环全量口。**不带证书** —— 带了会被降级成受限连接，读不到别人的点。
            hs_read_addr=pick("AII_HS_READ", "127.0.0.1:5400"),
            # 写：mTLS 跨机口。空 = 未配置写路径 ⇒ 只读运行，结论不回流（启动时会吵）。
            hs_write_addr=pick("AII_HS_WRITE", ""),
            # 对外口：**只面向 AICloud 后端**，故默认只绑回环。浏览器不直连我方。
            api_listen=pick("AII_API_LISTEN", "127.0.0.1:50070"),
            http_listen=pick("AII_HTTP_LISTEN", "127.0.0.1:50071"),
            log_capacity=int(pick("AII_LOG_CAPACITY", "20000")),
            source_identity=parse_switch(pick("AII_SOURCE_IDENTITY", "off"),
                                         what="AII_SOURCE_IDENTITY"),
            # `AI-61 §4` 定的写法：带主机名，照 hs 身份表现有的「AI边缘计算网关(AISERVER)」。
            source_name=pick("AII_SOURCE_NAME", f"AI 集成服务({socket.gethostname()})"),
            _sources=src,
        )

    # 证书三件（写路径用）
    @property
    def ca_file(self) -> Path:
        return self.cert_dir / "ca.cer"

    @property
    def cert_file(self) -> Path:
        return self.cert_dir / "client.cer"

    @property
    def key_file(self) -> Path:
        return self.cert_dir / "client.key"

    def write_addr_problem(self) -> str | None:
        """写路径地址不合规时的原因；没配（空）回 `None` —— 那是合法的只读运行。"""
        if not self.hs_write_addr:
            return None
        return addr_problem(self.hs_write_addr, what="AII_HS_WRITE")

    def validate_listen(self) -> None:
        """校验两个**监听**地址；不合规抛 `ConfigError`。

        ★监听地址错 = 服务起得来但没人找得到它（见 `check_addr` 模块注释）。
          故这一条是**拒绝启动**，不是降级：降级的前提是"还能干点什么"，这里什么也干不了。
        """
        check_addr(self.api_listen, what="AII_API_LISTEN")
        check_addr(self.http_listen, what="AII_HTTP_LISTEN")

    def can_write(self) -> bool:
        # ★地址不合规一律不当"能写"：拿着 :0 这种地址去连，现象是连不上而不是配置错，
        #   与 write_addr_problem() 的报错话术配套（service 启动时会把原因打出来）。
        if self.write_addr_problem() is not None:
            return False
        return bool(self.hs_write_addr) and all(
            p.is_file() for p in (self.ca_file, self.cert_file, self.key_file))

    def describe(self) -> list[str]:
        """生效值全集，启动时逐行打进日志。**缺省值也打** —— 看不见的配置等于不存在。"""
        rows = [
            ("AII_ROOT", self.root), ("AII_DOMAINS_DIR", self.domains_dir),
            ("AII_DATA_DIR", self.data_dir), ("AII_CERT_DIR", self.cert_dir),
            ("AII_GUID_PRIMARY", self.guid_paths[0]), ("AII_GUID_BACKUP", self.guid_paths[1]),
            ("AII_HS_READ", self.hs_read_addr), ("AII_HS_WRITE", self.hs_write_addr or "(未配置)"),
            ("AII_API_LISTEN", self.api_listen), ("AII_HTTP_LISTEN", self.http_listen),
            ("AII_LOG_CAPACITY", self.log_capacity),
            ("AII_SOURCE_IDENTITY", "on" if self.source_identity else "off"),
            ("AII_SOURCE_NAME", self.source_name),
        ]
        return [f"{k}={v}({self._sources.get(k, 'default')})" for k, v in rows]
