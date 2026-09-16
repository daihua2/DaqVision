"""训练数据的**来源性质** —— 仿真还是现场（契约 1.6，AICloud `C-36 §4`）。

回答的是一件事，且只有这一件：**这条结论能不能用来说现场设备的话。**

★由来：2026-09-16 训出的第一条基线采自实时库的**仿真信号**（`C-35 §4.4`），
  而"它是仿真的"当时只记在工件名、训练数据说明与标注备注里 —— 三处都是**给人看的字符串**。
  字符串一改名就断，而这恰恰是要挡在人眼前的判据。⇒ `C-36 §4` 请我方立成结构化字段。

★这一格**不是工件的好坏**：`simulated` 不是"差的工件"，跑链路、做回归都该用它。

三条语义（`C-36 §4.2`，与对端逐条议定，改动要发函）：

1. **空 ≠ 现场。** 本格之前产出的老工件读出来是空，界面按「未声明」显示，**不许当现场**。
2. **取最严。** 训练集里混了仿真与现场，工件那一格记 `simulated`
   （与 `origin=imported` 不设"已核实"状态同一条道理：宁可把不确定的往严了标）。
3. **不是封闭枚举。** 按字符串透传，别写死分支 —— 与 `Artifact.kind` / `origin` 同一风格。

★**来源性质记在绑定上**，不记在标注上（2026-09-16 用户裁定）：仿真与否是**点的性质**
  （绑的那组点接的就是仿真信号源），不是人对某一段数据的判断。绑定声明一次，
  标注/样本/工件自动继承 —— 人没有机会漏填某一段。
  ⇒ 样本**入集那一刻打快照**（照 `samples.label` 那条既有规矩）：日后真机接入、
    同一绑定改成 `field`，**不会反写**当初用仿真数据训出来的工件。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 仿真信号（试验台、信号发生器、实时库里的模拟点）。
SIMULATED = "simulated"

#: 真实现场工况。
FIELD = "field"

#: 未声明。★**不是"现场"的同义词** —— 见模块头第 1 条。
UNDECLARED = ""


def normalize(value: str | None) -> str:
    """取值规整：`None` / 空白 → 未声明；其余**原样保留**（不是封闭枚举，不猜、不纠错）。"""
    return (value or "").strip()


def is_field(value: str | None) -> bool:
    """★只有明写 `field` 才算现场。空、未知取值一律不是 —— 模块头第 1 条。"""
    return normalize(value) == FIELD


def effective(snapshot: str | None, binding_now: str | None) -> str:
    """样本入集时的快照 + 绑定**当前**取值 → 这条样本实际该算哪一档。

    ★**兜底只朝严的方向走**，这是本函数存在的全部理由：

    - 快照有值 ⇒ 用快照，绑定现在说什么都不算数（否则真机接入后改一次绑定，
      当初用仿真数据训出来的工件就被洗成了现场 —— 正是快照要挡的那件事）。
    - 快照为空（1.6 之前入集的样本）且绑定现在说 `simulated` ⇒ 按 `simulated` 计。
    - 快照为空而绑定说 `field` 或没说 ⇒ **仍是未声明**。

    ⇒ 这条兜底**不可能**产出一个假的 `field`，只可能把"不知道"抬成"仿真"。
    """
    snap = normalize(snapshot)
    if snap:
        return snap
    return SIMULATED if normalize(binding_now) == SIMULATED else UNDECLARED


def fold(values: Iterable[str | None]) -> str:
    """把一批样本的来源性质折成工件那一格。**取最严**（模块头第 2 条）。

    判序，逐条都有理由：

    1. 任一 `simulated` ⇒ `simulated`。哪怕只混进一段仿真，这个工件就不能用来说现场的话。
    2. 全部 `field` ⇒ `field`。
    3. 非空取值只有一种且无空 ⇒ 原样透传那一种（新取值随时加，骨架不认得也不该吞掉）。
    4. 其余（混杂、或含未声明）⇒ 未声明。★**不退回 `field`** —— 不知道就说不知道。

    空输入（一条样本都没有）返回未声明：没有样本就没有依据。
    """
    seen = {normalize(v) for v in values}
    if not seen:
        return UNDECLARED
    if SIMULATED in seen:
        return SIMULATED
    if seen == {FIELD}:
        return FIELD
    if len(seen) == 1 and UNDECLARED not in seen:
        return next(iter(seen))
    return UNDECLARED
