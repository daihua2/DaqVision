"""甲1 第一步：从 MAFAULDA 提两套特征，供「谱 vs 我方标量」对比。

★数据设计上的两条硬约束（先看清数据才定的，见 research/repo-survey-135）：
  1 时段混淆：只做「同一天内两类」的二分类，跨天不比；
  2 underhang/overhang 与其余四类隔 2.5 年，零重叠 ⇒ 只能组内比。

MAFAULDA 通道：1=转速计, 2-4=欠悬轴承三轴, 5-7=过悬轴承三轴, 8=麦克风
★采样率 51200 Hz（实测：文件名=转速，按 50000 算会偏 2.4%，见 repo-survey-135）
"""
import sys, time, zipfile, io
from pathlib import Path
import numpy as np

ZIP = "/mnt/d/AIRef/datasets/mafaulda/full.zip"
FS = 51200.0
G = 9.80665
BAND = (10.0, 1000.0)      # ISO 10816-3 评估带
SPEC_BINS = 20             # 谱分箱维数，与 FEMTO 实验一致

def scalars(x):
    """一路加速度(g) → 我方传感器形态的 4 个标量"""
    n = len(x); w = np.hanning(n); corr = np.sqrt(np.mean(w**2))
    Y = np.fft.rfft((x - x.mean()) * w)
    f = np.fft.rfftfreq(n, 1.0/FS)
    amp = np.abs(Y) * 2.0 / n / corr
    m = (f >= BAND[0]) & (f <= BAND[1])
    fa, aa = f[m], amp[m]
    if fa.size == 0: return (0.0, 0.0, 0.0, 0.0)
    rms = lambda a: float(np.sqrt(np.sum((a/np.sqrt(2.0))**2)))
    return (rms(aa),
            rms(aa*G/(2*np.pi*fa))*1e3,
            rms(aa*G/(2*np.pi*fa)**2)*1e6,
            float(fa[int(np.argmax(aa))]))

def spec_bins(x, nb=SPEC_BINS):
    """一路加速度 → 谱分箱（每箱取 max，同 FEMTO 实验）"""
    n = len(x); w = np.hanning(n)
    Y = np.abs(np.fft.rfft((x - x.mean()) * w)) * 2.0 / n
    f = np.fft.rfftfreq(n, 1.0/FS)
    m = (f >= BAND[0]) & (f <= BAND[1])
    y = Y[m]
    k = (len(y)//nb)*nb
    return y[:k].reshape(nb, -1).max(axis=1)

CLASSES = ["normal","imbalance","vertical-misalignment","horizontal-misalignment",
           "underhang","overhang"]

def main(out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    z = zipfile.ZipFile(ZIP)
    names = [n for n in z.namelist() if n.endswith(".csv")]
    by_cls = {}
    for n in names:
        c = n.split("/")[0]
        if c in CLASSES:
            by_cls.setdefault(c, []).append(n)
    for c in CLASSES:
        dst = out / f"{c}.npz"
        if dst.exists():
            print(f"{c:<26} 已有，跳过", flush=True); continue
        fl = sorted(by_cls.get(c, []))
        S, P, RPM, SUB = [], [], [], []
        t0 = time.time()
        for i, n in enumerate(fl):
            try:
                raw = z.read(n)
                a = np.loadtxt(io.BytesIO(raw), delimiter=",")
            except Exception as e:
                print(f"   ★跳过 {n}: {e}", flush=True); continue
            if a.ndim != 2 or a.shape[1] != 8 or a.shape[0] < 10000:
                print(f"   ★形状异常 {n}: {a.shape}", flush=True); continue
            # 通道 2-4（欠悬三轴）与 5-7（过悬三轴）
            sc, sp = [], []
            for ch in range(1, 7):
                sc.extend(scalars(a[:, ch]))
                sp.extend(spec_bins(a[:, ch]))
            S.append(sc); P.append(sp)
            RPM.append(float(Path(n).stem))          # 文件名 = 转速(Hz)
            SUB.append(n.split("/")[1] if n.count("/") > 1 else "")
            if (i+1) % 50 == 0:
                print(f"   {c} {i+1}/{len(fl)}  {time.time()-t0:.0f}s", flush=True)
        if not S:
            print(f"{c:<26} 无有效文件"); continue
        np.savez_compressed(dst, scal=np.array(S), spec=np.array(P),
                            rpm=np.array(RPM), sub=np.array(SUB))
        print(f"{c:<26} {len(S):>5} 个  标量{len(S[0])}维 谱{len(P[0])}维  "
              f"转速 {min(RPM):.1f}~{max(RPM):.1f} Hz  ({time.time()-t0:.0f}s)", flush=True)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/root/AIRef/mafaulda-feat")
