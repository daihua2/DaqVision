"""低频振动诊断 —— 第一个算法域（轨 A 第 ① 层：ISO 烈度判级 + 方向性倾向）。

> 这是"新增一个域 = 丢一个 `.py`"的第一次真实检验：本文件之外，骨架一行没改为它加分支。
> （骨架这轮确实动过一处 —— `Binding.params`，但那是**所有域都会用到**的通用缺口，
>   不是为本域开的口子。见 §0.3。）

依据：`daqvision/doc/AI振动诊断_算法侧改造方案_hs供数daqgate建模.md`
      §1.5（能力分档）与 §3 轨 A（特征值链路）。

---

## 0.1 这个域**不做**什么 —— 以及为什么

按上述文档 §1.5.2 逐款核实过六款 Modbus 振动传感器：**无一家给谱线数组**。
于是原 v5 赖以立身的那套 **阶次幅值 1X~5X + 规则卡 + MLP 工件**（`order_diagnosis/`）
**在现场没有输入** —— 它喂的是 `services/acquisition.py::simulate_frame` 造的仿真波形。

⇒ 本域**不移植那套**。移过来就是第二个"跑得很热闹但没有真输入"的域（v4 已经是先例）。
  它要回来，得等 L2 档的 `_e_p1..p8` 频带谱能量真的进了实时库 —— 那时它是**另一片**，
  按同样的分界另丢一个 `.py`，不是把它塞进这里。

## 0.2 本域**现在**做什么

| 层 | 内容 | 状态 |
| --- | --- | --- |
| ① | ISO 10816-3 烈度判级 + 方向性倾向 | ✅ 本文件 |
| ② | **基线与偏离**（μ/σ、三轴比例漂移、温升、异常分） | ✅ 本文件（2026-09-11） |
| ②′ | **趋势外推**（EWMA 斜率、剩余可用天数） | ⛔ 见 §0.5 —— 它要的东西骨架还没有，**不硬做** |
| ③ | 多变量异常检测（IsolationForest 一类） | 依赖 ②，且要引第三方库，另议 |
| ④ | 证据包 + LLM 解释 | 依赖 ②③，且 LLM 出网待议 |

★①**不需要训练、不需要历史状态、不需要第三方库**，所以它第一个落地，
  而且它恰好是文档里写的"最该先做的"：**把数值变成该不该停机**。
★②需要一条**基线**。基线走训练面产出（`train()`），落 `artifacts` 表 `kind='baseline'`
  —— 判据是 AICloud `C-11 §5` 给的："要能看见它是什么时候采的、能不能重采的，就是资产"。
  换工况必须重采，运维必须看得见是哪段数据采的 ⇒ 两样都成立 ⇒ 它是资产，不是中间量。

## 0.5 ★为什么**趋势外推**这一片不做（不是忘了）

改造方案 §3 轨A② 里还有两件：`ewma_trend`（劣化速度，dB/天）与
`days_to_threshold`（按趋势外推的剩余可用天数）。**本文件不做它们**，理由是：

它们要的不是"这一帧"，是**同一路输入在很多天上的走向**。而模块拿到的只有一帧
（骨架按节拍取的那个窗口）。要做只有两条路，**两条现在都不该走**：

| 路 | 为什么现在不走 |
| --- | --- |
| 把绑定的窗口拉到几十天 | 每一拍都取几十天的点，只为算一个斜率 —— 这是把实时库当批处理引擎用 |
| 让模块自己攒跨帧状态 | 与分界直接冲突（状态归骨架）。而且进程一重启就从头攒，界面上看不出"这条趋势是从什么时候开始的" |

⇒ **等骨架真给"跨帧运行状态"或"长窗口取数"那一格时再做**，
  而那一格应当由一个真需求逼出来 —— 就像台账参数（1.1）与推理时的工件（本轮）那样。
  在那之前**宁可少一条结论，也不给一个每次重启就归零的"剩余可用天数"**：
  那种数字看起来最像专业结论，也最容易被人当真。

## 0.3 为什么必须有台账参数（`Declaration.params`）

ISO 判级的边界值取决于**机组功率等级**与**刚性/柔性支承**；轴向是哪一根轴决定
"不对中还是不平衡"。这些是**每台机器一份的静态事实**：没有时刻、没有质量码，
绑不到测点上 —— 往实时库里塞一个恒定值的点，就是把台账伪装成测量。

⇒ 契约 1.1 新增 `Binding.params` / `DomainInfo.params`。**骨架只搬运不解释**，
  键名与取值在这里自述，AICloud 按自述渲染表单。⇒ 新增域两侧都不改代码。

## 0.4 一条贯穿全文件的纪律：**缺什么就落什么码，绝不猜缺省**

台账没填、口径没确认、样本全是坏值 —— 每一种都对应一条**特定**的坏质量结论，
而不是"给个看起来合理的数"。理由很直白：**猜错的 ISO 分级会把"该停机"说成
"可长期运行"，而且从数值上看不出来。**

⇒ 于是本域**逐条结论各自判质量**：台账缺 `axial_axis` 只让方向性那两条落码，
  ISO 那几条照出 —— 一坏全坏同样是在撒谎（说"什么都算不出来"）。
"""

from __future__ import annotations

from datetime import datetime

# ★域模块 import 骨架一律用**绝对包名**（`aiintegration.…`）：
#   装载器是按文件路径 exec 的，模块不在包里，相对 import 会当场炸。
import json
import math

from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    Dataset, Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec,
    ProgressSink, TrainedArtifact,
)

# ─────────────────────────── ISO 10816-3 边界表 ───────────────────────────
#
# 单位 mm/s（速度 RMS，10–1000 Hz 带内）。三个边界依次是 A/B、B/C、C/D。
#
#   A 区 新交付机器 │ B 区 可长期运行 │ C 区 不宜长期运行(安排检修) │ D 区 有损坏危险
#
# 组别（ISO 10816-3 的机组分类）：
#   1 = 大型机组（300 kW ~ 50 MW；电机轴中心高 > 315 mm）
#   2 = 中型机组（15 ~ 300 kW；电机轴中心高 160 ~ 315 mm）
#   3 = 泵类，独立驱动（额定 ≥ 15 kW）
#   4 = 泵类，驱动一体（额定 ≥ 15 kW）
#
# ★这张表是**判据本身**，不是可调参数：动它等于改国标结论。
#   要按客户机组特殊约定改限值，走"另加一组台账参数"的路，不许在这里就地改数。
_ISO_LIMITS: dict[tuple[str, str], tuple[float, float, float]] = {
    ("1", "rigid"):    (2.3, 4.5, 7.1),
    ("1", "flexible"): (3.5, 7.1, 11.0),
    ("2", "rigid"):    (1.4, 2.8, 4.5),
    ("2", "flexible"): (2.3, 4.5, 7.1),
    ("3", "rigid"):    (2.3, 4.5, 7.1),
    ("3", "flexible"): (3.5, 7.1, 11.0),
    ("4", "rigid"):    (1.4, 2.8, 4.5),
    ("4", "flexible"): (2.3, 4.5, 7.1),
}

_AXES = ("x", "y", "z")

#: 方向性判据的两个阈值。**经验值**，出处是改造方案 §3 轨A④ 那张现象/倾向对照表。
#: ★它们不是国标，所以结论一律标"倾向"，且 `evidence` 里写明"无频谱"。
_AXIAL_SIGNIFICANT = 0.5   # 轴向 / 径向 ≥ 此值 ⇒ 轴向占比异常
_RADIAL_BALANCED = 0.8     # 径向两轴互比落在 [0.8, 1/0.8] ⇒ 视为各向同性

#: 基线格式版本。**存进工件里**——将来改了格式，老工件要能被认出来而不是被误读。
BASELINE_FORMAT = "vibration_lowfreq/baseline@1"

#: 采基线至少要几帧。★少于这个数算出来的 σ 没有意义，
#: 而一个 σ≈0 的基线会让**任何**正常波动都变成"z 分数爆表"。
MIN_BASELINE_FRAMES = 5

#: σ 的下限（mm/s）。传感器分辨率与量化噪声决定它不可能真的是 0。
#: ★不设下限的后果是除零或者天文数字的 z 分数 —— 后者更坏，因为它看着像个结论。
MIN_SIGMA = 0.01

#: 认为"偏离显著"的 z 分数。经验值，用于合成异常分与证据措辞，**不是国标**。
_Z_NOTABLE = 3.0

#: 采基线时认哪个标签算"正常"。可被台账参数 `normal_label` 覆盖。
DEFAULT_NORMAL_LABEL = "正常"


class LowFreqVibration(Domain):
    """低频振动诊断（L1+ 档：烈度分级 + 方向性倾向）。"""

    key = "vibration_lowfreq"
    display = "低频振动诊断"
    version = "1.0.0"

    # ── 声明 ──────────────────────────────────────────────────────────────
    def declare(self) -> Declaration:
        return Declaration(
            inputs=(
                # ★只声明这一层**真正用到**的输入。声明了却用不上的角色，会让人白配一遍，
                #   还会让"配全了"这个信号失真。acc/disp/freq/temp 留给第②层，那时再加。
                InputSpec(role="x_vel", unit="mm/s", required=True,
                          description="X 轴速度（≥1 轴即可判 L1；单轴传感器就绑这一路）"),
                InputSpec(role="y_vel", unit="mm/s", required=False,
                          description="Y 轴速度"),
                InputSpec(role="z_vel", unit="mm/s", required=False,
                          description="Z 轴速度"),
                # 第②层用：温升是轴承劣化的**独立佐证**（振动没变而温度升了，多半是润滑/冷却）。
                InputSpec(role="temp", unit="℃", required=False,
                          description="测点温度（有就用它算温升，没有就不给那一条结论）"),
            ),
            params=(
                ParamSpec(
                    key="iso_group", display="ISO 10816-3 机组类别", value_type="enum",
                    choices=("1", "2", "3", "4"),
                    choice_displays=(
                        "1 组：大型机组 300kW~50MW（轴中心高>315mm）",
                        "2 组：中型机组 15~300kW（轴中心高 160~315mm）",
                        "3 组：泵类，独立驱动（≥15kW）",
                        "4 组：泵类，驱动一体（≥15kW）",
                    ),
                    required=True,
                    description="决定 A/B/C/D 的边界值。★没有缺省 —— 猜错会把该停机说成可长期运行"),
                ParamSpec(
                    key="mount_type", display="支承方式", value_type="enum",
                    choices=("rigid", "flexible"),
                    choice_displays=("刚性支承", "柔性支承"),
                    required=True,
                    description="同上，直接改边界值（柔性支承的限值约为刚性的 1.5 倍）"),
                ParamSpec(
                    key="vel_is_rms", display="速度口径确认为 RMS", value_type="enum",
                    choices=("true", "false"),
                    choice_displays=("是，已确认为 RMS", "否 / 未确认（峰值或手册未注明）"),
                    required=True,
                    description=(
                        "ISO 判级的前提是速度**有效值(RMS)**。六款传感器里有三款手册未注明口径，"
                        "必须台架标定后才能填「是」。★填「否」时我方**不给** ISO 分级"
                        "（落坏质量码），只给数值 —— 拿峰值套 RMS 判据会整档偏高")),
                ParamSpec(
                    key="axial_axis", display="轴向是哪一轴", value_type="enum",
                    choices=("x", "y", "z"),
                    choice_displays=("X 轴", "Y 轴", "Z 轴"),
                    required=True,
                    description=(
                        "沿转轴方向的那一轴。★没有缺省：'一般是 Z' 猜错会把不对中说成不平衡。"
                        "缺它只影响方向性两条结论，ISO 分级照出"),
                ),
                ParamSpec(
                    key="normal_label", display="采基线时认哪个标签算正常",
                    value_type="string", default=DEFAULT_NORMAL_LABEL, required=False,
                    description=(
                        "采基线只用被标成这个标签的样本。★这一项**允许有缺省**，"
                        "与上面四项不同 —— 猜错它的后果是**当场可见的**（一条样本都匹配不上，"
                        "训练直接失败并说清），而不是悄悄算出一个偏了的结论")),
            ),
            outputs=(
                OutputSpec(key="vel_max", display="速度最大值", value_type="float", unit="mm/s",
                           description="窗口内三轴的最大值（偏保守：宁可高报不漏报）。口径随传感器"),
                OutputSpec(key="dominant_axis", display="最大值所在轴", value_type="string",
                           description="x / y / z"),
                OutputSpec(key="iso_zone", display="ISO 烈度区", value_type="string",
                           description="A/B/C/D —— A 新交付, B 可长期运行, C 不宜长期运行, D 有损坏危险"),
                OutputSpec(key="iso_zone_code", display="ISO 烈度区(数值)", value_type="int",
                           description="1=A 2=B 3=C 4=D。给趋势曲线与报警门限用"),
                OutputSpec(key="iso_margin", display="距下一档余量", value_type="float", unit="mm/s",
                           description="离更差一档的边界还有多远；已在 D 区时为负（超出 C/D 界多少）"),
                OutputSpec(key="axial_ratio", display="轴向/径向比", value_type="float",
                           description="轴向轴 ÷ 径向两轴的较大者。偏高指向不对中"),
                OutputSpec(key="direction_hint", display="方向性倾向", value_type="string",
                           description="★倾向性判断，不是确诊 —— 无频谱数据，置信度明显低于谱诊断"),
                # ── 第②层：相对**这台机器自己的基线**的偏离 ──
                OutputSpec(key="vel_z_max", display="速度偏离(最大 z)", value_type="float",
                           description="各轴 (当前−基线μ)/基线σ 的最大值。>3 视为显著偏离"),
                OutputSpec(key="ratio_drift", display="三轴比例漂移", value_type="float",
                           description="轴向/径向比相对基线的变化量。★没有频谱时判故障类型的主要抓手"),
                OutputSpec(key="temp_rise", display="温升", value_type="float", unit="℃",
                           description="相对基线的温度变化。轴承劣化的独立佐证"),
                OutputSpec(key="anomaly_score", display="异常分", value_type="float",
                           description="0~100，由上面几项合成。★不是概率，是**排序用的分数**"),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="人能直接读的一句话：用了哪几轴、判到哪一档、为什么"),
            ),
        )

    # ── 训练（采基线）──────────────────────────────────────────────────────
    def train(self, dataset: Dataset, report: ProgressSink) -> TrainedArtifact:
        """采一条**基线**：这台机器"正常运行"时各轴速度与温度的 μ/σ 与三轴比例。

        ★这不是在训一个模型，是在记录"这台机器好的时候长什么样"。
          走同一条训练面，是因为它与模型有**同样的身份问题**：
          谁采的、什么时候采的、能不能重采、现在用的是哪一条 —— 都要答得上（`AI-13 §5`）。

        ★**只用被标成"正常"的样本**。哪个标签算正常由台账 `normal_label` 定，缺省"正常"。
          一条都匹配不上就**当场失败并说清**，不是拿全部样本凑合 ——
          拿故障数据采出来的基线，会把故障态当成常态，从此再也报不出这个故障。
        """
        wanted = DEFAULT_NORMAL_LABEL
        for it in dataset.items:                 # 台账在帧上，各帧同一个绑定故取第一个
            wanted = it.frame.params.get("normal_label", "").strip() or DEFAULT_NORMAL_LABEL
            break

        normals = [it for it in dataset.items if it.label == wanted]
        if len(normals) < MIN_BASELINE_FRAMES:
            raise ValueError(
                f"采基线需要至少 {MIN_BASELINE_FRAMES} 帧标为 {wanted!r} 的样本，"
                f"实际只有 {len(normals)} 帧（训练集里共 {len(dataset)} 帧，"
                f"标签分布 {dataset.label_counts()}）—— "
                "样本太少算出来的 σ 没有意义，而 σ≈0 的基线会让任何正常波动都变成异常")

        report.report(0.2, f"用 {len(normals)} 帧 {wanted!r} 样本采基线")

        channels: dict[str, dict[str, float]] = {}
        for role in [f"{a}_vel" for a in _AXES] + ["temp"]:
            vals = []
            for it in normals:
                v = _window_peak(it.frame, role) if role != "temp" else _window_mean(it.frame, role)
                if v is not None:
                    vals.append(v)
            if len(vals) >= MIN_BASELINE_FRAMES:
                mu, sigma = _mean_std(vals)
                channels[role] = {"mean": mu, "std": max(sigma, MIN_SIGMA), "n": len(vals)}

        if not any(k.endswith("_vel") for k in channels):
            raise ValueError(
                "一路速度都没能采到足够样本 —— 检查绑定与这段时间实时库里有没有数据")

        report.report(0.8, "统计完成")

        # 三轴比例（轴向/径向）也进基线：不对中的抓手是"比例变了"，不是"值变大了"。
        ratio = None
        axial = ""
        for it in normals:
            axial = (it.frame.params.get("axial_axis", "") or "").strip().lower()
            break
        if axial in _AXES:
            ax = channels.get(f"{axial}_vel")
            rad = [channels[f"{a}_vel"]["mean"] for a in _AXES
                   if a != axial and f"{a}_vel" in channels]
            if ax and rad and max(rad) > 0:
                ratio = ax["mean"] / max(rad)

        model = {
            "format": BASELINE_FORMAT,
            "label": wanted,
            "frames": len(normals),
            "t_from": min(it.frame.t_start for it in normals).isoformat(),
            "t_to": max(it.frame.t_end for it in normals).isoformat(),
            "axial_axis": axial,
            "axial_ratio": ratio,
            "channels": channels,
        }
        blob = json.dumps(model, ensure_ascii=False, indent=1).encode("utf-8")
        return TrainedArtifact(
            blob=blob, algo="基线统计", kind="baseline", suffix=".json",
            # ★基线没有"准确率"这回事 —— 给 None，别拿 1.0 顶（那会在界面上显示成"100%"）。
            accuracy=None, feature_count=len(channels),
            meta={"format": BASELINE_FORMAT, "normal_label": wanted,
                  "frames": str(len(normals)),
                  "t_from": model["t_from"], "t_to": model["t_to"]})

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        axis_peak, axis_t, bad_axes, empty_axes = _axis_peaks(frame)

        # ① 一路好样本都没有 —— 区分"没数据"与"有数据但全是坏值"。
        #    两者的现场处置完全相反：前者查采集链路，后者查设备/传感器。
        if not axis_peak:
            q = Quality.INPUT_BAD if bad_axes else Quality.NO_INPUT
            why = (f"{'/'.join(bad_axes)} 有样本但质量码全不可信"
                   if bad_axes else f"{'/'.join(empty_axes) or '所有绑定轴'} 在本窗口内没有样本")
            return _all_bad(self, frame, q, f"未出结论：{why}")

        dominant = max(axis_peak, key=lambda a: axis_peak[a])
        vel_max = axis_peak[dominant]
        # ★T 取**那笔样本自己的时刻**，不是帧右端、更不是算完的时刻。
        t = axis_t[dominant]

        used = "/".join(f"{a}={axis_peak[a]:.3f}" for a in _AXES if a in axis_peak)
        degraded: list[str] = []
        if bad_axes:
            degraded.append(f"{'/'.join(bad_axes)} 本窗口全是坏值，未参与判定")
        if empty_axes:
            degraded.append(f"{'/'.join(empty_axes)} 本窗口无样本")

        out: list[Finding] = [
            Finding(key="vel_max", value=round(vel_max, 4), quality=Quality.OK, t=t),
            Finding(key="dominant_axis", value=dominant, quality=Quality.OK, t=t),
        ]

        # ② ISO 分级 —— 三个前提缺一不可，缺哪个就说哪个。
        group = frame.params.get("iso_group", "").strip()
        mount = frame.params.get("mount_type", "").strip()
        is_rms = frame.params.get("vel_is_rms", "").strip().lower()
        limits = _ISO_LIMITS.get((group, mount))

        if is_rms != "true":
            iso_bad = ("速度口径未确认为 RMS（台账 vel_is_rms="
                       f"{is_rms or '未填'}）—— 拿峰值套 RMS 判据会整档偏高，故不给分级")
        elif limits is None:
            iso_bad = (f"台账不全或取值非法：iso_group={group or '未填'} "
                       f"mount_type={mount or '未填'}，查不到 ISO 边界")
        else:
            iso_bad = ""

        if iso_bad:
            out += _bad_group(frame, ("iso_zone", "iso_zone_code", "iso_margin"),
                              Quality.CONFIG_INCOMPLETE, t)
        else:
            zone, code, margin = _classify(vel_max, limits)  # type: ignore[arg-type]
            out += [
                Finding(key="iso_zone", value=zone, quality=Quality.OK, t=t),
                Finding(key="iso_zone_code", value=code, quality=Quality.OK, t=t),
                Finding(key="iso_margin", value=round(margin, 4), quality=Quality.OK, t=t),
            ]

        # ③ 方向性倾向 —— 只要轴向是哪根轴，与 ISO 那三条各自独立。
        axial = frame.params.get("axial_axis", "").strip().lower()
        if axial not in _AXES:
            dir_bad = f"台账 axial_axis={axial or '未填'} 非法 —— 不知道哪根是轴向就分不开不对中与不平衡"
            out += _bad_group(frame, ("axial_ratio", "direction_hint"),
                              Quality.CONFIG_INCOMPLETE, t)
        elif axial not in axis_peak:
            dir_bad = f"轴向轴 {axial} 本窗口没有可信样本"
            out += _bad_group(frame, ("axial_ratio", "direction_hint"), Quality.INPUT_BAD, t)
        else:
            radials = [a for a in _AXES if a != axial and a in axis_peak]
            if not radials:
                dir_bad = f"只有轴向轴 {axial} 有数据，没有径向轴可比"
                out += _bad_group(frame, ("axial_ratio", "direction_hint"),
                                  Quality.INSUFFICIENT_SAMPLES, t)
            else:
                dir_bad = ""
                ratio, hint = _direction(axis_peak, axial, radials)
                out += [
                    Finding(key="axial_ratio", value=round(ratio, 4), quality=Quality.OK, t=t),
                    Finding(key="direction_hint", value=hint, quality=Quality.OK, t=t),
                ]

        # ③′ 第②层：相对基线的偏离。没有基线就整组落 MODEL_NOT_LOADED。
        base_note = ""
        baseline = frame.artifacts.get("baseline")
        if baseline is None:
            base_note = "无可用基线（未采或未启用），偏离与异常分未给"
            out += _bad_group(frame, ("vel_z_max", "ratio_drift", "temp_rise",
                                      "anomaly_score"), Quality.MODEL_NOT_LOADED, t)
        else:
            try:
                model = _parse_baseline(baseline.blob)
            except Exception as exc:  # noqa: BLE001 —— 坏工件不许掀翻整拍推理
                base_note = f"基线工件读不懂（{type(exc).__name__}），偏离与异常分未给"
                out += _bad_group(frame, ("vel_z_max", "ratio_drift", "temp_rise",
                                          "anomaly_score"), Quality.MODEL_NOT_LOADED, t)
            else:
                out2, base_note = _deviation(model, axis_peak, frame, t, axial, dir_bad)
                out += out2

        # ④ 证据摘要 —— 把上面每一条"为什么没给"都摊开写。
        parts = [f"三轴取窗口内最大值：{used}；最大在 {dominant} 轴 {vel_max:.3f} mm/s"]
        if iso_bad:
            parts.append(f"ISO 分级未给：{iso_bad}")
        else:
            zone, _c, margin = _classify(vel_max, limits)  # type: ignore[arg-type]
            parts.append(
                f"ISO 10816-3（{group} 组 / {'刚性' if mount == 'rigid' else '柔性'}支承）判为 {zone} 区，"
                + (f"距 C/D 界还超出 {-margin:.3f} mm/s" if margin < 0
                   else f"距下一档还有 {margin:.3f} mm/s"))
        if dir_bad:
            parts.append(f"方向性倾向未给：{dir_bad}")
        else:
            parts.append(f"方向性：{hint}")
        if base_note:
            parts.append(base_note)
        parts.append("★本域无频谱数据，方向性为倾向判断而非确诊；要判到零件级需上频带谱能量(L2)以上的传感器")
        if degraded:
            parts.append("降级：" + "；".join(degraded))

        out.append(Finding(key="evidence", value="；".join(parts), quality=Quality.OK, t=t))
        return out


# ─────────────────────────────── 内部函数 ───────────────────────────────

def _axis_peaks(frame: Frame) -> tuple[dict[str, float], dict[str, datetime],
                                       list[str], list[str]]:
    """逐轴取窗口内**可信样本**的最大值。

    返回 `(轴→峰值, 轴→该峰值样本的时刻, 全是坏值的轴, 无样本的轴)`。

    ★三条：
      · 只有 `Quality.OK` 的样本参与 —— 坏值参与判级就是拿故障读数当测量值；
      · **不过滤后当作没发生**：全坏的轴单独记出来，进 `evidence` 与坏质量结论；
      · 取最大值（不是均值）偏保守。均值会把一次真实冲击摊平，而 ISO 判的是"能不能长期跑"。
    """
    peaks: dict[str, float] = {}
    times: dict[str, datetime] = {}
    bad: list[str] = []
    empty: list[str] = []
    for axis in _AXES:
        role = f"{axis}_vel"
        if role not in frame.channels:
            continue          # 这一路没绑 —— 与"绑了但没数据"不是一回事，不记进 empty
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
                continue      # 质量说好、值却不是数 —— 上游自相矛盾，按不可用处置
            if best_v is None or v > best_v:
                best_v, best_t = v, s.t
        if best_v is None or best_t is None:
            bad.append(axis)
        else:
            peaks[axis] = best_v
            times[axis] = best_t
    return peaks, times, bad, empty


def _as_float(value) -> float | None:
    """★不用 `float(x) except: 0.0`。

    那正是 v5 `_to_float` 的写法，也正是"坏值静默变成 0"的来源 ——
    0 在速度上是**最好的读数**，于是设备故障被读成了运行完美。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    return None


def _classify(vel: float, limits: tuple[float, float, float]) -> tuple[str, int, float]:
    """按边界表判区，并给出"距下一档还有多远"。D 区返回负数（已超出 C/D 界多少）。"""
    ab, bc, cd = limits
    if vel <= ab:
        return "A", 1, ab - vel
    if vel <= bc:
        return "B", 2, bc - vel
    if vel <= cd:
        return "C", 3, cd - vel
    return "D", 4, cd - vel


def _direction(peaks: dict[str, float], axial: str, radials: list[str]) -> tuple[float, str]:
    """方向性倾向。**经验判据**（改造方案 §3 轨A④ 的现象/倾向表），不是国标。"""
    radial_max = max(peaks[a] for a in radials)
    if radial_max <= 0:
        # 径向为 0 而轴向不为 0：比值无意义（除零），如实说，不造一个大数出来。
        return 0.0, "径向读数为 0，比值无意义；仅轴向有振动，建议现场核对安装与接线"
    ratio = peaks[axial] / radial_max

    # ★判据顺序不是可换的：三轴均衡时轴向比同样 ≥ _AXIAL_SIGNIFICANT，
    #   先判"轴向偏高"会把每一台各向同性的机器都说成不对中。**均衡这一档必须先判**
    #   —— 它是"没有方向性"，而不对中的定义恰恰是"有一个特定方向突出"。
    if len(radials) == 2:
        a, b = (peaks[r] for r in radials)
        lo, hi = (a, b) if a <= b else (b, a)
        if hi > 0 and lo / hi >= _RADIAL_BALANCED and ratio >= _RADIAL_BALANCED:
            return ratio, "三轴接近均衡、无明显方向性 —— 倾向松动 / 基础问题"
    if ratio >= _AXIAL_SIGNIFICANT:
        return ratio, "轴向占比偏高 —— 倾向不对中 / 联轴器问题"
    return ratio, "径向主导且轴向较弱 —— 倾向不平衡"


def _window_peak(frame: Frame, role: str) -> float | None:
    """窗口内可信样本的最大值。与推理侧取值口径**一致** —— 基线与当前值必须同口径，
    否则 z 分数比的是两把不同的尺子。"""
    best = None
    for s in frame.channels.get(role, []):
        if s.quality is not Quality.OK:
            continue
        v = _as_float(s.value)
        if v is not None and (best is None or v > best):
            best = v
    return best


def _window_mean(frame: Frame, role: str) -> float | None:
    """窗口内可信样本的均值（温度用它 —— 温度取峰值没有意义，它本来就慢）。"""
    vals = [v for s in frame.channels.get(role, [])
            if s.quality is Quality.OK and (v := _as_float(s.value)) is not None]
    return sum(vals) / len(vals) if vals else None


def _mean_std(vals: list[float]) -> tuple[float, float]:
    """样本均值与**样本标准差**（n-1）。n<2 时 σ 给 0，由调用方套下限。"""
    n = len(vals)
    mu = sum(vals) / n
    if n < 2:
        return mu, 0.0
    var = sum((v - mu) ** 2 for v in vals) / (n - 1)
    return mu, math.sqrt(var)


def _parse_baseline(blob: bytes) -> dict:
    """解析基线工件。**格式不认就抛** —— 拿不认识的结构去算，算出来的数没人看得出是错的。"""
    model = json.loads(blob.decode("utf-8"))
    if not isinstance(model, dict):
        raise ValueError("基线工件不是一个对象")
    fmt = model.get("format", "")
    if fmt != BASELINE_FORMAT:
        raise ValueError(f"基线格式是 {fmt!r}，本域只认 {BASELINE_FORMAT!r}")
    if not isinstance(model.get("channels"), dict) or not model["channels"]:
        raise ValueError("基线里一路通道都没有")
    return model


def _deviation(model: dict, axis_peak: dict[str, float], frame: Frame,
               t: datetime, axial: str, dir_bad: str) -> tuple[list[Finding], str]:
    """相对基线的偏离。逐条各判质量 —— 与第①层同一条纪律。"""
    chans = model["channels"]
    out: list[Finding] = []
    notes: list[str] = []

    # ① 各轴 z 分数取最大。**只比基线里有的那些轴**：基线没采过的轴，比不了。
    zs: dict[str, float] = {}
    for axis, cur in axis_peak.items():
        c = chans.get(f"{axis}_vel")
        if not c:
            continue
        sigma = max(float(c.get("std") or 0.0), MIN_SIGMA)
        zs[axis] = (cur - float(c["mean"])) / sigma
    if zs:
        worst = max(zs, key=lambda a: zs[a])
        out.append(Finding(key="vel_z_max", value=round(zs[worst], 3),
                           quality=Quality.OK, t=t))
        if zs[worst] >= _Z_NOTABLE:
            notes.append(f"{worst} 轴较基线偏高 {zs[worst]:.1f}σ")
    else:
        out.append(Finding(key="vel_z_max", value=None,
                           quality=Quality.INSUFFICIENT_SAMPLES, t=t))
        notes.append("本帧没有与基线同轴的可信样本，z 分数未给")

    # ② 三轴比例漂移。要基线里存过比例，且本帧方向性算得出来。
    base_ratio = model.get("axial_ratio")
    if base_ratio is None or dir_bad or axial not in axis_peak:
        out.append(Finding(key="ratio_drift", value=None,
                           quality=Quality.CONFIG_INCOMPLETE if base_ratio is None
                           else Quality.INSUFFICIENT_SAMPLES, t=t))
        if base_ratio is None:
            notes.append("基线里没有三轴比例（采基线时轴向未填），比例漂移未给")
    else:
        radials = [axis_peak[a] for a in _AXES if a != axial and a in axis_peak]
        cur_ratio = axis_peak[axial] / max(radials) if radials and max(radials) > 0 else None
        if cur_ratio is None:
            out.append(Finding(key="ratio_drift", value=None,
                               quality=Quality.INSUFFICIENT_SAMPLES, t=t))
        else:
            drift = cur_ratio - float(base_ratio)
            out.append(Finding(key="ratio_drift", value=round(drift, 4),
                               quality=Quality.OK, t=t))
            if abs(drift) >= 0.2:
                notes.append(f"三轴比例较基线漂移 {drift:+.2f}"
                             f"（{'轴向占比升高，倾向不对中' if drift > 0 else '轴向占比下降'}）")

    # ③ 温升。没绑温度、或基线里没温度，就不给 —— 不拿 0 顶。
    base_temp = chans.get("temp")
    cur_temp = _window_mean(frame, "temp")
    if base_temp is None or cur_temp is None:
        out.append(Finding(key="temp_rise", value=None,
                           quality=Quality.NO_INPUT if cur_temp is None
                           else Quality.MODEL_NOT_LOADED, t=t))
    else:
        rise = cur_temp - float(base_temp["mean"])
        out.append(Finding(key="temp_rise", value=round(rise, 3), quality=Quality.OK, t=t))
        if rise >= 5.0:
            notes.append(f"温度较基线高 {rise:.1f}℃")

    # ④ 异常分：把上面几项合成一个 0~100 的**排序用**分数。
    #    ★不是概率、不是置信度。写清楚是因为一个 0~100 的数最容易被当成概率读。
    if not zs:
        out.append(Finding(key="anomaly_score", value=None,
                           quality=Quality.INSUFFICIENT_SAMPLES, t=t))
    else:
        z_part = min(1.0, max(0.0, max(zs.values())) / (2 * _Z_NOTABLE))
        got_drift = next((f for f in out if f.key == "ratio_drift"), None)
        d_part = min(1.0, abs(got_drift.value) / 0.5) if (
            got_drift is not None and got_drift.value is not None) else 0.0
        got_temp = next((f for f in out if f.key == "temp_rise"), None)
        t_part = min(1.0, max(0.0, got_temp.value) / 10.0) if (
            got_temp is not None and got_temp.value is not None) else 0.0
        score = 100.0 * (0.6 * z_part + 0.25 * d_part + 0.15 * t_part)
        out.append(Finding(key="anomaly_score", value=round(score, 1),
                           quality=Quality.OK, t=t))

    head = (f"基线采自 {model.get('t_from', '?')[:16]}~{model.get('t_to', '?')[:16]}"
            f"（{model.get('frames', '?')} 帧 {model.get('label', '?')}）")
    return out, head + ("；" + "；".join(notes) if notes else "；本帧未见相对基线的显著偏离")


def _bad_group(frame: Frame, keys: tuple[str, ...], q: Quality, t: datetime) -> list[Finding]:
    """给一组结论落同一个坏质量码。**值为 None** —— 坏质量下不许有值。"""
    return [Finding(key=k, value=None, quality=q, t=t) for k in keys]


def _all_bad(domain: Domain, frame: Frame, q: Quality, why: str) -> list[Finding]:
    """一条都算不出来时：**所有**声明过的输出各落一个坏值锚点，外加一句人话。

    ★不是"什么都不发"。什么都不发在下游看来是"这段没数据"，而真相是"这段算不出来"，
      两者的处置方向相反（查采集链路 vs 查模型与输入）。
    """
    t = frame.t_end
    keys = [o.key for o in domain.declare().outputs if o.key != "evidence"]
    out = [Finding(key=k, value=None, quality=q, t=t) for k in keys]
    # `evidence` 本身是能给的 —— 它说的正是"为什么没给"。质量码随大流，值照写。
    out.append(Finding(key="evidence", value=why, quality=q, t=t))
    return out
