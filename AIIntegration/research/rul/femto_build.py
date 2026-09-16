"""一次读取，产出两种特征，供实验 A / B 共用。

对每个轴承目录，按文件序号（= 时间，FEMTO 固定 10 s 一采）产出：
  spec (N,20)  谱分箱 —— 用原项目 create_fft 原件 + 每箱取 max，与其 bucket_size=64 一致
  scal (N, 8)  ★我方传感器风格的标量 —— 2 轴 × {加速度RMS, 速度RMS, 位移RMS, 主频}
  secs (N,)    该帧的秒数

★不使用 create_date_dict：它靠文件 mtime 拼时间戳，复制文件就会被污染。
  FEMTO 采样间隔固定 10 s，用序号更可靠。
"""
import sys, time
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/root/AIRef/weibull-knowledge-informed-ml")
sys.path.insert(0, str(REPO))
from src.features.build_features import create_fft      # 原件，不改

FS = 25600.0
G = 9.80665
BAND = (10.0, 1000.0)      # ISO 10816-3 评估带
BUCKET = 64
COLS = ["hr", "min", "sec", "micro_sec", "acc_horz", "acc_vert"]

def scalars(x):
    """加速度(g) → (acc_rms[g], vel_rms[mm/s], disp_rms[um], dom_freq[Hz])"""
    n = len(x)
    w = np.hanning(n)
    corr = np.sqrt(np.mean(w ** 2))
    Y = np.fft.rfft((x - x.mean()) * w)
    f = np.fft.rfftfreq(n, 1.0 / FS)
    amp = np.abs(Y) * 2.0 / n / corr
    m = (f >= BAND[0]) & (f <= BAND[1])
    fa, aa = f[m], amp[m]
    if fa.size == 0:
        return (0.0, 0.0, 0.0, 0.0)
    rms = lambda a: float(np.sqrt(np.sum((a / np.sqrt(2.0)) ** 2)))
    return (rms(aa),
            rms(aa * G / (2 * np.pi * fa)) * 1e3,
            rms(aa * G / (2 * np.pi * fa) ** 2) * 1e6,
            float(fa[int(np.argmax(aa))]))

def one_bearing(folder):
    files = sorted(folder.glob("acc_*.csv"))
    spec, scal = [], []
    for p in files:
        # FEMTO 已知坑：部分轴承（如 Bearing1_4）的 acc 文件用分号分隔。
        # pandas 读错分隔符时不抛异常，而是把整行塞进第一列、其余填 NaN，
        # 所以不能靠 try/except 兜 —— 必须显式验列。
        df = pd.read_csv(p, names=COLS, engine="c")
        if df["acc_horz"].isna().any() or df["acc_vert"].isna().any():
            df = pd.read_csv(p, names=COLS, sep=";", engine="c")
        h_chk = pd.to_numeric(df["acc_horz"], errors="coerce")
        v_chk = pd.to_numeric(df["acc_vert"], errors="coerce")
        if h_chk.isna().any() or v_chk.isna().any():
            raise SystemExit(f"{p}: 两种分隔符都读不出数值列 —— 停下来看，不静默跳过")
        df["acc_horz"], df["acc_vert"] = h_chk, v_chk
        h = df["acc_horz"].to_numpy(dtype="float64")
        v = df["acc_vert"].to_numpy(dtype="float64")
        if h.size < 2560:
            continue
        _, _, _, yf = create_fft(df, y_name="acc_horz", sample_freq=FS,
                                 window="kaiser", beta=3)
        b = yf[: (len(yf) // BUCKET) * BUCKET].reshape(-1, BUCKET)
        spec.append(b.max(axis=1))                 # 每箱取 max，同原项目
        scal.append(scalars(h) + scalars(v))
    spec = np.array(spec); scal = np.array(scal)
    secs = np.arange(len(spec), dtype=np.float64) * 10.0
    return spec, scal, secs

if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    roots = [Path("/root/AIRef/femto/Learning_set"), Path("/root/AIRef/femto/Full_Test_Set")]
    for root in roots:
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            dst = out / f"{folder.name}.npz"
            if dst.exists():
                print(f"{folder.name:<14} 已有，跳过", flush=True); continue
            t0 = time.time()
            spec, scal, secs = one_bearing(folder)
            np.savez_compressed(dst, spec=spec, scal=scal, secs=secs)
            print(f"{folder.name:<14} {spec.shape[0]:>5} 帧  谱{spec.shape[1]}维 标量{scal.shape[1]}维  "
                  f"寿命 {secs[-1]/3600:6.2f} h  "
                  f"vel_rms {scal[:,1].min():7.3f}~{scal[:,1].max():8.3f} mm/s  "
                  f"({time.time()-t0:.0f}s)", flush=True)
