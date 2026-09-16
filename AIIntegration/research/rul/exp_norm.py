"""补充实验：实验 A（原项目 processed 数据）上威布尔版领先 41%，
实验 A2（我方重建、按列归一化）上没复现。差别在哪？
候选：① 归一化方式（原项目全局 min-max vs 我方按列）② 排序/管线细节。
这里只改归一化，其余全同 ⇒ 若翻转随之出现，即归一化所致。
"""
import sys, time, statistics as st
from pathlib import Path
import numpy as np, torch, torch.nn as nn

REPO = Path("/root/AIRef/weibull-knowledge-informed-ml")
sys.path.insert(0, str(REPO))
from src.models.model import Net
from src.models.loss import WeibullLossRMSE, RMSELoss

FEAT = Path("/root/AIRef/femto-feat")
TRAIN = ["Bearing1_1", "Bearing2_1", "Bearing3_1"]
VAL   = ["Bearing1_2", "Bearing2_2", "Bearing3_2"]
TEST  = ["Bearing1_3", "Bearing2_3", "Bearing3_3"]
TRUNC = {"Bearing1_3": 1802, "Bearing2_3": 1202, "Bearing3_3": 352}

def load(n):
    d = np.load(FEAT / f"{n}.npz"); return d["spec"], d["scal"], d["secs"]

def assemble(names, which, trunc=False):
    X, D, Y = [], [], []
    for n in names:
        spec, scal, secs = load(n)
        if trunc:
            k = TRUNC[n]; spec, scal, secs = spec[:k], scal[:k], secs[:k]
        days = secs / 86400.0; run = days[-1]
        X.append(spec if which == "spec" else scal)
        D.append(days); Y.append((run - days) / run)
    c = lambda a: torch.tensor(np.concatenate(a)).float()
    return c(X), c(D).unsqueeze(1), c(Y).unsqueeze(1)

rmse_l, mse_l, w_orig = RMSELoss(), nn.MSELoss(), WeibullLossRMSE()
ETA, BETA, LAM = 0.1998, 2.0, 2.0

def run(which, kind, mode, seed, epochs=300, patience=30, bs=256):
    torch.manual_seed(seed); np.random.seed(seed)
    xtr, dtr, ytr = assemble(TRAIN, which)
    xva, _, yva   = assemble(VAL, which)
    xtt, _, ytt   = assemble(TEST, which, trunc=True)
    xtf, _, ytf   = assemble(TEST, which, trunc=False)
    if mode == "global":                       # ★原项目做法：整个数组一个 min/max
        lo, hi = xtr.min(), xtr.max()
    else:                                      # 按列
        lo, hi = xtr.min(0).values, xtr.max(0).values
    ap = lambda x: ((x - lo) / (hi - lo)).clamp(0.0, 1.0) if mode == "column" else (x - lo) / (hi - lo)
    xtr, xva, xtt, xtf = (ap(a) for a in (xtr, xva, xtt, xtf))
    net = Net(feat_in=xtr.shape[1], n_layers=4, n_units=128, prob_drop=0.1)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    def lf(yh, y, d):
        if kind == "mse":          return mse_l(yh, y)
        if kind == "rmse+w_orig":  return rmse_l(yh, y) + w_orig(yh, y, d, LAM, ETA, BETA)
        if kind == "w_only_orig":  return w_orig(yh, y, d, LAM, ETA, BETA)
    best, state, bad, n = float("inf"), None, 0, len(xtr)
    for _ in range(epochs):
        net.train(); perm = torch.randperm(n)
        for i in range(0, n, bs):
            j = perm[i:i+bs]; opt.zero_grad()
            l = lf(net(xtr[j]), ytr[j], dtr[j])
            if not torch.isfinite(l): return None
            l.backward(); opt.step()
        net.eval()
        with torch.no_grad(): v = mse_l(net(xva), yva).item()
        if v < best - 1e-6: best, bad, state = v, 0, {k: t.clone() for k, t in net.state_dict().items()}
        else:
            bad += 1
            if bad >= patience: break
    net.load_state_dict(state); net.eval()
    with torch.no_grad():
        r = lambda x, y: torch.sqrt(mse_l(net(x), y)).item()
        return best ** 0.5, r(xtt, ytt), r(xtf, ytf)

_, _, y0 = assemble(TRAIN, "spec"); C = float(y0.mean())
print(f"常数基线 C={C:.4f}")
for label, names, tr in (("val", VAL, False), ("test截断", TEST, True), ("test完整", TEST, False)):
    _, _, yy = assemble(names, "spec", trunc=tr)
    print(f"  {label:<9} {float(((yy-C)**2).mean()**0.5):.4f}")
print()
print(f"{'归一化':<9}{'损失':<14}{'val':>9}{'test截断':>10}{'test完整':>10}")
print("-" * 54)
res = {}
for mode in ("global", "column"):
    for kind in ("mse", "rmse+w_orig", "w_only_orig"):
        o = [x for x in (run("spec", kind, mode, s) for s in (1,2,3,4,5)) if x]
        if not o: print(f"{mode:<9}{kind:<14} 全部发散"); continue
        m = [st.median([x[i] for x in o]) for i in range(3)]
        res[(mode, kind)] = m
        print(f"{mode:<9}{kind:<14}{m[0]:>9.4f}{m[1]:>10.4f}{m[2]:>10.4f}")
print()
for mode in ("global", "column"):
    sub = {k[1]: v for k, v in res.items() if k[0] == mode}
    if "mse" not in sub: continue
    print(f"[{mode}] 威布尔版相对 mse 的 test截断 变化："
          + "  ".join(f"{k} {(v[1]-sub['mse'][1])/sub['mse'][1]*100:+.1f}%"
                      for k, v in sub.items() if k != "mse"))
