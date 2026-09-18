"""AI 变频器（VFD）运行监测 —— 15 路电气量判 6 类故障，并给当前与未来严重度。

> 定案见 `AIIntegration/doc/诊断配置流程定案.md`「模块 5」（2026-09-17 用户定：
> 采集点 15 个、1 Hz、每 10 秒取最近 60 秒、单一模型、无待定项）。
> 通道表与类别表**照搬原项目 VFD**（2026-09-18 只读核查 `app/backend/model.py`），
> 名字与次序与那边逐字一致 —— 对不上就是换了一套语义。

---

## 0.1 做什么

取 15 路电气量的 60 点窗口（1 Hz × 60 秒），交给模型给出：

| 结论 | 说明 |
| --- | --- |
| 故障类型 | 6 类之一（正常 / 冷却系统退化 / 功率单元异常 / 输出电流不平衡 / 输入电网异常 / 过载） |
| 置信度 | 该类别的概率 |
| 当前严重度 | 0~1 |
| 30 / 60 / 120 秒后严重度 | 0~1 各一条。★**定案明确要求保留这三项** |

## 0.2 ★本域不内置模型（与模块 4 同）

原项目的权重是 torch `best_model.pt` + 另一份 `scaler.json`，**不在本仓**，也不该由本仓携带。
⇒ 走「外部模型导入为工件」（契约 1.5）：没启用工件 ⇒ 整组落 `MODEL_NOT_LOADED`，
**不拿默认模型顶**。

## 0.3 ★模型必须自述四件事，否则导入时就拒

工件是 **ONNX**。四件缺任一都拒收：

| 键 | 缺了会怎样 |
| --- | --- |
| `channels` | 15 路的**顺序**。喂错不报错，只给一个自信的错结论 |
| `rate_hz` | 据此核对窗口疏密对不对得上 |
| `classes` | 6 类的**顺序**。反了就是把"正常"报成"过载" |
| `normalization` | ★**原项目的归一化在模型之外**（`scaler.json` 的 mean/std）。ONNX 若没把它烘进图里，就必须在这里说清并带上 `scaler_mean` / `scaler_std`；**不说 = 拒收** |

★为什么 `normalization` 单列一条：**归一化用错不会报错**。少做一次 z-score，
  输入量级完全不对，模型照样吐出一个像模像样的概率和严重度 —— 这是本域最容易
  静默出错的一处，比通道顺序还隐蔽（顺序至少还可能看出量纲荒谬）。

## 0.4 现场一个点都没接

`doc/模块划分.md` 已核实：VFD 那 15 路现场**一个点没接**。
与模块 3、模块 4 同一条做法：**模块先落地，能配置、能保存，无数据如实回报**。

## 0.5 纪律

- **不补数、不插值、不重采样**：点数不足就说不足。
- **一路坏就不算**：15 路是一个整体。
- **严重度不外推**：未来三项是**模型给的**，本域不拿当前值自己推 —— 编出来的趋势最像真的。
"""

from __future__ import annotations

import json

REQUIRES = ("numpy", "onnxruntime")

import numpy as np
import onnxruntime as ort

from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec,
)

#: 15 路电气量。★**名字与次序照搬原项目 `model.py` 的 `CHANNELS`**，显示名照搬 `report.py`。
CHANNELS: tuple[tuple[str, str, str], ...] = (
    # (role, 显示名, 单位)
    ("vin_l1l2",           "L1L2 输入电压",      "V"),
    ("vin_l2l3",           "L2L3 输入电压",      "V"),
    ("vin_l3l1",           "L3L1 输入电压",      "V"),
    ("iout_u",             "U 相输出电流",       "A"),
    ("iout_v",             "V 相输出电流",       "A"),
    ("iout_w",             "W 相输出电流",       "A"),
    ("output_freq",        "输出频率",           "Hz"),
    ("motor_power_pct",    "电机输出功率",       "%"),
    ("output_torque_pct",  "输出转矩",           "%"),
    ("cabinet_temp",       "机柜温度",           "℃"),
    ("transformer_temp_a", "变压器 A 相温度",    "℃"),
    ("transformer_temp_b", "变压器 B 相温度",    "℃"),
    ("transformer_temp_c", "变压器 C 相温度",    "℃"),
    ("unit_vdc_mean",      "功率单元 Vdc 均值",  "V"),
    ("unit_vdc_spread",    "功率单元 Vdc 极差",  "V"),
)
CHANNEL_ROLES: tuple[str, ...] = tuple(c[0] for c in CHANNELS)

#: 6 类故障。★键与次序照搬原项目 `LABEL_MAP`，显示名照搬 `LABEL_DISPLAY`。
CLASS_DISPLAY: dict[str, str] = {
    "normal": "正常",
    "cooling_degradation": "冷却系统退化",
    "power_unit_anomaly": "功率单元异常",
    "output_current_imbalance": "输出电流不平衡",
    "input_grid_anomaly": "输入电网异常",
    "overload": "过载 / 异常负载",
}
KNOWN_CLASSES: tuple[str, ...] = tuple(CLASS_DISPLAY)

#: 未来严重度这三条的时间点（秒）。★次序即模型 `future_head` 的输出次序。
HORIZONS: tuple[int, ...] = (30, 60, 120)

#: 窗口跨度与模型期望长度的相对容差（同模块 4）。
SPAN_TOLERANCE = 0.25


class _VfdOnnx:
    """一个已经校验过的 VFD 模型。**构造即校验**。

    ★与 `servo_health` 里那个类形状相似但**各写各的** —— 域之间互不 import
      （分界文档的规矩）。两边的判据若要保持一致，靠用例钉，不靠共享代码。
    """

    def __init__(self, model_bytes: bytes) -> None:
        self.sess = ort.InferenceSession(model_bytes, providers=["CPUExecutionProvider"])
        ins = self.sess.get_inputs()
        if len(ins) != 1:
            raise ValueError(f"模型有 {len(ins)} 个输入，本域只会喂一个时序张量")
        self.input_name = ins[0].name
        self.shape = list(ins[0].shape)
        if len(self.shape) != 3:
            raise ValueError(f"输入形状 {self.shape} 不是三维 (batch, …, …)")

        meta = self.sess.get_modelmeta().custom_metadata_map or {}
        self.channels = _split(meta.get("channels", ""))
        self.classes = _split(meta.get("classes", ""))

        if not self.channels:
            raise ValueError(
                "模型元数据缺 `channels` —— 必须写明 15 路输入的顺序。"
                "★顺序喂错不会报错，只会给出一个自信的错结论，所以这一项没有缺省")
        _check_set(self.channels, CHANNEL_ROLES, "channels", "路")
        if not self.classes:
            raise ValueError(
                "模型元数据缺 `classes` —— 必须写明 6 类的顺序。"
                "★反了就是把「正常」报成「过载」，没有缺省")
        _check_set(self.classes, KNOWN_CLASSES, "classes", "类")

        rate = str(meta.get("rate_hz", "")).strip()
        try:
            self.rate_hz = float(rate)
        except ValueError:
            raise ValueError(
                "模型元数据缺 `rate_hz` 或写得不是数 —— 据此核对窗口跨度对不对得上；"
                f"收到 {rate!r}") from None
        if not (self.rate_hz > 0):
            raise ValueError(f"`rate_hz` 必须为正，收到 {self.rate_hz}")

        # ★归一化：原项目把它放在模型之外（scaler.json），所以这里必须问清楚。
        norm = str(meta.get("normalization", "")).strip().lower()
        if norm not in ("builtin", "zscore"):
            raise ValueError(
                "模型元数据缺 `normalization`（应为 `builtin` 或 `zscore`）—— "
                "原项目的归一化在模型之外（scaler.json 的 mean/std）。"
                "★少做一次 z-score 不会报错，模型照样吐出像模像样的概率与严重度，"
                f"这是本域最容易静默出错的一处，所以不许猜；收到 {norm!r}")
        self.normalization = norm
        self.mean = self.std = None
        if norm == "zscore":
            self.mean = _nums(meta.get("scaler_mean", ""), len(self.channels), "scaler_mean")
            self.std = _nums(meta.get("scaler_std", ""), len(self.channels), "scaler_std")
            if not np.all(self.std > 0):
                raise ValueError("`scaler_std` 里有非正数 —— 除下去会得到 inf/nan")

        n = len(self.channels)
        d1, d2 = self.shape[1], self.shape[2]
        c1, c2 = _dim_eq(d1, n), _dim_eq(d2, n)
        if c1 and c2:
            raise ValueError(
                f"输入形状 {self.shape} 两维都等于 {n}，分不清哪一维是通道")
        if not (c1 or c2):
            raise ValueError(f"输入形状 {self.shape} 没有哪一维等于通道数 {n}")
        self.channels_first = c1
        t_dim = d2 if c1 else d1
        if not isinstance(t_dim, int) or t_dim <= 0:
            raise ValueError(
                f"时间维是 {t_dim!r}（动态）—— 本域要模型把窗口长度固定下来，"
                "否则「这一窗该取多长」没有答案，只能靠猜")
        self.window_len = int(t_dim)

        outs = self.sess.get_outputs()
        if len(outs) != 3:
            raise ValueError(
                f"模型有 {len(outs)} 个输出，本域要三个头："
                "分类（6 类）、当前严重度（1）、未来严重度（3）")
        self.output_names = [o.name for o in outs]

    @property
    def window_sec(self) -> float:
        return (self.window_len - 1) / self.rate_hz if self.window_len > 1 else 0.0

    def predict(self, mat: "np.ndarray") -> dict:
        """`mat` 是 (window_len, 15)，列序 = `self.channels`。"""
        x = mat
        if self.normalization == "zscore":
            x = (x - self.mean) / self.std
        if self.channels_first:
            x = x.T
        x = np.ascontiguousarray(x[None, ...], dtype=np.float32)
        logits, current, future = self.sess.run(self.output_names, {self.input_name: x})

        probs = _softmax_if_needed(np.asarray(logits).reshape(-1).astype(np.float64))
        if probs.size != len(self.classes):
            raise ValueError(
                f"分类头给了 {probs.size} 个数，而 `classes` 说有 {len(self.classes)} 类")
        i = int(np.argmax(probs))

        cur = np.asarray(current).reshape(-1).astype(np.float64)
        if cur.size != 1:
            raise ValueError(f"当前严重度头给了 {cur.size} 个数，应为 1")
        fut = np.asarray(future).reshape(-1).astype(np.float64)
        if fut.size != len(HORIZONS):
            raise ValueError(f"未来严重度头给了 {fut.size} 个数，应为 {len(HORIZONS)}")

        return {
            "cls": self.classes[i],
            "confidence": float(probs[i]),
            "severity": float(cur[0]),
            "future": [float(v) for v in fut],
        }


def _split(s: str) -> list[str]:
    return [p.strip() for p in str(s or "").replace("，", ",").split(",") if p.strip()]


def _check_set(got: list[str], want: tuple[str, ...], what: str, unit: str) -> None:
    unknown = [c for c in got if c not in want]
    if unknown:
        raise ValueError(f"`{what}` 里有本域不认得的项 {unknown}；本域的是 {list(want)}")
    if len(set(got)) != len(got):
        raise ValueError(f"`{what}` 有重复：{got}")
    if len(got) != len(want):
        missing = [c for c in want if c not in got]
        raise ValueError(f"`{what}` 只有 {len(got)} {unit}，缺 {missing}")


def _nums(raw: str, n: int, what: str) -> "np.ndarray":
    parts = _split(raw)
    if not parts:
        raise ValueError(f"`normalization=zscore` 却没给 `{what}`")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"`{what}` 里有不是数的项：{raw!r}") from None
    if len(vals) != n:
        raise ValueError(f"`{what}` 有 {len(vals)} 个数，应为 {n} 个（每路一个）")
    arr = np.asarray(vals, dtype=np.float32)
    if not np.isfinite(arr).all():
        raise ValueError(f"`{what}` 里有 NaN/Inf")
    return arr


def _dim_eq(dim, n: int) -> bool:
    return isinstance(dim, int) and dim == n


def _softmax_if_needed(vec: "np.ndarray") -> "np.ndarray":
    """已经是概率就原样用；是 logits 就过一遍 softmax（同模块 4 的判据）。"""
    if vec.size == 0:
        return vec
    if float(vec.min()) >= 0.0 and abs(float(vec.sum()) - 1.0) < 1e-3:
        return vec
    z = vec - float(vec.max())
    e = np.exp(z)
    return e / float(e.sum())


class VfdHealth(Domain):
    key = "vfd_health"
    display = "AI 变频器（VFD）运行监测"
    version = "1.0.0"

    #: 落坏码时要一起落的数值类结论。
    _NUMERIC = ("confidence", "severity") + tuple(f"severity_{h}s" for h in HORIZONS)

    def __init__(self) -> None:
        self._models: dict[int, _VfdOnnx] = {}

    def capabilities(self) -> set[str]:
        # 有模型工件可供查看/启用；本域不训练（权重来自外部导入）。
        return {"infer", "artifact"}

    def declare(self) -> Declaration:
        outs = [
            OutputSpec(key="fault_type", display="故障类型", value_type="string",
                       description=" / ".join(CLASS_DISPLAY.values())),
            OutputSpec(key="confidence", display="置信度", value_type="float",
                       description="该类别的概率 0~1。★是模型的自评，不是可靠度"),
            OutputSpec(key="severity", display="当前严重度", value_type="float",
                       description="0~1，模型直接给出"),
        ]
        for h in HORIZONS:
            outs.append(OutputSpec(
                key=f"severity_{h}s", display=f"{h} 秒后严重度", value_type="float",
                description=f"0~1。★**模型给的**，不是本域拿当前值外推的 —— "
                            f"编出来的趋势最像真的"))
        outs.append(OutputSpec(key="evidence", display="判据摘要", value_type="string",
                               description="用了哪个工件、窗口取了多少点、为什么没给"))
        return Declaration(
            inputs=tuple(
                InputSpec(role=role, unit=unit, required=True, display=name,
                          description=f"{name}（变频器电气量，1 Hz）")
                for role, name, unit in CHANNELS
            ),
            # 定案：单一模型、无待定项 ⇒ **不设参数**。
            params=(),
            outputs=tuple(outs),
        )

    # ── 外部工件校验 ──────────────────────────────────────────────────────
    def validate_artifact(self, kind: str, blob: bytes) -> tuple[str, dict[str, str]]:
        if kind != "model":
            return f"本域只接受 kind=model 的工件，收到 {kind!r}", {}
        facts: dict[str, str] = {}
        try:
            m = _VfdOnnx(bytes(blob))
        except Exception as exc:  # noqa: BLE001
            return f"不是可用的 VFD ONNX 模型：{type(exc).__name__}: {exc}", facts
        meta = m.sess.get_modelmeta().custom_metadata_map or {}
        for k in ("description", "version", "license", "date", "algo"):
            if k in meta:
                facts[k] = str(meta[k])[:300]
        facts["channels"] = ",".join(m.channels)
        facts["classes"] = ",".join(m.classes)
        facts["rate_hz"] = f"{m.rate_hz:g}"
        facts["window_len"] = str(m.window_len)
        facts["window_sec"] = f"{m.window_sec:.3f}"
        facts["normalization"] = m.normalization
        facts["layout"] = "(batch, 通道, 时间)" if m.channels_first else "(batch, 时间, 通道)"
        return "", facts

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        t = frame.t_end
        art = frame.artifacts.get("model")
        if art is None:
            return self._none(t, Quality.MODEL_NOT_LOADED,
                              "这台变频器还没有启用模型工件 —— 本域不内置模型，也不拿默认模型顶")
        try:
            model = self._model_of(art)
        except Exception as exc:  # noqa: BLE001
            return self._none(t, Quality.MODEL_NOT_LOADED,
                              f"启用的工件用不了：{type(exc).__name__}: {exc}")

        mat, why = self._window(frame, model)
        if mat is None:
            q = Quality.NO_INPUT if why.startswith("一路都没有") else Quality.INSUFFICIENT_SAMPLES
            return self._none(t, q, why)

        try:
            out = model.predict(mat)
        except Exception as exc:  # noqa: BLE001
            return self._none(t, Quality.COMPUTE_ERROR,
                              f"模型算不出来：{type(exc).__name__}: {exc}")

        ev = {
            "工件": f"#{art.id} {art.name}" + (f"（{art.algo}）" if art.algo else ""),
            "窗口": f"{model.window_len} 点 @ {model.rate_hz:g} Hz ≈ {model.window_sec:.1f} 秒",
            "归一化": model.normalization,
            "各类概率": "见模型",
        }
        findings = [
            Finding(key="fault_type", value=CLASS_DISPLAY[out["cls"]],
                    quality=Quality.OK, t=t),
            Finding(key="confidence", value=round(out["confidence"], 4),
                    quality=Quality.OK, t=t),
            Finding(key="severity", value=round(out["severity"], 4),
                    quality=Quality.OK, t=t),
        ]
        for h, v in zip(HORIZONS, out["future"]):
            findings.append(Finding(key=f"severity_{h}s", value=round(v, 4),
                                    quality=Quality.OK, t=t))
        findings.append(Finding(key="evidence", value=json.dumps(ev, ensure_ascii=False),
                                quality=Quality.OK, t=t))
        return findings

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _model_of(self, art) -> _VfdOnnx:
        m = self._models.get(art.id)
        if m is None:
            m = _VfdOnnx(art.blob)
            self._models[art.id] = m
        return m

    def _window(self, frame: Frame, model: _VfdOnnx):
        """按模型自述的通道顺序与窗口长度取一段。**不补数、不插值、不重采样。**"""
        need = model.window_len
        cols: list[list[float]] = []
        spans = []
        present = 0
        for role in model.channels:
            samples = list(frame.channels.get(role) or ())
            if samples:
                present += 1
            if len(samples) < need:
                if present == 0:
                    continue
                return None, (f"「{_name_of(role)}」只有 {len(samples)} 个点，"
                              f"不足模型要的 {need} 个 —— 不补数")
            tail = samples[-need:]
            bad = [s for s in tail if not s.quality.is_good()]
            if bad:
                return None, (f"「{_name_of(role)}」窗口内有 {len(bad)} 个坏值 —— "
                              "15 路是一个整体，一路坏结论就不成立")
            vals = []
            for s in tail:
                if not isinstance(s.value, (int, float)) or isinstance(s.value, bool):
                    return None, f"「{_name_of(role)}」有非数值样本 {s.value!r}"
                vals.append(float(s.value))
            cols.append(vals)
            spans.append((tail[0].t, tail[-1].t))

        if present == 0:
            return None, "一路都没有数据 —— VFD 那 15 路现场可能还一个点都没接"
        if len(cols) != len(model.channels):
            return None, f"15 路里只有 {present} 路有数据 —— 缺一路结论就不成立"

        want_sec = model.window_sec
        if want_sec > 0:
            for role, (a, b) in zip(model.channels, spans):
                got_sec = (b - a).total_seconds()
                if abs(got_sec - want_sec) > want_sec * SPAN_TOLERANCE:
                    return None, (
                        f"「{_name_of(role)}」取到的 {need} 个点跨了 {got_sec:.1f} 秒，"
                        f"而模型按 {model.rate_hz:g} Hz 期望约 {want_sec:.1f} 秒 —— "
                        "疏密对不上就不算，不拉伸时间轴")
        return np.asarray(cols, dtype=np.float32).T, ""

    def _none(self, t, quality: Quality, why: str) -> list[Finding]:
        """一条都给不出时：故障类型与各数值全落同一个码，判据摘要说明原因。

        ★判据摘要**是好质量**：它本身没算错，它说的就是"为什么没给"。
        """
        out = [Finding(key="fault_type", value=None, quality=quality, t=t)]
        out += [Finding(key=k, value=None, quality=quality, t=t) for k in self._NUMERIC]
        out.append(Finding(key="evidence", value=why, quality=Quality.OK, t=t))
        return out


def _name_of(role: str) -> str:
    for r, name, _u in CHANNELS:
        if r == role:
            return name
    return role
