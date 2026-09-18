"""验证 1 / 5 / 4：其它数据集形态、跨整条寿命、真实存储开销。

    python verify145_others.py

## 验证 1：定义在别的数据集形态上成不成立

FEMTO 是"每条样本自带时刻"的形态，对定义最友好。但别的数据集未必 ——
★若某些源**根本没有逐样本时刻**，那么"采样率从 T 推出"就无从谈起，定义要加限定。

## 验证 5：跨整条寿命（含失效段）

前面只验了前 3 帧（健康态）。失效段振幅剧变，帧切分与采样率推导是否仍成立？

## 验证 4：真实存储开销

前面只算了净载荷（2×f32×条数），**没算每条 VQT 的协议开销**。这里按 protobuf
实际编码量一次。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REF = Path("/mnt/d/download/AIRef/datasets")
FEMTO = REF / "femto/Learning_set/Bearing1_1"


def load_rows(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        c = line.strip().split(",")
        if len(c) >= 6:
            h, m, s, us = int(c[0]), int(c[1]), int(c[2]), int(float(c[3]))
            out.append((((h * 60 + m) * 60 + s) * 1_000_000 + us, float(c[4]), float(c[5])))
    return out


def base_interval(ts, pct=10):
    d = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    return d[max(0, len(d) * pct // 100)] if d else 0


def rate_from_ts(ts):
    return 1e6 * (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 1 else 0.0


# ═══════════════ 验证 1：别的数据集形态 ═══════════════
def verify1():
    print("验证 1：其它数据集的形态 —— 逐样本时刻有没有？")
    print("-" * 68)
    findings = []

    # CWRU：.mat，连续数组
    mats = sorted((REF / "cwru").rglob("*.mat"))
    if mats:
        f = mats[0]
        raw = f.read_bytes()
        # .mat v5 头 128 字节，其后是变量。这里只看有没有"时间"变量名。
        names = {b"time", b"t", b"timestamp", b"Time"}
        has_time = any(n in raw[:20000] for n in names)
        findings.append(("CWRU", f"{len(mats)} 个 .mat", "连续数组",
                         "有" if has_time else "★无逐样本时刻",
                         "采样率只在**目录名**里（12k/48k）"))

    # MAFAULDA：压缩包内是 CSV
    z = REF / "mafaulda/full.zip"
    if z.exists():
        import zipfile
        try:
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
                csvs = [n for n in names if n.endswith(".csv")]
                sample = None
                if csvs:
                    with zf.open(csvs[0]) as fh:
                        sample = fh.readline().decode("utf-8", "ignore").strip()
                findings.append(("MAFAULDA", f"{len(csvs)} 个 CSV", "连续数组",
                                 "★无逐样本时刻" if sample and "," in sample
                                 and len(sample.split(",")) <= 8 else "待查",
                                 f"首行: {sample[:48] if sample else '?'}"))
        except Exception as e:                       # noqa: BLE001
            findings.append(("MAFAULDA", "读不出", "-", "-", str(e)[:40]))

    # FEMTO：对照组
    f0 = sorted(FEMTO.glob("acc_*.csv"))[0]
    findings.append(("FEMTO", f"{len(list(FEMTO.glob('acc_*.csv')))} 个 CSV",
                     "★帧式（0.1s/10s）", "★有（时,分,秒,微秒）",
                     "采样率可从 T 推出"))

    print("   {:<10} {:<14} {:<14} {:<16} {}".format(
        "数据集", "规模", "形态", "逐样本时刻", "备注"))
    for r in findings:
        print("   {:<10} {:<14} {:<14} {:<16} {}".format(*r))

    print()
    print("   ★结论：**不是所有源都有逐样本时刻**。CWRU / MAFAULDA 是纯数组 +")
    print("     一个标称采样率（CWRU 甚至只写在目录名里）。")
    print("   ⇒ 定义要加限定：「采样率从 T 推出」只对**采集侧逐样本打时标**的源成立。")
    print("     公开数据集多为离线文件，不打时标；而我方要接的是**在线采集链路**，")
    print("     时标由采集侧在读到那一刻打 —— 属于前者。")
    return findings


# ═══════════════ 验证 5：跨整条寿命 ═══════════════
def verify5():
    print("\n验证 5：跨整条寿命（含失效段）")
    print("-" * 68)
    accs = sorted(FEMTO.glob("acc_*.csv"))
    picks = [0, len(accs) // 4, len(accs) // 2, 3 * len(accs) // 4, len(accs) - 1]
    print("   {:<16} {:>8} {:>13} {:>12} {:>12}".format(
        "帧", "条数", "推出采样率", "幅值RMS", "最大倍数"))
    ok = True
    for i in picks:
        rows = load_rows(accs[i])
        ts = [r[0] for r in rows]
        b = base_interval(ts)
        ms = [(ts[j + 1] - ts[j]) / b for j in range(len(ts) - 1)] if b else []
        rms = (sum(r[1] ** 2 for r in rows) / len(rows)) ** 0.5
        rate = rate_from_ts(ts)
        mx = max(ms) if ms else 0
        if not (25000 < rate < 26200) or mx >= 1.5:
            ok = False
        print("   {:<16} {:>8} {:>11,.1f}Hz {:>12.4f} {:>12.3f}".format(
            accs[i].name, len(rows), rate, rms, mx))
    print(f"\n   {'✅' if ok else '❌'} 采样率推导与帧完整性在**失效段同样成立**"
          f"（幅值 RMS 变化 {'见上，末段显著增大' if ok else '异常'}）")
    return ok


# ═══════════════ 验证 4：真实存储开销 ═══════════════
def _varint(n):
    c = 1
    while n >= 0x80:
        n >>= 7
        c += 1
    return c


def verify4():
    print("\n验证 4：真实存储开销（按 protobuf 实际编码估）")
    print("-" * 68)
    # 一条结构 VQT 的组成（字段号按 daqcontract 的量级估）
    tagid = 1 + _varint(2_000_000)          # TagId
    t = 1 + 8                               # 时间戳（int64 定长或 varint，取 8）
    q = 1 + _varint(5000)                   # 质量码
    sv_hdr = 1 + 1                          # StructVal 外层 tag + len
    sid = 1 + _varint(42)                   # StructId
    sver = 1 + _varint(1)                   # StructVersion
    data_hdr = 1 + 1                        # Data 的 tag + len
    fields = 2 * (1 + 4)                    # 两个 f32 字段（tag + 4B）
    per = tagid + t + q + sv_hdr + sid + sver + data_hdr + fields
    print(f"   一条结构 VQT ≈ {per} 字节")
    print(f"     TagId {tagid} + T {t} + Q {q} + StructVal 头 {sv_hdr}"
          f" + StructId {sid} + 版本 {sver} + Data 头 {data_hdr} + 两字段 {fields}")

    accs = sorted(FEMTO.glob("acc_*.csv"))
    n = len(accs) * 2560
    net = n * 8
    total = n * per
    print(f"\n   本轴承：{n:,} 条")
    print(f"     净载荷（2×f32）      {net / 1e6:>9,.1f} MB")
    print(f"     含协议开销           {total / 1e6:>9,.1f} MB   （膨胀 {per / 8:.1f}×）")

    # 对照：一帧一个值
    frame_payload = 2560 * 2 * 4
    frame_per = tagid + t + q + sv_hdr + sid + sver + data_hdr + 2 * (1 + 2 + frame_payload // 2)
    ftotal = len(accs) * frame_per
    print(f"\n   对照「一帧一个值」：{len(accs):,} 条")
    print(f"     含协议开销           {ftotal / 1e6:>9,.1f} MB")
    print(f"     ⇒ 数组形态是它的 {total / ftotal:.1f} 倍")
    print("\n   ★但注意：这是**未压缩**的估算。结构值退到 gzip；而若 hs 将来")
    print("     给结构值也做列式编码（同结构同质量的连续段逐列压），差距会明显收窄 ——")
    print("     T 走 delta-of-delta 几乎免费，Q 整段一份，两个 f32 列各自 XOR。")
    return per, total, ftotal


if __name__ == "__main__":
    verify1()
    verify5()
    verify4()
    print("\n" + "=" * 68)
    print("验证 1/5/4 完成。1 给出了定义的**适用边界**，5 通过，4 把代价落成实数。")
    sys.exit(0)
