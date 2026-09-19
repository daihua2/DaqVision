"""SCA（纸浆厂真实自然故障）上复刻现场 vibration_baseline 的「相对基线偏离」，看它报不报得出、误报多少。

用法：~/research-venv/bin/python sca_baseline.py /mnt/d/download/AIRef/datasets/sca

复刻的是现场在跑的那套（domains/vibration_baseline.py）：
  · 标量 = 速度有效值（mm/s）—— 现场传感器（有人 SVT10）给的就是它；
  · 基线 = 健康段的 中位数 / (IQR/1.349)；z = (当前 − 中位数) / 尺度；z ≥ 3 为「显著」；
  · 尺度下限 MIN_SCALE = 0.01 mm/s。

★与现场不同、必须说清的：
  · 每案例**固定一个速度频带**：10 Hz ~ min(1000, 0.9 × 该案例最低采样率的奈奎斯特)。
    ★不能按每次测量的采样率各取上限 —— 案例 9 的 test 段混着 2560 / 5120 Hz，那样同一台机器
    前后算的是两把尺子（本脚本第一版就犯了这个错）；
  · 现场一拍取窗口内最大值，这里一次测量就是一帧（约一天一次）。

三种误报口径（都要看，意思不同）：
  · FA_half  健康段按时间对半，前半建基线、后半算 —— **建基线后短期内**的误报；
  · FA_later test 文件里标签为 0 的测量（含非故障侧）—— **基线建好几个月后**的误报，更贴现场；
  · 扣转速（待议 D3）：训练段拟合 log(v) = a + b·log(rpm)，对残差做同样的稳健 z。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import scipy.io as sio

Z_NOTABLE = 3.0
IQR_TO_SIGMA = 1.349
MIN_SCALE = 0.01


def vel_rms_mm_s(acc: np.ndarray, fs: float, lo: float, hi: float) -> float:
    """加速度 (m/s²) → 频域积分 → [lo, hi] 带内速度有效值 (mm/s)。"""
    x = acc - acc.mean()
    n = x.size
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    keep = (f >= lo) & (f <= hi)
    V = np.zeros_like(X)
    V[keep] = X[keep] / (2j * np.pi * f[keep])
    v = np.fft.irfft(V, n)
    return float(np.sqrt(np.mean(v ** 2)) * 1000.0)


def sides_of(m: dict) -> list[str]:
    return [k for k in m if not k.startswith("__") and hasattr(m[k], "_fieldnames")
            and "rawData" in m[k]._fieldnames]


def raw_list(s) -> list[np.ndarray]:
    rd = s.rawData
    # ★有的案例每次测量点数不一（参差数组，scipy 读成 object 数组），逐条取。
    if isinstance(rd, np.ndarray) and rd.dtype == object:
        return [np.asarray(x, dtype=float).ravel() for x in rd.ravel()]
    return list(np.atleast_2d(np.asarray(rd, dtype=float)))


def load(path: Path):
    return sio.loadmat(str(path), squeeze_me=True, struct_as_record=False)


def side_data(m: dict, side: str, band: tuple[float, float]):
    s = m[side]
    raw = raw_list(s)
    fs = np.atleast_1d(np.asarray(s.samplingRate, dtype=float))
    rpm = np.atleast_1d(np.asarray(s.RPM, dtype=float))
    lab = np.atleast_1d(np.asarray(s.label))
    t = np.array([np.datetime64(str(x)[:19], "s") for x in np.atleast_1d(np.asarray(s.time))])
    v = np.array([vel_rms_mm_s(raw[i], fs[i], *band) for i in range(len(raw))])
    o = np.argsort(t)
    return dict(t=t[o], v=v[o], rpm=rpm[o], lab=lab[o], fs=fs[o])


def robust(vals: np.ndarray) -> tuple[float, float]:
    q1, med, q3 = np.percentile(vals, [25, 50, 75])
    return float(med), max(float(q3 - q1) / IQR_TO_SIGMA, MIN_SCALE)


def first_run(mask: np.ndarray, n: int) -> int | None:
    run = 0
    for i, m in enumerate(mask):
        run = run + 1 if m else 0
        if run >= n:
            return i - n + 1
    return None


def days(a, b) -> float:
    return round(float((b - a) / np.timedelta64(1, "D")), 1)


def main(root: str) -> None:
    rp = Path(root)
    print("case side   band(Hz)   n_h  FA_half FA_later FA_later_spd | n_f hit  hit_spd | "
          "lead1 lead3 lead_spd onset | gap(d) rpm_span  b")
    for case in range(1, 12):
        mtr, mte = load(rp / str(case) / "train.mat"), load(rp / str(case) / "test.mat")
        sides = [s for s in sides_of(mtr) if s in sides_of(mte)]
        fs_min = min(float(np.min(np.atleast_1d(m[s].samplingRate))) for m in (mtr, mte) for s in sides)
        band = (10.0, min(1000.0, 0.9 * fs_min / 2))
        meta = {k: np.asarray(mte[k]).item() for k in ("assetDescription", "faultOrigin", "faultType", "fixedSpeed")}
        print(f"-- 案例 {case}: {meta['assetDescription']}, 故障侧 {meta['faultOrigin']}, "
              f"类型 {meta['faultType']}（0 正常 1 内圈 2 滚动体 3 外圈）, 定速 {meta['fixedSpeed']}")
        for side in sides:
            tr, te = side_data(mtr, side, band), side_data(mte, side, band)
            ok = tr["lab"] == 0
            v_h, r_h, t_h = tr["v"][ok], tr["rpm"][ok], tr["t"][ok]
            half = len(v_h) // 2
            if half < 5:
                print(f"{case:>4} {side:<5} 健康段不足 10 次，跳过")
                continue
            med_h, sc_h = robust(v_h[:half])
            fa_half = float(np.mean((v_h[half:] - med_h) / sc_h >= Z_NOTABLE))
            # 现场做法：基线用整个健康段
            med, sc = robust(v_h)
            pos = r_h > 0
            use_spd = bool(pos.sum() > 5 and np.ptp(np.log(r_h[pos])) > 0.05)
            if use_spd:
                b, a = np.polyfit(np.log(r_h[pos]), np.log(v_h[pos]), 1)
                rmed, rsc = robust(np.log(v_h[pos]) - (a + b * np.log(r_h[pos])))
                rsc = max(rsc, 1e-3)
            else:
                a = b = rmed = 0.0
                rsc = 1.0

            def zr(v):
                return (v - med) / sc

            def zs(v, r):
                if not use_spd:
                    return zr(v)
                return (np.log(v) - (a + b * np.log(np.maximum(r, 1e-6))) - rmed) / rsc

            run = te["lab"] >= 0
            tt, vv, rr, ll = te["t"][run], te["v"][run], te["rpm"][run], te["lab"][run]
            normal, fault = ll == 0, ll > 0
            fa_later = float(np.mean(zr(vv[normal]) >= Z_NOTABLE)) if normal.any() else float("nan")
            fa_later_s = float(np.mean(zs(vv[normal], rr[normal]) >= Z_NOTABLE)) if normal.any() else float("nan")
            gap = days(t_h[-1], tt[0]) if len(tt) else float("nan")
            span = f"{r_h.min():.0f}-{r_h.max():.0f}"
            line = (f"{case:>4} {side:<5} {band[0]:.0f}-{band[1]:<6.0f} {len(v_h):>4}  {fa_half:6.2f}  "
                    f"{fa_later:7.2f}  {fa_later_s:10.2f}  | ")
            if fault.any():
                z1, z2 = zr(vv), zs(vv, rr)
                end = tt[-1]

                def lead(i):
                    return "-" if i is None else f"{days(tt[i], end):.0f}"
                onset = f"{days(tt[int(np.argmax(fault))], end):.0f}"
                line += (f"{int(fault.sum()):>3} {np.mean(z1[fault] >= Z_NOTABLE):4.2f} "
                         f"{np.mean(z2[fault] >= Z_NOTABLE):7.2f}  | "
                         f"{lead(first_run(z1 >= Z_NOTABLE, 1)):>5} {lead(first_run(z1 >= Z_NOTABLE, 3)):>5} "
                         f"{lead(first_run(z2 >= Z_NOTABLE, 1)):>8} {onset:>5} | ")
            else:
                line += f"{'':>3} {'':>4} {'':>7}  | {'':>5} {'':>5} {'':>8} {'':>5} | "
            line += f"{gap:6.1f} {span:>9} {b:5.2f}" + ("" if use_spd else "(定速,不扣)")
            print(line)


if __name__ == "__main__":
    main(sys.argv[1])
