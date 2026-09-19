"""实验六：拿 CWRU 校我方自写实现 —— 文献在这批数据上有公认结果，我方实现若在这里也失灵，就是写错了。

用法：~/research-venv/bin/python cwru_check.py /mnt/d/download/AIRef/datasets/cwru

由来：实验四、五里 CPW 在 SCA 上一直最差，与 Smith & Randall 2015（CWRU 基准，CPW 最好）相反。
两种可能：数据不同，或**我方实现有错**。CWRU 就是那篇基准的数据 ⇒ 在这里校。

参照（文献大意【文】）：12 kHz 驱动端 —— 内圈、外圈多数诊断得出；滚动体多数诊断不出。
CWRU 驱动端轴承 6205-2RS JEM SKF，故障阶次（CWRU 官网）：BPFI 5.4152、BPFO 3.5848、滚动体 4.7135、FTF 0.39828。

做法：每文件取 DE 通道，切 1 秒一段（12000 点，包络谱分辨率 1 Hz），逐段按实验五的规则判触发；
正常文件（48 kHz）降采样到 12 kHz 后同样判，作误报。转速取文件里的 *RPM，没有则按负载 0/1/2/3 → 1797/1772/1750/1730。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import scipy.io as sio
from scipy.signal import decimate

from sca_methods import cpw, ies, ses, sk_band
import sca_rule
from sca_rule import n_harm

ORD = {"IR": 5.4152, "OR": 3.5848, "B": 4.7135}
RPM_BY_LOAD = {0: 1797.0, 1: 1772.0, 2: 1750.0, 3: 1730.0}
SEG = 12000


def read(path: Path):
    m = sio.loadmat(str(path))
    de = next((m[k].ravel() for k in m if k.endswith("_DE_time")), None)
    rk = next((k for k in m if k.endswith("RPM")), None)
    rpm = float(np.asarray(m[rk]).ravel()[0]) if rk else None
    if rpm is None or not np.isfinite(rpm) or rpm < 100:
        load = int(re.search(r"_(\d)\.mat$", path.name).group(1))
        rpm = RPM_BY_LOAD[load]
    return de, rpm


def trig_rate(x: np.ndarray, fs: float, rpm: float, kind: str, pre: str, N: int) -> float:
    ff, shaft = ORD[kind] * rpm / 60.0, rpm / 60.0
    hits = []
    for i in range(0, x.size - SEG + 1, SEG):
        seg = x[i:i + SEG]
        if pre == "band":
            f, S = ses(seg, fs, 0.2 * fs, 0.45 * fs)
        elif pre == "cpw":
            f, S = ses(cpw(seg), fs, 1.0, 0.49 * fs)
        elif pre == "cpwband":
            # CPW 之后仍只在共振带里解调（文献里「CPW + 高通」一路的思路）
            f, S = ses(cpw(seg), fs, 0.2 * fs, 0.45 * fs)
        elif pre == "ies":
            f, S = ies(seg, fs)
        else:  # sk
            lo, hi = sk_band(seg, fs)
            f, S = ses(seg, fs, lo, hi)
        hits.append(n_harm(f, S, ff, shaft, inner=(kind == "IR")) >= N)
    return float(np.mean(hits)) if hits else float("nan")


def main(root: str) -> None:
    rp = Path(root)
    fs = 12000.0
    pres = tuple(p for p in sys.argv[2:] if p != "noside") or ("band", "cpw", "cpwband", "sk")
    N = 2
    print(f"触发 = 连续 ≥{N} 次谐波 ≥3× 局部噪底（内圈另要转频边带）；每格 = 1 秒段中的触发比例")
    print("文件                         " + "  ".join(f"{p:>5}" for p in pres))
    agg: dict[tuple[str, str], list[float]] = {}
    for path in sorted((rp / "12k_Drive_End_Bearing_Fault_Data").rglob("*.mat")):
        kind = path.relative_to(rp / "12k_Drive_End_Bearing_Fault_Data").parts[0]
        x, rpm = read(path)
        if x is None:
            continue
        row = [trig_rate(x, fs, rpm, kind, p, N) for p in pres]
        for p, r in zip(pres, row):
            agg.setdefault((kind, p), []).append(r)
        rel = str(path.relative_to(rp / "12k_Drive_End_Bearing_Fault_Data"))
        print(f"{rel:<28} " + "  ".join(f"{r:5.2f}" for r in row))
    print("== 正常文件（48k→12k），对三种故障频率各判一次 = 误报")
    for path in sorted((rp / "Normal").glob("*.mat")):
        x, rpm = read(path)
        x = decimate(x, 4, ftype="fir")
        for kind in ORD:
            row = [trig_rate(x, fs, rpm, kind, p, N) for p in pres]
            for p, r in zip(pres, row):
                agg.setdefault(("正常→" + kind, p), []).append(r)
            print(f"{path.name + ' 按' + kind:<28} " + "  ".join(f"{r:5.2f}" for r in row))
    print("== 汇总：各类文件中「过半段触发」的文件比例")
    for (kind, p), rs in sorted(agg.items()):
        print(f"{kind:<8} {p:<5} {np.mean(np.array(rs) > 0.5):.2f}  (n={len(rs)}, 平均触发 {np.mean(rs):.2f})")


if __name__ == "__main__":
    if "noside" in sys.argv[2:]:
        sca_rule.SIDEBAND_GATE = False
    print(f"== 内圈边带门槛 {'开' if sca_rule.SIDEBAND_GATE else '关'}")
    main(sys.argv[1])
