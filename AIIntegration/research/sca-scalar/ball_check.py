"""实验八：滚动体故障 —— 哪种证据真的显现？会不会在正常 / 内外圈文件上也触发？

用法：~/research-venv/bin/python ball_check.py /mnt/d/download/AIRef/datasets/cwru

由来：实验六里 CWRU 滚动体 16 个文件几乎全判不出（0.00），与文献「滚动体多数诊断不出」一致，
是我方做法最大的空白。滚动体为什么难【文】：
  1. 缺陷点不总朝向滚道（自转 + 歪斜）⇒ 冲击**断续**；
  2. 自转一周分别撞内、外圈各一次 ⇒ 特征常见 **2×BSF** 而非 BSF；
  3. 随保持架公转进出载荷区 ⇒ 按 **FTF** 调制：可能表现为 2×BSF 两侧 ±FTF 边带，**也可能是 FTF 自身的谐波系列**。

我方现规则只找 2×BSF 系列。这里试四种证据，每种都在三类文件上数「过半段触发」：
  · 滚动体文件（要触发）；· 正常文件（不该触发 = 误报）；· 内圈/外圈文件（不该触发 = 误判类型）。

CWRU 6205（CWRU 官网阶次）：2×BSF 4.7135 ⇒ BSF 2.3568；FTF 0.39828。
段长 2 秒（24000 点，包络谱分辨率 0.5 Hz；FTF ≈ 11.9 Hz 需要这个分辨率）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.signal import decimate

import sca_rule
from cwru_check import read
from sca_methods import _local_noise, ies, ses, sk_band
from sca_rule import n_harm

sca_rule.SIDEBAND_GATE = False
BSF2, FTF = 4.7135, 0.39828
BPFO, BPFI = 3.5848, 5.4152
SEG = 24000
FS = 12000.0


def sideband_ok(f, S, center, spacing, thr=2.0) -> bool:
    df = f[1] - f[0]
    j = int(round(center / df))
    if j <= 2 or j >= S.size - 3:
        return False
    noise = _local_noise(S, j)
    for sgn in (-1, 1):
        w = (f >= center + sgn * spacing - 2 * df) & (f <= center + sgn * spacing + 2 * df)
        if w.any() and S[w].max() >= thr * noise:
            return True
    return False


def evidences(seg: np.ndarray, rpm: float, pre: str) -> dict[str, bool]:
    if pre == "ies":
        f, S = ies(seg, FS)
    elif pre == "band":
        f, S = ses(seg, FS, 0.2 * FS, 0.45 * FS)
    else:
        lo, hi = sk_band(seg, FS)
        f, S = ses(seg, FS, lo, hi)
    fr = rpm / 60.0
    b2, bs, ft = BSF2 * fr, BSF2 / 2 * fr, FTF * fr
    out = {
        "2BSF系列": n_harm(f, S, b2, fr, False) >= 2,
        "BSF系列": n_harm(f, S, bs, fr, False) >= 2,
        "FTF系列": n_harm(f, S, ft, fr, False) >= 3,     # FTF 低，要求连续 3 次才算
    }
    out["2BSF+FTF边带"] = (n_harm(f, S, b2, fr, False) >= 1) and sideband_ok(f, S, b2, ft)
    # ★排他：外圈/内圈证据成立时，不把 2BSF 归给滚动体（CWRU：4×BPFO=14.34 与 3×2BSF=14.14 只差 1.4%）
    other = n_harm(f, S, BPFO * fr, fr, False) >= 2 or n_harm(f, S, BPFI * fr, fr, False) >= 2
    out["2BSF排他"] = out["2BSF系列"] and not other
    return out


def file_rates(x: np.ndarray, rpm: float, pre: str) -> dict[str, float]:
    acc: dict[str, list[bool]] = {}
    for i in range(0, x.size - SEG + 1, SEG):
        for k, v in evidences(x[i:i + SEG], rpm, pre).items():
            acc.setdefault(k, []).append(v)
    return {k: float(np.mean(v)) for k, v in acc.items()}


def main(root: str) -> None:
    rp = Path(root)
    base = rp / "12k_Drive_End_Bearing_Fault_Data"
    groups: dict[str, list[tuple[str, np.ndarray, float]]] = {"滚动体": [], "内圈": [], "外圈": [], "正常": []}
    for path in sorted(base.rglob("*.mat")):
        kind = path.relative_to(base).parts[0]
        x, rpm = read(path)
        groups[{"B": "滚动体", "IR": "内圈", "OR": "外圈"}[kind]].append((str(path.relative_to(base)), x, rpm))
    for path in sorted((rp / "Normal").glob("*.mat")):
        x, rpm = read(path)
        groups["正常"].append((path.name, decimate(x, 4, ftype="fir"), rpm))

    for pre in (sys.argv[2:] or ["band", "sk", "ies"]):
        print(f"== 前处理 {pre}")
        print("滚动体逐文件（段触发率）：")
        per: dict[str, dict[str, list[float]]] = {}
        for g, items in groups.items():
            for name, x, rpm in items:
                r = file_rates(x, rpm, pre)
                for k, v in r.items():
                    per.setdefault(g, {}).setdefault(k, []).append(v)
                if g == "滚动体":
                    print(f"  {name:<22} " + "  ".join(f"{k} {v:4.2f}" for k, v in r.items()))
        print("汇总：各组「过半段触发」的文件比例（滚动体要高；正常、内圈、外圈要低）")
        keys = list(next(iter(per.values())).keys())
        print("  组      " + "  ".join(f"{k:>12}" for k in keys))
        for g, d in per.items():
            print(f"  {g:<6} " + "  ".join(f"{np.mean(np.array(d[k]) > 0.5):12.2f}" for k in keys)
                  + f"   (n={len(d[keys[0]])})")


if __name__ == "__main__":
    main(sys.argv[1])
