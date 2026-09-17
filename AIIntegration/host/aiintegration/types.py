"""骨架与算法模块之间的类型 —— **VQT 三元组在这一层被强制**。

设计见 `doc/骨架与算法模块分界.md` §4。一句话：

    构造一个结论必须同时给出 V、Q、T；没有默认参数、没有可选字段，
    **"只填 V"在代码层不可能** —— 即便模块作者不懂 VQT，也写不出违反铁律的结论。

两条各自从不同事故来的规矩，合在这里：

  · **Q 不许伪造**：算不出来就落质量码，不许悄悄给一个数。
    出处 —— v4 把 `NaN/Inf` 静默写成 `0.0`（我方）。
  · **T 不许伪造**：`t` 是**该结论所依据的数据的时刻**，不是"算完的时刻"、不是"写入的时刻"。
    出处 —— 组态图回放把"播放时刻"当成"样本时刻"（AICloud，C-9 §2.3）。

  两件事的共同点：**混用不报错**。所以只能靠类型层挡，不能靠人记得。

★本模块**不 import 任何第三方库**（连 numpy 都不）。骨架要能装在只有 grpcio/protobuf
  的环境里；采样点用普通序列表达，模块自己转 numpy。
  代价：高频波形逐点走 Python 对象不划算 —— 那一档等"高频波形进不进实时库"定了再说，
  当前四个域都是标量节拍（v5 每传感器 13 点），不构成问题。
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timezone
from typing import Any, Sequence

from .quality import Quality

# 允许作为结论值的类型。**不含 None 之外的"空"表达** —— 空串、空列表这类
# 在下游会被当成合法值画出来。
Value = float | int | bool | str | None


def _require_utc(t: datetime, what: str) -> datetime:
    """时刻必须带时区。

    ★裸 datetime 是 T 伪造的头号来源：它在本机看着对，跨时区/跨机就是另一个时刻，
    而且**不报错**。现场还有无 RTC 的设备（e52c），未校时的裸时刻会写进库里永久留着。
    """
    if not isinstance(t, datetime):
        raise TypeError(f"{what} 必须是 datetime，收到 {type(t).__name__}")
    if t.tzinfo is None or t.tzinfo.utcoffset(t) is None:
        raise ValueError(f"{what} 必须带时区（裸 datetime 会静默错位）: {t!r}")
    return t.astimezone(timezone.utc)


@dataclasses.dataclass(frozen=True, slots=True)
class Finding:
    """一条结论 = V + Q + T。四个字段**全部必填**，一个都没有默认值。

    骨架据此写回实时库；`key` 必须是本域 `declare()` 声明过的输出之一，
    未声明的结论会被骨架拒收（模块不碰 id，声明是它与点表之间唯一的桥）。
    """

    key: str
    """本域内稳定的结论名，如 `health_score`。与 `OutputSpec.key` 对应。"""

    value: Value
    """V。质量非 OK 时允许为 `None`（表达"没有值"），见 `__post_init__`。"""

    quality: Quality
    """Q。**真实质量** —— 算不出来就落码，不许悄悄给个数。"""

    t: datetime
    """T。**该结论所依据的数据的时刻**（帧时刻），不是算完的时刻、不是写入的时刻。"""

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError(f"Finding.key 必须是非空字符串，收到 {self.key!r}")
        if not isinstance(self.quality, Quality):
            raise TypeError(
                f"Finding.quality 必须是 Quality 枚举，收到 {type(self.quality).__name__}"
                "（不接受裸字符串/数字：那正是质量码被随手编出来的方式）"
            )
        object.__setattr__(self, "t", _require_utc(self.t, "Finding.t"))

        if self.quality.is_good():
            # 质量说"可信"，就必须真有一个可信的值。
            if self.value is None:
                raise ValueError(
                    f"Finding({self.key}) 质量为 OK 却没有值 —— "
                    "算不出来请落质量码，不要用 OK+None 表达"
                )
            if isinstance(self.value, float) and not math.isfinite(self.value):
                # ★v4 那条教训的正面拦截：NaN/Inf 不是合法结论，更不许被改写成 0.0。
                raise ValueError(
                    f"Finding({self.key}) 质量为 OK 但值是 {self.value!r} —— "
                    "NaN/Inf 不是合法结论；请落 COMPUTE_ERROR 或 INSUFFICIENT_SAMPLES"
                )
        elif isinstance(self.value, float) and not math.isfinite(self.value):
            # 坏质量下也不让 NaN 流下去：下游画图会把它变成断点或 0，两种都在说谎。
            object.__setattr__(self, "value", None)


@dataclasses.dataclass(frozen=True, slots=True)
class Sample:
    """输入侧的一个采样点，同样是完整 VQT。"""

    t: datetime
    value: Value
    quality: Quality
    status_code: int = 0
    """上游原始状态码，**原样留着**。骨架只把它折成好/不好两档给 `quality`，
    要细分（区分"通讯失败"与"设备故障"）的模块自己看这一格。"""

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", _require_utc(self.t, "Sample.t"))


@dataclasses.dataclass(frozen=True, slots=True)
class InputBlob:
    """一份**非测点**输入（图片等），由现场送来。

    ★`t` 是**采集时刻**（拍照时刻），不是上传时刻、不是收到的时刻。巡检 App 拍完可能半小时后
      才有网 —— 拿上传时刻当 T，就是把"半小时前的违规"记成"现在的违规"，而且看不出来。
      所以这里**没有缺省**，且必须带时区。
    """

    t: datetime
    content_type: str
    data: bytes
    source: str = ""
    """来源说明（上传通道 / 巡检任务号…）。骨架只搬运，模块可写进判据摘要。"""

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", _require_utc(self.t, "InputBlob.t"))
        if not isinstance(self.data, (bytes, bytearray)) or not self.data:
            raise ValueError("InputBlob.data 不能为空 —— 空上传不是'一张什么都没有的图'，是没传上来")
        if not self.content_type:
            raise ValueError("InputBlob.content_type 必填（image/jpeg 之类）")


@dataclasses.dataclass(frozen=True, slots=True)
class ArtifactBlob:
    """一个**已经读进内存**的工件，交给模块用。

    ★模块只拿到"字节 + 几个说明"，不知道它存在哪、叫什么文件名 —— 与 `TrainedArtifact`
      是同一条分界的两个方向（交出去 / 拿回来）。
    """

    id: int
    kind: str
    name: str
    blob: bytes
    algo: str = ""
    accuracy: float | None = None
    meta: dict[str, str] = dataclasses.field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.blob:
            # 空工件到不了这里：`TrainedArtifact` 那侧已经挡了。真到了说明文件被截断过。
            raise ValueError(
                f"ArtifactBlob({self.id}) 的字节是空的 —— 工件文件被截断或写坏了，"
                "不许当成一个可用模型交给算法")


@dataclasses.dataclass(frozen=True, slots=True)
class Frame:
    """骨架交给模块的一帧输入。数据已经取好、对齐好，**带真实质量码与真实时刻**。

    模块拿到它就只管算 —— 不知道这些数据来自哪个实时库、哪条连接、哪个 id。
    """

    domain: str
    binding: str
    """这一帧属于哪个绑定（= 被诊断的那个对象）。骨架分配，模块只透传。"""

    t_start: datetime
    t_end: datetime
    """本帧覆盖的数据时间范围。`Finding.t` 通常取 `t_end`，由模块自行决定但必须在本区间内。"""

    channels: dict[str, Sequence[Sample]]
    """按 `InputSpec.role` 索引的采样序列。缺失的可选输入不出现在字典里。"""

    blobs: dict[str, "InputBlob"] = dataclasses.field(default_factory=dict)
    """按 `InputSpec.role` 索引的**非测点输入**（图片等）。测点类输入仍在 `channels` 里。

    事件驱动的帧（来一张图算一次）`t_start == t_end == 拍照时刻`，`channels` 为空。
    """

    artifacts: dict[str, "ArtifactBlob"] = dataclasses.field(default_factory=dict)
    """本对象**当前启用**的工件，按 `kind` 索引（`model` / `baseline` / …）。

    ★模块要用自己训出来的东西，但**模块不碰存储** —— 于是骨架在成帧时把它读好放进来。
      没启用就**没有这一档**（不是给一个空的）：模块据此落 `MODEL_NOT_LOADED`，
      而不是"拿个默认模型顶上"。默认模型顶上去的结论看起来完全正常，这是最坏的一种。

    ★骨架按工件 id 缓存，不每拍重读磁盘；工件一换（有人在界面上点了启用），下一拍就换过来。
    """

    params: dict[str, str] = dataclasses.field(default_factory=dict)
    """被诊断对象的**台账参数**（额定功率等级、支承方式、轴向是哪一轴…），照抄绑定里那份。

    ★**骨架只搬运不解释**：键名与取值由域在 `Declaration.params` 里自述，骨架不枚举、不校验语义。
      模块自己读、自己校验；**读不到就落质量码，不许替它猜一个缺省**
      —— 猜错的 ISO 分级会把"该停机"说成"可长期运行"，而且看不出来。
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "t_start", _require_utc(self.t_start, "Frame.t_start"))
        object.__setattr__(self, "t_end", _require_utc(self.t_end, "Frame.t_end"))
        if self.t_end < self.t_start:
            raise ValueError(f"Frame 时间区间倒挂: {self.t_start} → {self.t_end}")


@dataclasses.dataclass(frozen=True, slots=True)
class InputSpec:
    """模块声明它要什么输入。**只描述语义，不写任何 id** —— 绑定由骨架按平台配置建立。"""

    role: str
    """本域内的角色名，如 `x_acc`。绑定时由人把它对到实际测点上。"""

    unit: str = ""
    """期望单位。**骨架不做单位换算** —— 只作绑定时的核对提示，换算错了比不换更危险。"""

    required: bool = True
    description: str = ""

    kind: str = "point"
    """输入的**形态**：`point`（测点，绑到 globalId、骨架按节拍取）/ `image`（图片，由上传触发）。

    ★两种形态走两条完全不同的路：测点是骨架**去取**，图片是现场**送来**。
      混在一个字段里靠约定区分（比如"roles 里没写就是图片"），骨架就得猜 —— 猜错的样子是
      给图片域起一个轮询线程，每拍拿空帧推理、落一串"没数据"。所以显式声明。
    """

    display: str = ""
    """给人看的名字，如「X 轴速度」。选采集点的表单显示它；空则前端回退显示 `role`（契约 1.8）。"""

    _KINDS = ("point", "image")

    def __post_init__(self) -> None:
        if self.kind not in InputSpec._KINDS:
            raise ValueError(f"InputSpec({self.role}).kind 只能是 {InputSpec._KINDS}，收到 {self.kind!r}")


@dataclasses.dataclass(frozen=True, slots=True)
class OutputSpec:
    """模块声明它产出什么结论。骨架据此在实时库里声明结论点。"""

    key: str
    display: str
    value_type: str
    """`float` | `int` | `bool` | `string`。"""

    unit: str = ""
    description: str = ""

    _ALLOWED = ("float", "int", "bool", "string")

    def __post_init__(self) -> None:
        if self.value_type not in OutputSpec._ALLOWED:
            raise ValueError(
                f"OutputSpec({self.key}).value_type 只能是 {OutputSpec._ALLOWED}，"
                f"收到 {self.value_type!r}"
            )


@dataclasses.dataclass(frozen=True, slots=True)
class ParamSpec:
    """模块声明它要什么**台账参数** —— "每台机器一份"的静态事实，不是测点。

    ★为什么不能当成输入角色绑到点上：额定功率等级、刚性/柔性支承这些既没有时刻也没有质量码，
      往实时库里塞一个恒定值的点，就是把台账伪装成测量。

    ★为什么不能让模块自己读配置文件：那样"新增一个域 = 丢一个 .py"当场不成立，
      而且状态散在文件里没人备份、没人授权（分界文档 §2 第 5 条）。
    """

    key: str
    display: str
    value_type: str
    """`float` | `int` | `bool` | `string` | `enum`。"""

    choices: tuple[str, ...] = ()
    """`enum` 的候选取值。**取值本身**，显示名走 `choice_displays`（同序）。"""

    choice_displays: tuple[str, ...] = ()
    default: str = ""
    """空 = 无缺省，必须由人填。★**不给"看起来合理"的缺省** —— 见下面 `required`。"""

    required: bool = True
    """必填。★缺了它，模块该落坏质量码而不是猜一个值。"""

    unit: str = ""
    description: str = ""

    level: str = ""
    """参数归属（契约 1.8）：`machine` = 设备固有属性，在设备上填一次、各诊断共用；
    `position` = 随这条诊断填。空 = 未声明。★骨架只搬运，**合并由界面在下发前做**，模块只看合并后的 `params`。"""

    _ALLOWED = ("float", "int", "bool", "string", "enum")
    _LEVELS = ("", "machine", "position")

    def __post_init__(self) -> None:
        if self.value_type not in ParamSpec._ALLOWED:
            raise ValueError(
                f"ParamSpec({self.key}).value_type 只能是 {ParamSpec._ALLOWED}，"
                f"收到 {self.value_type!r}")
        if self.level not in ParamSpec._LEVELS:
            raise ValueError(
                f"ParamSpec({self.key}).level 只能是 {ParamSpec._LEVELS}，收到 {self.level!r}")
        if self.value_type == "enum" and not self.choices:
            raise ValueError(f"ParamSpec({self.key}) 是 enum 却没有 choices")
        if self.value_type != "enum" and self.choices:
            raise ValueError(f"ParamSpec({self.key}) 不是 enum 却给了 choices")
        if self.choice_displays and len(self.choice_displays) != len(self.choices):
            raise ValueError(
                f"ParamSpec({self.key}).choice_displays 与 choices 长度不一致 "
                f"({len(self.choice_displays)} vs {len(self.choices)}) —— 同序对应，错位会让界面显示成别的选项")
        if self.default and self.choices and self.default not in self.choices:
            raise ValueError(
                f"ParamSpec({self.key}).default={self.default!r} 不在 choices {self.choices} 里")


@dataclasses.dataclass(frozen=True, slots=True)
class Declaration:
    """`Domain.declare()` 的返回值：这个域要什么、出什么。"""

    inputs: tuple[InputSpec, ...]
    outputs: tuple[OutputSpec, ...]
    params: tuple[ParamSpec, ...] = ()
    """台账参数自述。骨架原样回给 AICloud 渲染绑定表单 —— **不枚举、不解释**。"""

    def __post_init__(self) -> None:
        _reject_dup([i.role for i in self.inputs], "InputSpec.role")
        _reject_dup([o.key for o in self.outputs], "OutputSpec.key")
        _reject_dup([p.key for p in self.params], "ParamSpec.key")
        if not self.outputs:
            raise ValueError("Declaration.outputs 为空 —— 不产出结论的域没有意义")

    def output_keys(self) -> frozenset[str]:
        return frozenset(o.key for o in self.outputs)


def _reject_dup(values: list[str], what: str) -> None:
    seen: set[str] = set()
    for v in values:
        if not v:
            raise ValueError(f"{what} 不能为空")
        if v in seen:
            raise ValueError(f"{what} 重复: {v!r}")
        seen.add(v)


# ═════════════════════ 训练面（2026-09-11）═════════════════════
#
# 分界文档 §3 里 `Domain.train(dataset, report) -> Artifact` 那一行的三个类型。
#
# ★同一条分界：**模块只懂算法**。它拿到的是"已经取好、对齐好、带标签的帧"，
#   交回的是"一坨字节 + 几个说明"。它不知道这些帧从哪个库取的、工件存到哪、
#   谁在看进度 —— 那些都是骨架的事。


@dataclasses.dataclass(frozen=True, slots=True)
class LabeledFrame:
    """一条训练样本：一帧数据 + 人给的标签。"""

    frame: Frame
    label: str
    """★入集那一刻的**快照**，不是标注的当前值 —— 否则"这个模型是用什么训的"没有答案。"""

    sample_id: int = 0
    """工作台里的样本 id。模块用不着，但出错时骨架要能指出是**哪一条**。"""


@dataclasses.dataclass(frozen=True, slots=True)
class Dataset:
    """交给 `Domain.train()` 的训练集。骨架已经按标注把数据取好、成帧。

    ★**取不到数据的样本不会出现在这里**（hs 里那段已被滚存删掉是常态），
      但**丢了多少、丢了哪些，骨架记在任务上**（`skipped`），不静默 ——
      "用 200 条训出来的"和"以为用 200 条、实际只用了 3 条"是两个模型。
    """

    domain: str
    binding: str
    """空 = 这个训练集跨对象（模型全域通用）。"""

    name: str
    items: tuple[LabeledFrame, ...]

    skipped: tuple[tuple[int, str], ...] = ()
    """`(sample_id, 原因)`。取不到数的那些。模块通常不看，但它有权知道自己少拿了多少。"""

    def label_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.label] = out.get(it.label, 0) + 1
        return out

    def __len__(self) -> int:
        return len(self.items)


@dataclasses.dataclass(frozen=True, slots=True)
class TrainedArtifact:
    """`Domain.train()` 交回来的东西。**骨架负责存储与版本，模块不碰文件系统**。"""

    blob: bytes
    """工件本体。模块自己序列化（joblib / json / onnx 都行，骨架不解释一个字节）。"""

    algo: str
    """用了什么算法。会显示在界面上（v5 那套是"决策树 / SVM / 神经网络"）。"""

    kind: str = "model"
    """产出的是哪一类工件：`model`（模型）/ `baseline`（基线）/ …

    ★由**域**说了算，骨架只搬运 —— 低频振动的第 ② 层"训练"出来的是一条基线，不是模型，
      而它走的是同一条训练面（`AI-13 §5`：基线是资产，与模型同级）。
      写死成 `model` 会让基线在界面上顶掉真模型的激活位（两者本该各占各的）。
    """

    accuracy: float | None = None
    """★**没测就给 `None`，不许给 0**。"没测过"与"测出来是 0"在界面上必须分得开。"""

    feature_count: int = 0
    meta: dict[str, str] = dataclasses.field(default_factory=dict)
    """随便挂。骨架原样存进 `artifacts.meta_json`，**只搬运不解释**。"""

    suffix: str = ".bin"
    """落盘时的扩展名。只影响文件名与下载时的 Content-Type。"""

    def __post_init__(self) -> None:
        if not isinstance(self.blob, (bytes, bytearray)) or not self.blob:
            raise ValueError(
                "TrainedArtifact.blob 不能为空 —— 空工件会被存成一个「看着训好了」的模型，"
                "加载时才炸，而那时已经没人记得是这次训练的事")
        if not self.algo:
            raise ValueError("TrainedArtifact.algo 必填：界面要显示「这个模型是怎么训出来的」")
        if self.accuracy is not None:
            a = float(self.accuracy)
            if not math.isfinite(a) or not (0.0 <= a <= 1.0):
                raise ValueError(
                    f"TrainedArtifact.accuracy 必须在 [0,1] 或为 None，收到 {self.accuracy!r}"
                    "（没测过就给 None，别拿 0 或 NaN 顶）")


class ProgressSink:
    """训练进度回传口。模块调它，骨架把进度写进任务，界面才看得见。

    ★**模块可以完全不调它** —— 那样进度就一直是 0，但任务状态照常流转。
      不强制，是因为"为了报进度把算法拆碎"比看不见进度更坏。
    """

    def __init__(self, on_progress=None) -> None:
        self._on = on_progress
        self.canceled = False
        """骨架置位。★长训练**应该**时不时看一眼它 —— 看了就能被叫停，不看就只能等它跑完。"""

    def report(self, progress: float, message: str = "") -> None:
        if self._on is not None:
            self._on(max(0.0, min(1.0, float(progress))), message)


def as_dict(obj: Any) -> dict:
    """给 HTTP/日志用的浅序列化（枚举转值、datetime 转 ISO）。"""
    def _conv(v: Any) -> Any:
        if isinstance(v, Quality):
            return v.value
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, tuple):
            return [_conv(x) for x in v]
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            return as_dict(v)
        return v

    return {f.name: _conv(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
