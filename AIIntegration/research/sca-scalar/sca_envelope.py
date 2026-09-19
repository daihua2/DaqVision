"""实验三：同一批真实数据，换成**要原始波形才算得出**的包络特征，看分不分得开。

用法：~/research-venv/bin/python sca_envelope.py /mnt/d/download/AIRef/datasets/sca [env|kurt|crest]

与实验一、二**只换特征、其余一概不变**（稳健 z、z ≥ 3、两种基线、健康侧同期对照）：
  · 特征 = 包络谱在故障特征频率 1~3 次谐波（±3%）处的峰值 ÷ 包络谱中位底噪（无量纲），取 log；
  · 带通 [0.2, 0.45] × fs（取高频共振区，轴承冲击的能量在这里）；
  · 故障频率 = 阶次 × 当次测量的转频（rpm/60），每侧用**它自己那颗轴承**的阶次；
  · 故障类型 1 内圈 → BPFI；2 滚动体 → BPF；3 外圈 → BPFO。

★这回答的是：「为波形付代价（维特 RAWFIFO、结构值、逐条存储）」值不值 ——
  若包络在真实故障上分得开而速度有效值分不开，代价就有了着落；反之则没有。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from sca_baseline import Z_NOTABLE, load, raw_list, robust, sides_of

MIN_NORMAL = 10
ORDER_KEY = {1: "BPFIMultiple", 2: "BPFMultiple", 3: "BPFOMultiple"}


def env_snr(x: np.ndarray, fs: float, f_fault: float) -> float:
    x = x - x.mean()
    n = x.size
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1.0 / fs)
    lo, hi = 0.2 * fs, 0.45 * fs
    H = np.zeros(n)
    H[(f >= lo) & (f <= hi)] = 2.0          # 带通 + 解析信号（只留正频）
    env = np.abs(np.fft.ifft(X * H))
    env = env - env.mean()
    E = np.abs(np.fft.rfft(env)) / n
    fe = np.fft.rfftfreq(n, 1.0 / fs)
    top = min(4 * f_fault, fe[-1])
    floor_band = (fe > 0.5) & (fe <= top)
    if f_fault <= 0 or floor_band.sum() < 10:
        return float("nan")
    floor = float(np.median(E[floor_band])) or 1e-12
    peak = 0.0
    for k in (1, 2, 3):
        c = k * f_fault
        if c > fe[-1]:
            break
        w = (fe >= 0.97 * c) & (fe <= 1.03 * c)
        if w.any():
            peak += float(E[w].max())
    return float(np.log(peak / floor + 1e-12))


def kurt(x: np.ndarray) -> float:
    x = x - x.mean()
    s2 = float(np.mean(x ** 2))
    return float(np.mean(x ** 4) / (s2 ** 2)) if s2 > 0 else float("nan")


def crest(x: np.ndarray) -> float:
    x = x - x.mean()
    r = float(np.sqrt(np.mean(x ** 2)))
    return float(np.max(np.abs(x)) / r) if r > 0 else float("nan")


#: 特征：env = 包络（要原始波形）；kurt / crest = 峭度 / 波峰因数（维特 VB02 寄存器里就有，传感器自己算）
FEATURE = "env"


def feature(x: np.ndarray, fs: float, f_fault: float) -> float:
    if FEATURE == "kurt":
        return kurt(x)
    if FEATURE == "crest":
        return crest(x)
    return env_snr(x, fs, f_fault)


def side_env(m: dict, side: str, ftype: int):
    s = m[side]
    raw = raw_list(s)
    fs = np.atleast_1d(np.asarray(s.samplingRate, dtype=float))
    rpm = np.atleast_1d(np.asarray(s.RPM, dtype=float))
    lab = np.atleast_1d(np.asarray(s.label))
    t = np.array([np.datetime64(str(x)[:19], "s") for x in np.atleast_1d(np.asarray(s.time))])
    order = float(getattr(s.faultFrequencies, ORDER_KEY[ftype]))
    e = np.array([feature(raw[i], fs[i], order * rpm[i] / 60.0) for i in range(len(raw))])
    o = np.argsort(t)
    return dict(t=t[o], e=e[o], lab=lab[o])


def zscore(base: np.ndarray, x: np.ndarray) -> np.ndarray:
    base = base[np.isfinite(base)]
    med, sc = robust(base)
    return (x - med) / max(sc, 1e-3)


def main(root: str) -> None:
    rp = Path(root)
    print("case 故障侧 | 健康段基线: hit  健康侧同期 | 近期基线: n_norm FA_half hit  健康侧同期 | 首报距末次 故障标签距末次")
    for case in range(1, 11):
        mtr, mte = load(rp / str(case) / "train.mat"), load(rp / str(case) / "test.mat")
        origin = str(np.asarray(mte["faultOrigin"]).item())
        ftype = int(np.asarray(mte["faultType"]).item())
        sides = [s for s in sides_of(mte) if s in sides_of(mtr)]
        if origin not in sides or ftype not in ORDER_KEY:
            continue
        te = side_env(mte, origin, ftype)
        tr = side_env(mtr, origin, ftype)
        run = te["lab"] >= 0
        t, e, lab = te["t"][run], te["e"][run], te["lab"][run]
        fault = lab > 0
        # A. 健康段（数据集给的 train）基线
        zA = zscore(tr["e"][tr["lab"] == 0], e)
        hitA = float(np.mean(zA[fault] >= Z_NOTABLE))
        otherA = []
        for s in sides:
            if s == origin:
                continue
            ote, otr = side_env(mte, s, ftype), side_env(mtr, s, ftype)
            r = ote["lab"] >= 0
            oz = zscore(otr["e"][otr["lab"] == 0], ote["e"][r])
            otA = ote["t"][r]
            sel = (otA >= t[fault][0]) & (otA <= t[fault][-1])
            if sel.any():
                otherA.append(f"{s} {np.mean(oz[sel] >= Z_NOTABLE):.2f}")
        line = f"{case:>4} {origin:<5} | {hitA:14.2f}  {', '.join(otherA) or '无':<10} | "
        # B. 近期基线
        onset = int(np.argmax(fault))
        pre = e[:onset][lab[:onset] == 0]
        if len(pre) >= MIN_NORMAL:
            half = len(pre) // 2
            fa = float(np.mean(zscore(pre[:half], pre[half:]) >= Z_NOTABLE))
            zB = zscore(pre, e)
            hitB = float(np.mean(zB[fault] >= Z_NOTABLE))
            otherB = []
            for s in sides:
                if s == origin:
                    continue
                o = side_env(mte, s, ftype)
                r = o["lab"] >= 0
                ot, oe = o["t"][r], o["e"][r]
                ob, oa = oe[ot < t[onset]], oe[ot >= t[onset]]
                if len(ob) >= MIN_NORMAL and len(oa):
                    otherB.append(f"{s} {np.mean(zscore(ob, oa) >= Z_NOTABLE):.2f}")
            first = next((i for i in range(onset, len(zB)) if zB[i] >= Z_NOTABLE), None)
            end = t[-1]
            lead = "-" if first is None else f"{float((end - t[first]) / np.timedelta64(1, 'D')):.0f}"
            ond = f"{float((end - t[onset]) / np.timedelta64(1, 'D')):.0f}"
            line += f"{len(pre):>6} {fa:7.2f} {hitB:4.2f}  {', '.join(otherB) or '无':<10} | {lead:>10} {ond:>12}"
        else:
            line += f"故障前正常段 {len(pre)} 次，不做"
        print(line)


if __name__ == "__main__":
    if len(sys.argv) > 2:
        FEATURE = sys.argv[2]
    print(f"== 特征 {FEATURE}")
    main(sys.argv[1])
