"""AI 压装机运行监测 —— 一次压装的力-位移曲线判四类。

> 定案见 `AIIntegration/doc/诊断配置流程定案.md`「模块 6」与
> `AIIntegration/doc/结构值点定案.md`（结构 `AI_PressCurve` 的字段表在 P3）。

---

## 0.1 ★输入形态：一次压装 = **一个结构值**，不是三个标量点

`H-246`（实时库）把这一类从"三个标量点 + 边界标记点"改成**一个结构值**，理由两条，
我方认下并撤回了原先的反对（`AI-50 §2.3`）：

1. **压装的横轴是位移，不是时间。** 拆成三个按时间对齐的标量点，等于强行给它安一个
   它没有的时间轴；
2. **事件边界自然就有了** —— 一个值 = 一次压装，自带时刻。原先那个"边界标记点"方案
   是为了在连续时序里切出"这一次压装从哪到哪"，现在不需要了。

⇒ 本域声明一路 `kind="struct"` 的输入，骨架把 `VQT.StructVal` 解好交过来
（`Frame.structs`）。**域不碰实时库、不碰字节解码**。

## 0.2 做什么

每次压装结束触发一次：`位移 / 速度 / 力` 三条等长曲线 → 模型 → **四类之一** + 概率。

| 类别 | |
| --- | --- |
| `normal` | 正常压装 |
| `jam` | 卡滞 |
| `under_press` | 压装不到位 |
| `clearance` | 配合间隙异常 |

★**界面须注明「本模型不判：漏装、零件破裂」**（定案 6.2）。
  不写清楚，用户会把"没报这两类"当成"这两类没发生"，而模型压根没学过它们。

## 0.3 ★本域不内置模型（与模块 4、5 同）

原项目 v4 的压装模型（SVM / CNN-LSTM）权重不在本仓，也不该由本仓携带。
⇒ 走「外部模型导入为工件」（契约 1.5）。没启用工件 ⇒ 整组落 `MODEL_NOT_LOADED`，
**不拿默认模型顶** —— 顶上去出来的「卡滞 0.87」看起来完全正常，这是最坏的一种。

## 0.4 ★模型必须自述四件，否则导入时就拒

与模块 4、5 同一条理由：**这几件运行期都不报错，只会给出一个看起来完全正常的错结论。**

| 键 | 缺了会怎样 |
| --- | --- |
| `curves` | 三条曲线喂进模型的**顺序**。喂错不报错，只给一个自信的错结论 |
| `points` | 模型期望的**重采样点数**。压装每次的采样点数不同（行程不同），必须重采到定长 |
| `classes` | 四类的**顺序**。反了就是把"正常"报成"卡滞" |
| `normalization` | `builtin`（烘进图里）或 `minmax`（另给各曲线的上下限）。★压装三条曲线量纲差三个数量级（mm / mm·s⁻¹ / N），少做一次归一化，模型照样吐出像样的概率 |

## 0.5 纪律

- **三条曲线必须等长**：不等长即这一次压装的数据有问题，落坏码并说清 ——
  ★这条**结构描述表达不了**（它没有跨字段约束那一格），只能由本域校验（定案 P3）。
- **按位移重采样，不按时间**：横轴是位移，这是本域与模块 4、5 最本质的差别。
- **不补数**：点数不足以重采样就说不够。
"""

from __future__ import annotations

import json

REQUIRES = ("numpy", "onnxruntime")

import numpy as np
import onnxruntime as ort

from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec, StructFieldSpec, StructSpec,
)

#: 结构名与字段名 —— 与 `doc/结构值点定案.md` P3 那张表逐字一致。
STRUCT_NAME = "AI_PressCurve"
CURVE_FIELDS = ("position", "velocity", "force")
ROLE = "press_curve"

#: 四类。★键与次序即模型输出的下标次序，反了就是张冠李戴。
CLASS_DISPLAY = {
    "normal": "正常压装",
    "jam": "卡滞",
    "under_press": "压装不到位",
    "clearance": "配合间隙异常",
}
KNOWN_CLASSES = tuple(CLASS_DISPLAY)

#: ★模型**不判**的两类。界面必须注明（定案 6.2）。
NOT_JUDGED = ("漏装", "零件破裂")

MODEL_CHOICES = ("cnn_lstm", "svm")
MODEL_DISPLAYS = ("CNN-LSTM", "SVM")
DEFAULT_MODEL = "cnn_lstm"


def press_curve_struct() -> StructSpec:
    """`AI_PressCurve` v1 —— 定案 P3 的字段表。

    ★三条曲线**等长**这条约束**不在这里** —— 结构描述没有"跨字段约束"那一格。
      写进 `description` 靠人眼的不算约束，所以由本域在推理时校验（见 `_curves`）。
    """
    return StructSpec(
        name=STRUCT_NAME, display="压装曲线",
        description="一次压装 = 一个值。位移/速度/力三条等长曲线，横轴是位移不是时间。",
        fields=(
            StructFieldSpec(name="sample_rate", type="int32", display="采样率", unit="Hz",
                            description="原始采样率。横轴虽是位移，采样仍按时间做，留它才还原得出每点时刻"),
            StructFieldSpec(name="position", type="numbuf", dtype="f32", shape=(-1,),
                            display="位移", unit="mm"),
            StructFieldSpec(name="velocity", type="numbuf", dtype="f32", shape=(-1,),
                            display="速度", unit="mm/s"),
            StructFieldSpec(name="force", type="numbuf", dtype="f32", shape=(-1,),
                            display="力", unit="N"),
        ))


class _PressOnnx:
    """一个已经校验过的压装模型。**构造即校验** —— 校验不过就抛，绝不半信半疑地留着。"""

    def __init__(self, model_bytes: bytes) -> None:
        self.sess = ort.InferenceSession(model_bytes, providers=["CPUExecutionProvider"])
        ins = self.sess.get_inputs()
        if len(ins) != 1:
            raise ValueError(f"模型有 {len(ins)} 个输入，本域只会喂一个 (曲线×点数) 张量")
        self.input_name = ins[0].name
        self.shape = list(ins[0].shape)
        if len(self.shape) != 3:
            raise ValueError(f"输入形状 {self.shape} 不是三维 (batch, …, …)")

        meta = self.sess.get_modelmeta().custom_metadata_map or {}
        self.curves = _split(meta.get("curves", ""))
        self.classes = _split(meta.get("classes", ""))

        if not self.curves:
            raise ValueError(
                "模型元数据缺 `curves` —— 必须写明三条曲线喂进模型的顺序。"
                "★顺序喂错不会报错，只会给出一个自信的错结论，所以这一项没有缺省")
        unknown = [c for c in self.curves if c not in CURVE_FIELDS]
        if unknown:
            raise ValueError(f"`curves` 里有本域不认得的曲线 {unknown}；本域的是 {list(CURVE_FIELDS)}")
        if len(set(self.curves)) != len(self.curves):
            raise ValueError(f"`curves` 有重复：{self.curves}")
        if len(self.curves) != len(CURVE_FIELDS):
            missing = [c for c in CURVE_FIELDS if c not in self.curves]
            raise ValueError(f"`curves` 只有 {len(self.curves)} 条，缺 {missing}")

        if not self.classes:
            raise ValueError(
                "模型元数据缺 `classes` —— 必须写明四类的顺序。★反了就是把「正常压装」报成「卡滞」")
        bad = [c for c in self.classes if c not in KNOWN_CLASSES]
        if bad:
            raise ValueError(f"`classes` 里有本域不认得的类别 {bad}；本域只判 {list(KNOWN_CLASSES)}")
        if len(set(self.classes)) != len(self.classes):
            raise ValueError(f"`classes` 有重复：{self.classes}")
        if len(self.classes) != len(KNOWN_CLASSES):
            # ★只声明一部分类别的模型必须拒：收下它，缺的那几类**永远报不出来**，
            #   而界面上写着四类 —— 用户会把"从没报过配合间隙异常"读成"没发生过"。
            missing = [c for c in KNOWN_CLASSES if c not in self.classes]
            raise ValueError(
                f"`classes` 只有 {len(self.classes)} 类，缺 {missing} —— "
                "本域的结论点声明了四类，模型判不全就不能当四类模型用")

        pts = str(meta.get("points", "")).strip()
        try:
            self.points = int(pts)
        except ValueError:
            raise ValueError(
                "模型元数据缺 `points` 或写得不是整数 —— 每次压装的采样点数都不同（行程不同），"
                f"必须重采到模型期望的定长；收到 {pts!r}") from None
        if self.points < 2:
            raise ValueError(f"`points` 必须 ≥2，收到 {self.points}")

        norm = str(meta.get("normalization", "")).strip().lower()
        if norm not in ("builtin", "minmax"):
            raise ValueError(
                "模型元数据缺 `normalization`（应为 `builtin` 或 `minmax`）—— "
                "★压装三条曲线量纲差三个数量级（mm / mm·s⁻¹ / N），"
                f"少做一次归一化不会报错，模型照样吐出像样的概率；收到 {norm!r}")
        self.normalization = norm
        self.lo = self.hi = None
        if norm == "minmax":
            self.lo = _nums(meta.get("curve_min", ""), len(self.curves), "curve_min")
            self.hi = _nums(meta.get("curve_max", ""), len(self.curves), "curve_max")
            if not np.all(self.hi > self.lo):
                raise ValueError("`curve_max` 必须逐条大于 `curve_min`")

        n = len(self.curves)
        d1, d2 = self.shape[1], self.shape[2]
        c1, c2 = _dim_eq(d1, n), _dim_eq(d2, n)
        if c1 and c2:
            raise ValueError(f"输入形状 {self.shape} 两维都等于 {n}，分不清哪一维是曲线")
        if not (c1 or c2):
            raise ValueError(f"输入形状 {self.shape} 没有哪一维等于曲线数 {n}")
        self.curves_first = c1
        t_dim = d2 if c1 else d1
        if isinstance(t_dim, int) and t_dim > 0 and t_dim != self.points:
            raise ValueError(
                f"模型说 `points={self.points}`，而输入形状的点数维是 {t_dim} —— 对不上")

        outs = self.sess.get_outputs()
        if len(outs) != 1:
            raise ValueError(f"模型有 {len(outs)} 个输出，本域只认一个分类输出")
        self.output_name = outs[0].name

    def predict(self, mat: "np.ndarray") -> tuple[str, float]:
        """`mat` 是 (points, 曲线数)，列序 = `self.curves`。"""
        x = mat
        if self.normalization == "minmax":
            x = (x - self.lo) / (self.hi - self.lo)
        if self.curves_first:
            x = x.T
        x = np.ascontiguousarray(x[None, ...], dtype=np.float32)
        raw = self.sess.run([self.output_name], {self.input_name: x})[0]
        vec = np.asarray(raw).reshape(-1).astype(np.float64)
        if vec.size != len(self.classes):
            raise ValueError(
                f"模型输出 {vec.size} 个数，而 `classes` 说有 {len(self.classes)} 类 —— 对不上")
        probs = _softmax_if_needed(vec)
        i = int(np.argmax(probs))
        return self.classes[i], float(probs[i])


def _split(s: str) -> list[str]:
    return [p.strip() for p in str(s or "").replace("，", ",").split(",") if p.strip()]


def _nums(raw: str, n: int, what: str) -> "np.ndarray":
    parts = _split(raw)
    if not parts:
        raise ValueError(f"`normalization=minmax` 却没给 `{what}`")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"`{what}` 里有不是数的项：{raw!r}") from None
    if len(vals) != n:
        raise ValueError(f"`{what}` 有 {len(vals)} 个数，应为 {n} 个（每条曲线一个）")
    arr = np.asarray(vals, dtype=np.float32)
    if not np.isfinite(arr).all():
        raise ValueError(f"`{what}` 里有 NaN/Inf")
    return arr


def _dim_eq(dim, n: int) -> bool:
    return isinstance(dim, int) and dim == n


def _softmax_if_needed(vec: "np.ndarray") -> "np.ndarray":
    if vec.size == 0:
        return vec
    if float(vec.min()) >= 0.0 and abs(float(vec.sum()) - 1.0) < 1e-3:
        return vec
    z = vec - float(vec.max())
    e = np.exp(z)
    return e / float(e.sum())


class PressFit(Domain):
    key = "press_fit"
    display = "AI 压装机运行监测"
    version = "1.0.0"

    _NUMERIC = ("probability", "peak_force", "stroke")

    def __init__(self) -> None:
        self._models: dict[int, _PressOnnx] = {}

    def capabilities(self) -> set[str]:
        return {"infer", "artifact"}

    def declare(self) -> Declaration:
        return Declaration(
            structs=(press_curve_struct(),),
            inputs=(
                InputSpec(role=ROLE, kind="struct", struct=STRUCT_NAME, required=True,
                          display="压装曲线",
                          description="一次压装 = 一个值（位移/速度/力三条等长曲线）。"
                                      "压装结束时由采集侧写入，每来一个值算一次"),
            ),
            params=(
                ParamSpec(
                    key="model", display="模型", value_type="enum",
                    choices=MODEL_CHOICES, choice_displays=MODEL_DISPLAYS,
                    default=DEFAULT_MODEL, has_default=True, required=False, level="position",
                    description="这条诊断打算用哪种模型（定案 6.1）。★它只表达意图，"
                                "真正算的是这台设备当前启用的模型工件；两者对不上时本域不硬算"),
            ),
            outputs=(
                OutputSpec(key="verdict", display="结论", value_type="string",
                           description=" / ".join(CLASS_DISPLAY.values())
                                       + f"。★本模型不判：{'、'.join(NOT_JUDGED)}",
                           choices=KNOWN_CLASSES,
                           choice_displays=tuple(CLASS_DISPLAY.values())),
                OutputSpec(key="probability", display="概率", value_type="float",
                           description="模型给该结论的概率 0~1。★是模型的自评，不是可靠度"),
                OutputSpec(key="peak_force", display="峰值力", value_type="float", unit="N",
                           description="★这条是**特征量**，由我方算完写回标量点 —— "
                                       "原始曲线在结构值点上，标量那套聚合/趋势对它不适用"),
                OutputSpec(key="stroke", display="行程", value_type="float", unit="mm",
                           description="本次压装的位移跨度（末 − 首）"),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="用了哪个工件、这次压装多少点、为什么没给"),
            ),
        )

    # ── 外部工件校验 ──────────────────────────────────────────────────────
    def validate_artifact(self, kind: str, blob: bytes) -> tuple[str, dict[str, str]]:
        if kind != "model":
            return f"本域只接受 kind=model 的工件，收到 {kind!r}", {}
        facts: dict[str, str] = {}
        try:
            m = _PressOnnx(bytes(blob))
        except Exception as exc:  # noqa: BLE001
            return f"不是可用的压装 ONNX 模型：{type(exc).__name__}: {exc}", facts
        meta = m.sess.get_modelmeta().custom_metadata_map or {}
        for k in ("description", "version", "license", "date", "algo"):
            if k in meta:
                facts[k] = str(meta[k])[:300]
        facts["curves"] = ",".join(m.curves)
        facts["classes"] = ",".join(m.classes)
        facts["points"] = str(m.points)
        facts["normalization"] = m.normalization
        facts["layout"] = "(batch, 曲线, 点)" if m.curves_first else "(batch, 点, 曲线)"
        facts["not_judged"] = "、".join(NOT_JUDGED)
        return "", facts

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        t = frame.t_end
        samples = list(frame.structs.get(ROLE) or ())
        art = frame.artifacts.get("model")

        if art is None:
            return self._none(t, Quality.MODEL_NOT_LOADED,
                              "这台压装机还没有启用模型工件 —— 本域不内置模型，也不拿默认模型顶")
        try:
            model = self._model_of(art)
        except Exception as exc:  # noqa: BLE001
            return self._none(t, Quality.MODEL_NOT_LOADED,
                              f"启用的工件用不了：{type(exc).__name__}: {exc}")

        want = (frame.params.get("model") or DEFAULT_MODEL).strip()
        got = (art.algo or "").strip()
        if want and got and want.lower() != got.lower():
            return self._none(
                t, Quality.CONFIG_INCOMPLETE,
                f"这条诊断选的是「{_display_of(want)}」，而启用的工件是「{got}」—— 不硬算")

        if not samples:
            return self._none(t, Quality.NO_INPUT,
                              "这一拍没有压装曲线 —— 压装是事件驱动的，没压就没有值；"
                              "若长期没有，请查采集侧有没有在压装结束时写入结构值点")

        # 一拍可能攒了多次压装，只判**最后一次**（每次压装一个值，结论点是"最近这次"）。
        s = samples[-1]
        if not s.quality.is_good():
            return self._none(t, Quality.INPUT_BAD,
                              f"最近这次压装的值质量为 {s.quality.value} —— 不拿坏值算结论")

        mat, curves_raw, why = self._curves(s, model)
        if mat is None:
            return self._none(t, Quality.INSUFFICIENT_SAMPLES, why)

        try:
            label, prob = model.predict(mat)
        except Exception as exc:  # noqa: BLE001
            return self._none(t, Quality.COMPUTE_ERROR,
                              f"模型算不出来：{type(exc).__name__}: {exc}")

        pos = curves_raw["position"]
        force = curves_raw["force"]
        ev = {
            "工件": f"#{art.id} {art.name}" + (f"（{art.algo}）" if art.algo else ""),
            "本次点数": len(pos),
            "重采到": model.points,
            "曲线顺序": ",".join(model.curves),
            "归一化": model.normalization,
            "不判": "、".join(NOT_JUDGED),
        }
        # ★结论的时刻取**这次压装自己的时刻**，不是帧右端 —— 一拍里可能攒了好几次。
        ts = s.t
        return [
            Finding(key="verdict", value=label, quality=Quality.OK, t=ts),
            Finding(key="probability", value=round(prob, 4), quality=Quality.OK, t=ts),
            Finding(key="peak_force", value=round(float(max(force)), 4),
                    quality=Quality.OK, t=ts),
            Finding(key="stroke", value=round(float(pos[-1] - pos[0]), 4),
                    quality=Quality.OK, t=ts),
            Finding(key="evidence", value=json.dumps(ev, ensure_ascii=False),
                    quality=Quality.OK, t=ts),
        ]

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _model_of(self, art) -> _PressOnnx:
        m = self._models.get(art.id)
        if m is None:
            m = _PressOnnx(art.blob)
            self._models[art.id] = m
        return m

    def _curves(self, s, model: _PressOnnx):
        """把一次压装的三条曲线取出、校验、按**位移**重采到模型要的点数。

        ★**按位移重采样，不按时间** —— 压装的横轴是位移，这是本域与模块 4、5 最本质的差别。
        ★**三条曲线等长**由这里校验：结构描述没有跨字段约束那一格（定案 P3）。
        """
        raw = {}
        for name in CURVE_FIELDS:
            buf = s.fields.get(name)
            if buf is None:
                return None, None, f"这次压装的值里没有「{name}」这条曲线"
            try:
                arr = np.frombuffer(buf.data, dtype=("<f4" if not buf.big_endian else ">f4"))
            except Exception as exc:  # noqa: BLE001
                return None, None, f"「{name}」的数值缓冲解不开：{exc}"
            raw[name] = arr

        lens = {k: len(v) for k, v in raw.items()}
        if len(set(lens.values())) != 1:
            return None, None, (f"三条曲线长度不一致 {lens} —— 同一次压装的同一批采样，"
                                "不等长说明这次的数据有问题，不拿它算结论")
        n = next(iter(lens.values()))
        if n < 2:
            return None, None, f"这次压装只有 {n} 个点，不足以重采样 —— 不补数"

        pos = raw["position"].astype(np.float64)
        span = float(pos[-1] - pos[0])
        if not np.isfinite(span) or abs(span) < 1e-9:
            return None, None, "位移没有跨度（首末相等或非有限）—— 按位移重采样无从下手"

        # 按位移等距重采。位移应单调；不单调就按累计行程排一遍，不静默丢点。
        order = np.argsort(pos) if span > 0 else np.argsort(-pos)
        pos_sorted = pos[order]
        grid = np.linspace(pos_sorted[0], pos_sorted[-1], model.points)
        cols = []
        for name in model.curves:
            y = raw[name].astype(np.float64)[order]
            cols.append(np.interp(grid, pos_sorted, y))
        return np.stack(cols, axis=1).astype(np.float32), raw, ""

    def _none(self, t, quality: Quality, why: str) -> list[Finding]:
        out = [Finding(key="verdict", value=None, quality=quality, t=t)]
        out += [Finding(key=k, value=None, quality=quality, t=t) for k in self._NUMERIC]
        out.append(Finding(key="evidence", value=why, quality=Quality.OK, t=t))
        return out


def _display_of(choice: str) -> str:
    for c, d in zip(MODEL_CHOICES, MODEL_DISPLAYS):
        if c == choice:
            return d
    return choice
