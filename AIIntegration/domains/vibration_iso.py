"""经典算法振动诊断 —— 按国标做振动烈度判级（工业机器 / 旋转动力泵）+ 方向性提示。

> 2026-09-17 由 `vibration_lowfreq` 拆出（用户定）：原文件把「经典判据」与「自训基线」
> 两种完全不同的算法合在一起。另一半见 `vibration_baseline.py`。
>
> ★2026-09-18 判级依据改用**国标**（用户定，与 AICloud `C-43` 的「有国标按国标」一致）：
> ISO 20816-3:2022 尚无对应国标，故
> - 工业机器按 **GB/T 6075.3-2011**（等同采用 ISO 10816-3:2009）；
> - 旋转动力泵按 **GB/T 6075.7-2015**（等同采用 ISO 10816-7:2009），本日新增。

依据与定案：`AIIntegration/doc/诊断配置流程定案.md`「模块 1、2 的内容」「判级改用国标与泵」。

---

## 0.1 做什么

| 结论 | 说明 |
| --- | --- |
| 速度最大值、最大值所在轴 | 窗口内全部已绑速度通道（含第二测点）的最大值 |
| 烈度区（文字 / 数值）、距下一档余量 | 工业机器按 GB/T 6075.3；泵按 GB/T 6075.7 |
| 轴向/径向比、方向性提示 | ★**经验判据，只作提示，不是结论**（实测单测点判不对中方向 0.767，基线 0.607） |
| 判据摘要 | 每一条"为什么没给"都写明 |

**不需要训练、不需要历史状态、不需要第三方库** —— 保存即出结论。

## 0.2 走哪套判据：看设备上填的是「泵类别」还是「机器分组」

| 设备上 | 判据 | 需要 |
| --- | --- | --- |
| 泵类别 = 第Ⅰ类 / 第Ⅱ类 | GB/T 6075.7-2015 | 额定功率（按 200 kW 分档）；**不看支承方式、不限转速** |
| 否则 | GB/T 6075.3-2011 | 机器分组（第 1 / 2 组）、支承方式 |

两者都填成有效值是**自相矛盾**，不猜哪个对，落 `CONFIG_INCOMPLETE` 并说清。
★这几项参数在声明上都不是必填（泵不需要机器分组、工业机器不需要泵类别），
  **必填性按所走判据在推理时逐条检查**，缺哪个说哪个 —— 仍然没有任何缺省值。

## 0.3 定案里的几条，落在代码的哪里

1. **机器分组只有第 1、2 组**，另有「不适用」—— 设备不在标准范围内时选它，**不出烈度分级**。
   适用范围（转速 15000 r/min、汽轮机/发电机 50 MW 等）由设备模型判定，本模块不另判。
2. **工业机器额定转速 < 600 r/min**：照常出分级，但判据摘要注明「低速设备，标准要求另看位移，本结果仅供参考」。
3. **方向性保留，显示为「提示」**。
4. **报警防抖先不做**。
5. **停机判定**（定案 1.6，2026-09-18）：填了「停机门槛」且速度最大值 < 门槛 ⇒ 停机。
   停机时文字结论写「停机」（烈度区、方向性提示、运行状态），烈度区数值写 0；
   **数值类结论（余量、轴向比）这一拍不写**，点上保留停机前最后一值及其时刻，界面凭「运行状态」置灰
   （用户 2026-09-18 定：不落坏质量、不新增状态码）。不填门槛不判，运行状态写「未判」。

## 0.4 纪律：缺什么落什么码，绝不猜缺省

参数没填、口径没确认、样本全是坏值 —— 每一种都对应一条特定的坏质量结论，
而不是"给个看起来合理的数"。**猜错的烈度分级会把"该停机"说成"可长期运行"，而且从数值上看不出来。**
逐条结论各自判质量：缺轴向只让方向性那两条落码，烈度分级那几条照出。
"""

from __future__ import annotations

from datetime import datetime

# ★域模块 import 骨架一律用**绝对包名**：装载器按文件路径 exec，相对 import 会当场炸。
from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec

# ─────────────────────────────── 限值表 ───────────────────────────────
#
# 速度有效值 mm/s。三个边界依次是 A/B、B/C、C/D。
#   A 新投运 │ B 可长期运行 │ C 不宜长期连续运行 │ D 足以造成损坏
# ★这两张表是**判据本身**，不是可调参数：动它等于改国标结论。

#: GB/T 6075.3-2011（等同 ISO 10816-3:2009）工业机器，(机器分组, 支承方式) → 边界。
#: 数值与 ISO 20816-3:2022 表 A.1、A.2 相同（2026-09-17 核对原文）。
_MACHINE_LIMITS: dict[tuple[str, str], tuple[float, float, float]] = {
    ("1", "rigid"):    (2.3, 4.5, 7.1),
    ("1", "flexible"): (3.5, 7.1, 11.0),
    ("2", "rigid"):    (1.4, 2.8, 4.5),
    ("2", "flexible"): (2.3, 4.5, 7.1),
}

#: GB/T 6075.7-2015（等同 ISO 10816-7:2009）旋转动力泵，(泵类别, 功率档) → 边界。
#: ★出处：AICloud `C-43 §4` 转引 Europump《Guidelines on Pump Vibration》（2013）对 ISO 10816-7 的摘录，
#:   **标准原文尚未取得、未核对**（用户 2026-09-18 定：先按此实现）。取得原文后须逐值核对。
_PUMP_LIMITS: dict[tuple[str, str], tuple[float, float, float]] = {
    ("1", "le200"): (2.5, 4.0, 6.6),
    ("1", "gt200"): (3.5, 5.0, 7.6),
    ("2", "le200"): (3.2, 5.1, 8.5),
    ("2", "gt200"): (4.2, 6.1, 9.5),
}
PUMP_POWER_SPLIT_KW = 200.0

_NA = "na"
_MACHINE_STD = "GB/T 6075.3-2011"
_PUMP_STD = "GB/T 6075.7-2015"

#: 低速阈值（r/min）。工业机器低于它时，标准要求评价频带改为 2~1000 Hz 且应另看位移。
LOW_SPEED_RPM = 600.0

#: 运行状态的三个取值。
RUNNING, STOPPED, UNJUDGED = "运行", "停机", "未判"

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
    version = "1.1.0"

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
                    key="iso_group", display="机器分组（GB/T 6075.3）", value_type="enum",
                    choices=("1", "2", _NA),
                    choice_displays=(
                        "第 1 组：大型机器，额定功率 >300 kW；电动机轴中心高 H≥315 mm",
                        "第 2 组：中型机器，额定功率 >15 kW 且 ≤300 kW；电动机轴中心高 160≤H<315 mm",
                        "不适用：不在 GB/T 6075.3 范围内",
                    ),
                    required=False, level="machine",
                    description="工业机器必填（泵不填）。决定 A/B/C/D 边界值，没有缺省：选错会把该停机说成可长期运行。"
                                "选「不适用」则不出烈度分级"),
                ParamSpec(
                    key="mount_type", display="支承方式", value_type="enum",
                    choices=("rigid", "flexible"), choice_displays=("刚性", "柔性"),
                    required=False, level="machine",
                    description="工业机器必填（泵不看支承）。机器与支承系统在测量方向上的最低固有频率比转频高 25% 以上为刚性，否则柔性"),
                ParamSpec(
                    key="pump_category", display="泵类别（GB/T 6075.7）", value_type="enum",
                    choices=("1", "2", _NA),
                    choice_displays=(
                        "第Ⅰ类：对可靠性、可用性或安全性要求高的泵",
                        "第Ⅱ类：一般用途的泵",
                        "不适用：不是泵，或不在 GB/T 6075.7 范围内",
                    ),
                    required=False, level="machine",
                    description="旋转动力泵必填（工业机器不填）。选第Ⅰ/Ⅱ类即按泵判级"),
                ParamSpec(
                    key="rated_power_kw", display="额定功率", value_type="float", unit="kW",
                    required=False, level="machine",
                    description="泵必填：按 200 kW 分两档取限值"),
                ParamSpec(
                    key="rated_speed_rpm", display="额定转速", value_type="float", unit="r/min",
                    required=False, level="machine",
                    description="工业机器低于 600 r/min 时照常出分级，但判据摘要注明结果仅供参考（标准要求另看位移）"),
                ParamSpec(
                    key="vel_is_rms", display="速度口径确认为有效值", value_type="enum",
                    choices=("true", "false"),
                    choice_displays=("是，已确认为有效值", "否 / 未确认（峰值或手册未注明）"),
                    required=True, level="position",
                    description="烈度判级要求速度有效值。选「否」时不出烈度分级，只给数值"),
                ParamSpec(
                    key="axial_axis", display="轴向是哪一轴", value_type="enum",
                    choices=("x", "y", "z"), choice_displays=("X 轴", "Y 轴", "Z 轴"),
                    required=True, level="position",
                    description="沿转轴方向的那一轴，两个测点按同一方向理解。没有缺省：猜错会把不对中说成不平衡。"
                                "缺它只影响方向性两条，烈度分级照出"),
                _stop_threshold_spec(),
            ),
            outputs=(
                OutputSpec(key="vel_max", display="速度最大值", value_type="float", unit="mm/s",
                           description="窗口内全部已选速度通道的最大值"),
                OutputSpec(key="dominant_axis", display="最大值所在轴", value_type="string",
                           description="x / y / z；第二测点记为 x2 / y2 / z2"),
                OutputSpec(key="iso_zone", display="烈度区", value_type="string",
                           description="A 新投运 / B 可长期运行 / C 不宜长期连续运行 / D 足以造成损坏；停机时为「停机」"),
                OutputSpec(key="iso_zone_code", display="烈度区(数值)", value_type="int",
                           description="1=A 2=B 3=C 4=D，0=停机，给趋势曲线与报警门限用"),
                OutputSpec(key="iso_margin", display="距下一档余量", value_type="float", unit="mm/s",
                           description="离更差一档的边界还有多远；已在 D 区时为负"),
                OutputSpec(key="axial_ratio", display="轴向/径向比", value_type="float",
                           description="轴向 ÷ 径向两轴较大者，取最大值所在测点"),
                OutputSpec(key="direction_hint", display="方向性提示", value_type="string",
                           description="★提示，不是结论：无频谱数据，仅凭三轴比例判断倾向；停机时为「停机」"),
                _run_state_spec(),
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

        # 运行状态。停机 ⇒ 不判烈度与方向；余量、轴向比这一拍不写（定案 1.6）。
        state, state_q, state_note = run_state(frame.params, vel_max)
        out.append(Finding(key="run_state", value=state, quality=state_q, t=t))
        if state == STOPPED:
            return out + [
                Finding(key="iso_zone", value=STOPPED, quality=Quality.OK, t=t),
                Finding(key="iso_zone_code", value=0, quality=Quality.OK, t=t),
                Finding(key="direction_hint", value=STOPPED, quality=Quality.OK, t=t),
                Finding(key="evidence", value=f"{state_note}；不判烈度与方向", quality=Quality.OK, t=t),
            ]

        # ② 烈度分级
        limits, iso_bad, basis, is_pump = _grading(frame.params)

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
            parts.append(f"烈度分级未给：{iso_bad}")
        else:
            zone, _c, margin = _classify(vel_max, limits)  # type: ignore[arg-type]
            parts.append(
                f"{basis} 判为 {zone} 区，"
                + (f"已超出 C/D 界 {-margin:.3f} mm/s" if margin < 0 else f"距下一档还有 {margin:.3f} mm/s"))
            if not is_pump:                  # 低速规定出自 GB/T 6075.3；泵标准不限转速
                parts.append(_speed_note(frame.params.get("rated_speed_rpm", "")))
        if dir_bad:
            parts.append(f"方向性提示未给：{dir_bad}")
        else:
            parts.append(f"方向性提示（测点{'2' if dir_point == '2' else '1'}）：{hint}")
        parts.append("★无频谱数据，方向性仅为提示而非结论")
        parts.append(state_note)
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


def _stop_threshold_spec() -> ParamSpec:
    return ParamSpec(
        key="stop_threshold", display="停机门槛", value_type="float", unit="mm/s",
        required=False, level="position",
        description="速度最大值低于它即判停机：停机时不判烈度与方向。不填不判；没有缺省，按设备自己定")


def _run_state_spec() -> OutputSpec:
    return OutputSpec(key="run_state", display="运行状态", value_type="string",
                      description="运行 / 停机 / 未判（未填停机门槛）。停机时数值类结论不更新，界面据此置灰")


def run_state(params: dict[str, str], vel_max: float) -> tuple[str | None, Quality, str]:
    """定案 1.6：返回 `(运行状态, 质量, 摘要)`。门槛非法落 CONFIG_INCOMPLETE，其余照常判。"""
    raw = (params.get("stop_threshold") or "").strip()
    if not raw:
        return UNJUDGED, Quality.OK, ""
    thr = _num(raw)
    if thr is None or thr <= 0:
        return None, Quality.CONFIG_INCOMPLETE, f"停机门槛 {raw!r} 不是正数，运行状态未判"
    if vel_max < thr:
        return STOPPED, Quality.OK, f"停机：速度最大值 {vel_max:.3f} < 停机门槛 {thr:g} mm/s"
    return RUNNING, Quality.OK, ""


def _as_float(value) -> float | None:
    """★不用 `float(x) except: 0.0`：那会让坏值静默变成 0，而 0 在速度上是最好的读数。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    return None


def _num(raw: str) -> float | None:
    try:
        v = float((raw or "").strip())
    except ValueError:
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _grading(params: dict[str, str]
             ) -> tuple[tuple[float, float, float] | None, str, str, bool]:
    """选判据、查边界。返回 `(边界, 未给原因, 判据说明, 是否按泵)`；原因为空表示可以判。

    ★不适用 / 缺参数 / 自相矛盾，都属于「判据立不起来」—— 正是 CONFIG_INCOMPLETE 的定义，不另立新码。
    """
    group = params.get("iso_group", "").strip()
    mount = params.get("mount_type", "").strip()
    pump = params.get("pump_category", "").strip()
    is_rms = params.get("vel_is_rms", "").strip().lower()

    is_pump = pump in ("1", "2")
    if is_pump and group in ("1", "2"):
        return None, (f"参数自相矛盾：既填了泵类别（第 {pump} 类）又填了机器分组（第 {group} 组），"
                      "不猜哪个对"), "", True
    if is_rms != "true":
        return None, (f"速度口径未确认为有效值（vel_is_rms={is_rms or '未填'}）"
                      "—— 拿峰值套有效值判据会整档偏高，故不给分级"), "", is_pump

    if is_pump:
        power = _num(params.get("rated_power_kw", ""))
        if power is None or power <= 0:
            raw = params.get("rated_power_kw", "").strip()
            return None, (f"泵按 {_PUMP_STD} 判级需要额定功率（rated_power_kw={raw or '未填'}），"
                          "无法确定功率档"), "", True
        band = "le200" if power <= PUMP_POWER_SPLIT_KW else "gt200"
        roman = "Ⅰ" if pump == "1" else "Ⅱ"
        basis = (f"{_PUMP_STD}（第{roman}类 / 额定 {power:g} kW，"
                 f"{'≤' if band == 'le200' else '>'}200 kW 档）")
        return _PUMP_LIMITS[(pump, band)], "", basis, True

    if group == _NA:
        return None, f"机器分组为「不适用」：设备不在 {_MACHINE_STD} 范围内，不给分级", "", False
    if pump == _NA and not group:
        return None, "机器分组未填（泵类别为「不适用」，按工业机器判需要机器分组）", "", False
    limits = _MACHINE_LIMITS.get((group, mount))
    if limits is None:
        return None, (f"参数不全或取值非法：iso_group={group or '未填'} "
                      f"mount_type={mount or '未填'}，查不到 {_MACHINE_STD} 边界"), "", False
    basis = f"{_MACHINE_STD}（第 {group} 组 / {'刚性' if mount == 'rigid' else '柔性'}支承）"
    return limits, "", basis, False


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
