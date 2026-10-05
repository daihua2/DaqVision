"""AI 模型自训振动诊断 —— 相对这台设备自己基线的偏离。

> 2026-09-17 由 `vibration_lowfreq` 拆出（用户定）；另一半见 `vibration_iso.py`。
> 配置与取舍定案见 `AIIntegration/doc/诊断配置流程定案.md`「模块 1、2 的内容」。
>
> ★2026-10-05 按 AICloud `C-64`（用户同意）改为 2.0：一条绑定 = 一个振动传感器，去掉「第二测点」组；
> 「轴向是哪一轴」键改为平台台账的 `axialAxis`，由传感器给（`level="sensor"`）。
> **检测状态只出「正常 / 注意」**（2.1.0，用户 10-05 同意）：速度偏离 ≥ 3 即「注意」。
> ★不出警告、危险 —— 那两档只由国标给（`vibration_iso`）；本模块的门槛与合成权重都没有标定依据，
>   一组没标定的数不该把设备说成危险。「注意」是最低一档，用来提前看到「偏离了自身常态」。
> ★**不防抖**：每拍如实写，去抖交给实时库报警（`OnDelaySec`），历史里留真实的每一拍。
> ★同日补一处缺陷：基线记下了采集时的轴向，推理时却不核 —— 平台一改轴向，比例漂移就拿新轴向的比值
> 去减旧轴向的基线，错了也看不出。现在两者不一致即落 `MODEL_NOT_LOADED` 并提示重采（`_deviation` ②）。

---

## 0.1 做什么

先采一条**基线**（这台设备正常运行时各通道长什么样），之后每拍给出：

| 结论 | 说明 |
| --- | --- |
| 速度偏离 | 各速度通道相对基线的稳健 z 分数，取最大 |
| 三轴比例漂移 | 轴向/径向比相对基线的变化 |
| 温升 | 相对基线的温度变化 |
| 异常分 | 以上几项合成的 0~100 排序用分数，★不是概率 |
| 判据摘要 | 每一条"为什么没给"都写明 |

## 0.2 定案里的四条，落在代码的哪里

1. **基线统计用中位数与四分位距**（原先是均值与标准差）。基线只有十来帧时，
   一两个离群值就能把标准差撑大，让之后的真偏离显得不显著。
   偏离按 `(当前 − 中位数) / (四分位距 / 1.349)` 算，1.349 使正态数据下它与标准差同尺度。
2. **样本少于 5 帧不出基线**，训练当场失败并说清。
3. **故障分类先不做**，等现场有带故障标签的数据。
4. **零第三方依赖**。
5. **停机判定**（定案 2.5，2026-09-18）：填了「停机门槛」且速度最大值 < 门槛 ⇒ 停机。
   停机时只写「运行状态」与判据摘要，**偏离、比例漂移、温升、异常分这一拍不写**
   （点上保留停机前最后一值，界面凭运行状态置灰）；**采基线时剔除停机帧**。
   门槛与判法同模块 1（`vibration_iso.run_state`，两处须一致，由用例钉住）。

## 0.3 纪律

- **只用被标成"正常"的样本采基线**：拿故障数据采出来的基线，会把故障态当成常态。
- **没有基线就说没有基线**：整组落 `MODEL_NOT_LOADED`，不拿任何默认值顶。
- **基线与推理同口径**：速度都取窗口内最大值、温度都取窗口内均值，否则比的是两把尺子。
"""

from __future__ import annotations

import json
from datetime import datetime

from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    ROLE_STATUS, STATUS_ATTENTION, STATUS_NORMAL, STATUS_STOPPED, STOP_LITERAL,
    STOP_NOT_WRITTEN, Dataset, Declaration, Finding, Frame, InputSpec, OutputSpec,
    ParamSpec, ProgressSink, TrainedArtifact,
)

_AXES = ("x", "y", "z")

#: 平台台账的键（`C-64 §2.3`），与 `vibration_iso` 同。
P_AXIAL = "axialAxis"
TEMP_ROLE = "temp"

#: 基线格式。**存进工件里**：格式一变，老工件要能被认出来而不是被误读。
BASELINE_FORMAT = "vibration_baseline/baseline@1"

#: 采基线至少要几帧（定案 2.2）。
MIN_BASELINE_FRAMES = 5

#: 稳健尺度的下限（mm/s，温度同用）。传感器分辨率决定它不可能真是 0；
#: ★不设下限的后果是除零或天文数字的 z 分数，后者更坏，因为它看着像个结论。
MIN_SCALE = 0.01

#: 四分位距折算成与标准差同尺度的系数（正态分布下 IQR ≈ 1.349σ）。
_IQR_TO_SIGMA = 1.349

#: 认为"偏离显著"的 z 分数。经验值，用于异常分与摘要措辞，**不是国标**。
_Z_NOTABLE = 3.0

DEFAULT_NORMAL_LABEL = "正常"

#: 运行状态的三个取值（与模块 1 同）。
RUNNING, STOPPED, UNJUDGED = "运行", "停机", "未判"


def _vel_role(axis: str) -> str:
    return f"{axis}_vel"


class VibrationBaseline(Domain):
    """AI 模型自训振动诊断（基线偏离）。"""

    key = "vibration_baseline"
    display = "AI 模型自训振动诊断"
    version = "2.1.0"

    # ── 声明 ──────────────────────────────────────────────────────────────
    def declare(self) -> Declaration:
        inputs = [
            InputSpec(
                role=_vel_role(axis), unit="mm/s", required=(axis == "x"),
                # ★按字段绑到结构值点时（契约 1.11）：物理量须是速度；x/y/z 必须出自同一个点
                #   （同一条记录），否则"同一时刻"的保证就没了。
                quantity="velocity", axis=axis, record="point1",
                display=f"{axis.upper()} 轴速度",
                description=("速度有效值。至少选 X 轴速度" if axis == "x" else "速度有效值，可不选"))
            for axis in _AXES]
        inputs.append(InputSpec(
            role=TEMP_ROLE, unit="℃", required=False, quantity="temperature",
            display="温度", description="有就算温升，没有就不给温升"))
        return Declaration(
            inputs=tuple(inputs),
            # ★没有启用中的基线，偏离与异常分就出不来（契约 1.13）。
            requires_artifacts=("baseline",),
            params=(
                ParamSpec(
                    key=P_AXIAL, display="轴向是哪一轴", value_type="enum",
                    choices=("x", "y", "z"), choice_displays=("X 轴", "Y 轴", "Z 轴"),
                    required=True, level="sensor",
                    description="沿转轴方向的那一轴，在振动传感器上填。没有缺省；"
                                "缺它只影响三轴比例漂移一条。★改了它，已采的基线的比例那一项作废，须重采"),
                ParamSpec(
                    key="normal_label", display="采基线时认哪个标签算正常",
                    value_type="string", default=DEFAULT_NORMAL_LABEL, has_default=True,
                    required=False,
                    level="position",
                    description="采基线只用被标成这个标签的样本。允许有缺省：猜错会当场可见"
                                "（一条样本都匹配不上，采基线直接失败并说清）"),
                ParamSpec(
                    key="stop_threshold", display="停机门槛", value_type="float", unit="mm/s",
                    required=False, level="position",
                    # ★与模块 1 逐字一致（两份实现必须同判据）：无缺省、留空则不评、下限正数。
                    blank_meaning="not_evaluated", min="0",
                    description="速度最大值低于它即判停机：停机时不出偏离与异常分，采基线时剔除停机帧。"
                                "不填不判；没有缺省，按设备自己定"),
            ),
            outputs=(
                OutputSpec(key="status", display="检测状态", value_type="string",
                           role=ROLE_STATUS, stop_behavior=STOP_LITERAL,
                           description="速度偏离 ≥ 3 → 注意，否则正常；停机 → 停机。本模块不出警告、危险（只由国标给）。"
                                       "没有基线或速度偏离算不出时同落坏质量码",
                           choices=(STATUS_NORMAL, STATUS_ATTENTION, STATUS_STOPPED),
                           choice_displays=("正常", "注意", STOPPED)),
                OutputSpec(key="vel_z_max", display="速度偏离", value_type="float", headline=True,
                           description="各速度通道 (当前−基线中位数)/(四分位距/1.349) 的最大值。>3 视为显著偏离",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="ratio_drift", display="三轴比例漂移", value_type="float",
                           description="轴向/径向比相对基线的变化量",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="temp_rise", display="温升", value_type="float", unit="℃",
                           description="相对基线的温度变化",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="anomaly_score", display="异常分", value_type="float", headline=True,
                           description="0~100，由上面几项合成。★不是概率，是排序用的分数",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="run_state", display="运行状态", value_type="string",
                           description="运行 / 停机 / 未判（未填停机门槛）。停机时数值类结论不更新，界面据此置灰",
                           choices=(RUNNING, STOPPED, UNJUDGED)),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="基线采自哪段、哪一项偏离、为什么没给"),
            ),
        )

    # ── 训练（采基线）──────────────────────────────────────────────────────
    def train(self, dataset: Dataset, report: ProgressSink) -> TrainedArtifact:
        """采一条基线：这台设备正常运行时各通道的中位数与四分位距，以及各测点的三轴比例。"""
        wanted = DEFAULT_NORMAL_LABEL
        axial = ""
        params: dict[str, str] = {}
        for it in dataset.items:            # 参数在帧上，各帧同一条诊断，取第一个
            params = it.frame.params
            wanted = params.get("normal_label", "").strip() or DEFAULT_NORMAL_LABEL
            axial = (params.get(P_AXIAL, "") or "").strip().lower()
            break

        # 定案 2.5：停机帧不进基线 —— 否则基线把"停着"当成常态，开机后每拍都显得偏高。
        _state, state_q, state_note = run_state(params, 0.0)
        if state_q is not Quality.OK:
            raise ValueError(f"{state_note} —— 判不了哪些帧是停机，不采基线")
        labeled = [it for it in dataset.items if it.label == wanted]
        normals = [it for it in labeled if not _frame_stopped(it.frame, params)]
        stopped = len(labeled) - len(normals)
        if len(normals) < MIN_BASELINE_FRAMES:
            raise ValueError(
                f"基线样本不足：需要至少 {MIN_BASELINE_FRAMES} 帧标为 {wanted!r} 的运行样本，"
                f"实际只有 {len(normals)} 帧（训练集共 {len(dataset)} 帧，"
                f"标签分布 {dataset.label_counts()}"
                + (f"，其中 {stopped} 帧判为停机已剔除" if stopped else "")
                + "）—— 样本太少算出来的离散度没有意义")

        report.report(0.2, f"用 {len(normals)} 帧 {wanted!r} 样本采基线"
                           + (f"（剔除停机帧 {stopped}）" if stopped else ""))

        channels: dict[str, dict[str, float]] = {}
        roles = [_vel_role(a) for a in _AXES] + [TEMP_ROLE]
        for role in roles:
            vals = []
            for it in normals:
                v = (_window_mean(it.frame, role) if role == TEMP_ROLE
                     else _window_peak(it.frame, role))
                if v is not None:
                    vals.append(v)
            if len(vals) >= MIN_BASELINE_FRAMES:
                med, iqr = _median_iqr(vals)
                channels[role] = {"median": med, "iqr": iqr, "n": len(vals)}

        if not any(k.endswith("_vel") for k in channels):
            raise ValueError("一路速度都没能采到足够样本 —— 检查所选采集点与这段时间实时库里有没有数据")

        report.report(0.8, "统计完成")

        # 三轴比例进基线：不对中的抓手是"比例变了"，不是"值变大了"。
        # ★格式沿用 `{"1": 比值}`（BASELINE_FORMAT 不变）：1.x 时代按测点存，现在只有一个传感器。
        ratios: dict[str, float] = {}
        if axial in _AXES:
            ax = channels.get(_vel_role(axial))
            rad = [channels[_vel_role(a)]["median"] for a in _AXES
                   if a != axial and _vel_role(a) in channels]
            if ax and rad and max(rad) > 0:
                ratios["1"] = ax["median"] / max(rad)

        model = {
            "format": BASELINE_FORMAT,
            "label": wanted,
            "frames": len(normals),
            "stopped_excluded": stopped,
            "t_from": min(it.frame.t_start for it in normals).isoformat(),
            "t_to": max(it.frame.t_end for it in normals).isoformat(),
            "axial_axis": axial,
            "axial_ratios": ratios,
            "channels": channels,
        }
        blob = json.dumps(model, ensure_ascii=False, indent=1).encode("utf-8")
        return TrainedArtifact(
            blob=blob, algo="基线统计（中位数/四分位距）", kind="baseline", suffix=".json",
            # ★基线没有"准确率"这回事 —— 给 None，别拿 1.0 顶（界面会显示成 100%）。
            accuracy=None, feature_count=len(channels),
            meta={"format": BASELINE_FORMAT, "normal_label": wanted,
                  "frames": str(len(normals)), "stopped_excluded": str(stopped),
                  "t_from": model["t_from"], "t_to": model["t_to"]})

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        peaks, times, bad, empty = _peaks(frame)
        if not peaks:
            q = Quality.INPUT_BAD if bad else Quality.NO_INPUT
            why = (f"{'/'.join(bad)} 有样本但质量码全不可信"
                   if bad else f"{'/'.join(empty) or '所有已选速度通道'} 在本窗口内没有样本")
            return _all_bad(self, frame, q, f"未出结论：{why}")

        dominant = max(peaks, key=lambda k: peaks[k])
        t = times[dominant]
        keys = ("status", "vel_z_max", "ratio_drift", "temp_rise", "anomaly_score")

        # 运行状态先于基线判：停机就不比，与有没有基线无关（定案 2.5）。
        state, state_q, state_note = run_state(frame.params, peaks[dominant])
        state_f = Finding(key="run_state", value=state, quality=state_q, t=t)
        if state == STOPPED:
            return [state_f,
                    Finding(key="status", value=STATUS_STOPPED, quality=Quality.OK, t=t),
                    Finding(key="evidence", value=f"{state_note}；不出偏离与异常分",
                            quality=Quality.OK, t=t)]

        baseline = frame.artifacts.get("baseline")
        if baseline is None:
            return (_bad_group(keys, Quality.MODEL_NOT_LOADED, t)
                    + [state_f,
                       Finding(key="evidence", value="无可用基线（未采或未启用），偏离与异常分未给",
                               quality=Quality.MODEL_NOT_LOADED, t=t)])
        try:
            model = _parse_baseline(baseline.blob)
        except Exception as exc:  # noqa: BLE001 —— 坏工件不许掀翻整拍推理
            return (_bad_group(keys, Quality.MODEL_NOT_LOADED, t)
                    + [state_f,
                       Finding(key="evidence",
                               value=f"基线工件读不懂（{type(exc).__name__}: {exc}），偏离与异常分未给",
                               quality=Quality.MODEL_NOT_LOADED, t=t)])

        out, note = _deviation(model, peaks, frame, t)
        out.append(_status(out, t))
        out.append(state_f)
        if state_note:
            note += f"；{state_note}"
        if bad:
            note += f"；降级：{'/'.join(bad)} 本窗口全是坏值，未参与"
        if empty:
            note += f"；降级：{'/'.join(empty)} 本窗口无样本"
        out.append(Finding(key="evidence", value=note, quality=Quality.OK, t=t))
        return out


# ─────────────────────────────── 内部函数 ───────────────────────────────

def _status(out: list[Finding], t: datetime) -> Finding:
    """检测状态只看速度偏离：≥ `_Z_NOTABLE` 即「注意」。偏离算不出 ⇒ 同码落坏，**不报正常**。

    ★为什么只看速度偏离、不看比例漂移与温升：那两项的门槛（0.2、5℃）只用于摘要措辞，
      比 z 分数更没有依据；状态是平台要拿去合成、去报警的，口径宁窄勿宽。
    """
    z = next(f for f in out if f.key == "vel_z_max")
    if z.value is None:
        return Finding(key="status", value=None, quality=z.quality, t=t)
    level = STATUS_ATTENTION if z.value >= _Z_NOTABLE else STATUS_NORMAL
    return Finding(key="status", value=level, quality=Quality.OK, t=t)


def _peaks(frame: Frame) -> tuple[dict[str, float], dict[str, datetime], list[str], list[str]]:
    """逐速度通道取窗口内可信样本的最大值。键为 `x`/`y`/`z`。
    没选的通道不记入"无样本"。"""
    peaks: dict[str, float] = {}
    times: dict[str, datetime] = {}
    bad: list[str] = []
    empty: list[str] = []
    for axis in _AXES:
        role = _vel_role(axis)
        if role not in frame.channels:
            continue
        samples = list(frame.channels[role])
        if not samples:
            empty.append(axis)
            continue
        best_v: float | None = None
        best_t: datetime | None = None
        for s in samples:
            if s.quality is not Quality.OK:
                continue
            v = _as_float(s.value)
            if v is not None and (best_v is None or v > best_v):
                best_v, best_t = v, s.t
        if best_v is None or best_t is None:
            bad.append(axis)
        else:
            peaks[axis] = best_v
            times[axis] = best_t
    return peaks, times, bad, empty


def run_state(params: dict[str, str], vel_max: float) -> tuple[str | None, Quality, str]:
    """定案 2.5（同模块 1 定案 1.6）：返回 `(运行状态, 质量, 摘要)`。门槛非法落 CONFIG_INCOMPLETE。"""
    raw = (params.get("stop_threshold") or "").strip()
    if not raw:
        return UNJUDGED, Quality.OK, ""
    try:
        thr = float(raw)
    except ValueError:
        thr = float("nan")
    if not (thr > 0 and thr != float("inf")):
        return None, Quality.CONFIG_INCOMPLETE, f"停机门槛 {raw!r} 不是正数，运行状态未判"
    if vel_max < thr:
        return STOPPED, Quality.OK, f"停机：速度最大值 {vel_max:.3f} < 停机门槛 {thr:g} mm/s"
    return RUNNING, Quality.OK, ""


def _frame_stopped(frame: Frame, params: dict[str, str]) -> bool:
    """采基线用：这一帧按停机门槛算不算停机。一路可信速度都没有的帧不算停机（交给后面按通道缺样本处理）。"""
    peaks = [p for p in (_window_peak(frame, _vel_role(a)) for a in _AXES) if p is not None]
    return bool(peaks) and run_state(params, max(peaks))[0] == STOPPED


def _as_float(value) -> float | None:
    """★不用 `float(x) except: 0.0`：那会让坏值静默变成 0。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    return None


def _window_peak(frame: Frame, role: str) -> float | None:
    """窗口内可信样本的最大值 —— 与推理侧同口径。"""
    best = None
    for s in frame.channels.get(role, []):
        if s.quality is not Quality.OK:
            continue
        v = _as_float(s.value)
        if v is not None and (best is None or v > best):
            best = v
    return best


def _window_mean(frame: Frame, role: str) -> float | None:
    """窗口内可信样本的均值（温度用它，温度本来就慢）。"""
    vals = [v for s in frame.channels.get(role, [])
            if s.quality is Quality.OK and (v := _as_float(s.value)) is not None]
    return sum(vals) / len(vals) if vals else None


def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 缺省口径一致）。"""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _median_iqr(vals: list[float]) -> tuple[float, float]:
    s = sorted(vals)
    return _quantile(s, 0.5), _quantile(s, 0.75) - _quantile(s, 0.25)


def _scale(chan: dict) -> float:
    """四分位距折算成与标准差同尺度，并套下限。"""
    return max(float(chan.get("iqr") or 0.0) / _IQR_TO_SIGMA, MIN_SCALE)


def _parse_baseline(blob: bytes) -> dict:
    """解析基线工件。**格式不认就抛** —— 拿不认识的结构去算，算出来的数没人看得出是错的。"""
    model = json.loads(blob.decode("utf-8"))
    if not isinstance(model, dict):
        raise ValueError("基线工件不是一个对象")
    fmt = model.get("format", "")
    if fmt != BASELINE_FORMAT:
        raise ValueError(f"基线格式是 {fmt!r}，本模块只认 {BASELINE_FORMAT!r}")
    if not isinstance(model.get("channels"), dict) or not model["channels"]:
        raise ValueError("基线里一路通道都没有")
    return model


def _deviation(model: dict, peaks: dict[str, float], frame: Frame,
               t: datetime) -> tuple[list[Finding], str]:
    """相对基线的偏离。逐条各判质量。"""
    chans = model["channels"]
    out: list[Finding] = []
    notes: list[str] = []

    # ① 速度偏离：只比基线里有的通道。
    zs: dict[str, float] = {}
    for axis, cur in peaks.items():
        c = chans.get(_vel_role(axis))
        if c:
            zs[axis] = (cur - float(c["median"])) / _scale(c)
    if zs:
        worst = max(zs, key=lambda k: zs[k])
        out.append(Finding(key="vel_z_max", value=round(zs[worst], 3), quality=Quality.OK, t=t))
        if zs[worst] >= _Z_NOTABLE:
            notes.append(f"{worst} 较基线偏高 {zs[worst]:.1f} 个尺度单位")
    else:
        out.append(Finding(key="vel_z_max", value=None, quality=Quality.INSUFFICIENT_SAMPLES, t=t))
        notes.append("本帧没有与基线同通道的可信样本，速度偏离未给")

    # ② 三轴比例漂移。
    axial = frame.params.get(P_AXIAL, "").strip().lower()
    base_ratio = (model.get("axial_ratios") or {}).get("1")
    base_axial = str(model.get("axial_axis") or "")
    if axial not in _AXES:
        out.append(Finding(key="ratio_drift", value=None, quality=Quality.CONFIG_INCOMPLETE, t=t))
        notes.append(f"轴向 {P_AXIAL}={axial or '未填'} 非法，比例漂移未给")
    elif base_ratio is None:
        out.append(Finding(key="ratio_drift", value=None, quality=Quality.CONFIG_INCOMPLETE, t=t))
        notes.append("基线里没有三轴比例（采基线时轴向未填或径向无数据），比例漂移未给")
    elif base_axial != axial:
        # ★基线的比例是按采集时的轴向算的。轴向改了还照减，得出的漂移是两把尺子的差，且看不出错。
        out.append(Finding(key="ratio_drift", value=None, quality=Quality.MODEL_NOT_LOADED, t=t))
        notes.append(f"基线采集时轴向为 {base_axial}，现在为 {axial}：比例漂移未给，请重采基线")
    else:
        ax = peaks.get(axial)
        rad = [peaks[a] for a in _AXES if a != axial and a in peaks]
        if ax is None or not rad or max(rad) <= 0:
            out.append(Finding(key="ratio_drift", value=None,
                               quality=Quality.INSUFFICIENT_SAMPLES, t=t))
        else:
            drift = ax / max(rad) - float(base_ratio)
            out.append(Finding(key="ratio_drift", value=round(drift, 4), quality=Quality.OK, t=t))
            if abs(drift) >= 0.2:
                notes.append(f"三轴比例较基线漂移 {drift:+.2f}"
                             f"（{'轴向占比升高，提示：可能不对中' if drift > 0 else '轴向占比下降'}）")

    # ③ 温升：没选温度 ⇒ NO_INPUT；选了但基线里没有 ⇒ MODEL_NOT_LOADED。
    rise: float | None = None
    temp_q = Quality.NO_INPUT
    if TEMP_ROLE in frame.channels:
        base = chans.get(TEMP_ROLE)
        cur = _window_mean(frame, TEMP_ROLE)
        if base is None:
            temp_q = Quality.MODEL_NOT_LOADED
        elif cur is not None:
            rise = cur - float(base["median"])
    if rise is not None:
        out.append(Finding(key="temp_rise", value=round(rise, 3), quality=Quality.OK, t=t))
        if rise >= 5.0:
            notes.append(f"温度较基线高 {rise:.1f}℃")
    else:
        out.append(Finding(key="temp_rise", value=None, quality=temp_q, t=t))

    # ④ 异常分：★不是概率、不是置信度，一个 0~100 的数最容易被当成概率读。
    if not zs:
        out.append(Finding(key="anomaly_score", value=None, quality=Quality.INSUFFICIENT_SAMPLES, t=t))
    else:
        z_part = min(1.0, max(0.0, max(zs.values())) / (2 * _Z_NOTABLE))
        got_drift = next((f for f in out if f.key == "ratio_drift"), None)
        d_part = (min(1.0, abs(got_drift.value) / 0.5)
                  if got_drift is not None and got_drift.value is not None else 0.0)
        got_temp = next((f for f in out if f.key == "temp_rise"), None)
        t_part = (min(1.0, max(0.0, got_temp.value) / 10.0)
                  if got_temp is not None and got_temp.value is not None else 0.0)
        score = 100.0 * (0.6 * z_part + 0.25 * d_part + 0.15 * t_part)
        out.append(Finding(key="anomaly_score", value=round(score, 1), quality=Quality.OK, t=t))

    head = (f"基线采自 {str(model.get('t_from', '?'))[:16]}~{str(model.get('t_to', '?'))[:16]}"
            f"（{model.get('frames', '?')} 帧「{model.get('label', '?')}」）")
    return out, head + ("；" + "；".join(notes) if notes else "；本帧未见相对基线的显著偏离")


def _bad_group(keys: tuple[str, ...], q: Quality, t: datetime) -> list[Finding]:
    """给一组结论落同一个坏质量码。**值为 None** —— 坏质量下不许有值。"""
    return [Finding(key=k, value=None, quality=q, t=t) for k in keys]


def _all_bad(domain: Domain, frame: Frame, q: Quality, why: str) -> list[Finding]:
    """一条都算不出来时：每个声明过的输出各落一个坏值锚点，外加一句人话。"""
    t = frame.t_end
    keys = [o.key for o in domain.declare().outputs if o.key != "evidence"]
    out = [Finding(key=k, value=None, quality=q, t=t) for k in keys]
    out.append(Finding(key="evidence", value=why, quality=q, t=t))
    return out
