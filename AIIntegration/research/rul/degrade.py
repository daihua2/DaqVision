"""看标量与谱分箱各自对退化的敏感度：寿命最后 20% 与最初 20% 的中位数之比。
比值远大于 1 ⇒ 该特征随退化上升，能被模型利用。"""
import numpy as np
from pathlib import Path
F = Path("/root/AIRef/femto-feat")
NAMES = ["acc_rms_h", "vel_rms_h", "dis_rms_h", "dom_f_h",
         "acc_rms_v", "vel_rms_v", "dis_rms_v", "dom_f_v"]
BEAR = ["Bearing1_1", "Bearing1_2", "Bearing2_1", "Bearing2_2", "Bearing3_1", "Bearing3_2"]

print("标量：末 20% 中位 / 初 20% 中位")
print(f"{'轴承':<13}" + "".join(f"{n:>11}" for n in NAMES))
rows = []
for b in BEAR:
    f = F / f"{b}.npz"
    if not f.exists(): continue
    s = np.load(f)["scal"]; n = len(s); k = max(1, n // 5)
    r = np.median(s[-k:], 0) / np.maximum(np.median(s[:k], 0), 1e-9)
    rows.append(r)
    print(f"{b:<13}" + "".join(f"{v:>11.2f}" for v in r))
if rows:
    print(f"{'中位数':<13}" + "".join(f"{v:>11.2f}" for v in np.median(rows, 0)))

print("\n谱分箱 20 维：同样的比值（只报 6 个统计量）")
print(f"{'轴承':<13}{'最小':>9}{'中位':>9}{'最大':>9}{'>2的箱数':>10}")
for b in BEAR:
    f = F / f"{b}.npz"
    if not f.exists(): continue
    s = np.load(f)["spec"]; n = len(s); k = max(1, n // 5)
    r = np.median(s[-k:], 0) / np.maximum(np.median(s[:k], 0), 1e-9)
    print(f"{b:<13}{r.min():>9.2f}{np.median(r):>9.2f}{r.max():>9.2f}{int((r>2).sum()):>10}")
