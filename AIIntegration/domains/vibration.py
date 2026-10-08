"""低频采集AI振动诊断 —— 一个振动传感器上的「经典算法（国标判级）」与「AI 自训（基线偏离）」，可选其一或两者都用。

> 2026-10-05 用户定：模块 1（`vibration_iso`）与模块 2（`vibration_baseline`）**合并为本模块**，
> 界面只放一个选项「低频采集AI振动诊断」（用户 10-05 定名；高频那一路即模块 3「高频采集AI振动诊断」）。理由：两种算法的结论要合成一个检测状态，彼此不一致时要能解释，
> 采基线时要先用国标查一遍所选时段 —— 这些**跨算法的判断**在一个模块里一次做完，不靠平台去拼。
> 现役两条旧绑定的数据是仿真（AICloud `C-66 §3.3`），换新 key 造成的历史断档没有损失。
> 两种算法各自的判据与取舍沿用原两模块（定案「模块 1、2 的内容」、契约 1.13），本文件只记合并带来的新东西。

依据与定案：`AIIntegration/doc/诊断配置流程定案.md`。

---

## 0.1 做什么

| 部分 | 结论 | 依据 |
| --- | --- | --- |
| 经典算法 | 速度最大值、最大值所在轴、烈度区（文字 / 数值）、距下一档余量、轴向/径向比、方向性提示 | 工业机器 GB/T 6075.3、泵 GB/T 6075.7；方向性是经验判据，只作提示 |
| AI 自训 | 速度 / 加速度 / 位移 / 频率偏离、三轴比例漂移、温升、异常分 | 与这台设备自己采的基线比（中位数 + 四分位距） |
| 故障分类 | 故障类别、票数占比 | 13 标量随机森林（§0.7），**只报、不推检测状态** |
| 共用 | **检测状态**、运行状态、判据摘要 | 见 §0.3 |

输入按原 v5 补齐为 13 标量（2026-10-08 用户定）：温度 + 三轴 ×（速度、加速度、位移、频率）。
★经典判级**只看速度** —— 国标限值本就是按速度有效值定的，别的量套不上那张表。

## 0.2 启用哪种算法：参数 `algorithms`，必填、无缺省

`classic` 经典 / `baseline` 自训 / `both` 两者。**没启用的那一半结论不写**（点上保持空），
判据摘要注明「未启用」；本绑定要不要基线也随之而定（`required_artifacts`，契约 1.14 `Binding.requires_artifacts`）。
没填或非法 ⇒ 整组落 `CONFIG_INCOMPLETE` —— 「没填」与「不想用」分得开，不猜。

## 0.3 检测状态怎么合

| 部分 | 会出的档 |
| --- | --- |
| 经典 | A、B → 正常；C → 警告；D → 危险（★B 区即「可长期运行」，不对「注意」） |
| 自训 | 速度、加速度、位移任一偏离 ≥ 3 → 注意，否则正常（★不出警告、危险：本部分门槛无标定依据，不该把设备说成危险；★频率偏离只报不推，主频在几个谱峰间跳是常态，10-08 用户定） |
| 故障分类 | **不参与**（10-08 用户定：现场没有真实故障标签，分类准不准没有依据） |

合成规则（`_combine_status`）：
1. 停机 ⇒ 停机；
2. 已启用的部分里**算得出的取最高档**；只要高于「正常」就照报 —— 另一部分算不出不能把它压下去；
3. ★**自训算不出（没采基线、基线读不懂、本帧与基线不可比）⇒ 按经典判级出状态**，好质量，判据摘要写明
   「只按经典判」；采到基线后自动回到第 2 条。只启用自训的绑定同样借经典判级，但要求判级参数齐全
   （借来的只是状态，经典那几个结论点仍不写）。
   由来：实时库对**任何**坏码都落 BAD 报警（严重度 500、不去抖，`H-282 §2.1`）；按原第 3 条「没基线就落坏码」，
   采到基线之前会一直挂着一条报警。经典算出来的是真结论、只是少了自训那一半，且已写明 ——
   这不是写一个假的「正常」（2026-10-06 hs 用户定、AICloud `C-67 §4` 同意、我方用户同日接受）；
4. 经典也算不出（判级参数不全、没取到数）⇒ 落经典的坏码，**不报正常** —— 这时确实判不了，挂报警是对的。

两部分**不一致**时判据摘要写明怎么读（`_disagreement`）：国标偏大而相对自身没变 ⇒ 振动可能长期偏高、
基线或采于异常状态、或判级参数需核对；国标正常而偏离自身常态 ⇒ 早期变化。

## 0.4 采基线先用国标查一遍（`train`）

判级参数齐全时，所选时段里**有任何一帧按国标落在 C / D 区就拒采**并说清：用偏大的那段作基线，
等于把异常当常态，之后再大也显得「没变」。判级参数不全时不查，并在工件元数据里写明「未做国标核查」。

## 0.5 不防抖

每拍如实写。去抖交给实时库报警状态机（`OnDelaySec`），历史里留真实的每一拍（`AI-74 §4`）。

## 0.6 纪律：缺什么落什么码，绝不猜缺省

参数没填、口径没确认、没有基线、样本全是坏值 —— 每一种都对应一条特定的坏质量结论，
而不是"给个看起来合理的数"。**猜错的烈度分级会把"该停机"说成"可长期运行"，而且从数值上看不出来。**

## 0.7 故障分类（13 标量 → 人标的类别）

> 2026-10-08 用户定：恢复原 v5 的 13 标量分类，**撤销定案 2.3「先不做」**；分类器手写、零第三方依赖
> （守定案 2.4：本域一旦声明 `REQUIRES`，现场 venv 缺包就整域不铺，现役判级跟着掉）。

- 参数 `fault_classify`：`true` 启用；**留空或 `false` 不启用**（`blank_meaning=not_evaluated`，老绑定不受影响）。
- 训练走同一条训练面，按 `Dataset.algo` 分派：空 / `baseline` 采基线（平台现役就传空），`classifier` 训分类器，
  其余拒绝并说清。分类器是**另一类工件**（`kind="classifier"`），与基线各占各的启用位。
- 算法对齐 v5 现场实际在跑的那个（`RandomForest fallback`：类权重均衡、深度 6），但补上 v5 缺的三件
  （research/lowfreq-layer3 §3）：**按样本分层留出验证集**（同一条样本切出的帧高度相关，按帧分会泄漏）、
  每类精确率/召回率与混淆矩阵、每类最小样本量门槛 —— 不够就训练失败并说清，不训一个「永远答多数类」的模型。
  `accuracy` 是**留出集**上的，不是训练集自测（v5 那 71 个 1.0 就是这么来的）。
- 结论「故障类别」「票数占比」**只报，不推检测状态**；票数占比是各树叶子分布的平均，★不是概率。
"""

from __future__ import annotations

import bisect
import json
import math
import random
from datetime import datetime

# ★域模块 import 骨架一律用**绝对包名**：装载器按文件路径 exec，相对 import 会当场炸。
from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    ROLE_STATUS, STATUS_ATTENTION, STATUS_DANGER, STATUS_LEVELS, STATUS_NORMAL, STATUS_STOPPED,
    STATUS_WARNING, STOP_LITERAL, STOP_NOT_WRITTEN, Dataset, Declaration, Finding, Frame,
    InputSpec, LabeledFrame, OutputSpec, ParamSpec, ProgressSink, TrainedArtifact,
)

# ─────────────────────────────── 限值表 ───────────────────────────────
#
# 速度有效值 mm/s。三个边界依次是 A/B、B/C、C/D。
#   A 新投运 │ B 可长期运行 │ C 不宜长期连续运行 │ D 足以造成损坏
# ★这两张表是**判据本身**，不是可调参数：动它等于改国标结论。

#: GB/T 6075.3-2011（等同 ISO 10816-3:2009）工业机器，(机器分组, 支承方式) → 边界。
#: 数值与 ISO 20816-3:2022 表 A.1、A.2 相同（2026-09-17 核对原文）。
#: ★键的取值逐字照平台台账（`C-64 §2.3`）：平台原样拷贝、不换算。
_MACHINE_LIMITS: dict[tuple[str, str], tuple[float, float, float]] = {
    ("group1", "rigid"):    (2.3, 4.5, 7.1),
    ("group1", "flexible"): (3.5, 7.1, 11.0),
    ("group2", "rigid"):    (1.4, 2.8, 4.5),
    ("group2", "flexible"): (2.3, 4.5, 7.1),
}

#: GB/T 6075.7-2015（等同 ISO 10816-7:2009）旋转动力泵，(泵类别, 功率档) → 边界。
#: ★出处：AICloud `C-43 §4` 转引 Europump《Guidelines on Pump Vibration》（2013）对 ISO 10816-7 的摘录，
#:   **标准原文尚未取得、未核对**（用户 2026-09-18 定：先按此实现）。取得原文后须逐值核对。
_PUMP_LIMITS: dict[tuple[str, str], tuple[float, float, float]] = {
    ("category1", "le200"): (2.5, 4.0, 6.6),
    ("category1", "gt200"): (3.5, 5.0, 7.6),
    ("category2", "le200"): (3.2, 5.1, 8.5),
    ("category2", "gt200"): (4.2, 6.1, 9.5),
}
PUMP_POWER_SPLIT_KW = 200.0

_GROUPS = ("group1", "group2")
_PUMPS = ("category1", "category2")
_NA = "notApplicable"

#: 平台台账的键（`C-64 §2.3`）。
P_GROUP, P_SUPPORT, P_PUMP = "machineGroup", "supportClass", "pumpCategory"
P_POWER, P_SPEED, P_AXIAL = "ratedPowerKw", "ratedSpeedRpm", "axialAxis"
P_ALGOS = "algorithms"
_MACHINE_STD = "GB/T 6075.3-2011"
_PUMP_STD = "GB/T 6075.7-2015"

#: 启用哪种算法（§0.2）。
ALGO_CLASSIC, ALGO_BASELINE, ALGO_BOTH = "classic", "baseline", "both"

#: 低速阈值（r/min）。工业机器低于它时，标准要求评价频带改为 2~1000 Hz 且应另看位移。
LOW_SPEED_RPM = 600.0

#: 运行状态的三个取值。
RUNNING, STOPPED, UNJUDGED = "运行", "停机", "未判"

#: 烈度区 → 检测状态（用户 2026-10-05 同意）。★B 区不对「注意」：国标 B 区即「可长期运行」。
_ZONE_STATUS = {"A": STATUS_NORMAL, "B": STATUS_NORMAL, "C": STATUS_WARNING, "D": STATUS_DANGER}

#: 档位高低（停机不参与比较）。
_RANK = {STATUS_NORMAL: 0, STATUS_ATTENTION: 1, STATUS_WARNING: 2, STATUS_DANGER: 3}
_LEVEL_TEXT = {STATUS_NORMAL: "正常", STATUS_ATTENTION: "注意", STATUS_WARNING: "警告", STATUS_DANGER: "危险"}

_AXES = ("x", "y", "z")
TEMP_ROLE = "temp"

#: 四个振动量（角色名后缀）。单位、显示分辨率取自有人 USR-SVT10-01B 手册（doc/传感器资料）。
VEL, ACC, DISP, FREQ = "vel", "acc", "disp", "freq"
#: 量 → (单位, 物理量 quantity, 显示名)。★`frequency` 不在 `H-261 §2.1` 首批五个里，已提请 hs 收录。
_QTY: dict[str, tuple[str, str, str]] = {
    VEL: ("mm/s", "velocity", "速度"),
    ACC: ("g", "acceleration", "加速度"),
    DISP: ("μm", "displacement", "位移"),
    FREQ: ("Hz", "frequency", "频率"),
}
#: 幅值类：窗口内取最大值（与速度同口径）；其余（频率、温度）取窗口均值。
_PEAK_QTYS = (VEL, ACC, DISP)
#: 偏离能把检测状态推到「注意」的量。★频率不在内（用户 10-08 定）：主频在几个谱峰之间跳是常态。
_STATUS_QTYS = (VEL, ACC, DISP)
#: 加速度、位移、频率偏离的结论键。速度偏离仍是 `vel_z_max`。
_Z_KEY = {ACC: "acc_z_max", DISP: "disp_z_max", FREQ: "freq_z_max"}
_STATUS_Z_KEYS = ("vel_z_max",) + tuple(_Z_KEY[q] for q in _STATUS_QTYS if q != VEL)


def _role(axis: str, qty: str) -> str:
    return f"{axis}_{qty}"


def _qty_of(role: str) -> str:
    """角色 → 量；温度回空串。"""
    return role.split("_", 1)[1] if "_" in role else ""


#: 13 标量，顺序照原 v5 `FEATURE_SCHEMA`（`single_frame_models.py:23`）。
FEATURE_ROLES = (TEMP_ROLE,) + tuple(_role(a, q) for a in _AXES for q in (ACC, FREQ, DISP, VEL))

#: 方向性判据阈值。**经验值**，不是国标。
#: ★出处要分清：判据**方向**（轴向偏高→不对中、径向主导→不平衡、三轴均衡→松动）出自改造方案 §3 轨A④
#:   现象/倾向表；那张表只有「显著」「异常升高」等定性说法，**这两个数是 2026-09-11 落码时我方取的，
#:   没有文献或实测标定**。★常被引的「单测点 0.767」**不是这条规则的成绩**：那是 MAFAULDA 上训练出来的
#:   12 维标量小网络判「垂直 vs 水平不对中」（research/scalar-capability §5），只说明单测点标量有判别力但有限；
#:   本规则本身从未上数据验过。
_AXIAL_SIGNIFICANT = 0.5   # 轴向 / 径向 ≥ 此值 ⇒ 轴向占比异常
_RADIAL_BALANCED = 0.8     # 径向两轴互比落在 [0.8, 1/0.8] ⇒ 视为各向同性

#: 基线格式。**存进工件里**：格式一变，老工件要能被认出来而不是被误读。
#: ★沿用 `vibration_baseline` 的格式号：内容一字未变，合并前采的基线照样能读。
BASELINE_FORMAT = "vibration_baseline/baseline@1"

#: 采基线至少要几帧（定案 2.2）。
MIN_BASELINE_FRAMES = 5

#: 稳健尺度的下限（mm/s，温度同用）。传感器分辨率决定它不可能真是 0；
#: ★不设下限的后果是除零或天文数字的 z 分数，后者更坏，因为它看着像个结论。
MIN_SCALE = 0.01
#: 加速度、位移、频率各按自己的**显示分辨率**设下限（SVT10-01B：0.01 g / 1 μm / 1 Hz）。
#: ★不能套速度的 0.01：位移分辨率 1 μm，四分位距为 0 时差 1 μm 就成了 100 个尺度单位。
_MIN_SCALE_BY_QTY = {ACC: 0.01, DISP: 1.0, FREQ: 1.0}

#: 四分位距折算成与标准差同尺度的系数（正态分布下 IQR ≈ 1.349σ）。
_IQR_TO_SIGMA = 1.349

#: 认为"偏离显著"的 z 分数。经验值，用于检测状态（注意）、异常分与摘要措辞，**不是国标**。
_Z_NOTABLE = 3.0

DEFAULT_NORMAL_LABEL = "正常"

#: 两部分各自的结论键。没启用的那一半不写。
CLASSIC_KEYS = ("vel_max", "dominant_axis", "iso_zone", "iso_zone_code", "iso_margin",
                "axial_ratio", "direction_hint")
BASELINE_KEYS = ("vel_z_max", "acc_z_max", "disp_z_max", "freq_z_max",
                 "ratio_drift", "temp_rise", "anomaly_score")
CLASSIFY_KEYS = ("fault_class", "fault_vote")

#: 启用故障分类的参数（§0.7）。
P_CLASSIFY = "fault_classify"

#: 训练时 `Dataset.algo` 认的值（§0.7）。★空串 = 采基线：平台现役采基线就传空（现场 train_jobs 实查）。
TRAIN_BASELINE, TRAIN_CLASSIFIER = "baseline", "classifier"
#: ★兼容：契约 1.17 之前 `algo` 从没交到域手里，调用方随手填过中文 —— 现场 09-15 有一条「基线统计」、
#:   本仓用例用「基线」。这两个照旧按采基线走；其余不认识的拒绝，不猜。
_BASELINE_ALIASES = ("", TRAIN_BASELINE, "基线", "基线统计")


def _vel_role(axis: str) -> str:
    return _role(axis, VEL)


class Vibration(Domain):
    """振动诊断（经典 / 自训，可选其一或两者）。"""

    key = "vibration"
    display = "低频采集AI振动诊断"
    version = "1.2.0"

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
        # 10-08 按原 v5 补齐：加速度、位移、频率。都可选；经典判级不用它们（§0.1）。
        _uses = {ACC: "AI 自训（偏离 ≥3 推「注意」）与故障分类用",
                 DISP: "AI 自训（偏离 ≥3 推「注意」）与故障分类用",
                 FREQ: "AI 自训（偏离只报、不推状态）与故障分类用"}
        for qty in (ACC, DISP, FREQ):
            unit, quantity, name = _QTY[qty]
            inputs += [InputSpec(
                role=_role(axis, qty), unit=unit, required=False,
                quantity=quantity, axis=axis, record="point1",
                display=f"{axis.upper()} 轴{name}", description=f"{_uses[qty]}，可不选")
                for axis in _AXES]
        inputs.append(InputSpec(
            role=TEMP_ROLE, unit="℃", required=False, quantity="temperature",
            display="温度", description="AI 自训与故障分类用：有就算温升，没有就不给温升"))
        return Declaration(
            inputs=tuple(inputs),
            # ★「可能要」：启用了 AI 自训才要基线、启用了故障分类才要分类器，按绑定算的见 `required_artifacts`。
            requires_artifacts=("baseline", "classifier"),
            params=(
                ParamSpec(
                    key=P_ALGOS, display="启用算法", value_type="enum",
                    choices=(ALGO_CLASSIC, ALGO_BASELINE, ALGO_BOTH),
                    choice_displays=("经典算法（国标判级）", "AI 自训（基线偏离）", "两者都用"),
                    required=True, level="position",
                    description="没有缺省。只启用经典即保存即出结论；启用 AI 自训须先采基线"),
                ParamSpec(
                    key=P_GROUP, display="机器分组（GB/T 6075.3）", value_type="enum",
                    choices=(*_GROUPS, _NA),
                    choice_displays=(
                        "第 1 组：大型机器，额定功率 >300 kW；电动机轴中心高 H≥315 mm",
                        "第 2 组：中型机器，额定功率 >15 kW 且 ≤300 kW；电动机轴中心高 160≤H<315 mm",
                        "不适用：不在 GB/T 6075.3 范围内",
                    ),
                    required=False, level="machine",
                    description="经典算法、工业机器必填（泵不填）。决定 A/B/C/D 边界值，没有缺省：选错会把该停机说成可长期运行。"
                                "选「不适用」则不出烈度分级"),
                ParamSpec(
                    key=P_SUPPORT, display="支承方式", value_type="enum",
                    choices=("rigid", "flexible"), choice_displays=("刚性", "柔性"),
                    required=False, level="machine",
                    description="经典算法、工业机器必填（泵不看支承）。机器与支承系统在测量方向上的最低固有频率比转频高 25% 以上为刚性，否则柔性"),
                ParamSpec(
                    key=P_PUMP, display="泵类别（GB/T 6075.7）", value_type="enum",
                    choices=(*_PUMPS, _NA),
                    choice_displays=(
                        "第Ⅰ类：对可靠性、可用性或安全性要求高的泵",
                        "第Ⅱ类：一般用途的泵",
                        "不适用：不是泵，或不在 GB/T 6075.7 范围内",
                    ),
                    required=False, level="machine",
                    description="经典算法、旋转动力泵必填（工业机器不填）。选第Ⅰ/Ⅱ类即按泵判级"),
                ParamSpec(
                    key=P_POWER, display="额定功率", value_type="float", unit="kW",
                    required=False, level="machine",
                    description="泵必填：按 200 kW 分两档取限值"),
                ParamSpec(
                    key=P_SPEED, display="额定转速", value_type="float", unit="r/min",
                    required=False, level="machine",
                    description="工业机器低于 600 r/min 时照常出分级，但判据摘要注明结果仅供参考（标准要求另看位移）"),
                ParamSpec(
                    key="vel_is_rms", display="速度口径确认为有效值", value_type="enum",
                    choices=("true", "false"),
                    choice_displays=("是，已确认为有效值", "否 / 未确认（峰值或手册未注明）"),
                    required=True, level="position",
                    description="烈度判级要求速度有效值。选「否」时不出烈度分级，只给数值"),
                ParamSpec(
                    key=P_AXIAL, display="轴向是哪一轴", value_type="enum",
                    choices=("x", "y", "z"), choice_displays=("X 轴", "Y 轴", "Z 轴"),
                    required=True, level="sensor",
                    description="沿转轴方向的那一轴，在振动传感器上填。没有缺省：猜错会把不对中说成不平衡。"
                                "缺它只影响方向性与比例漂移；★改了它，已采基线的比例那一项作废，须重采"),
                ParamSpec(
                    key="normal_label", display="采基线时认哪个标签算正常",
                    value_type="string", default=DEFAULT_NORMAL_LABEL, has_default=True,
                    required=False, level="position",
                    description="AI 自训用。采基线只用被标成这个标签的样本。允许有缺省：猜错会当场可见"
                                "（一条样本都匹配不上，采基线直接失败并说清）"),
                ParamSpec(
                    key="stop_threshold", display="停机门槛", value_type="float", unit="mm/s",
                    required=False, level="position",
                    # ★has_default=False（缺省即是）：界面**不要替它预置任何值** ——
                    #   替它填一个"看起来合理"的数，会在某些设备上把运行判成停机 ⇒ 静默停止诊断。
                    blank_meaning="not_evaluated", min="0",
                    description="速度最大值低于它即判停机：停机时不判烈度与方向、不出偏离与异常分，采基线时剔除停机帧。"
                                "不填不判；没有缺省，按设备自己定"),
                ParamSpec(
                    key=P_CLASSIFY, display="启用故障分类", value_type="enum",
                    choices=("true", "false"), choice_displays=("启用", "不启用"),
                    required=False, level="position", blank_meaning="not_evaluated",
                    description="13 标量随机森林，按人标的类别（如不平衡 / 不对中 / 松动）给出最像哪一类。"
                                "须先用多类标注样本训练分类器并启用。结果只作参考、不影响检测状态。留空即不启用"),
            ),
            outputs=(
                OutputSpec(key="status", display="检测状态", value_type="string",
                           role=ROLE_STATUS, stop_behavior=STOP_LITERAL,
                           description="经典：A、B → 正常，C → 警告，D → 危险；AI 自训：速度、加速度、位移任一偏离 ≥ 3 → 注意，否则正常"
                                       "（频率偏离与故障分类不参与）；"
                                       "两者都用取较高档。停机 → 停机。自训算不出（如未采基线）时按经典判、判据摘要注明；"
                                       "经典也算不出时落坏质量码，不报正常",
                           choices=(STATUS_NORMAL, STATUS_ATTENTION, STATUS_WARNING, STATUS_DANGER,
                                    STATUS_STOPPED),
                           choice_displays=("正常", "注意", "警告", "危险", STOPPED)),
                OutputSpec(key="vel_max", display="速度最大值", value_type="float", unit="mm/s",
                           headline=True, description="经典。窗口内三轴速度的最大值"),
                OutputSpec(key="dominant_axis", display="最大值所在轴", value_type="string",
                           description="经典。x / y / z"),
                OutputSpec(key="iso_zone", display="烈度区", value_type="string", headline=True,
                           description="经典。A 新投运 / B 可长期运行 / C 不宜长期连续运行 / D 足以造成损坏；停机时为「停机」",
                           stop_behavior=STOP_LITERAL,
                           choices=("A", "B", "C", "D", STOPPED),
                           choice_displays=("A 新投运", "B 可长期运行", "C 不宜长期连续运行",
                                            "D 足以造成损坏", STOPPED)),
                OutputSpec(key="iso_zone_code", display="烈度区(数值)", value_type="int",
                           description="经典。1=A 2=B 3=C 4=D，0=停机，给趋势曲线与报警门限用",
                           stop_behavior=STOP_LITERAL,
                           choices=("0", "1", "2", "3", "4"),
                           choice_displays=(STOPPED, "A", "B", "C", "D")),
                OutputSpec(key="iso_margin", display="距下一档余量", value_type="float", unit="mm/s",
                           description="经典。离更差一档的边界还有多远；已在 D 区时为负",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="axial_ratio", display="轴向/径向比", value_type="float",
                           description="经典。轴向 ÷ 径向两轴较大者",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="direction_hint", display="方向性提示", value_type="string",
                           description="经典。★提示，不是结论：无频谱数据，仅凭三轴比例判断倾向；停机时为「停机」",
                           stop_behavior=STOP_LITERAL,
                           choices=(STOPPED,)),
                OutputSpec(key="vel_z_max", display="速度偏离", value_type="float",
                           description="AI 自训。各速度通道 (当前−基线中位数)/(四分位距/1.349) 的最大值。≥3 视为显著偏离",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="acc_z_max", display="加速度偏离", value_type="float",
                           description="AI 自训。各加速度通道偏离的最大值，算法同速度偏离。≥3 推「注意」；没选加速度则不给",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="disp_z_max", display="位移偏离", value_type="float",
                           description="AI 自训。各位移通道偏离的最大值，算法同速度偏离。≥3 推「注意」；没选位移则不给",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="freq_z_max", display="频率偏离", value_type="float",
                           description="AI 自训。各频率通道偏离里绝对值最大的那个，带符号（负 = 比基线低）。"
                                       "只报、不推检测状态；没选频率则不给",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="ratio_drift", display="三轴比例漂移", value_type="float",
                           description="AI 自训。轴向/径向比相对基线的变化量",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="temp_rise", display="温升", value_type="float", unit="℃",
                           description="AI 自训。相对基线的温度变化",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="anomaly_score", display="异常分", value_type="float", headline=True,
                           description="AI 自训。0~100，由速度/加速度/位移偏离、比例漂移、温升合成。★不是概率，是排序用的分数",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="fault_class", display="故障类别", value_type="string",
                           description="故障分类。分类器判为最像的那一类（训练时人标的标签原文）。只作参考、不影响检测状态",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="fault_vote", display="票数占比", value_type="float",
                           description="故障分类。判出那一类在各棵树里的平均占比，0~1。★不是概率，也不是准确率",
                           stop_behavior=STOP_NOT_WRITTEN),
                OutputSpec(key="run_state", display="运行状态", value_type="string",
                           description="运行 / 停机 / 未判（未填停机门槛）。停机时数值类结论不更新，界面据此置灰",
                           choices=(RUNNING, STOPPED, UNJUDGED)),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="用了哪几路、判到哪一档、两种算法是否一致、为什么没给"),
            ),
        )

    def required_artifacts(self, params: dict[str, str]) -> tuple[str, ...]:
        """按绑定参数算：启用了 AI 自训才要基线、启用了故障分类才要分类器（契约 1.14 `Binding.requires_artifacts`）。
        `algorithms` 没填或非法、`fault_classify` 非法时按「可能要」回 —— 不替没填对的参数下结论说「不需要」。"""
        algos = _algos(params)
        need: list[str] = []
        if algos is None or ALGO_BASELINE in algos:
            need.append("baseline")
        if _classify_on(params) is not False:
            need.append("classifier")
        return tuple(need)

    # ── 训练 ──────────────────────────────────────────────────────────────
    def train(self, dataset: Dataset, report: ProgressSink) -> TrainedArtifact:
        """按 `dataset.algo` 分派（§0.7）：空 / `baseline` 采基线，`classifier` 训故障分类器。"""
        algo = (dataset.algo or "").strip().lower()
        if algo in _BASELINE_ALIASES:
            return self._train_baseline(dataset, report)
        if algo == TRAIN_CLASSIFIER:
            return _train_classifier(dataset, report)
        raise ValueError(f"不认识的训练算法 {dataset.algo!r}：本模块只认 {TRAIN_BASELINE!r}（采基线，留空同此）"
                         f"与 {TRAIN_CLASSIFIER!r}（故障分类器），不猜")

    def _train_baseline(self, dataset: Dataset, report: ProgressSink) -> TrainedArtifact:
        """采一条基线：这台设备正常运行时各通道的中位数与四分位距，以及三轴比例。

        ★判级参数齐全时先用国标查所选时段：有一帧落 C / D 区就拒采（§0.4）。
        """
        wanted = DEFAULT_NORMAL_LABEL
        params: dict[str, str] = {}
        for it in dataset.items:            # 参数在帧上，各帧同一条诊断，取第一个
            params = it.frame.params
            wanted = params.get("normal_label", "").strip() or DEFAULT_NORMAL_LABEL
            break
        axial = (params.get(P_AXIAL, "") or "").strip().lower()

        algos = _algos(params)
        if algos is not None and ALGO_BASELINE not in algos:
            raise ValueError(f"本诊断只启用了经典算法（{P_ALGOS}={params.get(P_ALGOS)!r}），不需要也不采基线")

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

        # ★国标核查：用偏大的那段作基线，之后再大也显得「没变」。
        limits, iso_bad, basis, _is_pump = _grading(params)
        if iso_bad:
            iso_check = f"未做国标核查：{iso_bad}"
        else:
            hot = []
            for it in normals:
                peak = max((p for p in (_window_peak(it.frame, _vel_role(a)) for a in _AXES)
                            if p is not None), default=None)
                if peak is not None and _classify(peak, limits)[0] in ("C", "D"):  # type: ignore[arg-type]
                    hot.append((it, peak))
            if hot:
                it_hot, v_hot = max(hot, key=lambda h: h[1])
                raise ValueError(
                    f"所选时段有 {len(hot)} 帧按 {basis} 已在 C / D 区（最大 {v_hot:.3f} mm/s，"
                    f"{_where_in_segment(it_hot, normals)}）—— 用它作基线会把异常当常态，之后再大也显得「没变」。"
                    "请另选一段国标判为 A / B 区的正常运行时段")
            iso_check = f"已按 {basis} 核查，所选 {len(normals)} 帧均在 A / B 区"

        report.report(0.2, f"用 {len(normals)} 帧 {wanted!r} 样本采基线"
                           + (f"（剔除停机帧 {stopped}）" if stopped else "") + f"；{iso_check}")

        channels: dict[str, dict[str, float]] = {}
        for role in FEATURE_ROLES:          # 13 路有数的都记（10-08 补齐）；没选的自然采不到
            vals = []
            for it in normals:
                v = _feature(it.frame, role)
                if v is not None:
                    vals.append(v)
            if len(vals) >= MIN_BASELINE_FRAMES:
                med, iqr = _median_iqr(vals)
                channels[role] = {"median": med, "iqr": iqr, "n": len(vals)}

        if not any(k.endswith("_vel") for k in channels):
            raise ValueError("一路速度都没能采到足够样本 —— 检查所选采集点与这段时间实时库里有没有数据")

        report.report(0.8, "统计完成")

        # 三轴比例进基线：不对中的抓手是"比例变了"，不是"值变大了"。
        # ★格式沿用 `{"1": 比值}`（BASELINE_FORMAT 不变）。
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
            "iso_check": iso_check,
            "channels": channels,
        }
        blob = json.dumps(model, ensure_ascii=False, indent=1).encode("utf-8")
        return TrainedArtifact(
            blob=blob, algo="基线统计（中位数/四分位距）", kind="baseline", suffix=".json",
            # ★基线没有"准确率"这回事 —— 给 None，别拿 1.0 顶（界面会显示成 100%）。
            accuracy=None, feature_count=len(channels),
            meta={"format": BASELINE_FORMAT, "normal_label": wanted,
                  "frames": str(len(normals)), "stopped_excluded": str(stopped),
                  "t_from": model["t_from"], "t_to": model["t_to"], "iso_check": iso_check})

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        algos = _algos(frame.params)
        if algos is None:
            raw = frame.params.get(P_ALGOS, "")
            return _bad_all(self, frame, None, Quality.CONFIG_INCOMPLETE,
                            f"未出结论：启用算法 {P_ALGOS}={raw or '未填'} 非法 —— "
                            f"须为 {ALGO_CLASSIC} / {ALGO_BASELINE} / {ALGO_BOTH}，不猜")
        classic, baseline_on = ALGO_CLASSIC in algos, ALGO_BASELINE in algos

        peaks, times, bad, empty = _peaks(frame)

        # ① 一路好样本都没有 —— 区分"没数据"与"有数据但全是坏值"，两者处置相反。
        if not peaks:
            q = Quality.INPUT_BAD if bad else Quality.NO_INPUT
            why = (f"{'/'.join(bad)} 有样本但质量码全不可信"
                   if bad else f"{'/'.join(empty) or '所有已选通道'} 在本窗口内没有样本")
            return _bad_all(self, frame, algos, q, f"未出结论：{why}")

        dominant = max(peaks, key=lambda k: peaks[k])
        vel_max = peaks[dominant]
        t = times[dominant]                    # ★T 取那笔样本自己的时刻

        out: list[Finding] = []
        if classic:
            out += [Finding(key="vel_max", value=round(vel_max, 4), quality=Quality.OK, t=t),
                    Finding(key="dominant_axis", value=dominant, quality=Quality.OK, t=t)]

        # 运行状态。停机 ⇒ 不判烈度与方向、不比基线；数值类这一拍不写（定案 1.6 / 2.5）。
        state, state_q, state_note = run_state(frame.params, vel_max)
        out.append(Finding(key="run_state", value=state, quality=state_q, t=t))
        if state == STOPPED:
            out.append(Finding(key="status", value=STATUS_STOPPED, quality=Quality.OK, t=t))
            if classic:
                out += [Finding(key="iso_zone", value=STOPPED, quality=Quality.OK, t=t),
                        Finding(key="iso_zone_code", value=0, quality=Quality.OK, t=t),
                        Finding(key="direction_hint", value=STOPPED, quality=Quality.OK, t=t)]
            out.append(Finding(key="evidence", value=f"{state_note}；不判烈度与方向、不比基线",
                               quality=Quality.OK, t=t))
            return out

        parts: list[str] = [f"取窗口内最大值：{'/'.join(f'{k}={peaks[k]:.3f}' for k in sorted(peaks))}"
                            f"；最大在 {dominant} {vel_max:.3f} mm/s"]
        c_level, c_bad = None, None          # 经典部分的档位 / 算不出的码
        b_level, b_bad = None, None          # 自训部分的档位 / 算不出的码

        # ② 经典：烈度分级 + 方向性
        if classic:
            c_level, c_bad = self._classic(frame, peaks, vel_max, t, out, parts)
        else:
            parts.append("经典算法未启用")

        # ③ 自训：相对基线的偏离
        if baseline_on:
            b_level, b_bad = self._baseline(frame, peaks, t, out, parts)
        else:
            parts.append("AI 自训未启用")

        # ③' 自训算不出 ⇒ 状态按经典判（§0.3 第 3 条，`H-282 §2.2`）
        if baseline_on and b_level is None:
            if not classic:
                c_level, c_bad, note = _classic_fallback(frame.params, vel_max)
                parts.append(note)
            elif c_level is not None:
                parts.append("★AI 自训部分本次算不出，检测状态只按经典算法判；采到可用基线后自动按两者合成")

        # ④ 检测状态与两部分是否一致
        level, q = _combine_status(c_level, c_bad, b_level, b_bad)
        out.append(Finding(key="status", value=level, quality=q, t=t))
        note = _disagreement(c_level, b_level)
        if note:
            parts.append(note)

        # ⑤ 故障分类：只报，不推检测状态（§0.7）
        _classify_frame(frame, t, out, parts)

        parts.append(state_note)
        if bad:
            parts.append(f"降级：{'/'.join(bad)} 本窗口全是坏值，未参与判定")
        if empty:
            parts.append(f"降级：{'/'.join(empty)} 本窗口无样本")
        out.append(Finding(key="evidence", value="；".join(p for p in parts if p),
                           quality=Quality.OK, t=t))
        return out

    # ── 两部分各自 ────────────────────────────────────────────────────────
    @staticmethod
    def _classic(frame: Frame, peaks: dict[str, float], vel_max: float, t: datetime,
                 out: list[Finding], parts: list[str]) -> tuple[str | None, Quality | None]:
        """烈度分级 + 方向性。返回 `(档位, 算不出时的码)`。"""
        limits, iso_bad, basis, is_pump = _grading(frame.params)
        level: str | None = None
        bad_q: Quality | None = None
        if iso_bad:
            out += _bad_group(("iso_zone", "iso_zone_code", "iso_margin"), Quality.CONFIG_INCOMPLETE, t)
            parts.append(f"烈度分级未给：{iso_bad}")
            bad_q = Quality.CONFIG_INCOMPLETE
        else:
            zone, code, margin = _classify(vel_max, limits)  # type: ignore[arg-type]
            level = _ZONE_STATUS[zone]
            out += [Finding(key="iso_zone", value=zone, quality=Quality.OK, t=t),
                    Finding(key="iso_zone_code", value=code, quality=Quality.OK, t=t),
                    Finding(key="iso_margin", value=round(margin, 4), quality=Quality.OK, t=t)]
            parts.append(
                f"{basis} 判为 {zone} 区，"
                + (f"已超出 C/D 界 {-margin:.3f} mm/s" if margin < 0 else f"距下一档还有 {margin:.3f} mm/s"))
            if not is_pump:                  # 低速规定出自 GB/T 6075.3；泵标准不限转速
                parts.append(_speed_note(frame.params.get(P_SPEED, "")))

        axial = frame.params.get(P_AXIAL, "").strip().lower()
        dir_bad, dir_q, ratio, hint = _direction_check(peaks, axial)
        if dir_bad:
            out += _bad_group(("axial_ratio", "direction_hint"), dir_q, t)
            parts.append(f"方向性提示未给：{dir_bad}")
        else:
            out += [Finding(key="axial_ratio", value=round(ratio, 4), quality=Quality.OK, t=t),
                    Finding(key="direction_hint", value=hint, quality=Quality.OK, t=t)]
            parts.append(f"方向性提示：{hint}")
        parts.append("★无频谱数据，方向性仅为提示而非结论")
        return level, bad_q

    @staticmethod
    def _baseline(frame: Frame, peaks: dict[str, float], t: datetime,
                  out: list[Finding], parts: list[str]) -> tuple[str | None, Quality | None]:
        """相对基线的偏离。返回 `(档位, 算不出时的码)`。"""
        artifact = frame.artifacts.get("baseline")
        if artifact is None:
            out += _bad_group(BASELINE_KEYS, Quality.MODEL_NOT_LOADED, t)
            parts.append("无可用基线（未采或未启用），偏离与异常分未给")
            return None, Quality.MODEL_NOT_LOADED
        try:
            model = _parse_baseline(artifact.blob)
        except Exception as exc:  # noqa: BLE001 —— 坏工件不许掀翻整拍推理
            out += _bad_group(BASELINE_KEYS, Quality.MODEL_NOT_LOADED, t)
            parts.append(f"基线工件读不懂（{type(exc).__name__}: {exc}），偏离与异常分未给")
            return None, Quality.MODEL_NOT_LOADED
        found, note = _deviation(model, peaks, frame, t)
        out += found
        parts.append(note)
        z = next(f for f in found if f.key == "vel_z_max")
        if z.value is None:                  # 「自训算不出」仍以速度为准（§0.3 不动）
            return None, z.quality
        return (STATUS_ATTENTION if _status_z(found) >= _Z_NOTABLE else STATUS_NORMAL), None


# ─────────────────────────────── 合成 ───────────────────────────────

def _algos(params: dict[str, str]) -> frozenset[str] | None:
    """`algorithms` → 启用的部分；没填或非法回 None。"""
    raw = (params.get(P_ALGOS) or "").strip()
    if raw == ALGO_BOTH:
        return frozenset({ALGO_CLASSIC, ALGO_BASELINE})
    if raw in (ALGO_CLASSIC, ALGO_BASELINE):
        return frozenset({raw})
    return None


def _classify_on(params: dict[str, str]) -> bool | None:
    """`fault_classify` → 是否启用故障分类。留空即不启用（`blank_meaning=not_evaluated`）；非法回 None。"""
    raw = (params.get(P_CLASSIFY) or "").strip().lower()
    if raw == "true":
        return True
    if raw in ("", "false"):
        return False
    return None


def _combine_status(c_level: str | None, c_bad: Quality | None,
                    b_level: str | None, b_bad: Quality | None) -> tuple[str | None, Quality]:
    """§0.3：算得出的取最高档、高于正常就照报；经典算不出 ⇒ 落经典的码；
    经典算得出（含只启用自训时借来的判级）⇒ 自训算不出不拦它（`H-282 §2.2`）。"""
    levels = [lv for lv in (c_level, b_level) if lv is not None]
    top = max(levels, key=lambda lv: _RANK[lv]) if levels else None
    if top is not None and _RANK[top] > _RANK[STATUS_NORMAL]:
        return top, Quality.OK
    if c_bad is not None:                    # 经典的码优先：它不依赖工件，算不出多半是配置问题
        return None, c_bad
    if top is not None:
        return top, Quality.OK
    assert b_bad is not None, "两部分都没给档位，算不出的那部分必须给码"
    return None, b_bad


def _classic_fallback(params: dict[str, str], vel_max: float
                      ) -> tuple[str | None, Quality | None, str]:
    """只启用自训、自训又算不出时，借经典判级出状态。返回 `(档位, 算不出时的码, 判据摘要)`。

    ★借的只是状态：经典那几个结论点**没启用就不写**（§0.2）。判级参数不全就不借 ——
      码返回 None，让状态落自训那部分的码（那才是这条绑定缺的东西）。
    """
    limits, why, basis, _ = _grading(params)
    if why:
        return None, None, f"无可用基线，判级参数也不全（{why}），检测状态无从借经典判"
    zone, _, _ = _classify(vel_max, limits)
    return _ZONE_STATUS[zone], None, (f"★无可用基线：检测状态暂借经典算法判（{basis} 判为 {zone} 区；"
                                      "经典未启用，其结论点不写）；采到基线后自动改按 AI 自训")


def _disagreement(c_level: str | None, b_level: str | None) -> str:
    """两部分不一致时怎么读。只在两部分都算得出时说。"""
    if c_level is None or b_level is None:
        return ""
    if _RANK[c_level] >= _RANK[STATUS_WARNING] and b_level == STATUS_NORMAL:
        return (f"★国标判为{_LEVEL_TEXT[c_level]}、相对自身基线无明显变化：振动可能长期偏高"
                "（基线或采于异常状态，不宜作健康参照），或判级参数需核对")
    if c_level == STATUS_NORMAL and b_level == STATUS_ATTENTION:
        return "★国标范围内、但已偏离自身常态：早期变化，宜关注"
    return ""


# ─────────────────────────────── 经典部分 ───────────────────────────────

def _peaks(frame: Frame) -> tuple[dict[str, float], dict[str, datetime], list[str], list[str]]:
    """逐轴取窗口内**可信样本**的最大值。键为 `x`/`y`/`z`。

    返回 `(轴→峰值, 轴→该峰值样本时刻, 全是坏值的轴, 选了但无样本的轴)`。
    ★只有 `Quality.OK` 的样本参与；全坏的通道单独记出来，不当作没发生。
    ★没选的通道不记入"无样本" —— 那与"选了但没数据"不是一回事。
    """
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
            if v is None:
                continue
            if best_v is None or v > best_v:
                best_v, best_t = v, s.t
        if best_v is None or best_t is None:
            bad.append(axis)
        else:
            peaks[axis] = best_v
            times[axis] = best_t
    return peaks, times, bad, empty


def run_state(params: dict[str, str], vel_max: float) -> tuple[str | None, Quality, str]:
    """定案 1.6 / 2.5：返回 `(运行状态, 质量, 摘要)`。门槛非法落 CONFIG_INCOMPLETE，其余照常判。"""
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
    group = params.get(P_GROUP, "").strip()
    mount = params.get(P_SUPPORT, "").strip()
    pump = params.get(P_PUMP, "").strip()
    is_rms = params.get("vel_is_rms", "").strip().lower()

    is_pump = pump in _PUMPS
    if is_pump and group in _GROUPS:
        return None, (f"参数自相矛盾：既填了泵类别（{pump}）又填了机器分组（{group}），"
                      "不猜哪个对"), "", True
    if is_rms != "true":
        return None, (f"速度口径未确认为有效值（vel_is_rms={is_rms or '未填'}）"
                      "—— 拿峰值套有效值判据会整档偏高，故不给分级"), "", is_pump

    if is_pump:
        power = _num(params.get(P_POWER, ""))
        if power is None or power <= 0:
            raw = params.get(P_POWER, "").strip()
            return None, (f"泵按 {_PUMP_STD} 判级需要额定功率（{P_POWER}={raw or '未填'}），"
                          "无法确定功率档"), "", True
        band = "le200" if power <= PUMP_POWER_SPLIT_KW else "gt200"
        roman = "Ⅰ" if pump == "category1" else "Ⅱ"
        basis = (f"{_PUMP_STD}（第{roman}类 / 额定 {power:g} kW，"
                 f"{'≤' if band == 'le200' else '>'}200 kW 档）")
        return _PUMP_LIMITS[(pump, band)], "", basis, True

    if group == _NA:
        return None, f"机器分组为「不适用」：设备不在 {_MACHINE_STD} 范围内，不给分级", "", False
    if pump == _NA and not group:
        return None, "机器分组未填（泵类别为「不适用」，按工业机器判需要机器分组）", "", False
    limits = _MACHINE_LIMITS.get((group, mount))
    if limits is None:
        return None, (f"参数不全或取值非法：{P_GROUP}={group or '未填'} "
                      f"{P_SUPPORT}={mount or '未填'}，查不到 {_MACHINE_STD} 边界"), "", False
    basis = (f"{_MACHINE_STD}（第 {group[-1]} 组 / "
             f"{'刚性' if mount == 'rigid' else '柔性'}支承）")
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


def _direction_check(peaks: dict[str, float], axial: str) -> tuple[str, Quality, float, str]:
    """返回 `(未给原因, 坏质量码, 比值, 提示)`；原因为空表示算出来了。"""
    if axial not in _AXES:
        return (f"轴向 {P_AXIAL}={axial or '未填'} 非法 —— 不知道哪根是轴向就分不开不对中与不平衡",
                Quality.CONFIG_INCOMPLETE, 0.0, "")
    if axial not in peaks:
        return f"轴向轴 {axial} 本窗口没有可信样本", Quality.INPUT_BAD, 0.0, ""
    radial_keys = [a for a in _AXES if a != axial and a in peaks]
    if not radial_keys:
        return "只有轴向轴有数据，没有径向轴可比", Quality.INSUFFICIENT_SAMPLES, 0.0, ""
    ratio, hint = _direction(peaks, axial, radial_keys)
    return "", Quality.OK, ratio, hint


def _direction(peaks: dict[str, float], ax_key: str, radial_keys: list[str]) -> tuple[float, str]:
    """方向性倾向。经验判据，不是国标；阈值出处见 `_AXIAL_SIGNIFICANT` 处。"""
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


# ─────────────────────────────── 自训部分 ───────────────────────────────

def _frame_stopped(frame: Frame, params: dict[str, str]) -> bool:
    """采基线用：这一帧按停机门槛算不算停机。一路可信速度都没有的帧不算停机（交给后面按通道缺样本处理）。"""
    peaks = [p for p in (_window_peak(frame, _vel_role(a)) for a in _AXES) if p is not None]
    return bool(peaks) and run_state(params, max(peaks))[0] == STOPPED


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


def _feature(frame: Frame, role: str) -> float | None:
    """一路在一个窗口上的特征值：幅值类取最大值、频率与温度取均值。★基线与分类同口径。"""
    if _qty_of(role) in _PEAK_QTYS:
        return _window_peak(frame, role)
    return _window_mean(frame, role)


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


def _scale(chan: dict, qty: str = VEL) -> float:
    """四分位距折算成与标准差同尺度，并套该量的下限。"""
    return max(float(chan.get("iqr") or 0.0) / _IQR_TO_SIGMA, _MIN_SCALE_BY_QTY.get(qty, MIN_SCALE))


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

    # ①' 加速度、位移、频率偏离（10-08 补齐输入）。
    for qty in (ACC, DISP, FREQ):
        found, note = _qty_deviation(chans, frame, qty, t)
        out.append(found)
        if note:
            notes.append(note)

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
        z_part = min(1.0, max(0.0, _status_z(out)) / (2 * _Z_NOTABLE))
        got_drift = next((f for f in out if f.key == "ratio_drift"), None)
        d_part = (min(1.0, abs(got_drift.value) / 0.5)
                  if got_drift is not None and got_drift.value is not None else 0.0)
        got_temp = next((f for f in out if f.key == "temp_rise"), None)
        t_part = (min(1.0, max(0.0, got_temp.value) / 10.0)
                  if got_temp is not None and got_temp.value is not None else 0.0)
        score = 100.0 * (0.6 * z_part + 0.25 * d_part + 0.15 * t_part)
        out.append(Finding(key="anomaly_score", value=round(score, 1), quality=Quality.OK, t=t))

    head = f"基线{_baseline_age(model, t)}（{model.get('frames', '?')} 帧）"
    return out, "；".join([head] + notes)


def _status_z(found: list[Finding]) -> float:
    """能推状态的那几项偏离（速度、加速度、位移）里最大的。调用方保证速度偏离已算出。"""
    return max(f.value for f in found if f.key in _STATUS_Z_KEYS and f.value is not None)


def _qty_deviation(chans: dict, frame: Frame, qty: str, t: datetime) -> tuple[Finding, str]:
    """一个量（加速度 / 位移 / 频率）相对基线的偏离：各轴取最大，频率按绝对值取、带符号。

    没选这个量 ⇒ NO_INPUT（照温升）；选了但基线里没有 ⇒ MODEL_NOT_LOADED，说清要重采；
    选了、基线也有，但本窗口没有可信样本 ⇒ INSUFFICIENT_SAMPLES。
    """
    key, name = _Z_KEY[qty], _QTY[qty][2]
    chosen = [a for a in _AXES if _role(a, qty) in frame.channels]
    if not chosen:
        return Finding(key=key, value=None, quality=Quality.NO_INPUT, t=t), ""
    zs: dict[str, float] = {}
    no_base: list[str] = []
    for a in chosen:
        role = _role(a, qty)
        base = chans.get(role)
        if base is None:
            no_base.append(a)
            continue
        cur = _feature(frame, role)
        if cur is not None:
            zs[a] = (cur - float(base["median"])) / _scale(base, qty)
    notes = []
    if no_base:
        notes.append(f"{'/'.join(no_base)} 轴{name}基线里没有（基线采于选它之前），重采基线后才有")
    if not zs:
        q = Quality.MODEL_NOT_LOADED if len(no_base) == len(chosen) else Quality.INSUFFICIENT_SAMPLES
        if q is Quality.INSUFFICIENT_SAMPLES:
            notes.append(f"{name}本窗口没有可信样本，{name}偏离未给")
        return Finding(key=key, value=None, quality=q, t=t), "；".join(notes)
    two_sided = qty not in _PEAK_QTYS        # 频率：偏高偏低都是变化
    worst = max(zs, key=lambda a: abs(zs[a]) if two_sided else zs[a])
    v = zs[worst]
    if (abs(v) if two_sided else v) >= _Z_NOTABLE:
        notes.append(f"{worst} 轴{name}较基线偏移 {v:+.1f} 个尺度单位（只报，不影响检测状态）"
                     if qty not in _STATUS_QTYS else f"{worst} 轴{name}较基线偏高 {v:.1f} 个尺度单位")
    return Finding(key=key, value=round(v, 3), quality=Quality.OK, t=t), "；".join(notes)


# ─────────────────────────────── 故障分类 ───────────────────────────────

#: 分类器格式。**存进工件里**：格式一变，老工件要能被认出来而不是被误读。
CLASSIFIER_FORMAT = "vibration_classifier/rf@1"

#: 每类至少几帧、几条样本（§0.7）。★至少 2 条样本：一条进训练、一条进留出集，否则这一类根本没被验过。
MIN_CLASS_FRAMES = 10
MIN_CLASS_SAMPLES = 2
#: 留出集占比（**按样本计**，不按帧）。
HOLDOUT_FRACTION = 0.25

#: 随机森林。深度与类权重对齐 v5 现场在跑的 `RandomForestClassifier(n_estimators=120, max_depth=6,
#: class_weight="balanced")`；树数取 100、切点按分位数取 ≤32 个 —— 纯 Python 要在十几秒内训完 5000 帧。
RF_TREES = 100
RF_MAX_DEPTH = 6
RF_MIN_LEAF = 2
RF_MAX_CUTS = 32
RF_SEED = 42


def _unit_of(role: str) -> str:
    return "℃" if role == TEMP_ROLE else _QTY[_qty_of(role)][0]


def _train_classifier(dataset: Dataset, report: ProgressSink) -> TrainedArtifact:
    """训一个 13 标量故障分类器（§0.7）。样本够不够、验没验过，都在这里说清，不够就失败。"""
    # ① 停机帧不进训练：停着的帧哪一类都像，只会把各类搅在一起。
    running: list[LabeledFrame] = []
    stopped = 0
    for it in dataset.items:
        _state, q, note = run_state(it.frame.params, 0.0)
        if q is not Quality.OK:
            raise ValueError(f"{note} —— 判不了哪些帧是停机，不训分类器")
        if _frame_stopped(it.frame, it.frame.params):
            stopped += 1
        else:
            running.append(it)

    # ② 特征集 = 训练帧里出现过的那几路（按 13 标量的固定顺序）；缺任一路的帧剔除并计数。
    rows = [(it, {r: _feature(it.frame, r) for r in FEATURE_ROLES}) for it in running]
    feats = [r for r in FEATURE_ROLES if any(v[r] is not None for _, v in rows)]
    if not feats:
        raise ValueError(f"训练集 {len(dataset)} 帧里一路可信特征都没取到"
                         + (f"（其中 {stopped} 帧判为停机已剔除）" if stopped else "")
                         + " —— 检查所选采集点与这段时间实时库里有没有数据")
    X: list[list[float]] = []
    y_lab: list[str] = []
    groups: list[tuple[str, int]] = []
    incomplete = 0
    for it, v in rows:
        if any(v[r] is None for r in feats):
            incomplete += 1
            continue
        X.append([v[r] for r in feats])         # type: ignore[misc]
        y_lab.append(it.label)
        groups.append((it.label, it.sample_id))

    labels = sorted(set(y_lab))
    frames_by = {lb: y_lab.count(lb) for lb in labels}
    samples_by = {lb: sorted({g[1] for g in groups if g[0] == lb}) for lb in labels}
    dist = "、".join(f"{lb} {frames_by[lb]} 帧 / {len(samples_by[lb])} 条样本" for lb in labels) or "无"
    dropped = "".join([f"；{stopped} 帧判为停机已剔除" if stopped else "",
                       f"；{incomplete} 帧缺特征已剔除" if incomplete else ""])
    if len(labels) < 2:
        raise ValueError(f"故障分类至少要两类，实际只有 {len(labels)} 类（{dist}{dropped}）—— 一类训不出分类器")
    short = [lb for lb in labels
             if frames_by[lb] < MIN_CLASS_FRAMES or len(samples_by[lb]) < MIN_CLASS_SAMPLES]
    if short:
        raise ValueError(
            f"类别 {'、'.join(short)} 样本不足：每类至少 {MIN_CLASS_FRAMES} 帧、{MIN_CLASS_SAMPLES} 条样本"
            f"（一条进训练、一条留作验证）；各类：{dist}{dropped}")
    report.report(0.2, f"特征 {len(feats)} 路，{len(X)} 帧（{dist}{dropped}）")

    # ③ ★按样本分层留出：同一条样本切出的帧高度相关，按帧分会把考题漏给考生。
    rng = random.Random(RF_SEED)
    hold: set[tuple[str, int]] = set()
    for lb in labels:
        sids = list(samples_by[lb])
        rng.shuffle(sids)
        k = min(len(sids) - 1, max(1, round(len(sids) * HOLDOUT_FRACTION)))
        hold.update((lb, s) for s in sids[:k])
    index = {lb: k for k, lb in enumerate(labels)}
    y = [index[lb] for lb in y_lab]
    tr = [i for i in range(len(X)) if groups[i] not in hold]
    ho = [i for i in range(len(X)) if groups[i] in hold]

    forest = _rf_fit([X[i] for i in tr], [y[i] for i in tr], len(labels),
                     lambda p: report.report(0.2 + 0.35 * p, "在训练部分上训练、准备验证"))
    pred = [_argmax(_rf_proba(forest, X[i], len(labels))) for i in ho]
    metrics = _metrics([y[i] for i in ho], pred, labels)
    metrics["samples"] = len(hold)
    report.report(0.6, f"留出集 {len(ho)} 帧（{len(hold)} 条样本）准确率 {metrics['accuracy']:.3f}；"
                       f"用全部 {len(X)} 帧重训")

    # ④ 评估完用全部数据重训 —— 交付的是它；准确率仍是上面留出集上的。
    forest = _rf_fit(X, y, len(labels), lambda p: report.report(0.6 + 0.35 * p, "用全部数据重训"))

    model = {
        "format": CLASSIFIER_FORMAT,
        "features": feats,
        "units": {r: _unit_of(r) for r in feats},
        "labels": labels,
        "class_frames": frames_by,
        "class_samples": {lb: len(samples_by[lb]) for lb in labels},
        "frames": len(X),
        "stopped_excluded": stopped,
        "incomplete_excluded": incomplete,
        "holdout": metrics,
        "params": {"trees": RF_TREES, "max_depth": RF_MAX_DEPTH, "min_leaf": RF_MIN_LEAF,
                   "max_cuts": RF_MAX_CUTS, "seed": RF_SEED, "holdout_fraction": HOLDOUT_FRACTION},
        "forest": forest,
    }
    blob = json.dumps(model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    report.report(1.0, "完成")
    return TrainedArtifact(
        blob=blob, algo="随机森林（13 标量）", kind="classifier", suffix=".json",
        # ★留出集上的准确率，不是训练集自测（v5 那 71 个 1.0 就是自测出来的）。
        accuracy=metrics["accuracy"], feature_count=len(feats),
        meta={"format": CLASSIFIER_FORMAT, "labels": json.dumps(labels, ensure_ascii=False),
              "features": json.dumps(feats), "frames": str(len(X)),
              "class_frames": json.dumps(frames_by, ensure_ascii=False),
              "stopped_excluded": str(stopped), "incomplete_excluded": str(incomplete),
              "holdout": json.dumps(metrics, ensure_ascii=False),
              "accuracy_basis": f"按样本分层留出 {len(hold)} 条样本 / {len(ho)} 帧"})


def _metrics(truth: list[int], pred: list[int], labels: list[str]) -> dict:
    """留出集上的混淆矩阵（行 = 真实，列 = 判成）与每类精确率 / 召回率。分母为 0 给 None，不给 0。"""
    n = len(labels)
    cm = [[0] * n for _ in range(n)]
    for a, b in zip(truth, pred):
        cm[a][b] += 1
    per = {}
    for k, lb in enumerate(labels):
        col = sum(cm[r][k] for r in range(n))
        row = sum(cm[k])
        per[lb] = {"precision": round(cm[k][k] / col, 4) if col else None,
                   "recall": round(cm[k][k] / row, 4) if row else None, "support": row}
    acc = sum(cm[k][k] for k in range(n)) / len(truth)
    return {"frames": len(truth), "accuracy": round(acc, 4), "per_class": per, "confusion": cm}


def _cut_points(col: list[float]) -> list[float]:
    """一个特征的候选切点：相邻不同值的中点，多于 `RF_MAX_CUTS` 个时按分位取。严格递增。"""
    u = sorted(set(col))
    mids = [(u[i] + u[i + 1]) / 2 for i in range(len(u) - 1)]
    if len(mids) <= RF_MAX_CUTS:
        return mids
    return sorted({mids[round(j * (len(mids) - 1) / (RF_MAX_CUTS - 1))] for j in range(RF_MAX_CUTS)})


def _rf_fit(X: list[list[float]], y: list[int], n_classes: int, progress=None) -> list[list]:
    """纯 Python 随机森林：有放回抽样、每次分裂随机看 ⌈√F⌉ 个特征、基尼系数、类权重均衡。

    ★先把每个特征分箱（`bisect_left(切点, x)` = 小于 x 的切点个数），分裂时只扫箱的直方图 ——
      不分箱就得每个节点每个特征排一次序，纯 Python 训 5000 帧要以分钟计。
    节点：内部 `[特征号, 阈值, 左, 右]`（x ≤ 阈值走左）；叶子 `[-1, 各类占比]`。
    """
    n, F = len(X), len(X[0])
    cuts = [_cut_points([row[f] for row in X]) for f in range(F)]
    bins = [[bisect.bisect_left(cuts[f], row[f]) for row in X] for f in range(F)]
    counts = [0] * n_classes
    for c in y:
        counts[c] += 1
    cw = [n / (n_classes * c) if c else 0.0 for c in counts]
    w = [cw[c] for c in y]
    mtry = max(1, math.ceil(math.sqrt(F)))
    rng = random.Random(RF_SEED)
    trees: list[list] = []
    for k in range(RF_TREES):
        idx = [rng.randrange(n) for _ in range(n)]
        nodes: list = []
        _grow(bins, cuts, y, w, idx, n_classes, mtry, rng, 0, nodes)
        trees.append(nodes)
        if progress is not None and k % 10 == 9:
            progress((k + 1) / RF_TREES)
    return trees


def _grow(bins, cuts, y, w, idx: list[int], n_classes: int, mtry: int,
          rng: random.Random, depth: int, nodes: list) -> int:
    dist = [0.0] * n_classes
    for i in idx:
        dist[y[i]] += w[i]
    me = len(nodes)
    nodes.append(None)
    total = sum(dist)
    if (depth >= RF_MAX_DEPTH or len(idx) < 2 * RF_MIN_LEAF
            or max(dist) >= total * (1 - 1e-12)):
        nodes[me] = [-1, [round(d / total, 6) for d in dist]]
        return me
    parent = total - sum(d * d for d in dist) / total      # 加权基尼 × 总权
    best: tuple[float, int, int] | None = None
    for f in rng.sample(range(len(cuts)), mtry):
        nb = len(cuts[f]) + 1
        if nb < 2:
            continue
        hist = [[0.0] * n_classes for _ in range(nb)]
        cnt = [0] * nb
        col = bins[f]
        for i in idx:
            b = col[i]
            hist[b][y[i]] += w[i]
            cnt[b] += 1
        left = [0.0] * n_classes
        nl = 0
        for k in range(nb - 1):
            for c in range(n_classes):
                left[c] += hist[k][c]
            nl += cnt[k]
            if cnt[k] == 0:                  # 与上一个切点分出的两边相同
                continue
            if nl < RF_MIN_LEAF or len(idx) - nl < RF_MIN_LEAF:
                continue
            wl = sum(left)
            wr = total - wl
            if wl <= 0 or wr <= 0:
                continue
            imp = ((wl - sum(v * v for v in left) / wl)
                   + (wr - sum((dist[c] - left[c]) ** 2 for c in range(n_classes)) / wr))
            if best is None or imp < best[0] - 1e-12:
                best = (imp, f, k)
    if best is None or best[0] >= parent - 1e-12:
        nodes[me] = [-1, [round(d / total, 6) for d in dist]]
        return me
    _imp, f, k = best
    col = bins[f]
    li = [i for i in idx if col[i] <= k]
    ri = [i for i in idx if col[i] > k]
    node = [f, cuts[f][k], 0, 0]
    nodes[me] = node
    node[2] = _grow(bins, cuts, y, w, li, n_classes, mtry, rng, depth + 1, nodes)
    node[3] = _grow(bins, cuts, y, w, ri, n_classes, mtry, rng, depth + 1, nodes)
    return me


def _rf_proba(trees: list[list], x: list[float], n_classes: int) -> list[float]:
    """各棵树叶子上的类别占比取平均。★是票数占比，不是概率。"""
    acc = [0.0] * n_classes
    for nodes in trees:
        j = 0
        while nodes[j][0] >= 0:
            f, thr, left, right = nodes[j]
            j = left if x[f] <= thr else right
        for c, p in enumerate(nodes[j][1]):
            acc[c] += p
    return [a / len(trees) for a in acc]


def _argmax(vals: list[float]) -> int:
    """并列取靠前的那一类 —— 结果确定，不随机。"""
    return max(range(len(vals)), key=lambda k: (vals[k], -k))


def _parse_classifier(blob: bytes) -> dict:
    """解析分类器工件。**格式不认就抛** —— 拿不认识的结构去算，算出来的类别没人看得出是错的。"""
    model = json.loads(blob.decode("utf-8"))
    if not isinstance(model, dict):
        raise ValueError("分类器工件不是一个对象")
    fmt = model.get("format", "")
    if fmt != CLASSIFIER_FORMAT:
        raise ValueError(f"分类器格式是 {fmt!r}，本模块只认 {CLASSIFIER_FORMAT!r}")
    feats, labels, forest = model.get("features"), model.get("labels"), model.get("forest")
    if not isinstance(feats, list) or not feats or any(r not in FEATURE_ROLES for r in feats):
        raise ValueError(f"分类器的特征表不认识：{feats!r}")
    if not isinstance(labels, list) or len(labels) < 2:
        raise ValueError(f"分类器的类别表不对：{labels!r}")
    if not isinstance(forest, list) or not forest:
        raise ValueError("分类器里一棵树都没有")
    return model


def _classify_frame(frame: Frame, t: datetime, out: list[Finding], parts: list[str]) -> None:
    """故障分类（§0.7）。只写结论与摘要，**不碰检测状态**。"""
    on = _classify_on(frame.params)
    if on is False:
        return
    if on is None:
        out += _bad_group(CLASSIFY_KEYS, Quality.CONFIG_INCOMPLETE, t)
        parts.append(f"故障分类未给：{P_CLASSIFY}={frame.params.get(P_CLASSIFY)!r} 非法，须为 true / false")
        return
    art = frame.artifacts.get("classifier")
    if art is None:
        out += _bad_group(CLASSIFY_KEYS, Quality.MODEL_NOT_LOADED, t)
        parts.append("故障分类未给：无可用分类器（未训练或未启用）")
        return
    try:
        model = _parse_classifier(art.blob)
    except Exception as exc:  # noqa: BLE001 —— 坏工件不许掀翻整拍推理
        out += _bad_group(CLASSIFY_KEYS, Quality.MODEL_NOT_LOADED, t)
        parts.append(f"故障分类未给：分类器工件读不懂（{type(exc).__name__}: {exc}）")
        return
    feats, labels = model["features"], model["labels"]
    unbound = [r for r in feats if r not in frame.channels]
    if unbound:
        out += _bad_group(CLASSIFY_KEYS, Quality.CONFIG_INCOMPLETE, t)
        parts.append(f"故障分类未给：分类器要 {'/'.join(unbound)}，本诊断没选 —— 选上，或按现有输入重训")
        return
    x = [_feature(frame, r) for r in feats]
    lacking = [r for r, v in zip(feats, x) if v is None]
    if lacking:
        out += _bad_group(CLASSIFY_KEYS, Quality.INSUFFICIENT_SAMPLES, t)
        parts.append(f"故障分类未给：本窗口 {'/'.join(lacking)} 没有可信样本")
        return
    proba = _rf_proba(model["forest"], x, len(labels))     # type: ignore[arg-type]
    k = _argmax(proba)
    out += [Finding(key="fault_class", value=str(labels[k]), quality=Quality.OK, t=t),
            Finding(key="fault_vote", value=round(proba[k], 3), quality=Quality.OK, t=t)]
    acc = (model.get("holdout") or {}).get("accuracy")
    basis = f"留出集准确率 {acc:.2f}" if isinstance(acc, (int, float)) else "未经留出验证"
    parts.append(f"故障分类：最像「{labels[k]}」（票数占比 {proba[k]:.2f}；{basis}）—— ★仅供参考，不影响检测状态")


# ─────────────────────────────── 落码 ───────────────────────────────

def _bad_group(keys: tuple[str, ...], q: Quality, t: datetime) -> list[Finding]:
    """给一组结论落同一个坏质量码。**值为 None** —— 坏质量下不许有值。"""
    return [Finding(key=k, value=None, quality=q, t=t) for k in keys]


def _baseline_age(model: dict, t: datetime) -> str:
    """基线是多久以前采的 —— 说**相对时长**，不写绝对时刻。

    ★判据摘要每拍写进实时库、平台原样照显；铁律是后台存 UTC、前端按当地时间显示，
      夹在文字里的绝对时刻前端换不了时区（原先写的是截掉时区的 UTC，人会读错 8 小时）。
      绝对时刻仍在工件 `meta`（ISO 8601 带 `+00:00`），界面要显示请从那里取、按当地时间显示。
    """
    try:
        end = datetime.fromisoformat(str(model.get("t_to", "")))
    except ValueError:
        return "采集时间不详"
    if end.tzinfo is None:
        return "采集时间不详"
    hours = (t - end).total_seconds() / 3600
    if hours < 1:
        return "采自不到 1 小时前"
    if hours < 48:
        return f"采自约 {round(hours)} 小时前"
    return f"采自约 {round(hours / 24)} 天前"


def _where_in_segment(hot: LabeledFrame, frames) -> str:
    """说出那一帧在人框的那一段里的**相对位置**，不写绝对时刻。

    ★这句话进任务说明、界面原样照显；铁律是后台存 UTC、前端按当地时间显示 ——
      夹在文字里的绝对时刻前端换不了时区，写 UTC 人会读错 8 小时。人是在界面上按当地时间框的这一段，
      说「开始后约几分钟」就找得到，且与时区无关。
    """
    seg_start = min(x.frame.t_start for x in frames if x.sample_id == hot.sample_id)
    minutes = round((hot.frame.t_start - seg_start).total_seconds() / 60)
    return "在该段开头那一窗" if minutes == 0 else f"在该段开始后约 {minutes} 分钟那一窗"


def _bad_all(domain: Domain, frame: Frame, algos: frozenset[str] | None,
             q: Quality, why: str) -> list[Finding]:
    """一条都算不出来时：**已启用部分**的每个输出各落一个坏值锚点，外加一句人话。
    ★不是"什么都不发"：那在下游看来是"这段没数据"，而真相是"这段算不出来"。
    ★没启用的那一半不落 —— 它本就不写；`algos=None`（启用算法本身没填）时全落。"""
    t = frame.t_end
    skip: set[str] = set()
    if algos is not None:
        if ALGO_CLASSIC not in algos:
            skip.update(CLASSIC_KEYS)
        if ALGO_BASELINE not in algos:
            skip.update(BASELINE_KEYS)
    if _classify_on(frame.params) is False:
        skip.update(CLASSIFY_KEYS)
    keys = [o.key for o in domain.declare().outputs if o.key != "evidence" and o.key not in skip]
    out = [Finding(key=k, value=None, quality=q, t=t) for k in keys]
    out.append(Finding(key="evidence", value=why, quality=q, t=t))
    return out


assert set(_RANK) | {STATUS_STOPPED} == set(STATUS_LEVELS)   # 词表变了这里要跟着改
