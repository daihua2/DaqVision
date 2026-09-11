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
| ① | ISO 10816-3 烈度判级 + 方向性倾向 | **本文件** |
| ② | 基线与趋势（μ/σ、EWMA 斜率、温升、剩余可用天数） | 待骨架给"域私有状态"的落点（要能备份/授权，不许模块自己存文件） |
| ③ | 多变量异常检测（IsolationForest 一类） | 依赖 ②的基线 |
| ④ | 证据包 + LLM 解释 | 依赖 ②③，且 LLM 出网待议 |

★①**不需要训练、不需要历史状态、不需要第三方库**，所以它能第一个落地，
  而且它恰好是文档里写的"最该先做的"：**把数值变成该不该停机**。

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
from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec,
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
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="人能直接读的一句话：用了哪几轴、判到哪一档、为什么"),
            ),
        )

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
