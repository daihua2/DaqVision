"""最后一步：实验 A（原项目 processed）与 A2（我方重建）唯一剩下的差别是目标方向。

原项目 create_x_y 第 1 列 = i / run_time，i 是龄期 ⇒ 这是「已消耗百分比」，
而注释/函数名/论文都称其为「剩余寿命」。我方 A2 用的是真正的剩余百分比。

⇒ 只换目标方向，其余全同。若威布尔版在 consumed 下重新领先，即坐实：
  「威布尔项之所以显得有效，是因为目标恰好是龄期的单调增函数，
    而威布尔项的参考点就是龄期 —— 它等于把答案的一半直接喂了进去。」
"""
import sys, statistics as st
from pathlib import Path
import numpy as np, torch, torch.nn as nn

REPO = Path("/root/AIRef/weibull-knowledge-informed-ml")
sys.path.insert(0, str(REPO))
from src.models.model import Net
from src.models.loss import WeibullLossRMSE, RMSELoss

F = Path("/root/AIRef/femto-feat")
TRAIN = ["Bearing1_1", "Bearing2_1", "Bearing3_1"]
VAL   = ["Bearing1_2", "Bearing2_2", "Bearing3_2"]
TEST  = ["Bearing1_3", "Bearing2_3", "Bearing3_3"]
TRUNC = {"Bearing1_3": 1802, "Bearing2_3": 1202, "Bearing3_3": 352}

def assemble(names, target, trunc=False):
    X, D, Y = [], [], []
    for n in names:
        z = np.load(F / f"{n}.npz"); spec, secs = z["spec"], z["secs"]
        if trunc:
            k = TRUNC[n]; spec, secs = spec[:k], secs[:k]
        d = secs / 86400.0; run = d[-1]
        X.append(spec); D.append(d)
        Y.append(d / run if target == "consumed" else (run - d) / run)
    c = lambda a: torch.tensor(np.concatenate(a)).float()
    return c(X), c(D).unsqueeze(1), c(Y).unsqueeze(1)

rmse_l, mse_l, w_orig = RMSELoss(), nn.MSELoss(), WeibullLossRMSE()
ETA, BETA, LAM = 0.1998, 2.0, 2.0

def run(kind, target, seed, epochs=300, patience=30, bs=256):
    torch.manual_seed(seed); np.random.seed(seed)
    xtr, dtr, ytr = assemble(TRAIN, target)
    xva, _, yva   = assemble(VAL, target)
    xtt, _, ytt   = assemble(TEST, target, trunc=True)
    xtf, _, ytf   = assemble(TEST, target, trunc=False)
    lo, hi = xtr.min(), xtr.max()                  # 原项目的全局 min-max
    xtr, xva, xtt, xtf = ((a - lo) / (hi - lo) for a in (xtr, xva, xtt, xtf))
    net = Net(feat_in=xtr.shape[1], n_layers=4, n_units=128, prob_drop=0.1)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    def lf(yh, y, d):
        if kind == "mse":         return mse_l(yh, y)
        if kind == "rmse+w_orig": return rmse_l(yh, y) + w_orig(yh, y, d, LAM, ETA, BETA)
        if kind == "w_only_orig": return w_orig(yh, y, d, LAM, ETA, BETA)
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

print("常数基线 0.2889（两种目标方向下相同，因两者互为 1−x）\n")
print(f"{'目标':<12}{'损失':<16}{'val':>9}{'test截断':>10}{'test完整':>10}")
print("-" * 57)
res = {}
for target in ("remaining", "consumed"):
    for kind in ("mse", "rmse+w_orig", "w_only_orig"):
        o = [x for x in (run(kind, target, s) for s in (1,2,3,4,5)) if x]
        if not o: print(f"{target:<12}{kind:<16} 全部发散"); continue
        m = [st.median([x[i] for x in o]) for i in range(3)]
        res[(target, kind)] = m
        print(f"{target:<12}{kind:<16}{m[0]:>9.4f}{m[1]:>10.4f}{m[2]:>10.4f}")
print()
for target in ("remaining", "consumed"):
    if (target, "mse") not in res: continue
    b = res[(target, "mse")]
    print(f"[{target:<9}] 威布尔版相对 mse（负=威布尔更好）：" + "   ".join(
        f"{k[1]} val{(v[0]-b[0])/b[0]*100:+.1f}% test截断{(v[1]-b[1])/b[1]*100:+.1f}%"
        for k, v in res.items() if k[0] == target and k[1] != "mse"))
