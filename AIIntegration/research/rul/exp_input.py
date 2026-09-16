"""实验 A2（决定性检验）+ 实验 B，一次跑完。

A2：同一批模型，在两个 test 上评 ——
    test_trunc  = 截断集（原项目口径：把截断点当失效，最后一帧标"剩余 0%"）
    test_full   = 完整集（真实失效，标签正确）
  ⇒ 若排名翻转，坐实「原项目 test 指标衡量的不是 RUL 预测能力」。

B ：同一网络、同一划分、同一损失，只换输入 ——
    spec(20维 谱分箱)  vs  scal(8维 我方传感器风格标量)
  ⇒ 回答「我方只有标量、没有波形，RUL 还能不能做、差多少」。

纪律：早停一律用 val 的 MSE（不同损失间才可比）；多种子报中位数；不挑最好的报。
"""
import sys, time, statistics as st
from pathlib import Path

import numpy as np, torch, torch.nn as nn

REPO = Path("/root/AIRef/weibull-knowledge-informed-ml")
sys.path.insert(0, str(REPO))
from src.models.model import Net
from src.models.loss import WeibullLossRMSE, RMSELoss

FEAT = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/AIRef/femto-feat")
TRAIN = ["Bearing1_1", "Bearing2_1", "Bearing3_1"]
VAL   = ["Bearing1_2", "Bearing2_2", "Bearing3_2"]
TEST  = ["Bearing1_3", "Bearing2_3", "Bearing3_3"]
TRUNC = {"Bearing1_3": 1802, "Bearing2_3": 1202, "Bearing3_3": 352}   # 原项目截断集的帧数（实测）

def load(name):
    d = np.load(FEAT / f"{name}.npz")
    return d["spec"], d["scal"], d["secs"]

def labels(secs):
    """→ (龄期天, 剩余百分比, 总寿命天)"""
    days = secs / 86400.0
    run = days[-1]
    return days, (run - days) / run, np.full_like(days, run)

def assemble(names, which, trunc=False):
    X, D, Y, T = [], [], [], []
    for n in names:
        spec, scal, secs = load(n)
        if trunc:
            k = TRUNC[n]; spec, scal, secs = spec[:k], scal[:k], secs[:k]
        f = spec if which == "spec" else scal
        d, y, t = labels(secs)
        X.append(f); D.append(d); Y.append(y); T.append(t)
    cat = lambda a: torch.tensor(np.concatenate(a)).float()
    return (cat(X), cat(D).unsqueeze(1), cat(Y).unsqueeze(1), cat(T).unsqueeze(1))

def norm_fit(x):
    return x.min(0).values, x.max(0).values          # ★按列 min-max（标量各维量纲差别大）
def norm_apply(x, lo, hi):
    return ((x - lo) / (hi - lo).clamp(min=1e-12)).clamp(0.0, 1.0)

def create_eta(t_array, beta, r):
    return float((np.sum(t_array ** beta) / r) ** (1.0 / beta))

rmse_l, mse_l = RMSELoss(), nn.MSELoss()
w_orig = WeibullLossRMSE()
LAM = 2.0

def make_loss(kind, eta, beta):
    def f(yh, y, d):
        if kind == "mse":          return mse_l(yh, y)
        if kind == "rmse":         return rmse_l(yh, y)
        if kind == "w_only_orig":  return w_orig(yh, y, d, LAM, eta, beta)
        if kind == "rmse+w_orig":  return rmse_l(yh, y) + w_orig(yh, y, d, LAM, eta, beta)
        raise ValueError(kind)
    return f

def train_eval(which, kind, seed, eta, beta, epochs=300, patience=30, bs=256):
    torch.manual_seed(seed); np.random.seed(seed)
    xtr, dtr, ytr, _ = assemble(TRAIN, which)
    xva, dva, yva, _ = assemble(VAL, which)
    xtf, _, ytf, _   = assemble(TEST, which, trunc=True)
    xff, _, yff, _   = assemble(TEST, which, trunc=False)
    lo, hi = norm_fit(xtr)
    xtr, xva, xtf, xff = (norm_apply(a, lo, hi) for a in (xtr, xva, xtf, xff))

    net = Net(feat_in=xtr.shape[1], n_layers=4, n_units=128, prob_drop=0.1)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    lossf = make_loss(kind, eta, beta)
    best, state, bad, n = float("inf"), None, 0, len(xtr)
    for _ in range(epochs):
        net.train(); perm = torch.randperm(n)
        for i in range(0, n, bs):
            j = perm[i:i+bs]; opt.zero_grad()
            l = lossf(net(xtr[j]), ytr[j], dtr[j])
            if not torch.isfinite(l): return None
            l.backward(); opt.step()
        net.eval()
        with torch.no_grad():
            v = mse_l(net(xva), yva).item()
        if v < best - 1e-6:
            best, bad, state = v, 0, {k: t.clone() for k, t in net.state_dict().items()}
        else:
            bad += 1
            if bad >= patience: break
    net.load_state_dict(state); net.eval()
    with torch.no_grad():
        r = lambda x, y: torch.sqrt(mse_l(net(x), y)).item()
        return best ** 0.5, r(xtf, ytf), r(xff, yff)

# ── η/β 从 train 三台的实际寿命估（同原项目 create_eta，r = 失效台数）──
t_arr = np.array([load(n)[2][-1] / 86400.0 for n in TRAIN])
BETA = 2.0
ETA = create_eta(t_arr, BETA, r=len(t_arr))
print(f"train 三台寿命/天 = {np.round(t_arr,4)}  即 {np.round(t_arr*24,2)} 小时")
print(f"eta={ETA:.4f} 天  beta={BETA}  r={len(t_arr)}  lambda={LAM}")
for w in ("spec", "scal"):
    x, _, _, _ = assemble(TRAIN, w)
    print(f"  {w}: train {tuple(x.shape)}")
print()

# ── ★常数基线：没有它，下面整张表都无法解读 ──────────────────────
_, _, ytr_c, _ = assemble(TRAIN, "spec")
C = float(ytr_c.mean())
print(f"★常数预测器（永远输出 train 均值 {C:.4f}）的 RMSE —— 模型须明显低于它才算学到东西：")
for label, names, tr in (("val", VAL, False), ("test截断", TEST, True), ("test完整", TEST, False)):
    _, _, yy, _ = assemble(names, "spec", trunc=tr)
    print(f"    {label:<10} {float(((yy - C) ** 2).mean() ** 0.5):.4f}   (n={len(yy)})")
print()

SEEDS = [1, 2, 3, 4, 5]
KINDS = ["mse", "rmse", "w_only_orig", "rmse+w_orig"]
print(f"{'输入':<6} {'损失':<14} {'val':>8} {'test截断':>9} {'★test完整':>10}   排名变化")
print("-" * 72)
table = {}
for which in ("spec", "scal"):
    for kind in KINDS:
        t0 = time.time()
        outs = [o for o in (train_eval(which, kind, s, ETA, BETA) for s in SEEDS) if o]
        if not outs:
            print(f"{which:<6} {kind:<14} 全部发散"); continue
        v  = st.median([o[0] for o in outs])
        tt = st.median([o[1] for o in outs])
        tf = st.median([o[2] for o in outs])
        table[(which, kind)] = (v, tt, tf)
        print(f"{which:<6} {kind:<14} {v:>8.4f} {tt:>9.4f} {tf:>10.4f}   ({time.time()-t0:.0f}s)")

print()
for which in ("spec", "scal"):
    sub = {k[1]: v for k, v in table.items() if k[0] == which}
    if not sub: continue
    rk = lambda i: [k for k, _ in sorted(sub.items(), key=lambda kv: kv[1][i])]
    print(f"[{which}] 按 test截断 排名: {' < '.join(rk(1))}")
    print(f"[{which}] 按 test完整 排名: {' < '.join(rk(2))}")
    print(f"[{which}] 按 val      排名: {' < '.join(rk(0))}")
print()
print("★相对常数基线（负=真的学到了东西；正=不如什么都不学）：")
bl = {}
for label, names, tr, i in (("val", VAL, False, 0), ("test截断", TEST, True, 1), ("test完整", TEST, False, 2)):
    _, _, yy, _ = assemble(names, "spec", trunc=tr)
    bl[i] = float(((yy - C) ** 2).mean() ** 0.5)
print(f"{'输入':<6} {'损失':<14} {'val':>9} {'test截断':>10} {'test完整':>10}")
for (which, kind), v in table.items():
    print(f"{which:<6} {kind:<14}" + "".join(
        f"{(v[i]-bl[i])/bl[i]*100:>+9.1f}%" if i == 0 else f"{(v[i]-bl[i])/bl[i]*100:>+10.1f}%"
        for i in (0, 1, 2)))
print()
if ("spec", "rmse") in table and ("scal", "rmse") in table:
    print("★实验 B —— 标量 vs 谱（同网络同划分，越小越好）：")
    for kind in KINDS:
        if (("spec", kind) in table) and (("scal", kind) in table):
            s, c = table[("spec", kind)], table[("scal", kind)]
            print(f"  {kind:<14} val {s[0]:.4f} → {c[0]:.4f} ({(c[0]-s[0])/s[0]*100:+6.1f}%)   "
                  f"test完整 {s[2]:.4f} → {c[2]:.4f} ({(c[2]-s[2])/s[2]*100:+6.1f}%)")
