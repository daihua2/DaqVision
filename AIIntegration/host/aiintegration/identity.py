"""系统身份（guid）—— 首启生成、冗余落盘、永不重生成。

规矩来自 AICloud C-5 §2（与网关既有流程逐条同形）：

    每个系统自己生成、自己保存、永不变；AICloud 只用 CA 给那个 guid 签一张证书。

★为什么"永不变"要紧（C-5 §2 末，原文照记）：
    那条连接上写进去的点**永远记在这个 guid 名下**。换 guid 不是改个号码，是把先前写的点
    变成"没有主人的数据"—— 查得到、归属指向一个再没人用的身份，而按对账那条铁律
    **绝不自动删**，残留清不掉。

★为什么"冗余落盘"要紧：只存一份，重铺应用目录就没了 ⇒ 换 guid ⇒ 上面那条后果。
  故除应用目录内一份外，另在应用目录**之外**存一份。

★本模块**不生成不落盘之外的任何副作用**：它不联网、不签证书、不注册。
  2026-09-10 有过一次教训 —— 服务还不存在时用 shell 手工造了一个 guid 报给 AICloud 请求签发，
  那不是"首启生成"，是绕过它伪造产物（AI-6）。**guid 只能由本模块在服务进程里生成。**
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# UUID v4 的字面形态。读回来的值必须过这一关 —— 文件被人手改坏时要能看出来，
# 而不是把一个畸形串当身份用（那会连出一条 hs 认不出 guid 的连接，且不报错）。
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class GuidError(RuntimeError):
    """身份不可用。**必须让服务起不来**，不能降级继续跑。

    降级的后果是"用一个临时 guid 写了一批点"，而那批点日后清不掉（绝不自动删）。
    起不来是吵的，写脏数据是安静的 —— 选吵的那个。
    """


class SystemGuid:
    """读得到就用，两处都没有才生成；生成后立刻两处都写。

    ``paths`` 按**优先级**给：读取时逐个试，第一个读到合法值的即采用。
    """

    def __init__(self, paths: list[Path]) -> None:
        if not paths:
            raise GuidError("未配置任何 guid 落点")
        self._paths = [Path(p) for p in paths]

    # ── 读 ────────────────────────────────────────────────────────────────
    def _read_one(self, path: Path) -> str | None:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            # 读不了 ≠ 不存在。把它和"没有"混为一谈，就会在一次权限故障后**重新生成**身份。
            raise GuidError(f"guid 落点读取失败，拒绝继续: {path}: {exc}") from exc
        if not raw:
            raise GuidError(f"guid 落点是空文件，拒绝把它当作『没有』: {path}")
        if not _UUID_RE.match(raw):
            raise GuidError(f"guid 落点内容不是合法 UUID v4: {path}: {raw!r}")
        return raw

    # ── 写 ────────────────────────────────────────────────────────────────
    def _write_all(self, value: str) -> None:
        written: list[Path] = []
        for path in self._paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # 先写临时文件再改名：断电/崩溃时不会留下半个 guid（那会被下次启动判成畸形而拒启）。
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(value + "\n", encoding="utf-8")
                os.replace(tmp, path)
                # 身份不是密钥，不设 600 —— 将来以非 root 跑要读得到。
                os.chmod(path, 0o644)
                written.append(path)
            except OSError as exc:
                logger.warning("guid 落点写入失败: %s: %s", path, exc)
        if not written:
            raise GuidError("guid 生成后一处都没写成，拒绝以内存中的身份运行")
        if len(written) < len(self._paths):
            # 只写成一处仍可运行，但**必须吵**：冗余的意义就是防"那一处没了"。
            logger.warning(
                "guid 只写成 %d/%d 处，冗余不完整（丢了那一处就会换身份）: 已写=%s",
                len(written), len(self._paths), [str(p) for p in written],
            )

    # ── 对外 ──────────────────────────────────────────────────────────────
    def load_or_create(self) -> str:
        found: dict[Path, str] = {}
        for path in self._paths:
            value = self._read_one(path)
            if value is not None:
                found[path] = value

        if found:
            values = set(found.values())
            if len(values) > 1:
                # 两处不一致 = 不知道哪个才是"这套系统的身份"。**绝不挑一个继续**：
                # 挑错了就等于换身份，而换身份的后果见模块头。
                raise GuidError(
                    "guid 冗余落点内容不一致，无法判定真身份，拒绝启动: "
                    + "; ".join(f"{p}={v}" for p, v in found.items())
                )
            value = values.pop()
            missing = [p for p in self._paths if p not in found]
            if missing:
                # 补回缺的那几处，让冗余重新完整（不改变身份）。
                logger.info("guid 冗余落点缺失 %s，按现有身份补回", [str(p) for p in missing])
                self._write_all(value)
            return value

        value = str(uuid.uuid4())
        logger.warning(
            "首次启动：生成系统 guid %s 并冗余落盘 %s。"
            "★此值一经生成永不变 —— 换掉它会让先前写入的点变成无主数据且清不掉。",
            value, [str(p) for p in self._paths],
        )
        self._write_all(value)
        return value
