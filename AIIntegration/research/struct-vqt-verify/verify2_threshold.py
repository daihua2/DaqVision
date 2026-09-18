"""验证 2：★「等间隔可验证」的**判别阈值有没有分离带**。

    python verify2_threshold.py [Bearing 目录] [--frames=N]

## 为什么必须单独验这一条

《结构 VQT 与波形数组定义》§3.1 的核心论断是：

> 丢样本（SDT 那类）会让 T 序列出现异常空档，**算法当场看得出**。

第一轮只在 1 帧上、用 1 种抽稀方式试过就下了结论，**不够**：真语料里本来就有时钟毛刺
（FEMTO `acc_00001` 有 158 个 30 μs 的间隔）。若毛刺与真实丢样在数值上重叠，
任何阈值要么误报要么漏报 ⇒ **该论断必须降级**。

## ★第一次验证失败，是判据错了（记下来，免得再犯）

第一版判据是「间隔相对**整段平均**的偏离」，结论是"无分离带、论断不成立"。
查下来是**方法错**：

> 抽稀会把平均间隔一起抬高 —— **基准跟着被污染**，效应自己抵消掉。
> 于是被拉长的间隔反而显得"接近新均值"，而正常间隔显得偏短。

⇒ 正确判据不是"偏离均值"，而是**间隔是否量化到单一基值**：
完好数据全部 ≈1 倍基准；丢一个样本立刻出现 ≈2 倍，丢两个 ≈3 倍。
基准取**间隔的低分位数** —— 因为**抽稀不会制造更短的间隔**，低分位对抽稀免疫。
"""

from __future__ import annotations

import sys
from pathlib import Path


def load_rows(path: Path):
    """CSV → [(微秒时刻, 水平, 垂直)]。微秒列有科学计数法，必须先 float。"""
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        c = line.strip().split(",")
        if len(c) >= 6:
            h, m, s, us = int(c[0]), int(c[1]), int(c[2]), int(float(c[3]))
            out.append((((h * 60 + m) * 60 + s) * 1_000_000 + us, float(c[4]), float(c[5])))
    return out


def base_interval(ts, pct=10):
    """基准采样间隔 = 间隔的低分位数。

    为什么不用别的：
      · **均值**：抽稀后被抬高 —— 这正是第一次验证失败的根因；
      · **众数**：1 μs 分辨率下在 39/40 之间跳（定义文档 §8.1 踩过）；
      · **低分位**：抽稀**不会制造更短的间隔**，故对抽稀免疫；
        又因绝大多数间隔仍在基值上，也不被少量毛刺带偏。
    """
    d = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    return d[max(0, len(d) * pct // 100)] if d else 0


def multiples(ts):
    """各间隔 ÷ 基准。完好 ≈1；丢一个样本 ≈2；丢两个 ≈3。"""
    b = base_interval(ts)
    if not b:
        return []
    return [(ts[i + 1] - ts[i]) / b for i in range(len(ts) - 1)]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = Path(args[0] if args else
                "/mnt/d/download/AIRef/datasets/femto/Learning_set/Bearing1_1")
    limit = 0
    for a in sys.argv[1:]:
        if a.startswith("--frames="):
            limit = int(a.split("=", 1)[1])

    accs = sorted(base.glob("acc_*.csv"))
    if limit:
        accs = accs[:limit]
    if not accs:
        print(f"找不到数据：{base}")
        return 2
    print(f"语料：{base}")
    print(f"扫描帧数：{len(accs)}（★全扫，不是挑一帧）\n")

    # ── A. 完好数据的倍数分布 ────────────────────────────────────────────
    worst, worst_at, hist, n_int = 0.0, None, {}, 0
    for f in accs:
        ts = [r[0] for r in load_rows(f)]
        for i, m in enumerate(multiples(ts)):
            n_int += 1
            k = round(m, 1)
            hist[k] = hist.get(k, 0) + 1
            if m > worst:
                worst, worst_at = m, (f.name, i)
    print("A. 完好数据（无任何抽稀）的 间隔÷基准")
    print(f"   间隔总数 {n_int:,}")
    for k in sorted(hist)[:8]:
        print(f"   倍数 ≈ {k:>4.1f} : {hist[k]:>9,} 次")
    print(f"   ★最大倍数 {worst:.3f}，出现在 {worst_at[0]} 第 {worst_at[1]} 个间隔\n")

    # ── B. 抽稀后的倍数 ──────────────────────────────────────────────────
    print("B. 丢样本（SDT 那类按值抽稀）后的 间隔÷基准，用 " + accs[0].name)
    rows = load_rows(accs[0])
    head = "   {:>8} {:>7} {:>7} {:>9} {:>14}".format(
        "抽稀容差", "剩余", "丢掉", "最大倍数", ">1.5倍个数")
    print(head)
    overs = []
    for tol_v in (0.005, 0.01, 0.02, 0.05, 0.1, 0.2):
        kept = [rows[0]]
        for r in rows[1:]:
            if abs(r[1] - kept[-1][1]) > tol_v:
                kept.append(r)
        if len(kept) < 3:
            continue
        ms = multiples([r[0] for r in kept])
        mx = max(ms) if ms else 0.0
        over = sum(1 for m in ms if m > 1.5)
        overs.append(over)
        print("   {:>8.3f} {:>7} {:>7} {:>9.2f} {:>14,}".format(
            tol_v, len(kept), len(rows) - len(kept), mx, over))

    # ── 分离带 ───────────────────────────────────────────────────────────
    print()
    print("=" * 68)
    print(f"完好数据最大倍数 = {worst:.3f}（{n_int:,} 个间隔，{len(accs)} 帧全扫）")
    print(f"各抽稀档 >1.5 倍的间隔数 = {overs}")
    ok = worst < 1.5 and bool(overs) and all(m > 0 for m in overs)
    if ok:
        print("★分离带成立：完好数据无一超过 1.5 倍；而任何强度的抽稀都立刻产生 ≥2 倍间隔")
        print("  ⇒ 判据：**存在 >1.5 倍基准间隔者 ⇒ 这段数据被丢过样本**")
        print("✅ 「等间隔可验证」成立")
        return 0
    print("❌ 分离带不成立 ⇒ 该论断必须降级")
    return 1


if __name__ == "__main__":
    sys.exit(main())
