"""安装期：按域**自述的第三方依赖**决定铺不铺这个域。

为什么要这一层（2026-09-12 的实账）：
出包把仓里所有域打进发布件，`install.sh` 的「域只补不删」再把它们全铺到现场 ——
视觉域 `vision_helmet.py` 要 `cv2`/`numpy`/`onnxruntime`，而发布件**不带**这些依赖，
于是现场启动时它装载失败、`GetInfo.load_errors` 里常驻一条。
AICloud 界面会把 `load_errors` 显出来（`C-17 §2.1`），现场看到的就是一条永远红着的装载错误。
把文件手工移走只治标：**下次出包部署它照样回来**。

做法：域在模块顶层自述

    REQUIRES = ("cv2", "numpy", "onnxruntime")

没有这一行 = 零第三方依赖（如 `vibration_iso`）。安装时**绝不 import 域模块**
—— import 了就当场炸在缺的那个包上，这正是要避免的 —— 而是用 `ast` 读那个常量，
再拿**目标 venv** 查这些模块在不在；缺就不铺，并把原因打印出来。

★**不改运行时语义**：骨架仍然是「`domains/` 里有什么就装什么」，装不起来仍记 `load_errors`
  （那是现场被人手工放了个域时该有的反馈）。这里管的只是「发布件里的域要不要落到现场」。
★**仍是只补不删**：跳过不铺 ≠ 删掉现场已有的同名域。现场若已有一份且依赖仍缺，
  它会继续 `load_errors` —— 那是现场遗留，安装脚本不替人做删除决定。
"""

from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
from pathlib import Path

REQUIRES_NAME = "REQUIRES"


def parse_requires(path: Path) -> tuple[str, ...]:
    """读域模块顶层的 `REQUIRES`，**不执行也不 import** 这个文件。

    读不到（没有这一行）→ 空元组 = 零第三方依赖。
    文件语法都不对 → 也回空元组：那是另一类问题，交给运行时装载器去报 `load_errors`，
    这里不替它判死刑（**不猜**）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return ()
    for node in tree.body:                      # ★只看模块顶层，不进函数/类
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == REQUIRES_NAME:
                try:
                    val = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                    return ()
                if isinstance(val, str):        # REQUIRES = "cv2" 也认
                    return (val,)
                if isinstance(val, (list, tuple, set)):
                    return tuple(str(x) for x in val)
                return ()
    return ()


def missing_modules(reqs: tuple[str, ...]) -> list[str]:
    """这些模块在**当前解释器**里能不能找到；返回缺的那些（原序、去重）。

    用 `find_spec` 而不是真 import：只查得到查不到，不执行包的初始化代码。
    """
    out: list[str] = []
    for m in reqs:
        if m in out:
            continue
        try:
            found = importlib.util.find_spec(m) is not None
        except (ImportError, ValueError, AttributeError):
            # 父包不在、名字不合法等：一律按"找不到"处理
            found = False
        if not found:
            out.append(m)
    return out


def plan(src_dir: Path) -> list[tuple[Path, tuple[str, ...], list[str]]]:
    """扫发布件里的域，逐个给出 (文件, 自述依赖, 缺的依赖)。按文件名排序，结果可复现。"""
    if not src_dir.is_dir():
        return []
    rows = []
    for p in sorted(src_dir.glob("*.py")):
        if p.name.startswith("_"):              # `__init__.py` 一类不是域
            continue
        reqs = parse_requires(p)
        rows.append((p, reqs, missing_modules(reqs)))
    return rows


def install(src_dir: Path, dst_dir: Path, *, out=None) -> tuple[int, int]:
    """把依赖齐的域铺到 `dst_dir`；缺依赖的跳过。返回 (铺了几个, 跳过几个)。"""
    out = out or sys.stdout
    dst_dir.mkdir(parents=True, exist_ok=True)
    laid = skipped = 0
    for path, reqs, miss in plan(src_dir):
        if miss:
            skipped += 1
            print(f"    跳过域 {path.name}：缺 {', '.join(miss)}"
                  f"（自述依赖 {', '.join(reqs)}）—— 要部署它请连依赖一起出包",
                  file=out)
            continue
        shutil.copy2(path, dst_dir / path.name)
        laid += 1
        print(f"    铺入域 {path.name}"
              + (f"（依赖齐：{', '.join(reqs)}）" if reqs else "（零第三方依赖）"), file=out)
    return laid, skipped


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("用法: python -m aiintegration.domaindeps <发布件domains目录> <现场domains目录>",
              file=sys.stderr)
        return 2
    laid, skipped = install(Path(argv[0]), Path(argv[1]))
    print(f"    域：铺入 {laid} 个，跳过 {skipped} 个")
    return 0                                     # ★跳过不算失败：安装照常完成


if __name__ == "__main__":
    raise SystemExit(main())
