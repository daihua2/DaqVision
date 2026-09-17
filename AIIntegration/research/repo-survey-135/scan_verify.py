"""第二轮：关键词命中 ≠ 真有数据。逐个核实候选仓的实际数据文件与数据来源描述。"""
import io, re
from pathlib import Path

ROOT = Path("/mnt/d/AIRef/projects/vibration-analysis-repos-all")
CAND = ["acoustic-monitor","MCIFT","Tier3_PdM_System","acousticpinn-machine-failure-prediction",
        "predictive-maintenance-dashboard-poc",
        "Anomaly_Detection_in_Wind_Turbines_using_Variational_Autoencoder_and_Isolation_Forest",
        "bearing-analytics-pipeline","bearing-fault-detection","bearing_faults_daignosis",
        "c-spectrum","industrial-predictive-maintenance-twin","structural-health-monitoring-python",
        "Bogieflow","nv-vibration-rejection","flight-log-analyzer","awesome-bearing-dataset",
        "PDM-Predictive-Maintenance","Predictive-maintenance-of-Gearbox-using-vibration-sensors",
        "AIoT_Vibration_Prediction_EdgeDL","Motor_Vibration_Classification_TinyML"]
DATA_EXT = {".csv",".parquet",".mat",".h5",".hdf5",".npz",".npy",".json",".txt",".tdms",".xlsx"}

def readme(d):
    for n in ("README.md","readme.md","README.rst","README.txt","README"):
        p = d / n
        if p.is_file():
            return io.open(p, encoding="utf-8", errors="ignore").read()
    return ""

for name in CAND:
    d = ROOT / name
    if not d.is_dir():
        print(f"{name}: 目录不存在"); continue
    # 实际数据文件（排除代码与配置）
    files = []
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in DATA_EXT and ".git" not in p.parts:
            try: sz = p.stat().st_size
            except OSError: continue
            if sz > 20000:                       # 只看 >20KB 的，滤掉配置
                files.append((sz, str(p.relative_to(d))))
    files.sort(reverse=True)
    txt = readme(d)
    # 抓数据来源相关的句子
    sents = []
    for m in re.finditer(r"[^\n.。]{0,160}(dataset|data source|sampling rate|sampl\w+ freq|Hz|SCADA|RMS|10[- ]min)[^\n.。]{0,160}", txt, re.I):
        s = m.group(0).strip()
        if 25 < len(s) < 260:
            sents.append(s)
    print(f"\n{'='*92}\n{name}   （数据文件 >20KB：{len(files)} 个）")
    for sz, rel in files[:5]:
        print(f"    {sz/1048576:8.2f} MB  {rel[:76]}")
    for s in sents[:4]:
        print(f"    │ {s[:170]}")
