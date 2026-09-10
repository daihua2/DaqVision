"""配置 —— 全部走环境变量，**没有配置文件**。

★为什么不给配置文件：这套东西的配置项少（十来个），而配置文件会立刻带来
"文件在哪、谁改的、和 systemd 里的环境变量哪个赢"三个问题 ——
现场排查时最费时间的恰恰是这类。环境变量只有一个来源：systemd 的 drop-in。

★启动时**把生效值全打出来**（`describe()`）。缺省值悄悄生效是排查噩梦：
  日志里看不见的配置，等于不存在。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path("/home/Project/AIIntegration")


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

    def can_write(self) -> bool:
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
        ]
        return [f"{k}={v}({self._sources.get(k, 'default')})" for k, v in rows]
