"""经典算法振动诊断 —— ISO 20816-3 烈度判级 + 方向性提示。

> 2026-09-17 由 `vibration_lowfreq` 拆出（用户定）：原文件把「经典判据」与「自训基线」
> 两种完全不同的算法合在一起。现在设备参数改在设备上填一次、界面也分设两个选项，
> 合并的唯一理由（共用输入、参数不填两遍）不再成立。另一半见 `vibration_baseline.py`。

依据：
- ISO 20816-3:2022 原文，核对记录见 `AIIntegration/doc/ISO20816-3机组分类与限值.md`；
- 本模块的配置与取舍定案见 `AIIntegration/doc/诊断配置流程定案.md`「模块 1、2 的内容」。

---

## 0.1 做什么

| 结论 | 说明 |
| --- | --- |
| 速度最大值、最大值所在轴 | 窗口内全部已绑速度通道（含第二测点）的最大值 |
| ISO 烈度区（文字 / 数值）、距下一档余量 | ISO 20816-3:2022 附录 A 表 A.1、A.2 |
| 轴向/径向比、方向性提示 | ★**经验判据，只作提示，不是结论**（实测单测点判不对中方向 0.767，基线 0.607） |
| 判据摘要 | 每一条"为什么没给"都写明 |

**不需要训练、不需要历史状态、不需要第三方库** —— 保存即出结论。

## 0.2 定案里的四条，落在代码的哪里

1. **机组类别只有第 1、2 组**（ISO 20816-3:2022 删去了旧版的泵类第 3、4 组，泵归 ISO 10816-7）；
   另有「不适用」—— 设备不在 ISO 20816-3 范围内时选它，**不出 ISO 分级**。
2. **额定转速 < 600 r/min**：照常出分级，但判据摘要注明「低速设备，标准要求另看位移，本结果仅供参考」。
   原文附录 A：低速机器评价频带为 2~1000 Hz，且应同时按速度与位移评价；
   我方拿到的是传感器算好的有效值，频带由传感器决定，我方改不了。
3. **方向性保留，显示为「提示」**。
4. **报警防抖先不做**。

## 0.3 纪律：缺什么落什么码，绝不猜缺省

参数没填、口径没确认、样本全是坏值 —— 每一种都对应一条特定的坏质量结论，
而不是"给个看起来合理的数"。**猜错的 ISO 分级会把"该停机"说成"可长期运行"，而且从数值上看不出来。**
逐条结论各自判质量：缺轴向只让方向性那两条落码，ISO 那几条照出。
"""

from __future__ import annotations

from datetime import datetime

# ★域模块 import 骨架一律用**绝对包名**：装载器按文件路径 exec，相对 import 会当场炸。
from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec

# ───────────────────────── ISO 20816-3:2022 附录 A 限值 ─────────────────────────
#
# 速度有效值 mm/s。三个边界依次是 A/B、B/C、C/D（表 A.1 第 1 组、表 A.2 第 2 组）。
#   A 新投运 │ B 可不受限长期运行 │ C 不宜长期连续运行 │ D 足以造成损坏
#
# ★这张表是**判据本身**，不是可调参数：动它等于改国标结论。
_ISO_LIMITS: dict[tuple[str, str], tuple[float, float, float]] = {
    ("1", "rigid"):    (2.3, 4.5, 7.1),
    ("1", "flexible"): (3.5, 7.1, 11.0),
    ("2", "rigid"):    (1.4, 2.8, 4.5),
    ("2", "flexible"): (2.3, 4.5, 7.1),
}
_GROUP_NA = "na"

#: 低速阈值（r/min）。ISO 20816-3:2022 附录 A：低于它评价频带改为 2~1000 Hz 且应另看位移。
LOW_SPEED_RPM = 600.0

_AXES = ("x", "y", "z")
#: 测点后缀：第一测点无后缀，第二测点为 "2"（定案 2.1）。
_POINTS = ("", "2")

#: 方向性判据阈值。**经验值**（改造方案 §3 轨A④ 现象/倾向表），不是国标。
_AXIAL_SIGNIFICANT = 0.5   # 轴向 / 径向 ≥ 此值 ⇒ 轴向占比异常
_RADIAL_BALANCED = 0.8     # 径向两轴互比落在 [0.8, 1/0.8] ⇒ 视为各向同性


def _vel_role(axis: str, point: str) -> str:
    return f"{axis}{point}_vel"


class VibrationIso(Domain):
    """经典算法振动诊断。"""

    key = "vibration_iso"
    display = "经典算法振动诊断"
    version = "1.0.0"

    # ── 声明 ──────────────────────────────────────────────────────────────
    def declare(self) -> Declaration:
        inputs = []
        for point, name in (("", "测点 1"), ("2", "测点 2")):
            for axis in _AXES:
                inputs.append(InputSpec(
                    role=_vel_role(axis, point), unit="mm/s",
                    # 至少要有测点 1 的 X 轴；其余都可选。
                    required=(point == "" and axis == "x"),
                    display=f"{name} {axis.upper()} 轴速度",
                    description=("速度有效值。至少选测点 1 的 X 轴速度；第二测点不配即按单测点判"
                                 if point == "" and axis == "x" else "速度有效值，可不选")))
        return Declaration(
            inputs=tuple(inputs),
            params=(
                ParamSpec(
                    key="iso_group", display="ISO 20816-3 机组类别", value_type="enum",
                    choices=("1", "2", _GROUP_NA),
                    choice_displays=(
                        "第 1 组：大型机组，额定功率 >300 kW；电机轴中心高 H≥315 mm",
                        "第 2 组：中型机组，额定功率 >15 kW 且 ≤300 kW；电机轴中心高 160≤H<315 mm",
                        "不适用：设备不在 ISO 20816-3 范围内（如 ≤15 kW、回转动力泵、往复机械等）",
                    ),
                    required=True, level="machine",
                    description="决定 A/B/C/D 边界值。没有缺省：选错会把该停机说成可长期运行。"
                                "选「不适用」则不出 ISO 分级"),
                ParamSpec(
                    key="mount_type", display="支承方式", value_type="enum",
                    choices=("rigid", "flexible"), choice_displays=("刚性", "柔性"),
                    required=True, level="machine",
                    description="机器与支承系统在测量方向上的最低固有频率比转频高 25% 以上为刚性，否则柔性"),
                ParamSpec(
                    key="rated_speed_rpm", display="额定转速", value_type="float", unit="r/min",
                    required=False, level="machine",
                    description="低于 600 r/min 时照常出分级，但判据摘要注明结果仅供参考（标准要求另看位移）"),
                ParamSpec(
                    key="vel_is_rms", display="速度口径确认为有效值", value_type="enum",
                    choices=("true", "false"),
                    choice_displays=("是，已确认为有效值", "否 / 未确认（峰值或手册未注明）"),
                    required=True, level="position",
                    description="ISO 判级要求速度有效值。选「否」时不出 ISO 分级，只给数值"),
                ParamSpec(
                    key="axial_axis", display="轴向是哪一轴", value_type="enum",
                    choices=("x", "y", "z"), choice_displays=("X 轴", "Y 轴", "Z 轴"),
                    required=True, level="position",
                    description="沿转轴方向的那一轴，两个测点按同一方向理解。没有缺省：猜错会把不对中说成不平衡。"
                                "缺它只影响方向性两条，ISO 分级照出"),
            ),
            outputs=(
                OutputSpec(key="vel_max", display="速度最大值", value_type="float", unit="mm/s",
                           description="窗口内全部已选速度通道的最大值"),
                OutputSpec(key="dominant_axis", display="最大值所在轴", value_type="string",
                           description="x / y / z；第二测点记为 x2 / y2 / z2"),
                OutputSpec(key="iso_zone", display="ISO 烈度区", value_type="string",
                           description="A 新投运 / B 可长期运行 / C 不宜长期连续运行 / D 足以造成损坏"),
                OutputSpec(key="iso_zone_code", display="ISO 烈度区(数值)", value_type="int",
                           description="1=A 2=B 3=C 4=D，给趋势曲线与报警门限用"),
                OutputSpec(key="iso_margin", display="距下一档余量", value_type="float", unit="mm/s",
                           description="离更差一档的边界还有多远；已在 D 区时为负"),
                OutputSpec(key="axial_ratio", display="轴向/径向比", value_type="float",
                           description="轴向 ÷ 径向两轴较大者，取最大值所在测点"),
                OutputSpec(key="direction_hint", display="方向性提示", value_type="string",
                           description="★提示，不是结论：无频谱数据，仅凭三轴比例判断倾向"),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="用了哪几路、判到哪一档、为什么没给"),
            ),
        )

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        peaks, times, bad, empty = _peaks(frame)

        # ① 一路好样本都没有 —— 区分"没数据"与"有数据但全是坏值"，两者处置相反。
        if not peaks:
            q = Quality.INPUT_BAD if bad else Quality.NO_INPUT
            why = (f"{'/'.join(bad)} 有样本但质量码全不可信"
                   if bad else f"{'/'.join(empty) or '所有已选通道'} 在本窗口内没有样本")
            return _all_bad(self, frame, q, f"未出结论：{why}")

        dominant = max(peaks, key=lambda k: peaks[k])
        vel_max = peaks[dominant]
        t = times[dominant]                    # ★T 取那笔样本自己的时刻

        out: list[Finding] = [
            Finding(key="vel_max", value=round(vel_max, 4), quality=Quality.OK, t=t),
            Finding(key="dominant_axis", value=dominant, quality=Quality.OK, t=t),
        ]

        # ② ISO 分级
        group = frame.params.get("iso_group", "").strip()
        mount = frame.params.get("mount_type", "").strip()
        is_rms = frame.params.get("vel_is_rms", "").strip().lower()
        limits = _ISO_LIMITS.get((group, mount))
        if group == _GROUP_NA:
            # ★不适用 = 判据立不起来，正是 CONFIG_INCOMPLETE 的定义；不另立新码。
            iso_bad = "机组类别为「不适用」：设备不在 ISO 20816-3 范围内，不给分级"
        elif is_rms != "true":
            iso_bad = (f"速度口径未确认为有效值（vel_is_rms={is_rms or '未填'}）"
                       "—— 拿峰值套有效值判据会整档偏高，故不给分级")
        elif limits is None:
            iso_bad = (f"参数不全或取值非法：iso_group={group or '未填'} "
                       f"mount_type={mount or '未填'}，查不到 ISO 边界")
        else:
            iso_bad = ""

        if iso_bad:
            out += _bad_group(("iso_zone", "iso_zone_code", "iso_margin"), Quality.CONFIG_INCOMPLETE, t)
        else:
            zone, code, margin = _classify(vel_max, limits)  # type: ignore[arg-type]
            out += [
                Finding(key="iso_zone", value=zone, quality=Quality.OK, t=t),
                Finding(key="iso_zone_code", value=code, quality=Quality.OK, t=t),
                Finding(key="iso_margin", value=round(margin, 4), quality=Quality.OK, t=t),
            ]

        # ③ 方向性提示：优先取最大值所在测点，算不出再看另一个测点。
        axial = frame.params.get("axial_axis", "").strip().lower()
        dir_bad, dir_q, ratio, hint, dir_point = _direction_any(peaks, axial, dominant)
        if dir_bad:
            out += _bad_group(("axial_ratio", "direction_hint"), dir_q, t)
        else:
            out += [
                Finding(key="axial_ratio", value=round(ratio, 4), quality=Quality.OK, t=t),
                Finding(key="direction_hint", value=hint, quality=Quality.OK, t=t),
            ]

        # ④ 判据摘要
        used = "/".join(f"{k}={peaks[k]:.3f}" for k in sorted(peaks))
        parts = [f"取窗口内最大值：{used}；最大在 {dominant} {vel_max:.3f} mm/s"]
        if iso_bad:
            parts.append(f"ISO 分级未给：{iso_bad}")
        else:
            zone, _c, margin = _classify(vel_max, limits)  # type: ignore[arg-type]
            parts.append(
                f"ISO 20816-3:2022（第 {group} 组 / {'刚性' if mount == 'rigid' else '柔性'}支承）判为 {zone} 区，"
                + (f"已超出 C/D 界 {-margin:.3f} mm/s" if margin < 0 else f"距下一档还有 {margin:.3f} mm/s"))
            parts.append(_speed_note(frame.params.get("rated_speed_rpm", "")))
        if dir_bad:
            parts.append(f"方向性提示未给：{dir_bad}")
        else:
            parts.append(f"方向性提示（测点{'2' if dir_point == '2' else '1'}）：{hint}")
        parts.append("★无频谱数据，方向性仅为提示而非结论")
        if bad:
            parts.append(f"降级：{'/'.join(bad)} 本窗口全是坏值，未参与判定")
        if empty:
            parts.append(f"降级：{'/'.join(empty)} 本窗口无样本")
        out.append(Finding(key="evidence", value="；".join(p for p in parts if p),
                           quality=Quality.OK, t=t))
        return out


# ─────────────────────────────── 内部函数 ───────────────────────────────

def _peaks(frame: Frame) -> tuple[dict[str, float], dict[str, datetime], list[str], list[str]]:
    """逐通道取窗口内**可信样本**的最大值。键为 `x`/`y`/`z`/`x2`/`y2`/`z2`。

    返回 `(通道→峰值, 通道→该峰值样本时刻, 全是坏值的通道, 选了但无样本的通道)`。
    ★只有 `Quality.OK` 的样本参与；全坏的通道单独记出来，不当作没发生。
    ★没选的通道不记入"无样本" —— 那与"选了但没数据"不是一回事。
    """
    peaks: dict[str, float] = {}
    times: dict[str, datetime] = {}
    bad: list[str] = []
    empty: list[str] = []
    for point in _POINTS:
        for axis in _AXES:
            role = _vel_role(axis, point)
            if role not in frame.channels:
                continue
            key = f"{axis}{point}"
            samples = list(frame.channels[role])
            if not samples:
                empty.append(key)
                continue
            best_v: float | None = None
            best_t: datetime | None = None
            for s in samples:
                if s.quality is not Quality.OK:
                    continue
                v = _as_float(s.value)
                if v is None:
                    continue
                if best_v is None or v > best_v:
                    best_v, best_t = v, s.t
            if best_v is None or best_t is None:
                bad.append(key)
            else:
                peaks[key] = best_v
                times[key] = best_t
    return peaks, times, bad, empty


def _as_float(value) -> float | None:
    """★不用 `float(x) except: 0.0`：那会让坏值静默变成 0，而 0 在速度上是最好的读数。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    return None


def _classify(vel: float, limits: tuple[float, float, float]) -> tuple[str, int, float]:
    """按边界表判区；边界值本身归好的那一档（≤）。D 区余量为负。"""
    ab, bc, cd = limits
    if vel <= ab:
        return "A", 1, ab - vel
    if vel <= bc:
        return "B", 2, bc - vel
    if vel <= cd:
        return "C", 3, cd - vel
    return "D", 4, cd - vel


def _direction_any(peaks: dict[str, float], axial: str, dominant: str
                   ) -> tuple[str, Quality, float, str, str]:
    """返回 `(未给原因, 坏质量码, 比值, 提示, 所用测点)`；原因为空表示算出来了。"""
    if axial not in _AXES:
        return (f"轴向 axial_axis={axial or '未填'} 非法 —— 不知道哪根是轴向就分不开不对中与不平衡",
                Quality.CONFIG_INCOMPLETE, 0.0, "", "")
    dom_point = dominant[1:] if len(dominant) > 1 else ""
    order = [dom_point] + [p for p in _POINTS if p != dom_point]
    first_bad: tuple[str, Quality] | None = None
    for point in order:
        ax_key = f"{axial}{point}"
        radial_keys = [f"{a}{point}" for a in _AXES if a != axial and f"{a}{point}" in peaks]
        if ax_key not in peaks:
            if any(f"{a}{point}" in peaks for a in _AXES):
                first_bad = first_bad or (f"测点{point or '1'} 轴向轴 {axial} 本窗口没有可信样本",
                                          Quality.INPUT_BAD)
            continue
        if not radial_keys:
            first_bad = first_bad or (f"测点{point or '1'} 只有轴向轴有数据，没有径向轴可比",
                                      Quality.INSUFFICIENT_SAMPLES)
            continue
        ratio, hint = _direction(peaks, ax_key, radial_keys)
        return "", Quality.OK, ratio, hint, point
    why, q = first_bad or ("没有可用于方向性判断的测点", Quality.INSUFFICIENT_SAMPLES)
    return why, q, 0.0, "", ""


def _direction(peaks: dict[str, float], ax_key: str, radial_keys: list[str]) -> tuple[float, str]:
    """方向性倾向。经验判据，不是国标。"""
    radial_max = max(peaks[k] for k in radial_keys)
    if radial_max <= 0:
        return 0.0, "径向读数为 0，比值无意义；仅轴向有振动，建议现场核对安装与接线"
    ratio = peaks[ax_key] / radial_max
    # ★均衡这一档必须先判：三轴均衡时轴向比同样 ≥ _AXIAL_SIGNIFICANT，先判轴向偏高会把
    #   每一台各向同性的机器都说成不对中。
    if len(radial_keys) == 2:
        a, b = (peaks[k] for k in radial_keys)
        lo, hi = (a, b) if a <= b else (b, a)
        if hi > 0 and lo / hi >= _RADIAL_BALANCED and ratio >= _RADIAL_BALANCED:
            return ratio, "三轴接近均衡、无明显方向性 —— 提示：可能是松动 / 基础问题"
    if ratio >= _AXIAL_SIGNIFICANT:
        return ratio, "轴向占比偏高 —— 提示：可能是不对中 / 联轴器问题"
    return ratio, "径向主导且轴向较弱 —— 提示：可能是不平衡"


def _speed_note(raw: str) -> str:
    """定案 1.2：低速设备照常出分级，但注明仅供参考。"""
    raw = (raw or "").strip()
    if not raw:
        return "额定转速未填，无法判断是否低速设备（<600 r/min 时标准要求另看位移）"
    try:
        rpm = float(raw)
    except ValueError:
        return f"额定转速 {raw!r} 不是数，无法判断是否低速设备"
    if rpm < LOW_SPEED_RPM:
        return (f"低速设备（额定 {rpm:g} r/min < 600），标准要求另看位移，本结果仅供参考")
    return ""


def _bad_group(keys: tuple[str, ...], q: Quality, t: datetime) -> list[Finding]:
    """给一组结论落同一个坏质量码。**值为 None** —— 坏质量下不许有值。"""
    return [Finding(key=k, value=None, quality=q, t=t) for k in keys]


def _all_bad(domain: Domain, frame: Frame, q: Quality, why: str) -> list[Finding]:
    """一条都算不出来时：每个声明过的输出各落一个坏值锚点，外加一句人话。
    ★不是"什么都不发"：那在下游看来是"这段没数据"，而真相是"这段算不出来"。"""
    t = frame.t_end
    keys = [o.key for o in domain.declare().outputs if o.key != "evidence"]
    out = [Finding(key=k, value=None, quality=q, t=t) for k in keys]
    out.append(Finding(key="evidence", value=why, quality=q, t=t))
    return out
