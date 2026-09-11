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

    _ALLOWED = ("float", "int", "bool", "string", "enum")

    def __post_init__(self) -> None:
        if self.value_type not in ParamSpec._ALLOWED:
            raise ValueError(
                f"ParamSpec({self.key}).value_type 只能是 {ParamSpec._ALLOWED}，"
                f"收到 {self.value_type!r}")
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
