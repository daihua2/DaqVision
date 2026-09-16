"""实验 A：在 FEMTO 上比 6 种损失，回答两个问题 ——
  Q1 威布尔项到底有没有帮助？
  Q2 我方读出的两处实现问题（mean 后平方、量纲混用）修正后是否更好？
纪律（§6.4）：统一早停判据、统一网络与划分、多种子报中位数，不挑单次最好的报。
"""
import sys, time, itertools, statistics as st
from pathlib import Path

import h5py, numpy as np, torch, torch.nn as nn

REPO = Path("/root/AIRef/weibull-knowledge-informed-ml")
sys.path.insert(0, str(REPO))
from src.models.model import Net                      # 原件，不改
from src.models.loss import (WeibullLossRMSE, WeibullLossRMSLE, WeibullLossMSE,
                             RMSELoss, RMSLELoss)     # 原件，不改

DATA = REPO / "data/processed/FEMTO"
DEV = "cpu"

def load(name):
    with h5py.File(DATA / f"{name}.hdf5") as f:
        return torch.tensor(f[name][:]).float()

X = {s: load(f"x_{s}") for s in ("train", "val", "test")}
Y = {s: load(f"y_{s}") for s in ("train", "val", "test")}
with h5py.File(DATA / "eta_beta_r.hdf5") as f:
    ETA, BETA, _ = f["eta_beta_r"][:]
ETA, BETA = float(ETA), float(BETA)

# y 三列：[龄期(天), 剩余百分比, 剩余寿命(天)]
def split(y):
    return y[:, 0:1], y[:, 1:2], y[:, 0:1] + y[:, 2:3]   # days, pct(目标), total(天)

# ── ★我方修正版：量纲改对 + mean(平方) 而非 mean 后平方 ──────────────
class WeibullFixed(nn.Module):
    """按威布尔 CDF 比「预测龄期」与「真实龄期」的失效概率之差。
    与原件两处不同：
      1 原件 y_hat_days = (y_days + y) − y_hat 把「天」和「百分比」相加；
        这里用 total(天) × (1 − y_hat) 得到预测龄期，量纲自洽。
      2 原件 sqrt(mean(d)**2) 会让批内正负误差抵消；这里 sqrt(mean(d**2))。"""
    def __init__(self, eps=1e-8):
        super().__init__(); self.eps = eps
    def forward(self, y_hat, y_days, total, lambda_mod, eta, beta):
        cdf = lambda t: 1.0 - torch.exp(-((t.clamp(min=0) / eta) ** beta))
        t_hat = total * (1.0 - y_hat)
        d = cdf(t_hat) - cdf(y_days)
        return lambda_mod * torch.sqrt(torch.mean(d ** 2) + self.eps)

class WeibullMeanSqOnly(nn.Module):
    """只改量纲、保留原件的 mean 后平方 —— 用来分离两处问题各自的影响。"""
    def __init__(self, eps=1e-8):
        super().__init__(); self.eps = eps
    def forward(self, y_hat, y_days, total, lambda_mod, eta, beta):
        cdf = lambda t: 1.0 - torch.exp(-((t.clamp(min=0) / eta) ** beta))
        t_hat = total * (1.0 - y_hat)
        d = cdf(t_hat) - cdf(y_days)
        return lambda_mod * torch.sqrt(torch.mean(d) ** 2 + self.eps)

rmse_l, mse_l = RMSELoss(), nn.MSELoss()
w_orig_rmse, w_orig_mse = WeibullLossRMSE(), WeibullLossMSE()
w_fixed, w_dimonly = WeibullFixed(), WeibullMeanSqOnly()
LAM = 2.0

def make_loss(kind):
    def f(y_hat, y, y_days, total):
        if kind == "mse":                 return mse_l(y_hat, y)
        if kind == "rmse":                return rmse_l(y_hat, y)
        if kind == "w_only_orig":         return w_orig_rmse(y_hat, y, y_days, LAM, ETA, BETA)
        if kind == "rmse+w_orig":         return rmse_l(y_hat, y) + w_orig_rmse(y_hat, y, y_days, LAM, ETA, BETA)
        if kind == "w_only_orig_mse":     return w_orig_mse(y_hat, y, y_days, LAM, ETA, BETA)
        if kind == "w_only_fixed":        return w_fixed(y_hat, y_days, total, LAM, ETA, BETA)
        if kind == "rmse+w_fixed":        return rmse_l(y_hat, y) + w_fixed(y_hat, y_days, total, LAM, ETA, BETA)
        if kind == "rmse+w_dimonly":      return rmse_l(y_hat, y) + w_dimonly(y_hat, y_days, total, LAM, ETA, BETA)
        raise ValueError(kind)
    return f

KINDS = ["mse", "rmse", "w_only_orig", "w_only_orig_mse", "rmse+w_orig",
         "w_only_fixed", "rmse+w_fixed", "rmse+w_dimonly"]

def run(kind, seed, epochs=300, patience=30, bs=256):
    torch.manual_seed(seed); np.random.seed(seed)
    net = Net(feat_in=X["train"].shape[1], n_layers=4, n_units=128, prob_drop=0.1).to(DEV)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    lossf = make_loss(kind)
    xtr, (dtr, ytr, ttr) = X["train"], split(Y["train"])
    xva, (dva, yva, tva) = X["val"],   split(Y["val"])
    best, best_state, bad = float("inf"), None, 0
    n = len(xtr)
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(n)
        for i in range(0, n, bs):
            idx = perm[i:i + bs]
            opt.zero_grad()
            out = net(xtr[idx])
            l = lossf(out, ytr[idx], dtr[idx], ttr[idx])
            if not torch.isfinite(l):
                return None                      # 损失发散，记为失败
            l.backward(); opt.step()
        net.eval()
        with torch.no_grad():                    # ★早停统一用 MSE，否则不同损失不可比
            v = mse_l(net(xva), yva).item()
        if v < best - 1e-6:
            best, bad = v, 0
            best_state = {k: t.clone() for k, t in net.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    net.load_state_dict(best_state); net.eval()
    with torch.no_grad():
        _, yte, _ = split(Y["test"])
        rmse_te = torch.sqrt(mse_l(net(X["test"]), yte)).item()
        rmse_va = best ** 0.5
    return rmse_va, rmse_te

SEEDS = [1, 2, 3, 4, 5]
print(f"eta={ETA:.4f} 天  beta={BETA:.1f}  lambda={LAM}")
print(f"train {tuple(X['train'].shape)}  val {tuple(X['val'].shape)}  test {tuple(X['test'].shape)}\n")
print(f"{'损失':<20} {'val RMSE 中位':>13} {'test RMSE 中位':>14} {'test 各种子':<34} {'失败'}")
print("-" * 96)
res = {}
for kind in KINDS:
    t0 = time.time()
    outs = [run(kind, s) for s in SEEDS]
    ok = [o for o in outs if o is not None]
    fail = len(outs) - len(ok)
    if not ok:
        print(f"{kind:<20} {'—':>13} {'—':>14} {'全部发散':<34} {fail}")
        continue
    vm = st.median([o[0] for o in ok]); tm = st.median([o[1] for o in ok])
    res[kind] = tm
    tl = " ".join(f"{o[1]:.3f}" for o in ok)
    print(f"{kind:<20} {vm:>13.4f} {tm:>14.4f} {tl:<34} {fail}   ({time.time()-t0:.0f}s)")

print()
if "mse" in res:
    base = res["mse"]
    print("相对纯 MSE 基线的 test RMSE 变化（负=更好）：")
    for k, v in sorted(res.items(), key=lambda kv: kv[1]):
        print(f"  {k:<20} {(v-base)/base*100:+7.1f}%")
