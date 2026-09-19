"""实验五：专家判定规则按它**本来的用法**评 —— 规则触发与否，不套基线 z 分数。

用法：~/research-venv/bin/python sca_rule.py /mnt/d/download/AIRef/datasets/sca

由来：实验四把规则得分（多数为 0，近乎二值）塞进稳健 z 分数里评，框架不对 —— 规则本来就是
「判有没有」的**绝对**判定，不需要基线。这里直接数触发率：
  · 触发 = 平方包络谱上，故障特征频率（±2% 重定、此后 ±1%）**连续 ≥ N 次谐波**各自 ≥3 倍局部噪底；
    内圈另要求 ±转频边带 ≥2 倍噪底；
  · 两种前处理：CPW 全带 / 固定带通 0.2~0.45 fs；N = 1、2 各看一遍；
  · 报：故障侧故障段触发率、同期健康侧触发率、健康段（train）触发率（= 规则的误报）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

import sca_envelope as E
from sca_baseline import load, raw_list, sides_of
from sca_methods import _local_noise, cpw, ses


#: 内圈是否把「±转频边带」当硬门槛。实验七：当门槛只增漏报、不减误报 ⇒ 可关（命令行 noside）。
SIDEBAND_GATE = True


def n_harm(f, S, ff, shaft, inner, harm_thr=3.0, side_thr=2.0, max_h=6) -> int:
    df = f[1] - f[0]
    if ff <= 0 or ff * 1.02 >= f[-1]:
        return 0
    fc, n = ff, 0
    for k in range(1, max_h + 1):
        tol = 0.02 if k == 1 else 0.01
        lo, hi = int(k * fc * (1 - tol) / df), int(k * fc * (1 + tol) / df) + 1
        if hi >= S.size - 3 or hi <= lo:
            break
        j = lo + int(np.argmax(S[lo:hi]))
        noise = _local_noise(S, j)
        if k == 1:
            fc = f[j]
        if S[j] < harm_thr * noise:
            break
        if inner and shaft > 0 and SIDEBAND_GATE:
            ok = any(((w := (f >= f[j] + s * shaft - 2 * df) & (f <= f[j] + s * shaft + 2 * df)).any()
                      and S[w].max() >= side_thr * noise) for s in (-1, 1))
            if not ok:
                break
        n += 1
    return n


def side_counts(m, side, ftype, pre):
    s = m[side]
    raw = raw_list(s)
    fs = np.atleast_1d(np.asarray(s.samplingRate, dtype=float))
    rpm = np.atleast_1d(np.asarray(s.RPM, dtype=float))
    lab = np.atleast_1d(np.asarray(s.label))
    t = np.array([np.datetime64(str(x)[:19], "s") for x in np.atleast_1d(np.asarray(s.time))])
    order = float(getattr(s.faultFrequencies, E.ORDER_KEY[ftype]))
    out = []
    for i in range(len(raw)):
        x = raw[i]
        if pre == "cpw":
            f, S = ses(cpw(x), fs[i], 1.0, 0.49 * fs[i])
        else:
            f, S = ses(x, fs[i], 0.2 * fs[i], 0.45 * fs[i])
        out.append(n_harm(f, S, order * rpm[i] / 60.0, rpm[i] / 60.0, ftype == 1))
    o = np.argsort(t)
    return t[o], np.array(out)[o], lab[o]


def main(root: str) -> None:
    rp = Path(root)
    for pre in ("band", "cpw"):
        for N in (1, 2):
            print(f"== 前处理 {pre}，触发 = 连续 ≥{N} 次谐波")
            print("case 故障侧 | 故障段触发 | 同期健康侧触发 | 健康段(train)触发=误报")
            for case in range(1, 11):
                mtr, mte = load(rp / str(case) / "train.mat"), load(rp / str(case) / "test.mat")
                origin = str(np.asarray(mte["faultOrigin"]).item())
                ftype = int(np.asarray(mte["faultType"]).item())
                sides = [s for s in sides_of(mte) if s in sides_of(mtr)]
                if origin not in sides or ftype not in E.ORDER_KEY:
                    continue
                t, c, lab = side_counts(mte, origin, ftype, pre)
                run = lab >= 0
                fault = (lab > 0) & run
                hit = float(np.mean(c[fault] >= N))
                _, ctr, ltr = side_counts(mtr, origin, ftype, pre)
                fa = float(np.mean(ctr[ltr == 0] >= N))
                others = []
                for sd in sides:
                    if sd == origin:
                        continue
                    ot, oc, ol = side_counts(mte, sd, ftype, pre)
                    sel = (ol >= 0) & (ot >= t[fault][0]) & (ot <= t[fault][-1])
                    if sel.any():
                        others.append(f"{sd} {np.mean(oc[sel] >= N):.2f}")
                print(f"{case:>4} {origin:<5} | {hit:9.2f} | {', '.join(others) or '无':<14} | {fa:6.2f}")


if __name__ == "__main__":
    if "noside" in sys.argv[2:]:
        SIDEBAND_GATE = False
    print(f"== 内圈边带门槛 {'开' if SIDEBAND_GATE else '关'}")
    main(sys.argv[1])
