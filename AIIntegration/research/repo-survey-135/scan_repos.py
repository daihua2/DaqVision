"""扫 137 个振动仓：① 业内算法现状 ② 有没有「低频 + 标量」的数据集。

判别思路：
  标量特征 —— 项目吃的是传感器算好的 RMS/峰值/OA 之类，而不是原始波形
  低频特征 —— 风机主轴、桥梁/结构、低速轴承、SCADA 10 分钟均值
"""
import io, re, json
from pathlib import Path
from collections import Counter

ROOT = Path("/mnt/d/AIRef/projects/vibration-analysis-repos-all")
README_NAMES = ["README.md", "readme.md", "README.rst", "README.txt", "README", "readme.txt"]

ALGO = {
    "CNN": r"\bCNN\b|convolutional", "LSTM": r"\bLSTM\b|\bGRU\b",
    "Transformer": r"transformer|attention", "AutoEncoder": r"autoencoder|auto-encoder|\bAE\b|\bVAE\b",
    "RandomForest": r"random forest|randomforest|\bRF\b", "SVM": r"\bSVM\b|support vector",
    "XGBoost": r"xgboost|lightgbm|catboost|gradient boost",
    "IsolationForest": r"isolation forest|isolationforest|one-class|oneclass",
    "kNN": r"\bkNN\b|k-nearest|nearest neighbor",
    "FFT/谱": r"\bFFT\b|spectrum|spectral|spectrogram",
    "包络解调": r"envelope|hilbert|demodulat",
    "小波": r"wavelet|\bCWT\b|\bDWT\b|\bWPT\b",
    "EMD/VMD": r"\bEMD\b|\bEEMD\b|\bVMD\b|empirical mode",
    "阶次分析": r"order (analysis|tracking|spectrum)",
    "ISO标准": r"ISO ?10816|ISO ?20816|ISO ?13373|ISO ?2372",
    "RUL": r"\bRUL\b|remaining useful life|prognos",
    "模态/SHM": r"modal analysis|structural health|\bSHM\b|eigen(value|frequency)|natural frequenc",
    "统计特征": r"kurtosis|crest factor|skewness|\bRMS\b",
    "物理模型": r"physics[- ]informed|\bPINN\b|digital twin|finite element|\bFEM\b|jeffcott|rotor dynamic",
}
DATASET = {
    "CWRU": r"CWRU|case western", "IMS": r"\bIMS\b(?! ?sensor)|NASA bearing",
    "PRONOSTIA/FEMTO": r"PRONOSTIA|FEMTO", "Paderborn": r"paderborn|\bKAt\b|\bPU\b dataset",
    "MAFAULDA": r"MAFAULDA|machinery fault database", "XJTU": r"XJTU",
    "MFPT": r"\bMFPT\b", "SEU/JNU": r"\bSEU\b|\bJNU\b",
    "风机SCADA": r"SCADA", "Z24桥": r"\bZ24\b",
    "NREL/风电": r"\bNREL\b|wind turbine",
}
LOWFREQ = {
    "低频/低速": r"low[- ]frequency|low[- ]speed|slow[- ]speed|低频|低速",
    "风机主轴": r"wind turbine|gearbox.*wind|main bearing",
    "结构/桥梁": r"bridge|structural health|\bSHM\b|civil",
    "10分钟均值": r"10[- ]min|ten[- ]minute|10min",
}
SCALAR = {
    "标量/特征值输入": r"\bRMS\b|overall (vibration|level)|\bOA\b value|velocity rms|feature[- ]based|统计特征",
    "SCADA/工业采集": r"SCADA|Modbus|OPC[- ]?UA|\bMQTT\b|IoT|wireless sensor",
    "边缘/嵌入式": r"edge|embedded|tinyml|arduino|esp32|raspberry",
}

def read_readme(d):
    for n in README_NAMES:
        p = d / n
        if p.is_file():
            try:
                return io.open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                return ""
    # 退而求其次：任何 md
    for p in list(d.glob("*.md"))[:1]:
        try:
            return io.open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            return ""
    return ""

rows = []
algo_c, ds_c, lf_c, sc_c = Counter(), Counter(), Counter(), Counter()
no_readme = []
for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
    txt = read_readme(d)
    if not txt.strip():
        no_readme.append(d.name); continue
    low = txt.lower()
    hit = lambda D: [k for k, pat in D.items() if re.search(pat, txt, re.I)]
    a, ds, lf, sc = hit(ALGO), hit(DATASET), hit(LOWFREQ), hit(SCALAR)
    for k in a: algo_c[k] += 1
    for k in ds: ds_c[k] += 1
    for k in lf: lf_c[k] += 1
    for k in sc: sc_c[k] += 1
    rows.append({"name": d.name, "algo": a, "ds": ds, "lowfreq": lf, "scalar": sc,
                 "len": len(txt)})

print(f"扫了 {len(rows)} 个仓（{len(no_readme)} 个无 README）\n")
def show(title, c, n=20):
    print(f"── {title} ──")
    for k, v in c.most_common(n):
        print(f"   {k:<16} {v:>4}  {'█'*min(40, v)}")
    print()
show("算法分布", algo_c)
show("数据集引用", ds_c)
show("低频线索", lf_c)
show("标量输入线索", sc_c)

# ★重点：同时命中「低频」与「标量」的
key = [r for r in rows if r["lowfreq"] and r["scalar"]]
print(f"★同时命中 低频 + 标量 的仓：{len(key)} 个")
for r in sorted(key, key=lambda x: -len(x["lowfreq"]) - len(x["scalar"]))[:25]:
    print(f"   {r['name'][:52]:<54} 低频={','.join(r['lowfreq'])[:28]:<30} 标量={','.join(r['scalar'])[:34]}")

json.dump(rows, io.open("/tmp/repo_scan.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n明细已存 /tmp/repo_scan.json")
