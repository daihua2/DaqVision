"""验证《结构 VQT 与波形数组定义》的四条 —— 用 FEMTO 真语料，不用合成数据。

    python verify.py [FEMTO 的 Bearing 目录]

★为什么必须用真数据：本定义的全部要害是"采样率与帧边界是**数据本身的事实**，
  不是声明出来的元数据"。拿合成数据验，等于自己先规定好事实再去证明它 ——
  那什么都证明不了。

验的四条（对应定义文档 §8）：
  ① 帧能从 T 序列里**自然浮现**，不靠任何元数据；
  ② 采样率可从相邻 T 推出，且与标称 25.6 kHz 一致；
  ③ 丢样本（SDT 那类）能从 T 序列**当场发现**；
  ④ 记录数与字节量落成实数，把"唯一要盯的代价"量出来。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

NOMINAL_HZ = 25_600.0       # FEMTO 标称采样率（只用来**对照**，不参与推导）
NOMINAL_ROWS = 2560
NOMINAL_PERIOD_S = 10.0     # 标称帧间隔


def parse_row(line: str):
    """CSV 一行 → (微秒时刻, 水平, 垂直)。

    ★列是 `时,分,秒,微秒,水平,垂直`。微秒列有科学计数法（`1.6562e+005`），
      float() 认得，int() 不认得 —— 这一处踩过。
    """
    p = line.strip().split(",")
    if len(p) < 6:
        return None
    h, m, s, us = int(p[0]), int(p[1]), int(p[2]), int(float(p[3]))
    return (((h * 60 + m) * 60 + s) * 1_000_000 + us, float(p[4]), float(p[5]))


def load_as_struct_vqts(path: Path):
    """把一个 acc 文件读成**结构 VQT 列表** —— 一行一条，T 归 VQT，两路归结构。

    这正是定义二说的形态：波形 = 连续时间段内结构 VQT 的数组。
    """
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        r = parse_row(line)
        if r is not None:
            out.append({"T_us": r[0], "V": {"acc_h": r[1], "acc_v": r[2]}})
    return out


def split_frames(vqts, gap_factor=5.0):
    """★验证①：**只看 T 序列**把帧切出来，不用任何元数据。

    做法：相邻间隔超过"常见间隔 × gap_factor"即认为换帧。
    ★注意这里**没有用到** NOMINAL_HZ / NOMINAL_ROWS / 文件名 —— 全靠数据自己。
    """
    if len(vqts) < 2:
        return [vqts] if vqts else []
    deltas = [vqts[i + 1]["T_us"] - vqts[i]["T_us"] for i in range(len(vqts) - 1)]
    typical = Counter(deltas).most_common(1)[0][0]        # 众数 = 正常采样间隔
    frames, cur = [], [vqts[0]]
    for i, d in enumerate(deltas):
        if d > typical * gap_factor:
            frames.append(cur); cur = []
        cur.append(vqts[i + 1])
    if cur:
        frames.append(cur)
    return frames


def infer_rate_hz(frame):
    """★验证②：从相邻 T 推采样率 —— 用**整帧平均**，不用众数。

    ★这一处是被真语料纠正的：FEMTO 的真实间隔是 1/25600 = 39.0625 μs，
      而时间戳只有 **1 μs 分辨率** ⇒ 间隔必然在 39/40 之间抖动
      （实测 40×1575、39×825，另有 158 个 30 μs 的时钟毛刺）。
      取**众数**得 40 μs → 25,000 Hz，偏 2.3%；
      取**整帧平均**（首末跨度 ÷ 间隔数）得 39.0606 μs → 25,601 Hz，偏 0.005%。
    ⇒ 采样率是**整段的性质**，不能用单个间隔去代表它。
    """
    if len(frame) < 2:
        return 0.0
    span = frame[-1]["T_us"] - frame[0]["T_us"]
    return 1_000_000.0 * (len(frame) - 1) / span if span else 0.0


def find_gaps(frame, tol=0.5):
    """★验证③：帧内有没有"不该有的空档"（= 样本被丢过）。

    判据：间隔偏离众数超过 tol（相对），即异常。
    ★这就是本定义最要害的一条 —— 等间隔**可验证**，不必信任任何声明。
    """
    if len(frame) < 3:
        return []
    deltas = [frame[i + 1]["T_us"] - frame[i]["T_us"] for i in range(len(frame) - 1)]
    # ★基准同样用平均而非众数（见 infer_rate_hz）。真语料里 1 μs 分辨率造成的
    #   39/40 抖动是**正常**的，不该报成异常 —— 故 tol 要大于那个抖动幅度。
    typical = (frame[-1]["T_us"] - frame[0]["T_us"]) / (len(frame) - 1)
    return [(i, d, d / typical) for i, d in enumerate(deltas)
            if abs(d - typical) > typical * tol]


def main():
    base = Path(sys.argv[1] if len(sys.argv) > 1 else
                r"D:\download\AIRef\datasets\femto\Learning_set\Bearing1_1")
    if not base.is_dir():
        print(f"找不到目录：{base}"); return 2
    accs = sorted(base.glob("acc_*.csv"))
    temps = sorted(base.glob("temp_*.csv"))
    print(f"语料：{base}")
    print(f"  加速度文件 {len(accs)} 个 / 温度文件 {len(temps)} 个\n")

    # ── ① 帧从 T 序列自然浮现 ────────────────────────────────────────────
    # 把**连续三个文件**拼成一条时间序列再切 —— 若定义成立，应当切回 3 帧。
    merged = []
    for f in accs[:3]:
        merged += load_as_struct_vqts(f)
    frames = split_frames(merged)
    print("① 帧能否只凭 T 序列浮现（把 3 个文件拼成一条序列再切）")
    print(f"   拼接后 {len(merged)} 条结构 VQT → 切出 {len(frames)} 帧，"
          f"各帧条数 {[len(x) for x in frames]}")
    ok1 = len(frames) == 3 and all(len(x) == NOMINAL_ROWS for x in frames)
    print(f"   {'✅' if ok1 else '❌'} 期望 3 帧 × {NOMINAL_ROWS} 条"
          f"（★未使用采样率/帧长/文件名等任何元数据）\n")

    # ── ② 采样率从相邻 T 推出 ────────────────────────────────────────────
    rates = [infer_rate_hz(fr) for fr in frames]
    print("② 采样率能否从相邻 T 推出")
    for i, r in enumerate(rates):
        print(f"   帧{i + 1}: 推出 {r:,.1f} Hz")
    ok2 = all(abs(r - NOMINAL_HZ) / NOMINAL_HZ < 0.02 for r in rates)
    print(f"   标称 {NOMINAL_HZ:,.0f} Hz  {'✅' if ok2 else '❌'} 偏差 <2%\n")

    # 帧间隔也该浮现出来
    starts = [fr[0]["T_us"] for fr in frames]
    periods = [(starts[i + 1] - starts[i]) / 1e6 for i in range(len(starts) - 1)]
    print(f"   帧间隔实测 {periods} 秒（标称 {NOMINAL_PERIOD_S}）")
    ok2b = all(abs(p - NOMINAL_PERIOD_S) < 0.05 for p in periods)
    print(f"   {'✅' if ok2b else '❌'} 帧周期同样是数据自证的\n")

    # ── ③ 丢样本能否当场发现 ────────────────────────────────────────────
    print("③ 丢样本（SDT 那类）能否从 T 序列发现")
    intact = frames[0]
    print(f"   原帧 {len(intact)} 条，异常间隔 {len(find_gaps(intact))} 处")
    # 构造：模拟 SDT 按值抽稀 —— 丢掉幅值变化小的那些
    thinned = [intact[0]]
    for a, b in zip(intact, intact[1:]):
        if abs(b["V"]["acc_h"] - thinned[-1]["V"]["acc_h"]) > 0.05:
            thinned.append(b)
    gaps = find_gaps(thinned)
    print(f"   抽稀后 {len(thinned)} 条（丢了 {len(intact) - len(thinned)} 条），"
          f"异常间隔 {len(gaps)} 处")
    ok3 = len(find_gaps(intact)) == 0 and len(gaps) > 0
    print(f"   {'✅' if ok3 else '❌'} 原帧无异常、抽稀后当场暴露 "
          f"—— ★这正是「一帧一个值」发现不了的\n")

    # ── ④ 代价落成实数 ──────────────────────────────────────────────────
    print("④ 代价（本定义唯一要盯的一处）")
    n_rows = len(accs) * NOMINAL_ROWS
    span_s = (len(accs) - 1) * NOMINAL_PERIOD_S
    print(f"   本轴承：{len(accs)} 帧 × {NOMINAL_ROWS} 行 = {n_rows:,} 条结构 VQT")
    print(f"   覆盖时长约 {span_s / 3600:.2f} 小时 ⇒ 平均 {n_rows / span_s:,.0f} 条/秒")
    print(f"   净载荷（2×f32）：{n_rows * 8 / 1e6:,.1f} MB")
    print(f"   ★逐帧打包则只有 {len(accs):,} 条 —— 条数差 {NOMINAL_ROWS} 倍，")
    print("     但那样采样率与帧边界就只能靠声明，③ 那条便失效。")

    print("\n" + "=" * 64)
    allok = ok1 and ok2 and ok2b and ok3
    print(("✅ 四条全部通过：帧、采样率、帧周期都是**数据自证**的，"
           "丢样本当场可见" if allok else "❌ 有未通过项，见上"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
