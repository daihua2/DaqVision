"""实验四：把「成熟做法」逐项加进包络，看每一步在真实数据上换来多少。

用法：~/research-venv/bin/python sca_methods.py /mnt/d/download/AIRef/datasets/sca

出处（方法，不是代码 —— 别人的代码不进我方仓，这里是自写实现）：
  · Randall & Antoni,《Rolling element bearing diagnostics — A tutorial》, MSSP 25(2), 2011；
  · Smith & Randall, CWRU 基准, MSSP 64-65, 2015 —— 三法中「倒谱预白化 + 包络」总成绩最好；
  · VibPy（Klausen，MIT）的 `diagnosefft` 判定规则：±2% 找峰并以实测峰重定特征频率、此后 ±1%；
    局部噪底取峰两侧谷底；**连续**谐波各 ≥3 倍噪底才累计，断即止；内圈要求转频边带。

四种特征（其余与实验一~三一概相同：稳健 z、z≥3、两种基线、同期健康侧对照）：
  A  固定带通 0.2~0.45 fs → 包络谱 → 1~3 次谐波峰 ÷ 全谱中位底噪（= 实验三 env）
  B  倒谱预白化（CPW）→ 全带平方包络谱 → 同 A 的读法
  C  谱峭度（STFT）选带 → 平方包络谱 → 同 A 的读法
  D  CPW → 平方包络谱 → ★专家判定规则（重定频率、局部噪底、连续谐波、内圈看边带）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

import sca_envelope as E
from sca_baseline import Z_NOTABLE, load, raw_list, robust, sides_of

MIN_NORMAL = 10


# ── 信号处理（自写） ─────────────────────────────────────────────
def cpw(x: np.ndarray) -> np.ndarray:
    """倒谱预白化：幅值压成 1、留相位。离散频率线（齿轮、转频）被压平，随机冲击显出。"""
    X = np.fft.fft(x - x.mean())
    mag = np.abs(X)
    return np.real(np.fft.ifft(X / np.maximum(mag, 1e-15)))


def band_analytic(x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
    n = x.size
    X = np.fft.fft(x - x.mean())
    f = np.fft.fftfreq(n, 1.0 / fs)
    H = np.zeros(n)
    H[(f >= lo) & (f <= hi)] = 2.0
    return np.fft.ifft(X * H)


def ses(x: np.ndarray, fs: float, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
    """平方包络谱（Randall 推荐平方包络）。"""
    a = band_analytic(x, fs, lo, hi)
    e2 = np.abs(a) ** 2
    e2 = e2 - e2.mean()
    S = np.abs(np.fft.rfft(e2)) / e2.size
    return np.fft.rfftfreq(e2.size, 1.0 / fs), S


def sk_band(x: np.ndarray, fs: float) -> tuple[float, float]:
    """STFT 谱峭度选带：多个窗长下逐频点算 SK，取全局最大处，带宽 = 2·fs/窗长。"""
    best = (-np.inf, 0.1 * fs, 0.45 * fs)
    x = x - x.mean()
    for nw in (32, 64, 128, 256):
        if x.size < 8 * nw:
            continue
        hop = nw // 4
        win = np.hanning(nw)
        frames = np.lib.stride_tricks.sliding_window_view(x, nw)[::hop] * win
        S = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        m2 = S.mean(axis=0)
        sk = (S ** 2).mean(axis=0) / np.maximum(m2 ** 2, 1e-30) - 2.0
        f = np.fft.rfftfreq(nw, 1.0 / fs)
        sk[(f < 0.02 * fs) | (f > 0.48 * fs)] = -np.inf   # 避开直流与奈奎斯特边
        i = int(np.argmax(sk))
        if sk[i] > best[0]:
            bw = 2.0 * fs / nw
            best = (sk[i], max(f[i] - bw / 2, 1.0), min(f[i] + bw / 2, 0.49 * fs))
    return best[1], best[2]


# ── 读法 ────────────────────────────────────────────────────────
def simple_read(f: np.ndarray, S: np.ndarray, ff: float) -> float:
    """同实验三：1~3 次谐波 ±3% 内峰之和 ÷ (0.5, 4ff] 内中位底噪，取 log。"""
    band = (f > 0.5) & (f <= min(4 * ff, f[-1]))
    if ff <= 0 or band.sum() < 10:
        return float("nan")
    floor = float(np.median(S[band])) or 1e-30
    peak = sum(float(S[(f >= 0.97 * k * ff) & (f <= 1.03 * k * ff)].max(initial=0.0))
               for k in (1, 2, 3) if k * ff < f[-1])
    return float(np.log(peak / floor + 1e-12))


def _local_noise(S: np.ndarray, j: int) -> float:
    """峰两侧谷底（VibPy diagnosefft v2 的取法，自写）。"""
    l = j
    while l > 1 and S[l - 1] <= S[l]:
        l -= 1
    r = j
    while r < S.size - 2 and S[r + 1] <= S[r]:
        r += 1
    return (float(np.mean(S[max(l - 2, 0):l + 1])) + float(np.mean(S[r:r + 3]))) / 2.0 or 1e-30


def expert_read(f: np.ndarray, S: np.ndarray, ff: float, shaft: float, inner: bool,
                harm_thr: float = 3.0, side_thr: float = 2.0, max_h: int = 6) -> float:
    """专家判定：重定频率、局部噪底、连续谐波、内圈看转频边带。返回连续达标谐波的「超噪倍数」之和（log）。"""
    df = f[1] - f[0]
    if ff <= 0 or ff * 1.02 >= f[-1]:
        return float("nan")
    score, fc = 0.0, ff
    for k in range(1, max_h + 1):
        tol = 0.02 if k == 1 else 0.01
        lo, hi = int((k * fc * (1 - tol)) / df), int((k * fc * (1 + tol)) / df) + 1
        if hi >= S.size - 3 or hi <= lo:
            break
        j = lo + int(np.argmax(S[lo:hi]))
        noise = _local_noise(S, j)
        ratio = S[j] / noise
        if k == 1:
            fc = f[j]                      # ★以实测峰重定特征频率（滑移 1~2%）
        if ratio < harm_thr:
            break
        if inner and shaft > 0:
            ok_side = False
            for sgn in (-1, 1):
                c = f[j] + sgn * shaft
                w = (f >= c - 2 * df) & (f <= c + 2 * df)
                if w.any() and S[w].max() >= side_thr * noise:
                    ok_side = True
            if not ok_side:
                break
        score += ratio
    return float(np.log1p(score))


# ── 特征分发 ─────────────────────────────────────────────────────
METHOD = "A"


def feat(x: np.ndarray, fs: float, ff: float, shaft: float, ftype: int) -> float:
    if METHOD == "A":
        return E.env_snr(x, fs, ff)
    if METHOD == "B":
        f, S = ses(cpw(x), fs, 1.0, 0.49 * fs)
        return simple_read(f, S, ff)
    if METHOD == "C":
        lo, hi = sk_band(x, fs)
        f, S = ses(x, fs, lo, hi)
        return simple_read(f, S, ff)
    if METHOD == "D":
        f, S = ses(cpw(x), fs, 1.0, 0.49 * fs)
        return expert_read(f, S, ff, shaft, inner=(ftype == 1))
    raise ValueError(METHOD)


def side_feat(m: dict, side: str, ftype: int):
    s = m[side]
    raw = raw_list(s)
    fs = np.atleast_1d(np.asarray(s.samplingRate, dtype=float))
    rpm = np.atleast_1d(np.asarray(s.RPM, dtype=float))
    lab = np.atleast_1d(np.asarray(s.label))
    t = np.array([np.datetime64(str(x)[:19], "s") for x in np.atleast_1d(np.asarray(s.time))])
    order = float(getattr(s.faultFrequencies, E.ORDER_KEY[ftype]))
    v = np.array([feat(raw[i], fs[i], order * rpm[i] / 60.0, rpm[i] / 60.0, ftype) for i in range(len(raw))])
    o = np.argsort(t)
    return dict(t=t[o], e=v[o], lab=lab[o])


def main(root: str) -> None:
    E.side_env = side_feat          # 复用实验三的主流程与判法，只换特征
    E.main(root)


if __name__ == "__main__":
    for mth in (sys.argv[2:] or ["A", "B", "C", "D"]):
        METHOD = mth
        print(f"== 方法 {mth}")
        main(sys.argv[1])
