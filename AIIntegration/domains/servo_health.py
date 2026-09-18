"""AI 伺服/机械臂运行监测 —— 从伺服驱动器内部遥测判轴承退化。

> 定案见 `AIIntegration/doc/诊断配置流程定案.md`「模块 4」（2026-09-17 用户定）。
> 机械臂**不单独设模块**：每个关节配一条本诊断（定案 4.1）。

---

## 0.1 做什么

取 9 路伺服驱动器内部量的一段窗口（定案：10 Hz、每 5 秒取最近 20 秒 —— 这两个数配在**绑定**上，
不是本域的参数），交给模型判**正常 / 轴承退化**并给概率。

★**它不是振动诊断**：一个振动传感器都不用。要接它需要的是**驱动器通讯口**（读内部寄存器），
  与振动那条采集路径完全不同 —— 现场此刻**还没有这一路**（见 §0.4）。

## 0.2 ★为什么本域自己不带模型

原项目 v4 的伺服模型（CNN-LSTM / 决策树 / SVM 三条路）**训练代码与训练数据都没找到**
（`doc/原四项目调查.md`）。我方手上只有推理端的架构描述，没有权重。

⇒ 本域**不内置任何模型**，走「外部模型导入为工件」那条路（契约 1.5）：
  没启用工件 ⇒ 整组落 `MODEL_NOT_LOADED`，**不拿任何默认值顶**。
  拿个"看起来合理"的模型顶上去，出来的「轴承退化 0.83」看起来完全正常，这是最坏的一种。

## 0.3 ★模型必须自述三件事，否则拒收

工件是 **ONNX**（与 `vision_helmet` 同一套：单文件、自带图与元数据、可校验）。
但光是"能跑"不够 —— 本域**在导入时就要求**模型的元数据里写明：

| 键 | 为什么非要不可 |
| --- | --- |
| `channels` | 9 路的**顺序**。喂错顺序不会报错，只会给出一个**自信的错结论** —— 这是本域最危险的一处，只能在导入时挡 |
| `rate_hz` | 采样率。据此核对"这段窗口的时间跨度对不对得上模型期望的长度"，否则会把一段稀疏数据当成正常窗口喂进去 |
| `classes` | 类别顺序。哪个下标是"正常"、哪个是"轴承退化"，反了就是把好说成坏 |

三件缺任何一件都**拒收**，并在回执里说清要补什么。**我方不猜**：
这三件都属于"猜错了看不出来"的那一类。

## 0.4 现场还没有这一路（与模块 3 同一处境）

`doc/原四项目调查.md` 已核实：伺服那 9 个量现场**没有输入**（点已建、链路未通）。
按模块 3 第 3.3 条立下的同一条做法 —— **模块先落地：能配置、能保存，无数据如实回报**。
本域不会因为"反正现在没数据"就少写判据：现场一通就要能用，而不是那时才开始想。

## 0.5 纪律

- **不补数、不插值**：窗口内样本不够就说不够，绝不拿前后值凑出模型要的长度。
- **不重采样**：跨度对不上就说对不上（`rate_hz` 核对），不悄悄拉伸时间轴。
- **一路坏就不算**：9 路是一个整体，缺一路或有一路坏值，结论就不成立。
- **参数与工件不一致就不算**：诊断上选了「决策树」而启用的工件是 CNN-LSTM ⇒ 说清，不硬算。
"""

from __future__ import annotations

import json
from datetime import datetime

REQUIRES = ("numpy", "onnxruntime")

import numpy as np
import onnxruntime as ort

from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec,
)

#: 9 路伺服驱动器内部量。**顺序即本域的规范顺序** —— 模型元数据里的 `channels`
#: 必须是这 9 个的一个排列；我方按模型自述的顺序去取，不按这里的顺序硬喂。
CHANNELS: tuple[tuple[str, str, str, str], ...] = (
    # (role, 显示名, 单位, 说明)
    ("pos_cmd",      "位置指令",   "",   "驱动器收到的位置指令"),
    ("pos_act",      "实际位置",   "",   "编码器反馈的实际位置"),
    ("follow_err",   "跟随误差",   "",   "指令与实际之差，轴承退化的主要征兆之一"),
    ("speed",        "转速",       "rpm", "电机转速"),
    ("torque",       "转矩",       "",   "输出转矩"),
    ("iq",           "电流 Iq",    "A",  "交轴电流，与负载转矩对应"),
    ("motor_temp",   "电机温度",   "℃",  "电机绕组温度"),
    ("bearing_temp", "轴承温度",   "℃",  "★与温升同理：振动没变而温度升了，多半是润滑/冷却"),
    ("igbt_temp",    "IGBT 温度",  "℃",  "驱动器功率器件温度"),
)
CHANNEL_ROLES: tuple[str, ...] = tuple(c[0] for c in CHANNELS)

#: 本域认得的两类结论。★下标由模型元数据 `classes` 指定，不在这里写死次序。
CLASS_NORMAL = "正常"
CLASS_BEARING = "轴承退化"
KNOWN_CLASSES = (CLASS_NORMAL, CLASS_BEARING)

#: 「模型」参数的取值（定案 4.2）。值是算法标识，与工件记的 `algo` 对照。
MODEL_CHOICES = ("cnn_lstm", "decision_tree", "svm")
MODEL_DISPLAYS = ("CNN-LSTM", "决策树", "SVM")
DEFAULT_MODEL = "cnn_lstm"

#: 窗口时间跨度与模型期望长度的相对容差。
#: ★不是"差不多就行"：超出即说明这段数据的疏密与模型训练时不是一回事，
#:   照喂会得到一个**按错误时间尺度**算出来的结论，而它看起来完全正常。
SPAN_TOLERANCE = 0.25


class _ServoOnnx:
    """一个已经校验过的伺服模型。**构造即校验** —— 校验不过就抛，绝不半信半疑地留着。"""

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
        rate = str(meta.get("rate_hz", "")).strip()

        if not self.channels:
            raise ValueError(
                "模型元数据缺 `channels` —— 必须写明 9 路输入的顺序。"
                "★顺序喂错不会报错，只会给出一个自信的错结论，事后查不出来，所以这一项没有缺省")
        unknown = [c for c in self.channels if c not in CHANNEL_ROLES]
        if unknown:
            raise ValueError(f"`channels` 里有本域不认得的量 {unknown}；本域的 9 路是 {list(CHANNEL_ROLES)}")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError(f"`channels` 有重复：{self.channels}")
        if len(self.channels) != len(CHANNEL_ROLES):
            missing = [c for c in CHANNEL_ROLES if c not in self.channels]
            raise ValueError(f"`channels` 只有 {len(self.channels)} 路，缺 {missing}")

        if not self.classes:
            raise ValueError(
                "模型元数据缺 `classes` —— 必须写明类别顺序（哪个下标是正常、哪个是轴承退化）。"
                "★反了就是把好的说成坏的，没有缺省")
        bad = [c for c in self.classes if c not in KNOWN_CLASSES]
        if bad:
            raise ValueError(f"`classes` 里有本域不认得的类别 {bad}；本域只判 {list(KNOWN_CLASSES)}")
        if len(set(self.classes)) != len(self.classes):
            raise ValueError(f"`classes` 有重复：{self.classes}")

        try:
            self.rate_hz = float(rate)
        except ValueError:
            raise ValueError(
                "模型元数据缺 `rate_hz` 或写得不是数 —— 据此核对窗口跨度对不对得上；"
                f"收到 {rate!r}") from None
        if not (self.rate_hz > 0):
            raise ValueError(f"`rate_hz` 必须为正，收到 {self.rate_hz}")

        # 9 落在哪一维就按哪一维摆：(batch, C, T) 还是 (batch, T, C)。
        n = len(self.channels)
        d1, d2 = self.shape[1], self.shape[2]
        c_is_1 = _dim_eq(d1, n)
        c_is_2 = _dim_eq(d2, n)
        if c_is_1 and c_is_2:
            raise ValueError(
                f"输入形状 {self.shape} 两维都等于 {n}，分不清哪一维是通道 —— "
                "请在导出时把时间维固定成别的长度")
        if not (c_is_1 or c_is_2):
            raise ValueError(f"输入形状 {self.shape} 没有哪一维等于通道数 {n}")
        self.channels_first = c_is_1
        t_dim = d2 if c_is_1 else d1
        if not isinstance(t_dim, int) or t_dim <= 0:
            raise ValueError(
                f"时间维是 {t_dim!r}（动态）—— 本域要模型把窗口长度固定下来，"
                "否则「这一窗该取多长」没有答案，只能靠猜")
        self.window_len = int(t_dim)

        outs = self.sess.get_outputs()
        if len(outs) != 1:
            raise ValueError(f"模型有 {len(outs)} 个输出，本域只认一个分类输出")
        self.output_name = outs[0].name

    @property
    def window_sec(self) -> float:
        return (self.window_len - 1) / self.rate_hz if self.window_len > 1 else 0.0

    def predict(self, mat: "np.ndarray") -> tuple[str, float]:
        """`mat` 是 (window_len, 9)，列序 = `self.channels`。返回 (类别, 概率)。"""
        x = mat.T if self.channels_first else mat
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


def _dim_eq(dim, n: int) -> bool:
    return isinstance(dim, int) and dim == n


def _softmax_if_needed(vec: "np.ndarray") -> "np.ndarray":
    """已经是概率就原样用；是 logits 就过一遍 softmax。

    ★判据：非负且和≈1 才当成概率。猜错的方向是安全的 —— softmax 一个已是概率的向量
      会把差距压平（概率变保守），而把 logits 当概率用会给出 >1 或负的"概率"。
    """
    if vec.size == 0:
        return vec
    if float(vec.min()) >= 0.0 and abs(float(vec.sum()) - 1.0) < 1e-3:
        return vec
    z = vec - float(vec.max())
    e = np.exp(z)
    return e / float(e.sum())


class ServoHealth(Domain):
    key = "servo_health"
    display = "AI 伺服/机械臂运行监测"
    version = "1.0.0"

    def __init__(self) -> None:
        # 工件 id → 已校验的模型。工件只增不改，同一个 id 就是同一个模型，不必每拍重建会话。
        self._models: dict[int, _ServoOnnx] = {}

    def capabilities(self) -> set[str]:
        # 有模型工件可供查看/启用；本域**不训练**（原项目的训练代码与数据都没找到，见模块头 §0.2）。
        return {"infer", "artifact"}

    def declare(self) -> Declaration:
        return Declaration(
            inputs=tuple(
                InputSpec(role=role, unit=unit, required=True, display=name,
                          description=desc)
                for role, name, unit, desc in CHANNELS
            ),
            params=(
                ParamSpec(
                    key="model", display="模型", value_type="enum",
                    choices=MODEL_CHOICES, choice_displays=MODEL_DISPLAYS,
                    default=DEFAULT_MODEL, has_default=True, required=False, level="position",
                    description="这条诊断打算用哪种模型（定案 4.2）。★它只表达意图，"
                                "真正算的是这台设备当前启用的模型工件；两者对不上时本域不硬算，"
                                "在判据摘要里说清"),
            ),
            outputs=(
                OutputSpec(key="verdict", display="结论", value_type="string",
                           description=f"{CLASS_NORMAL} / {CLASS_BEARING}"),
                OutputSpec(key="probability", display="概率", value_type="float",
                           description="模型给该结论的概率 0~1。★是模型的自评，不是可靠度"),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="用了哪个工件、窗口取了多少点、为什么没给"),
            ),
        )

    # ── 外部工件校验（导入时由骨架调用）─────────────────────────────────
    def validate_artifact(self, kind: str, blob: bytes) -> tuple[str, dict[str, str]]:
        """能不能当本域的模型用。返回 (拒收原因, 从模型里读出的事实)。

        ★这里挡的是"喂错顺序/类别反了/疏密对不上"这三类 —— 它们的共同点是
          **运行期不会报错，只会给出一个看起来完全正常的错结论**。所以只能在导入这一刻挡。
        """
        if kind != "model":
            return f"本域只接受 kind=model 的工件，收到 {kind!r}", {}
        facts: dict[str, str] = {}
        try:
            m = _ServoOnnx(bytes(blob))
        except Exception as exc:  # noqa: BLE001
            return f"不是可用的伺服 ONNX 模型：{type(exc).__name__}: {exc}", facts
        meta = m.sess.get_modelmeta().custom_metadata_map or {}
        for k in ("description", "version", "license", "date", "algo"):
            if k in meta:
                facts[k] = str(meta[k])[:300]
        facts["channels"] = ",".join(m.channels)
        facts["classes"] = ",".join(m.classes)
        facts["rate_hz"] = f"{m.rate_hz:g}"
        facts["window_len"] = str(m.window_len)
        facts["window_sec"] = f"{m.window_sec:.3f}"
        facts["layout"] = "(batch, 通道, 时间)" if m.channels_first else "(batch, 时间, 通道)"
        return "", facts

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        t = frame.t_end
        art = frame.artifacts.get("model")
        if art is None:
            return self._none(t, Quality.MODEL_NOT_LOADED,
                              "这台设备还没有启用伺服模型工件 —— 本域不内置模型，"
                              "也不拿默认模型顶（顶上去的结论看起来完全正常）")

        try:
            model = self._model_of(art)
        except Exception as exc:  # noqa: BLE001
            return self._none(t, Quality.MODEL_NOT_LOADED,
                              f"启用的工件用不了：{type(exc).__name__}: {exc}")

        # 诊断上选的模型与工件实际是什么，对不上就不算（定案 4.2 那一项只表达意图）。
        want = (frame.params.get("model") or DEFAULT_MODEL).strip()
        got = (art.algo or "").strip()
        if want and got and want.lower() != got.lower():
            return self._none(
                t, Quality.CONFIG_INCOMPLETE,
                f"这条诊断选的是「{_display_of(want)}」，而启用的工件是「{got}」—— "
                "不硬算：两者判据不同，拿哪个都说不清结论是谁给的。"
                "请改诊断上的模型参数，或启用对应的工件")

        mat, why = self._window(frame, model)
        if mat is None:
            q = Quality.NO_INPUT if why.startswith("一路都没有") else Quality.INSUFFICIENT_SAMPLES
            return self._none(t, q, why)

        try:
            label, prob = model.predict(mat)
        except Exception as exc:  # noqa: BLE001
            return self._none(t, Quality.COMPUTE_ERROR,
                              f"模型算不出来：{type(exc).__name__}: {exc}")

        ev = {
            "工件": f"#{art.id} {art.name}" + (f"（{art.algo}）" if art.algo else ""),
            "窗口": f"{model.window_len} 点 @ {model.rate_hz:g} Hz ≈ {model.window_sec:.1f} 秒",
            "通道顺序": ",".join(model.channels),
        }
        return [
            Finding(key="verdict", value=label, quality=Quality.OK, t=t),
            Finding(key="probability", value=round(prob, 4), quality=Quality.OK, t=t),
            Finding(key="evidence", value=json.dumps(ev, ensure_ascii=False),
                    quality=Quality.OK, t=t),
        ]

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _model_of(self, art) -> _ServoOnnx:
        m = self._models.get(art.id)
        if m is None:
            m = _ServoOnnx(art.blob)
            self._models[art.id] = m
        return m

    def _window(self, frame: Frame, model: _ServoOnnx):
        """按模型自述的通道顺序与窗口长度取一段。返回 ((T,9) 矩阵, "") 或 (None, 原因)。

        ★**不补数、不插值、不重采样**。凑出来的那几个点在下游看来与真数据无异。
        """
        need = model.window_len
        picked: list[list[float]] = []
        spans: list[tuple[datetime, datetime]] = []
        present = 0
        for role in model.channels:
            samples = list(frame.channels.get(role) or ())
            if samples:
                present += 1
            if len(samples) < need:
                if present == 0:
                    continue
                return None, (f"「{_name_of(role)}」这一路只有 {len(samples)} 个点，"
                              f"不足模型要的 {need} 个 —— 不补数")
            tail = samples[-need:]
            bad = [s for s in tail if not s.quality.is_good()]
            if bad:
                return None, (f"「{_name_of(role)}」窗口内有 {len(bad)} 个坏值 —— "
                              "9 路是一个整体，一路坏结论就不成立")
            vals = []
            for s in tail:
                if not isinstance(s.value, (int, float)) or isinstance(s.value, bool):
                    return None, f"「{_name_of(role)}」有非数值样本 {s.value!r}"
                vals.append(float(s.value))
            picked.append(vals)
            spans.append((tail[0].t, tail[-1].t))

        if present == 0:
            return None, "一路都没有数据 —— 伺服 9 个量要驱动器通讯口，现场这一路可能还没通"
        if len(picked) != len(model.channels):
            have = present
            return None, (f"9 路里只有 {have} 路有数据 —— 缺一路结论就不成立")

        # 跨度核对：这段窗口的疏密与模型训练时是不是一回事。
        want_sec = model.window_sec
        if want_sec > 0:
            for role, (a, b) in zip(model.channels, spans):
                got_sec = (b - a).total_seconds()
                if abs(got_sec - want_sec) > want_sec * SPAN_TOLERANCE:
                    return None, (
                        f"「{_name_of(role)}」取到的 {need} 个点跨了 {got_sec:.1f} 秒，"
                        f"而模型按 {model.rate_hz:g} Hz 期望约 {want_sec:.1f} 秒 —— "
                        "疏密对不上就不算，不拉伸时间轴")

        return np.asarray(picked, dtype=np.float32).T, ""

    def _none(self, t, quality: Quality, why: str) -> list[Finding]:
        """一条都给不出时：三条结论全落同一个码，并把原因写进判据摘要。

        ★判据摘要这一条**是好质量**：它本身没算错，它说的就是"为什么没给"。
        """
        return [
            Finding(key="verdict", value=None, quality=quality, t=t),
            Finding(key="probability", value=None, quality=quality, t=t),
            Finding(key="evidence", value=why, quality=Quality.OK, t=t),
        ]


def _name_of(role: str) -> str:
    for r, name, _u, _d in CHANNELS:
        if r == role:
            return name
    return role


def _display_of(choice: str) -> str:
    for c, d in zip(MODEL_CHOICES, MODEL_DISPLAYS):
        if c == choice:
            return d
    return choice
