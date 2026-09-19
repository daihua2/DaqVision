"""实验二：「近期基线」—— 同一段数据里、故障标签出现之前的正常测量建基线，看故障段报不报得出。

用法：~/research-venv/bin/python sca_recent.py /mnt/d/download/AIRef/datasets/sca

由来：实验一（sca_baseline.py）里 SCA 的健康段（train）比故障段（test）**晚 7~20 个月**（换新轴承之后采的），
几个月前的基线与现在比，报警多少主要看隔了多久，而非有没有故障。这里改为**紧挨着故障之前**的正常段建基线，
即待议 D3 的候选处置「滑动近期基线」。

只对「故障侧在 test 里先有 ≥10 次正常、后有故障」的案例做；误报 = 正常段按时间对半，前半建、后半算。
另报「同期健康侧」的报警率作对照（同一基线口径、同一时段）—— 故障侧报得多、健康侧报得少，才算分得开。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from sca_baseline import Z_NOTABLE, load, robust, side_data, sides_of

MIN_NORMAL = 10


def main(root: str) -> None:
    rp = Path(root)
    print("case 故障侧 n_norm FA_half | n_f hit(全段基线) | 健康侧同期报警率 | 首报距末次(d) 故障标签距末次(d)")
    for case in range(1, 12):
        mte = load(rp / str(case) / "test.mat")
        origin = str(np.asarray(mte["faultOrigin"]).item())
        sides = sides_of(mte)
        if origin not in sides:
            continue
        fs_min = min(float(np.min(np.atleast_1d(mte[s].samplingRate))) for s in sides)
        band = (10.0, min(1000.0, 0.9 * fs_min / 2))
        d = side_data(mte, origin, band)
        run = d["lab"] >= 0
        t, v, lab = d["t"][run], d["v"][run], d["lab"][run]
        if not (lab > 0).any():
            continue
        onset = int(np.argmax(lab > 0))
        pre = v[:onset][lab[:onset] == 0]
        if len(pre) < MIN_NORMAL:
            print(f"{case:>4} {origin:<5} 故障前正常段只有 {len(pre)} 次，不足 {MIN_NORMAL}，跳过")
            continue
        half = len(pre) // 2
        m1, s1 = robust(pre[:half])
        fa_half = float(np.mean((pre[half:] - m1) / s1 >= Z_NOTABLE))
        med, sc = robust(pre)
        z = (v - med) / sc
        fault = lab > 0
        hit = float(np.mean(z[fault] >= Z_NOTABLE))
        # 同期健康侧：用它自己「故障出现前」同一时段的正常测量建基线，看故障时段它报多少
        other = []
        t_on = t[onset]
        for s in sides:
            if s == origin:
                continue
            o = side_data(mte, s, band)
            r = o["lab"] >= 0
            ot, ov = o["t"][r], o["v"][r]
            ob = ov[ot < t_on]
            oa = ov[ot >= t_on]
            if len(ob) >= MIN_NORMAL and len(oa):
                om, osc = robust(ob)
                other.append(f"{s} {np.mean((oa - om) / osc >= Z_NOTABLE):.2f}")
        first = next((i for i in range(onset, len(z)) if z[i] >= Z_NOTABLE), None)
        end = t[-1]
        lead = "-" if first is None else f"{float((end - t[first]) / np.timedelta64(1, 'D')):.0f}"
        onset_d = f"{float((end - t_on) / np.timedelta64(1, 'D')):.0f}"
        print(f"{case:>4} {origin:<5} {len(pre):>6} {fa_half:7.2f} | {int(fault.sum()):>3} {hit:6.2f}        | "
              f"{', '.join(other) or '无':<16} | {lead:>10} {onset_d:>14}")


if __name__ == "__main__":
    main(sys.argv[1])
