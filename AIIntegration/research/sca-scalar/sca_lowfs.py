"""实验七：SCA 案例 1、3（内圈、采样率 640/512 Hz）为什么漏 —— 两个假设分开验。

用法：~/research-venv/bin/python sca_lowfs.py /mnt/d/download/AIRef/datasets/sca

由来：实验五里规则在这两例漏报。算一下：案例 1 转频约 20 Hz、BPFI 约 180 Hz，而 0.2~0.45 fs 的「共振带」
只有 128~288 Hz —— 故障频率本身就在解调带里，低采样率下根本没有高频共振带可解调。

  甲：内圈要求 ±转频边带，这条在此过严 ⇒ 去掉边带要求再看；
  乙：低采样率下该看**原始频谱**而非包络谱 ⇒ 直接在幅值谱上用同一规则。
  对照：同期健康侧、健康段（train）触发率 —— 放宽后误报涨多少，一并报。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

import sca_envelope as E
from sca_baseline import load, raw_list, sides_of
from sca_methods import ses
from sca_rule import n_harm


def spectrum(x: np.ndarray, fs: float):
    x = x - x.mean()
    S = np.abs(np.fft.rfft(x * np.hanning(x.size))) / x.size
    return np.fft.rfftfreq(x.size, 1.0 / fs), S


def counts(m, side, ftype, variant):
    s = m[side]
    raw = raw_list(s)
    fs = np.atleast_1d(np.asarray(s.samplingRate, dtype=float))
    rpm = np.atleast_1d(np.asarray(s.RPM, dtype=float))
    lab = np.atleast_1d(np.asarray(s.label))
    t = np.array([np.datetime64(str(x)[:19], "s") for x in np.atleast_1d(np.asarray(s.time))])
    order = float(getattr(s.faultFrequencies, E.ORDER_KEY[ftype]))
    out = []
    for i in range(len(raw)):
        ff, sh = order * rpm[i] / 60.0, rpm[i] / 60.0
        if variant == "包络+边带":
            f, S = ses(raw[i], fs[i], 0.2 * fs[i], 0.45 * fs[i]); inner = True
        elif variant == "包络无边带":
            f, S = ses(raw[i], fs[i], 0.2 * fs[i], 0.45 * fs[i]); inner = False
        elif variant == "原始谱+边带":
            f, S = spectrum(raw[i], fs[i]); inner = True
        else:
            f, S = spectrum(raw[i], fs[i]); inner = False
        out.append(n_harm(f, S, ff, sh, inner and ftype == 1))
    o = np.argsort(t)
    return t[o], np.array(out)[o], lab[o], order, float(np.median(rpm)) / 60.0


def main(root: str) -> None:
    rp = Path(root)
    for case in (1, 3, 4, 6):     # 1、3 为漏的两例；4、6 为同为内圈、采样率高的对照
        mtr, mte = load(rp / str(case) / "train.mat"), load(rp / str(case) / "test.mat")
        origin = str(np.asarray(mte["faultOrigin"]).item())
        ftype = int(np.asarray(mte["faultType"]).item())
        sides = [s for s in sides_of(mte) if s in sides_of(mtr)]
        fs = float(np.atleast_1d(mte[origin].samplingRate)[0])
        for v in ("包络+边带", "包络无边带", "原始谱+边带", "原始谱无边带"):
            t, c, lab, order, shaft = counts(mte, origin, ftype, v)
            fault = (lab > 0)
            hit = float(np.mean(c[fault] >= 2))
            _, ctr, ltr, _, _ = counts(mtr, origin, ftype, v)
            fa = float(np.mean(ctr[ltr == 0] >= 2))
            oth = []
            for sd in sides:
                if sd == origin:
                    continue
                ot, oc, ol, _, _ = counts(mte, sd, ftype, v)
                sel = (ol >= 0) & (ot >= t[fault][0]) & (ot <= t[fault][-1])
                if sel.any():
                    oth.append(f"{sd} {np.mean(oc[sel] >= 2):.2f}")
            print(f"案例 {case:>2} fs={fs:>6.0f} 转频≈{shaft:5.1f}Hz BPFI≈{order * shaft:6.1f}Hz | {v:<7} | "
                  f"故障段 {hit:4.2f} | 同期健康侧 {', '.join(oth) or '无':<10} | 健康段误报 {fa:4.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
