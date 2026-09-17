"""AI 模型自训振动诊断 —— 相对这台设备自己基线的偏离。

> 2026-09-17 由 `vibration_lowfreq` 拆出（用户定）；另一半见 `vibration_iso.py`。
> 配置与取舍定案见 `AIIntegration/doc/诊断配置流程定案.md`「模块 1、2 的内容」。

---

## 0.1 做什么

先采一条**基线**（这台设备正常运行时各通道长什么样），之后每拍给出：

| 结论 | 说明 |
| --- | --- |
| 速度偏离 | 各速度通道相对基线的稳健 z 分数，取最大 |
| 三轴比例漂移 | 轴向/径向比相对基线的变化，取变化最大的测点 |
| 温升 | 相对基线的温度变化，取最大的测点 |
| 异常分 | 以上几项合成的 0~100 排序用分数，★不是概率 |
| 判据摘要 | 每一条"为什么没给"都写明 |

## 0.2 定案里的四条，落在代码的哪里

1. **基线统计用中位数与四分位距**（原先是均值与标准差）。基线只有十来帧时，
   一两个离群值就能把标准差撑大，让之后的真偏离显得不显著。
   偏离按 `(当前 − 中位数) / (四分位距 / 1.349)` 算，1.349 使正态数据下它与标准差同尺度。
2. **样本少于 5 帧不出基线**，训练当场失败并说清。
3. **故障分类先不做**，等现场有带故障标签的数据。
4. **零第三方依赖**。

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
    Dataset, Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec,
    ProgressSink, TrainedArtifact,
)

_AXES = ("x", "y", "z")
_POINTS = ("", "2")

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


def _vel_role(axis: str, point: str) -> str:
    return f"{axis}{point}_vel"


def _temp_role(point: str) -> str:
    return f"temp{point}"


class VibrationBaseline(Domain):
    """AI 模型自训振动诊断（基线偏离）。"""

    key = "vibration_baseline"
    display = "AI 模型自训振动诊断"
    version = "1.0.0"

    # ── 声明 ──────────────────────────────────────────────────────────────
    def declare(self) -> Declaration:
        inputs = []
        for point, name in (("", "测点 1"), ("2", "测点 2")):
            for axis in _AXES:
                first = point == "" and axis == "x"
                inputs.append(InputSpec(
                    role=_vel_role(axis, point), unit="mm/s", required=first,
                    display=f"{name} {axis.upper()} 轴速度",
                    description=("速度有效值。至少选测点 1 的 X 轴速度；第二测点不配即按单测点算"
                                 if first else "速度有效值，可不选")))
            inputs.append(InputSpec(
                role=_temp_role(point), unit="℃", required=False,
                display=f"{name} 温度",
                description="有就算温升，没有就不给温升"))
        return Declaration(
            inputs=tuple(inputs),
            params=(
                ParamSpec(
                    key="axial_axis", display="轴向是哪一轴", value_type="enum",
                    choices=("x", "y", "z"), choice_displays=("X 轴", "Y 轴", "Z 轴"),
                    required=True, level="position",
                    description="沿转轴方向的那一轴，两个测点按同一方向理解。没有缺省；"
                                "缺它只影响三轴比例漂移一条"),
                ParamSpec(
                    key="normal_label", display="采基线时认哪个标签算正常",
                    value_type="string", default=DEFAULT_NORMAL_LABEL, required=False,
                    level="position",
                    description="采基线只用被标成这个标签的样本。允许有缺省：猜错会当场可见"
                                "（一条样本都匹配不上，采基线直接失败并说清）"),
            ),
            outputs=(
                OutputSpec(key="vel_z_max", display="速度偏离", value_type="float",
                           description="各速度通道 (当前−基线中位数)/(四分位距/1.349) 的最大值。>3 视为显著偏离"),
                OutputSpec(key="ratio_drift", display="三轴比例漂移", value_type="float",
                           description="轴向/径向比相对基线的变化量，取变化最大的测点"),
                OutputSpec(key="temp_rise", display="温升", value_type="float", unit="℃",
                           description="相对基线的温度变化，取最大的测点"),
                OutputSpec(key="anomaly_score", display="异常分", value_type="float",
                           description="0~100，由上面几项合成。★不是概率，是排序用的分数"),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="基线采自哪段、哪一项偏离、为什么没给"),
            ),
        )

    # ── 训练（采基线）──────────────────────────────────────────────────────
    def train(self, dataset: Dataset, report: ProgressSink) -> TrainedArtifact:
        """采一条基线：这台设备正常运行时各通道的中位数与四分位距，以及各测点的三轴比例。"""
        wanted = DEFAULT_NORMAL_LABEL
        axial = ""
        for it in dataset.items:            # 参数在帧上，各帧同一条诊断，取第一个
            wanted = it.frame.params.get("normal_label", "").strip() or DEFAULT_NORMAL_LABEL
            axial = (it.frame.params.get("axial_axis", "") or "").strip().lower()
            break

        normals = [it for it in dataset.items if it.label == wanted]
        if len(normals) < MIN_BASELINE_FRAMES:
            raise ValueError(
                f"基线样本不足：需要至少 {MIN_BASELINE_FRAMES} 帧标为 {wanted!r} 的样本，"
                f"实际只有 {len(normals)} 帧（训练集共 {len(dataset)} 帧，"
                f"标签分布 {dataset.label_counts()}）—— 样本太少算出来的离散度没有意义")

        report.report(0.2, f"用 {len(normals)} 帧 {wanted!r} 样本采基线")

        channels: dict[str, dict[str, float]] = {}
        roles = [_vel_role(a, p) for p in _POINTS for a in _AXES] + [_temp_role(p) for p in _POINTS]
        for role in roles:
            vals = []
            for it in normals:
                v = (_window_mean(it.frame, role) if role.startswith("temp")
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
        ratios: dict[str, float] = {}
        if axial in _AXES:
            for point in _POINTS:
                ax = channels.get(_vel_role(axial, point))
                rad = [channels[_vel_role(a, point)]["median"] for a in _AXES
                       if a != axial and _vel_role(a, point) in channels]
                if ax and rad and max(rad) > 0:
                    ratios[point or "1"] = ax["median"] / max(rad)

        model = {
            "format": BASELINE_FORMAT,
            "label": wanted,
            "frames": len(normals),
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
                  "frames": str(len(normals)),
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
        keys = ("vel_z_max", "ratio_drift", "temp_rise", "anomaly_score")

        baseline = frame.artifacts.get("baseline")
        if baseline is None:
            return (_bad_group(keys, Quality.MODEL_NOT_LOADED, t)
                    + [Finding(key="evidence", value="无可用基线（未采或未启用），偏离与异常分未给",
                               quality=Quality.MODEL_NOT_LOADED, t=t)])
        try:
            model = _parse_baseline(baseline.blob)
        except Exception as exc:  # noqa: BLE001 —— 坏工件不许掀翻整拍推理
            return (_bad_group(keys, Quality.MODEL_NOT_LOADED, t)
                    + [Finding(key="evidence",
                               value=f"基线工件读不懂（{type(exc).__name__}: {exc}），偏离与异常分未给",
                               quality=Quality.MODEL_NOT_LOADED, t=t)])

        out, note = _deviation(model, peaks, frame, t)
        if bad:
            note += f"；降级：{'/'.join(bad)} 本窗口全是坏值，未参与"
        if empty:
            note += f"；降级：{'/'.join(empty)} 本窗口无样本"
        out.append(Finding(key="evidence", value=note, quality=Quality.OK, t=t))
        return out


# ─────────────────────────────── 内部函数 ───────────────────────────────

def _peaks(frame: Frame) -> tuple[dict[str, float], dict[str, datetime], list[str], list[str]]:
    """逐速度通道取窗口内可信样本的最大值。键为 `x`/`y`/`z`/`x2`/`y2`/`z2`。
    没选的通道不记入"无样本"。"""
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
                if v is not None and (best_v is None or v > best_v):
                    best_v, best_t = v, s.t
            if best_v is None or best_t is None:
                bad.append(key)
            else:
                peaks[key] = best_v
                times[key] = best_t
    return peaks, times, bad, empty


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
    for key, cur in peaks.items():
        point = key[1:]
        c = chans.get(_vel_role(key[0], point))
        if c:
            zs[key] = (cur - float(c["median"])) / _scale(c)
    if zs:
        worst = max(zs, key=lambda k: zs[k])
        out.append(Finding(key="vel_z_max", value=round(zs[worst], 3), quality=Quality.OK, t=t))
        if zs[worst] >= _Z_NOTABLE:
            notes.append(f"{worst} 较基线偏高 {zs[worst]:.1f} 个尺度单位")
    else:
        out.append(Finding(key="vel_z_max", value=None, quality=Quality.INSUFFICIENT_SAMPLES, t=t))
        notes.append("本帧没有与基线同通道的可信样本，速度偏离未给")

    # ② 三轴比例漂移。
    axial = frame.params.get("axial_axis", "").strip().lower()
    base_ratios: dict = model.get("axial_ratios") or {}
    if axial not in _AXES:
        out.append(Finding(key="ratio_drift", value=None, quality=Quality.CONFIG_INCOMPLETE, t=t))
        notes.append(f"轴向 axial_axis={axial or '未填'} 非法，比例漂移未给")
    elif not base_ratios:
        out.append(Finding(key="ratio_drift", value=None, quality=Quality.CONFIG_INCOMPLETE, t=t))
        notes.append("基线里没有三轴比例（采基线时轴向未填或径向无数据），比例漂移未给")
    else:
        drifts: dict[str, float] = {}
        for point in _POINTS:
            base = base_ratios.get(point or "1")
            ax = peaks.get(f"{axial}{point}")
            rad = [peaks[f"{a}{point}"] for a in _AXES if a != axial and f"{a}{point}" in peaks]
            if base is None or ax is None or not rad or max(rad) <= 0:
                continue
            drifts[point or "1"] = ax / max(rad) - float(base)
        if not drifts:
            out.append(Finding(key="ratio_drift", value=None,
                               quality=Quality.INSUFFICIENT_SAMPLES, t=t))
        else:
            p = max(drifts, key=lambda k: abs(drifts[k]))
            drift = drifts[p]
            out.append(Finding(key="ratio_drift", value=round(drift, 4), quality=Quality.OK, t=t))
            if abs(drift) >= 0.2:
                notes.append(f"测点{p} 三轴比例较基线漂移 {drift:+.2f}"
                             f"（{'轴向占比升高，提示：可能不对中' if drift > 0 else '轴向占比下降'}）")

    # ③ 温升：没选温度 ⇒ NO_INPUT；选了但基线里没有 ⇒ MODEL_NOT_LOADED。
    rises: dict[str, float] = {}
    any_temp_bound = False
    any_base_missing = False
    for point in _POINTS:
        role = _temp_role(point)
        if role not in frame.channels:
            continue
        any_temp_bound = True
        cur = _window_mean(frame, role)
        base = chans.get(role)
        if base is None:
            any_base_missing = True
            continue
        if cur is not None:
            rises[point or "1"] = cur - float(base["median"])
    if rises:
        p = max(rises, key=lambda k: rises[k])
        out.append(Finding(key="temp_rise", value=round(rises[p], 3), quality=Quality.OK, t=t))
        if rises[p] >= 5.0:
            notes.append(f"测点{p} 温度较基线高 {rises[p]:.1f}℃")
    else:
        q = (Quality.NO_INPUT if not any_temp_bound
             else Quality.MODEL_NOT_LOADED if any_base_missing else Quality.NO_INPUT)
        out.append(Finding(key="temp_rise", value=None, quality=q, t=t))

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
