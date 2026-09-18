"""验证 3：★算法侧 —— 丢样本对**频谱**到底造成多大破坏，量成实数。

    python verify3_algorithm.py [Bearing 目录]

## 为什么要验这一条

前两条验的是"**能不能发现**丢样"。本条验的是"**发现不了会怎样**" ——
即用户最初那句话的量化：

> 传感振动数据是不能用 SDT 的，否则会造成采样间隔的不对齐，会对分析算法造成干扰。

★**两种消费路径，结果完全不同**，这正是要害：

| 路径 | 算法看到什么 |
| --- | --- |
| **甲：按数组形态**（每条 VQT 一个真 T） | 能看出间隔不齐 ⇒ 可以**拒绝出结论** |
| **乙：按「一帧一个值」** | 采样率是**声明**的，算法把抽稀后的样本当成等间隔 ⇒ **照常算出一个谱，而且是错的** |

本脚本量乙路径的错有多大：拿同一帧，比较
  · 原始谱（完好数据）
  · 抽稀后**当作等间隔**算出来的谱
在**主峰频率**与**谱形**两个口径上的偏差。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


def load_rows(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        c = line.strip().split(",")
        if len(c) >= 6:
            h, m, s, us = int(c[0]), int(c[1]), int(c[2]), int(float(c[3]))
            out.append((((h * 60 + m) * 60 + s) * 1_000_000 + us, float(c[4]), float(c[5])))
    return out


def spectrum(x, fs):
    """单边幅值谱。"""
    n = len(x)
    w = np.hanning(n)
    X = np.abs(np.fft.rfft((x - np.mean(x)) * w)) / n
    f = np.fft.rfftfreq(n, d=1.0 / fs)
    return f, X


def top_peaks(f, X, k=5, fmin=10.0):
    """前 k 个谱峰（跳过极低频，避免直流残留占位）。"""
    m = f >= fmin
    idx = np.argsort(X[m])[::-1][:k]
    return sorted(zip(f[m][idx], X[m][idx]), key=lambda t: -t[1])


def thin_by_value(rows, tol):
    """模拟 SDT：与上一条**已保留**样本差值小于 tol 就丢掉。"""
    kept = [rows[0]]
    for r in rows[1:]:
        if abs(r[1] - kept[-1][1]) > tol:
            kept.append(r)
    return kept


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = Path(args[0] if args else
                "/mnt/d/download/AIRef/datasets/femto/Learning_set/Bearing1_1")
    f0 = sorted(base.glob("acc_*.csv"))[0]
    rows = load_rows(f0)
    ts = [r[0] for r in rows]
    fs = 1e6 * (len(ts) - 1) / (ts[-1] - ts[0])      # 从 T 推，不用标称值
    x = np.array([r[1] for r in rows])               # 水平通道
    print(f"语料：{f0}")
    print(f"  {len(rows)} 条，采样率由 T 推得 {fs:,.1f} Hz\n")

    f_ref, X_ref = spectrum(x, fs)
    peaks_ref = top_peaks(f_ref, X_ref)
    print("完好数据的前 5 个谱峰：")
    for fr, amp in peaks_ref:
        print(f"   {fr:>9.1f} Hz   幅值 {amp:.5f}")
    print()

    print("抽稀后**当作等间隔**（= 「一帧一个值」路径会做的事）算出来的谱：")
    hdr = "   {:>8} {:>7} {:>8} {:>12} {:>10} {:>9}".format(
        "抽稀容差", "剩余", "丢掉%", "主峰(Hz)", "主峰漂移", "谱形误差")
    print(hdr)
    worst_shift = 0.0
    worst_err = 0.0
    for tol in (0.005, 0.01, 0.02, 0.05, 0.1, 0.2):
        kept = thin_by_value(rows, tol)
        if len(kept) < 64:
            continue
        y = np.array([r[1] for r in kept])
        # ★关键：乙路径**不知道**样本被丢过，它按"声明的采样率"当等间隔处理。
        #   这里如实模拟：仍用原 fs，只是点数变少 —— 这正是错误的来源。
        f_t, X_t = spectrum(y, fs)
        p_t = top_peaks(f_t, X_t, k=1)
        shift = abs(p_t[0][0] - peaks_ref[0][0]) if p_t else float("nan")
        # 谱形误差：把两条谱插值到同一频轴后比 L2 相对误差
        Xi = np.interp(f_ref, f_t, X_t)
        err = float(np.linalg.norm(Xi - X_ref) / np.linalg.norm(X_ref))
        worst_shift = max(worst_shift, shift)
        worst_err = max(worst_err, err)
        print("   {:>8.3f} {:>7} {:>7.1f}% {:>12.1f} {:>9.1f}Hz {:>8.1%}".format(
            tol, len(kept), 100 * (1 - len(kept) / len(rows)), p_t[0][0], shift, err))

    print()
    print("=" * 70)
    print(f"★最严重：主峰漂移 {worst_shift:,.1f} Hz，谱形误差 {worst_err:.1%}")
    print()
    print("含义：")
    print("  · 乙路径（一帧一个值）：以上的谱**照样算得出、照样画得出**，")
    print("    没有任何报错或坏码 —— 而它是错的。这正是「静默出错」。")
    print("  · 甲路径（数组形态）：验证 2 已证明，只要有 >1.5 倍基准的间隔就能发现，")
    print("    算法可以**拒绝出结论**而不是给一个错的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
