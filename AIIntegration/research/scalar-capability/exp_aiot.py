"""甲2：AIoT 低频（31.25 Hz）三轴 —— 标量化后还分不分得开。

★已知缺陷：4 类各采一整段，Fault2 晚 2.21 天 ⇒ 类别与采集时段混淆。
  不因此放弃，而是加阴性对照量化它：
    T-real : 4 类真实分类
    T-null : ★取 Normal 同一段，前半标 A、后半标 B ——
             同类、同天、连续采集，理论上应分不开。
             T-null 的准确率 = 「时间漂移本身可学」的量，是读懂 T-real 的标尺。

对比：输入 A = 原始三轴窗口   vs   输入 B = 我方传感器形态的 12 个标量
纪律：常数基线打底、按时间块留出（不随机切窗）、多种子报中位数。
"""
import sys, time, statistics as st
import numpy as np, torch, torch.nn as nn

SRC = "/mnt/d/AIRef/projects/vibration-analysis-repos-all/AIoT_Vibration_Prediction_EdgeDL/XYZ_ACC_Labeled_Data.txt"
FS = 31.25
WIN = 125          # 4 秒
G = 9.80665

def load():
    lab, xyz = [], []
    with open(SRC, encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = line.strip().split(",")
            if len(p) < 6: continue
            try: v = (float(p[3]), float(p[4]), float(p[5]))
            except ValueError: continue
            lab.append(p[1]); xyz.append(v)
    return np.array(lab), np.array(xyz, dtype=np.float64)

def scalars(x):
    """一轴 → (加速度RMS, 速度RMS, 位移RMS, 主频)；31.25Hz ⇒ 奈奎斯特 15.6Hz"""
    n = len(x); w = np.hanning(n); corr = np.sqrt(np.mean(w**2))
    Y = np.fft.rfft((x - x.mean()) * w); f = np.fft.rfftfreq(n, 1.0/FS)
    amp = np.abs(Y)*2.0/n/corr
    m = f >= 0.5                                   # 去直流
    fa, aa = f[m], amp[m]
    if fa.size == 0: return (0.,0.,0.,0.)
    rms = lambda a: float(np.sqrt(np.sum((a/np.sqrt(2.0))**2)))
    return (rms(aa), rms(aa*G/(2*np.pi*fa))*1e3,
            rms(aa*G/(2*np.pi*fa)**2)*1e6, float(fa[int(np.argmax(aa))]))

def windows(xyz, idx):
    """切不重叠的窗，返回 (原始展平, 标量12维)"""
    raw, sca = [], []
    for s in range(0, len(idx)-WIN+1, WIN):
        seg = xyz[idx[s:s+WIN]]
        raw.append(seg.reshape(-1))
        sca.append([v for ch in range(3) for v in scalars(seg[:, ch])])
    return np.array(raw), np.array(sca)

class Net(nn.Module):
    def __init__(s, d, k):
        super().__init__()
        s.f = nn.Sequential(nn.Linear(d,128), nn.ReLU(), nn.Dropout(.2),
                            nn.Linear(128,64), nn.ReLU(), nn.Dropout(.2), nn.Linear(64,k))
    def forward(s, x): return s.f(x)

def run(Xtr, ytr, Xte, yte, k, seed, epochs=200, patience=25):
    torch.manual_seed(seed); np.random.seed(seed)
    lo, hi = Xtr.min(0), Xtr.max(0)
    nz = np.where((hi-lo) < 1e-12, 1.0, hi-lo)
    tr = torch.tensor((Xtr-lo)/nz).float(); te = torch.tensor((Xte-lo)/nz).float()
    ytr_t = torch.tensor(ytr).long(); yte_t = torch.tensor(yte).long()
    # 训练集内部再切 20% 作早停用
    n = len(tr); cut = int(n*0.8)
    perm = torch.randperm(n)
    tri, vai = perm[:cut], perm[cut:]
    net = Net(tr.shape[1], k); opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    lf = nn.CrossEntropyLoss()
    best, state, bad = 1e9, None, 0
    for _ in range(epochs):
        net.train()
        p = tri[torch.randperm(len(tri))]
        for i in range(0, len(p), 256):
            j = p[i:i+256]; opt.zero_grad()
            l = lf(net(tr[j]), ytr_t[j]); l.backward(); opt.step()
        net.eval()
        with torch.no_grad(): v = lf(net(tr[vai]), ytr_t[vai]).item()
        if v < best-1e-6: best, bad, state = v, 0, {a:b.clone() for a,b in net.state_dict().items()}
        else:
            bad += 1
            if bad >= patience: break
    net.load_state_dict(state); net.eval()
    with torch.no_grad():
        return float((net(te).argmax(1) == yte_t).float().mean())

print("读数据…", flush=True)
lab, xyz = load()
print(f"  {len(lab)} 行", flush=True)
segs = {}
prev, start = None, 0
for i, l in enumerate(lab):
    if l != prev:
        if prev is not None: segs.setdefault(prev, []).append((start, i))
        prev, start = l, i
segs.setdefault(prev, []).append((start, len(lab)))

def build(tasks):
    """tasks: [(类名, 起, 止, 标签id)] → 按每段前70%训练 后30%测试"""
    Rtr,Str,ytr,Rte,Ste,yte = [],[],[],[],[],[]
    for _, a, b, y in tasks:
        idx = np.arange(a, b)
        cut = a + int((b-a)*0.7)
        for lo_, hi_, R, S, Y in ((a,cut,Rtr,Str,ytr),(cut,b,Rte,Ste,yte)):
            r, s = windows(xyz, np.arange(lo_, hi_))
            R.append(r); S.append(s); Y.append(np.full(len(r), y))
    cat = lambda L: np.concatenate(L)
    return cat(Rtr),cat(Str),cat(ytr),cat(Rte),cat(Ste),cat(yte)

SEEDS = [1,2,3,4,5]
print(f"\n{'任务':<10}{'输入':<12}{'类数':>4}{'训练':>7}{'测试':>7}{'常数基线':>9}{'准确率':>9}{'相对基线':>10}")
print("-"*72)

def report(tag, tasks, k):
    R1,S1,y1,R2,S2,y2 = build(tasks)
    base = float(np.bincount(y2, minlength=k).max()/len(y2))
    for nm, Xtr, Xte in (("原始三轴(375维)", R1, R2), ("★我方标量(12维)", S1, S2)):
        acc = st.median([run(Xtr,y1,Xte,y2,k,s) for s in SEEDS])
        print(f"{tag:<10}{nm:<12}{k:>4}{len(y1):>7}{len(y2):>7}{base:>9.3f}{acc:>9.3f}{(acc-base)*100:>+9.1f}pt")

t0=time.time()
real = [(c, a, b, i) for i,(c,v) in enumerate(segs.items()) for (a,b) in v]
report("T-real", real, len(segs))

na, nb = segs["Normal"][0]
mid = na + (nb-na)//2
report("★T-null", [("A",na,mid,0), ("B",mid,nb,1)], 2)
print(f"\n用时 {time.time()-t0:.0f}s")
print("★读法：T-null 高出基线多少，就是「时间漂移本身可学」的量 —— T-real 的成绩要扣掉这一块才算数。")
