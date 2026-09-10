"""域插件宿主 —— 装载、能力自描述、发现。

设计目标（`doc/骨架与算法模块分界.md` §0）：

    新增一个 AI 能力域 = 往 `domains/` 丢一个 `.py`。**没有第二步。**
    不改骨架、不改路由、不改前端、不改配置。

★能力位是**契约**（AICloud C-7 §2）：AICloud 前端按位渲染而不是按域名分支，
  于是 **加位随时，改义发函** —— 加位老前端忽略即可；同名改义会让前端静默走错分支。
  故已知位集合写在 `CAPABILITIES` 里并附文字定义，改这里的含义要发函。
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import sys
from pathlib import Path

from .types import Declaration

logger = logging.getLogger(__name__)

# 域标识用作 URL 路径段与点名前缀：限死字符集，免得后面到处转义。
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: 已知能力位。**名字是契约**：加位随时，改义发函（AICloud C-7 §2）。
CAPABILITIES: dict[str, str] = {
    "infer": "能对一帧数据出结论。每个域必备。",
    "train": "能按标注训练出模型工件。前端据此决定出不出训练页。",
    "annotate": "该域的数据可被标注。前端据此决定出不出标注页。",
    "artifact": "有模型工件可供查看/启用/停用。",
}


class DomainError(RuntimeError):
    """模块本身有问题（声明不合法、缺必需方法…）。装载期就该发现。"""


class Domain:
    """域模块的基类。模块作者继承它，实现 `declare` 与 `infer` 即可。

    ★这个类**不知道实时库存在、不知道 VQT 怎么写、不碰任何 id**。
      它只描述"要什么输入、出什么结论"，然后"给数据、出结论"。
    """

    key: str = ""
    display: str = ""
    version: str = "0.0.0"

    # ── 必须实现 ──────────────────────────────────────────────────────────
    def declare(self) -> Declaration:
        raise NotImplementedError

    def infer(self, frame):  # -> list[Finding]
        raise NotImplementedError

    # ── 可选 ──────────────────────────────────────────────────────────────
    # 不实现 train，即表示本域不支持训练（能力位 `train` 自动不出现）。
    # def train(self, dataset, report): ...

    def capabilities(self) -> set[str]:
        """缺省按"实现了哪些方法"推断。模块可覆盖它显式声明（如 `annotate`）。"""
        caps = {"infer"}
        if _overrides(type(self), "train"):
            caps.add("train")
        return caps


def _overrides(cls: type, name: str) -> bool:
    """该类是否真的实现了这个方法（而不是继承自 `Domain` 的占位）。"""
    own = getattr(cls, name, None)
    base = getattr(Domain, name, None)
    return own is not None and own is not base


class LoadedDomain:
    """装载好的一个域：实例 + 它的声明 + 能力位。骨架只跟这个打交道。"""

    __slots__ = ("instance", "declaration", "caps", "source")

    def __init__(self, instance: Domain, declaration: Declaration,
                 caps: frozenset[str], source: Path) -> None:
        self.instance = instance
        self.declaration = declaration
        self.caps = caps
        self.source = source

    @property
    def key(self) -> str:
        return self.instance.key

    def describe(self) -> dict:
        """给 `GET /api/ai/domains` 用。**前端按 `capabilities` 渲染，不按 `key` 分支。**"""
        return {
            "key": self.instance.key,
            "display": self.instance.display or self.instance.key,
            "version": self.instance.version,
            "capabilities": sorted(self.caps),
        }


def _validate(instance: Domain, source: Path) -> LoadedDomain:
    key = getattr(instance, "key", "")
    if not _KEY_RE.match(key or ""):
        raise DomainError(
            f"{source}: 域 key 不合法 {key!r} —— 需匹配 {_KEY_RE.pattern}"
            "（它要用作 URL 路径段与点名前缀）"
        )

    decl = instance.declare()
    if not isinstance(decl, Declaration):
        raise DomainError(f"{source}: declare() 必须返回 Declaration，收到 {type(decl).__name__}")

    if not _overrides(type(instance), "infer"):
        raise DomainError(f"{source}: 域 {key} 没有实现 infer()")

    caps = set(instance.capabilities())
    unknown = caps - set(CAPABILITIES)
    if unknown:
        # 不静默忽略：未知位到了前端就是"渲染不出来的能力"，而前端不会报错。
        raise DomainError(
            f"{source}: 域 {key} 声明了未知能力位 {sorted(unknown)}；"
            f"已知位 {sorted(CAPABILITIES)} —— 新增能力位要先发函议定（加位随时，改义发函）"
        )
    if "infer" not in caps:
        raise DomainError(f"{source}: 域 {key} 未声明 infer 能力位")

    return LoadedDomain(instance, decl, frozenset(caps), source)


def _instantiate(module, source: Path) -> Domain | None:
    """从模块里找出域实例：优先 `DOMAIN` 变量，其次唯一的 `Domain` 子类。"""
    obj = getattr(module, "DOMAIN", None)
    if isinstance(obj, Domain):
        return obj
    if inspect.isclass(obj) and issubclass(obj, Domain):
        return obj()

    subs = [
        v for v in vars(module).values()
        if inspect.isclass(v) and issubclass(v, Domain) and v is not Domain
        and v.__module__ == module.__name__
    ]
    if len(subs) == 1:
        return subs[0]()
    if len(subs) > 1:
        raise DomainError(
            f"{source}: 找到 {len(subs)} 个 Domain 子类 —— 一个文件放一个域，"
            "或用 DOMAIN = <实例> 指明哪一个"
        )
    return None


def discover(directory: Path) -> tuple[list[LoadedDomain], list[tuple[Path, Exception]]]:
    """扫描目录装载所有域。

    返回 `(装好的, 失败的)`。**一个模块坏了不拖垮其余的** —— 但失败要吵：
    静默跳过会变成"某个域莫名其妙不见了"，而那是最难查的一类。
    """
    directory = Path(directory)
    loaded: list[LoadedDomain] = []
    failed: list[tuple[Path, Exception]] = []
    if not directory.is_dir():
        logger.warning("域目录不存在，本次没有装载任何域: %s", directory)
        return loaded, failed

    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            mod_name = f"aiintegration_domain_{path.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise DomainError(f"{path}: 无法作为 Python 模块加载")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            instance = _instantiate(module, path)
            if instance is None:
                raise DomainError(f"{path}: 没找到 Domain 子类，也没有 DOMAIN 变量")

            item = _validate(instance, path)
            if item.key in seen:
                raise DomainError(f"{path}: 域 key {item.key!r} 与 {seen[item.key]} 重复")
            seen[item.key] = path
            loaded.append(item)
            logger.info("装载域 %s (%s) 能力=%s 来自 %s",
                        item.key, item.instance.version, sorted(item.caps), path.name)
        except Exception as exc:  # noqa: BLE001 —— 装载期任何异常都只影响这一个域
            failed.append((path, exc))
            logger.error("域装载失败，已跳过: %s: %s", path.name, exc)

    return loaded, failed
