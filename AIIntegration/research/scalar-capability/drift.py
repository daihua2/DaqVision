"""查：Normal 那一段 66 分钟里，12 个标量各自漂了多少。
对每一维报：与时间的 Spearman ρ、前后半中位数比、★单维 AUC（单独用它分前后半能到多准）。
"""
import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score

SRC = "/mnt/d/AIRef/projects/vibration-analysis-repos-all/AIoT_Vibration_Prediction_EdgeDL/XYZ_ACC_Labeled_Data.txt"
FS, WIN, G = 31.25, 125, 9.80665
NAMES = [f"{a}_{m}" for a in "xyz" for m in ("acc_rms","vel_rms","dis_rms","dom_f")]

lab, xyz = [], []
with open(SRC, encoding="utf-8", errors="ignore") as f:
    for line in f:
        p = line.strip().split(",")
        if len(p) < 6: continue
        try: v = (float(p[3]), float(p[4]), float(p[5]))
        except ValueError: continue
        lab.append(p[1]); xyz.append(v)
lab = np.array(lab); xyz = np.array(xyz)
idx = np.where(lab == "Normal")[0]
seg = xyz[idx]
print(f"Normal 段 {len(seg)} 点 = {len(seg)/FS/60:.1f} 分钟")

def scalars(x):
    n=len(x); w=np.hanning(n); corr=np.sqrt(np.mean(w**2))
    Y=np.fft.rfft((x-x.mean())*w); f=np.fft.rfftfreq(n,1/FS)
    amp=np.abs(Y)*2/n/corr; m=f>=0.5; fa,aa=f[m],amp[m]
    if fa.size==0: return (0.,0.,0.,0.)
    rms=lambda a: float(np.sqrt(np.sum((a/np.sqrt(2))**2)))
    return (rms(aa), rms(aa*G/(2*np.pi*fa))*1e3,
            rms(aa*G/(2*np.pi*fa)**2)*1e6, float(fa[int(np.argmax(aa))]))

F=[]
for s in range(0, len(seg)-WIN+1, WIN):
    w=seg[s:s+WIN]
    F.append([v for ch in range(3) for v in scalars(w[:,ch])])
F=np.array(F); n=len(F); half=n//2
t=np.arange(n)
y=np.r_[np.zeros(half), np.ones(n-half)]
print(f"切出 {n} 个窗（每窗 4 秒）\n")
print(f"{'维':<12}{'Spearman ρ':>12}{'前半中位':>11}{'后半中位':>11}{'后/前':>8}{'★单维AUC':>10}")
print("-"*66)
rows=[]
for i,nm in enumerate(NAMES):
    v=F[:,i]
    rho=spearmanr(t,v).statistic
    a,b=np.median(v[:half]), np.median(v[half:])
    auc=roc_auc_score(y, v); auc=max(auc, 1-auc)
    rows.append((abs(rho), nm, rho, a, b, b/a if a else np.nan, auc))
for _,nm,rho,a,b,r,auc in sorted(rows, reverse=True):
    print(f"{nm:<12}{rho:>+12.3f}{a:>11.4f}{b:>11.4f}{r:>8.3f}{auc:>10.3f}")
print("\n★AUC 0.5=完全分不开，1.0=完全分得开。单维 AUC 高 ⇒ 该维自己就在随时间漂。")
