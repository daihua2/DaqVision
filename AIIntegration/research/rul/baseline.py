"""★没有这条基线，整张实验表都无法解读：
一个什么都不学、永远输出常数的预测器，RMSE 是多少？
模型只有明显低于它，才算真的学到了东西。"""
import sys
from pathlib import Path
import h5py, numpy as np

P = Path("/root/AIRef/weibull-knowledge-informed-ml/data/processed/FEMTO")
def y(n):
    with h5py.File(P / f"y_{n}.hdf5") as f:
        return f[f"y_{n}"][:][:, 1]          # 剩余百分比

ytr, yva, yte = y("train"), y("val"), y("test")
c_tr = ytr.mean()
print(f"train 剩余百分比：均值 {c_tr:.4f}  标准差 {ytr.std():.4f}  n={len(ytr)}")
print()
print(f"{'集合':<8}{'n':>7}{'均值':>9}{'常数=train均值':>16}{'常数=本集最优':>16}")
for name, yy in (("train", ytr), ("val", yva), ("test", yte)):
    r_tr = float(np.sqrt(np.mean((yy - c_tr) ** 2)))      # 用 train 均值预测（诚实）
    r_best = float(yy.std())                               # 用本集自己的均值（作弊上限）
    print(f"{name:<8}{len(yy):>7}{yy.mean():>9.4f}{r_tr:>16.4f}{r_best:>16.4f}")

print()
print("对照实验 A 的中位 test RMSE：")
for k, v in [("mse", .2854), ("rmse", .2790), ("w_only_orig", .1701),
             ("rmse+w_orig", .1684), ("rmse+w_fixed", .2959)]:
    base = float(np.sqrt(np.mean((yte - c_tr) ** 2)))
    print(f"  {k:<16}{v:.4f}   相对常数基线 {(v-base)/base*100:+6.1f}%"
          f"{'   ← 不如常数' if v >= base else ''}")
