"""实验九：FEMTO 17 台全寿命 —— 哪个指标最早、最稳地报出「开始劣化」，误报多少。

用法：~/research-venv/bin/python femto_trend.py /mnt/d/download/AIRef/datasets/femto [缓存目录]

与 research/rul 的区别：那边问「回归得出剩余寿命吗」（标量基本不行）；这里问**报警**：
从健康走到失效，谁先、谁稳地说「开始坏了」。

★FEMTO 没有公开可靠的轴承几何 ⇒ 算不出故障特征频率 ⇒ **不用特定频率的规则**，只比不依赖故障频率的指标：
  vel   速度有效值 10~1000 Hz（= 现场 SVT10 给的、模块 1/2 在吃的）
  acc   加速度有效值（全带）
  kurt  峭度      crest 波峰因数
  hf    高频带（0.2~0.45 fs = 5.1~11.5 kHz）有效值
  envpk 平方包络谱（0.2~0.45 fs 解调）在 5~500 Hz 内 最大值 ÷ 中位数 —— 出现周期性冲击就升，不必知道故障频率

判法（同现场模块 2）：寿命前 10% 帧建基线（中位数 / IQR÷1.349），稳健 z ≥ 3；
★**连续 6 帧（1 分钟）才算报警**（单帧毛刺不算）；两轴取 z 较大者。
★陷阱：试验在振动 > 20 g 时停 ⇒ 终点前加速度必飙 —— 「最后几分钟报出来」无意义，看**提前多久**。
指标：
  · 提前量 = 首次持续报警到寿命终点的时长（分钟）与占寿命比例；
  · 早段误报 = 寿命 10%~50% 段里处于报警的帧比例（这段多数台仍健康 —— 仅作参考，不是金标准）；
  · 单调性 = 指标与时间的 Spearman 秩相关；
  · ★报警后持续 = 首次持续报警之后仍处于报警的帧比例 —— 早段报警可能是真劣化也可能是误报，
    「10~50% 段健康」这个假设不牢；持续性把两者分开：真劣化会一直报，误报时有时无。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

FS = 25600.0
IND = ("vel", "acc", "kurt", "crest", "hf", "envpk")
RUN = 6


def feats(x: np.ndarray) -> dict[str, float]:
    x = x - x.mean()
    n = x.size
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / FS)
    keep = (f >= 10) & (f <= 1000)
    V = np.zeros_like(X)
    V[keep] = X[keep] / (2j * np.pi * f[keep])
    vel = float(np.sqrt(np.mean(np.fft.irfft(V, n) ** 2)) * 9.81 * 1000)   # g → mm/s
    s2 = float(np.mean(x ** 2))
    hb = (f >= 0.2 * FS) & (f <= 0.45 * FS)
    H = np.zeros_like(X); H[hb] = X[hb]
    xh = np.fft.irfft(H, n)
    Xa = np.fft.fft(x); fa = np.fft.fftfreq(n, 1.0 / FS)
    Ha = np.zeros(n); Ha[(fa >= 0.2 * FS) & (fa <= 0.45 * FS)] = 2.0
    e2 = np.abs(np.fft.ifft(Xa * Ha)) ** 2
    E = np.abs(np.fft.rfft(e2 - e2.mean()))
    fe = np.fft.rfftfreq(n, 1.0 / FS)
    w = (fe >= 5) & (fe <= 500)
    return dict(vel=vel, acc=float(np.sqrt(s2)), kurt=float(np.mean(x ** 4) / s2 ** 2),
                crest=float(np.max(np.abs(x)) / np.sqrt(s2)), hf=float(np.sqrt(np.mean(xh ** 2))),
                envpk=float(E[w].max() / max(np.median(E[w]), 1e-30)))


def load_bearing(d: Path) -> np.ndarray:
    files = sorted(d.glob("acc_*.csv"))
    out = np.empty((len(files), 2, len(IND)))
    for i, p in enumerate(files):
        a = np.loadtxt(p, delimiter="," if "," in p.read_text()[:200] else ";")
        for c in (0, 1):
            fd = feats(a[:, 4 + c])
            out[i, c] = [fd[k] for k in IND]
    return out


def first_run(mask: np.ndarray, n: int) -> int | None:
    run = 0
    for i, m in enumerate(mask):
        run = run + 1 if m else 0
        if run >= n:
            return i - n + 1
    return None


def main(root: str, cache: str) -> None:
    rp, cp = Path(root), Path(cache)
    cp.mkdir(parents=True, exist_ok=True)
    dirs = sorted(list((rp / "Learning_set").glob("Bearing*")) + list((rp / "Full_Test_Set").glob("Bearing*")))
    per: dict[str, list[tuple]] = {k: [] for k in IND}
    for d in dirs:
        c = cp / f"{d.name}.npy"
        if c.exists():
            F = np.load(c)
        else:
            F = load_bearing(d)
            np.save(c, F)
        N = F.shape[0]
        nb = max(int(0.1 * N), 30)
        row = [f"{d.name:<11} 帧{N:>5} 寿命{N * 10 / 60:6.0f}min"]
        for j, k in enumerate(IND):
            z = np.empty((N, 2))
            for ch in (0, 1):
                base = F[:nb, ch, j]
                q1, med, q3 = np.percentile(base, [25, 50, 75])
                sc = max((q3 - q1) / 1.349, 1e-9)
                z[:, ch] = (F[:, ch, j] - med) / sc
            zz = z.max(axis=1)
            alarm = zz >= 3.0
            i0 = first_run(alarm[nb:], RUN)
            lead = None if i0 is None else (N - (i0 + nb)) * 10 / 60
            early = float(np.mean(alarm[nb:int(0.5 * N)])) if int(0.5 * N) > nb else float("nan")
            # ★持续性：首次持续报警之后，仍处于报警的帧比例 —— 真劣化会一直报，误报时有时无
            persist = float("nan") if i0 is None else float(np.mean(alarm[i0 + nb:]))
            rho = spearmanr(np.arange(N), F[:, :, j].max(axis=1)).statistic
            per[k].append((lead, None if lead is None else lead / (N * 10 / 60), early, rho, persist))
            row.append(f"{k}:{'-' if lead is None else f'{lead:.0f}'}")
        print("  ".join(row))
    print("\n== 汇总（17 台）：提前量中位数（分钟 / 占寿命）、有报警的台数、早段误报中位数、单调性中位数")
    for k in IND:
        L = [x for x in per[k] if x[0] is not None]
        leads = np.array([x[0] for x in L]) if L else np.array([np.nan])
        frac = np.array([x[1] for x in L]) if L else np.array([np.nan])
        early = np.array([x[2] for x in per[k]])
        rho = np.array([x[3] for x in per[k]])
        pers = np.array([x[4] for x in L]) if L else np.array([np.nan])
        print(f"{k:<6} 提前 {np.nanmedian(leads):6.0f} min / {np.nanmedian(frac):4.2f}  报警台数 {len(L):>2}/17  "
              f"早段报警 {np.nanmedian(early):4.2f}（>0.2 的台数 {int(np.sum(early > 0.2))}）  单调 {np.nanmedian(rho):5.2f}  "
              f"报警后持续 {np.nanmedian(pers):4.2f}（≥0.8 的台数 {int(np.sum(pers >= 0.8))}）")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "/root/femto-trend-cache")
