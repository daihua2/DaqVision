"""甲1：MAFAULDA —— 标量化后，具体故障类型还能不能判。

★设计上的硬约束（先看清数据才定的）：
  只做「同一天内两类」的二分类 —— 该数据集不同类采于不同天
  （normal/imbalance 10-04；imbalance/vertical 10-06；vertical/horizontal 10-07；
    underhang/overhang 2017-04-08），跨天比就是在比时段。

划分：★按转速留出（训练/测试转速不重叠），比随机切更严，也更贴近现场
      （现场会遇到没见过的工况）。
输入：谱 120 维（6通道×20箱） vs 标量 24 维（6通道×4） vs ★标量 12 维（单位置三轴，最接近我方）
"""
import sys, zipfile, statistics as st
from pathlib import Path
import numpy as np, torch, torch.nn as nn

FEAT = Path("/root/AIRef/mafaulda-feat")
ZIP = "/mnt/d/AIRef/datasets/mafaulda/full.zip"

# 重建 sorted 文件序（与提特征时一致）→ 取每个文件的日期
z = zipfile.ZipFile(ZIP)
names = [n for n in z.namelist() if n.endswith(".csv")]
date_of = {n: f"{z.getinfo(n).date_time[0]}-{z.getinfo(n).date_time[1]:02d}-{z.getinfo(n).date_time[2]:02d}" for n in names}

def load(cls):
    d = np.load(FEAT / f"{cls}.npz")
    fl = sorted([n for n in names if n.split("/")[0] == cls])
    dates = np.array([date_of[n] for n in fl])
    n = len(d["scal"])
    return d["scal"], d["spec"], d["rpm"], dates[:n]   # 提特征时可能跳过少数文件

CACHE = {c: load(c) for c in ("normal","imbalance","vertical-misalignment",
                              "horizontal-misalignment","underhang","overhang")}
for c,(s,p,r,dt) in CACHE.items():
    u = sorted(set(dt))
    print(f"{c:<26} {len(s):>4} 个  日期 {u}")

class Net(nn.Module):
    def __init__(s, d, K_GLOBAL=2):
        super().__init__()
        s.f = nn.Sequential(nn.Linear(d,64), nn.ReLU(), nn.Dropout(.3),
                            nn.Linear(64,32), nn.ReLU(), nn.Dropout(.3), nn.Linear(32,K_GLOBAL))
    def forward(s,x): return s.f(x)

def fit_eval(Xtr,ytr,Xte,yte,K,seed,epochs=300,patience=40):
    torch.manual_seed(seed); np.random.seed(seed)
    lo,hi = Xtr.min(0), Xtr.max(0)
    nz = np.where((hi-lo)<1e-12, 1.0, hi-lo)
    tr = torch.tensor((Xtr-lo)/nz).float(); te = torch.tensor((Xte-lo)/nz).float()
    Ytr = torch.tensor(ytr).long(); Yte = torch.tensor(yte).long()
    n=len(tr); cut=int(n*0.8); perm=torch.randperm(n); tri,vai=perm[:cut],perm[cut:]
    net=Net(tr.shape[1],K); opt=torch.optim.Adam(net.parameters(),lr=2e-3); lf=nn.CrossEntropyLoss()
    best,state,bad=1e9,None,0
    for _ in range(epochs):
        net.train(); p=tri[torch.randperm(len(tri))]
        for i in range(0,len(p),64):
            j=p[i:i+64]; opt.zero_grad(); l=lf(net(tr[j]),Ytr[j]); l.backward(); opt.step()
        net.eval()
        with torch.no_grad(): v=lf(net(tr[vai]),Ytr[vai]).item()
        if v<best-1e-6: best,bad,state=v,0,{a:b.clone() for a,b in net.state_dict().items()}
        else:
            bad+=1
            if bad>=patience: break
    net.load_state_dict(state); net.eval()
    with torch.no_grad(): return float((net(te).argmax(1)==Yte).float().mean())

TASKS = [
 ("T1 正常vs不平衡",        ["normal","imbalance"], "2014-10-03"),
 ("T2 不平衡vs垂直不对中",  ["imbalance","vertical-misalignment"], "2014-10-06"),
 ("T3 垂直vs水平不对中",    ["vertical-misalignment","horizontal-misalignment"], "2014-10-06"),
 ("T4 欠悬vs过悬轴承",      ["underhang","overhang"], "2017-04-07"),
 ("T5 不平衡vs垂直vs水平",  ["imbalance","vertical-misalignment","horizontal-misalignment"], "2014-10-06"),
]
SEEDS=[1,2,3,4,5]
print(f"\n{'任务':<22}{'输入':<18}{'训练':>6}{'测试':>6}{'基线':>7}{'准确率':>8}{'相对基线':>10}")
print("-"*80)
for tag,cls,day in TASKS:
    parts={"spec":[], "scal24":[], "scal12":[]}; ys=[]; rpms=[]
    for lab,c in enumerate(cls):
        s,p,r,dt = CACHE[c]
        m = dt==day
        parts["spec"].append(p[m]); parts["scal24"].append(s[m]); parts["scal12"].append(s[m][:, :12])
        ys.append(np.full(m.sum(), lab)); rpms.append(r[m])
    y=np.concatenate(ys); rpm=np.concatenate(rpms)
    # ★按转速留出：转速分位切
    thr=np.quantile(rpm,0.7); tr_m=rpm<=thr; te_m=~tr_m
    K=len(cls)
    if te_m.sum()==0 or tr_m.sum()==0: print(f"{tag}: 划分为空，跳过"); continue
    base=float(max(np.bincount(y[te_m],minlength=K))/te_m.sum())
    for nm,key in (("谱 120维","spec"),("标量 24维","scal24"),("★标量 12维","scal12")):
        X=np.concatenate(parts[key])
        acc=st.median([fit_eval(X[tr_m],y[tr_m],X[te_m],y[te_m],K,s) for s in SEEDS])
        print(f"{tag:<22}{nm:<18}{tr_m.sum():>6}{te_m.sum():>6}{base:>7.3f}{acc:>8.3f}{(acc-base)*100:>+9.1f}pt")
    print()
