"""结论的质量码 —— AI 侧语义，以及它到 `daq.StatusCode` 的映射。

铁律（VQT 三元组）：结论必须带**真实质量**，不许把"算不出来"悄悄写成一个数。
出处是 v4 的教训：那边 `NaN/Inf` 被静默写成 `0.0`，坏值伪装成合法值。

★这里**不发明新的 StatusCode**。`daq.StatusCode` 是 daqgate 主管的公共契约，
  往里加码要走升版流程。故本模块的做法是：

  - AI 侧保留一套**语义清楚**的枚举（我方自己的库与界面用它，信息不丢）；
  - 出向映射到 `daq.StatusCode` 时，**只用语义确实对得上的既有码**；
  - 对不上的，一律折叠成 `QualityBad(-1000)`，**绝不挪用**别的码。

  挪用的后果是安静的：比如 `-1028 QualityStatisticsBad` 在现网已有既定含义
  （算术族补满栅格），拿它表达"AI 样本不足"不会报错，只会让对端按错的意思处置。

⇒ 下面标了「待批」的几档，等与 daqgate / AICloud 议定是否值得新增专用码；
  在那之前它们都落 `QualityBad`，**信息只在我方侧保留**。
"""

from __future__ import annotations

import enum

# daq.StatusCode 里我方用到的那几个（数值取自 daqcontract.proto，勿改）。
STATUS_OK = 1
STATUS_QUALITY_GOOD = 5000
STATUS_QUALITY_PENDING = 5004
STATUS_QUALITY_BAD = -1000
STATUS_QUALITY_CONFIG_ERROR = -1001
STATUS_QUALITY_NOT_CONNECTED = -1002
STATUS_QUALITY_OUT_OF_SERVICE = -1007
STATUS_QUALITY_MODEL_NOT_LOADED = -1034   # 2026-09-13 新增,见下方映射处的由来

# ── 入向：哪些码算「这笔输入可信」────────────────────────────────────────
# ★依据 `daq.StatusCode`，并经**现场实测**（2026-09-12，AISERVER 全量口回读 khb 组
#   gid 810/814/818/806）：实时库**点值的好码是 `QualityGood(5000)`**，
#   `Ok(1)` 属通用操作成功那一族。本模块此前只认 `1`，**把 5000 判成了坏** ——
#   后果是每一笔真实输入都被当不可信、结论每拍落坏质量。未上线前查出（见 AI-25）。
#   ⇒ 两个都留：`1` 有现网用法（别处的「原值 1=Ok」），`5000` 是点值实际用的。
# `5004 QualityPending`：实测**每个点的末拍恒为它**（未最终确认），值是真实采样、
#   不在 Bad 族里，故算可信。
# 其余 Uncertain 族（`5001/5002/5003/5005`、`6000`、`9000` 族）**一律不算可信** ——
#   它们语义各异，替上游发明「可不可信」不是我方的事；
#   原始码照旧原样留在 `Sample.status_code`，要细分的模块自己看。
TRUSTED_INPUT_CODES = frozenset({STATUS_OK, STATUS_QUALITY_GOOD, STATUS_QUALITY_PENDING})


class Quality(enum.Enum):
    """结论的质量。**没有"未知"这一档** —— 产出结论的地方必须知道自己算得准不准。"""

    OK = "ok"
    """算出来了，可信。"""

    INPUT_BAD = "input_bad"
    """输入里有坏值 —— 结论不可信。"""

    NO_INPUT = "no_input"
    """拿不到输入（上游断流 / 该时段无数据）。"""

    MODEL_NOT_LOADED = "model_not_loaded"
    """该域没有可用模型（没训练过 / 工件加载失败）。"""

    CONFIG_INCOMPLETE = "config_incomplete"
    """**台账参数缺失或非法**，这条结论的判据立不起来。

    ★与 `INPUT_BAD` 分开：那是"数据不可信"，去查设备；这是"没人填过这台机器的台账"，
      去补配置。两者的处置完全不同，混成一个码，现场只会往设备上查。

    ★与"给个缺省值算出来"分开：ISO 判级的边界取决于机组类别与支承方式，
      猜错会把"该停机"说成"可长期运行"，**而且从数值上看不出来**。
      由第一个算法域（低频振动）落地时发现并补上。

    ★出向映射 **`QualityConfigError(-1001)`**，不是 `-1007`。
      订正自 AI-12 §2.2 —— 那一版写的是复用 `-1007 OutofService`，**是错的**：
      `-1007` 在 hs 的读路径里已经是"采集中断的延续"（`historystore.proto` 那句
      「跨坏值/采集中断的延续 → 中断码(如 -1007)」），而"采集中断"恰恰是**要去查设备**的那一类。
      于是我方要求对端"把没填台账与设备坏了分开显示"，却给了同一个码 —— 自相矛盾。
      由 AICloud `C-10 §2` 逮到并给出取证（他们界面按码区间三分，`-1007` 落进"坏值"，
      与采集中断像素级一致）。**`-1001` 语义逐字对得上，且同样不必发明新 StatusCode。**
      已核：hs 源码从没往数据面写过 `-1001`（命中全在生成的 pb.h 里），这个码是干净的。
    """

    INSUFFICIENT_SAMPLES = "insufficient_samples"
    """样本不足，算了但不足以下结论。"""

    LOW_CONFIDENCE = "low_confidence"
    """算出来了，但置信度低于本域阈值。"""

    COMPUTE_ERROR = "compute_error"
    """算的过程中出错（模块抛异常等）。"""

    def is_good(self) -> bool:
        return self is Quality.OK

    def is_fault(self) -> bool:
        """这个码**算不算设备故障**。★界面据此决定渲不渲染成告警色。

        ★这一格是 AICloud `C-45 §3.6` 要来的，理由成立：`NO_INPUT`（没取到数）与
          `MODEL_NOT_LOADED`（没启用模型/基线）**不是设备坏了** —— 混成"异常"会让
          现场去查设备，而实际要查的是采集链路或工件页，方向正好反了。

        ★判据是"**该去查设备吗**"，不是"好不好"：
          `INPUT_BAD` 算故障（数据不可信，多半传感器/链路有问题），
          `CONFIG_INCOMPLETE` **不算**（没人填台账，去补配置），
          `COMPUTE_ERROR` 算（我方的模块出错了，要有人看）。
        """
        return self in _FAULT_CODES

    def to_status_code(self) -> int:
        """映射到 `daq.StatusCode`。见模块头：对不上的一律折叠成 QualityBad，不挪用。"""
        return _TO_STATUS[self]

    def display(self) -> str:
        return _DISPLAY[self]

    def hint(self) -> str:
        """现场该查什么。空 = 不必查（`OK`）。"""
        return _HINT[self]

    @staticmethod
    def from_status_code(code: int) -> "Quality":
        """**入向**：上游点值的状态码 → AI 侧质量。

        ★只分"好/不好"两档，**不试图把 hs 那四十来个码映射进我方这七档** ——
        那等于替上游发明语义（"设备故障"和"通讯失败"到了我方都只影响一件事：
        这笔输入不可信）。原始码由 `Sample.status_code` 原样留着，要细分的模块自己看。

        判据见 `TRUSTED_INPUT_CODES`：`Ok(1)` / `QualityGood(5000)` / `QualityPending(5004)`
        算好，其余一律不可信。★**不要写成 `code == 1`** —— 实时库点值的好码是 5000，
        只认 1 会把全部真实输入判成坏（2026-09-12 实测查出）。
        """
        return Quality.OK if code in TRUSTED_INPUT_CODES else Quality.INPUT_BAD


_TO_STATUS: dict[Quality, int] = {
    Quality.OK: STATUS_OK,
    # 上游断流 —— 语义与既有码逐字对上。
    Quality.NO_INPUT: STATUS_QUALITY_NOT_CONNECTED,
    # 模型/基线未加载 —— **专用码**（daqgate 2026-09-13 应 H-233 新增，D-231 取甲）。
    # ★由来：此前落 -1007(QualityOutofService)，而那个码已被占了两次 ——
    #   historystore 用它做「采集中断锚点」（读路径契约还带着行为约定"断线，别连过去"）、
    #   daqgate 用它表示「通道退出服务」。三个事实共用一个码 ⇒ 归因不可解：
    #   现场看到 AI 结论点写着「质量坏[服务退出]」，会去查采集链路，而采集完全正常。
    #   （2026-09-13 01:00 第一个绑定建成后，现场真出现了 4 个这样的点。）
    # ★切换前置（D-231 §1.4）：**消费端先升、生产方后改**。proto3 枚举开放，老 daqgate
    #   收到 -1034 只会显示「未知状态码:-1034」= 换一种误导。AISERVER 的 daqgate 已升
    #   v1.0.10-1265（D-233 已回执"AI 侧可以切了"），本映射才改。
    Quality.MODEL_NOT_LOADED: STATUS_QUALITY_MODEL_NOT_LOADED,
    # 台账没配全 ⇒ "配置错"，语义逐字对得上。★不是 -1007：那个码在 hs 读路径里
    # 已经是"采集中断"，与本档的处置方向（去补配置 vs 去查设备）正相反。见枚举处。
    Quality.CONFIG_INCOMPLETE: STATUS_QUALITY_CONFIG_ERROR,
    # ↓ 以下【待批】：无语义确实对得上的既有码，一律折叠 QualityBad，不挪用别的码。
    Quality.INPUT_BAD: STATUS_QUALITY_BAD,
    Quality.INSUFFICIENT_SAMPLES: STATUS_QUALITY_BAD,
    Quality.LOW_CONFIDENCE: STATUS_QUALITY_BAD,
    Quality.COMPUTE_ERROR: STATUS_QUALITY_BAD,
}

# 折叠掉的那几档 —— 供自检与将来发函议定专用码时对照。
COLLAPSED_TO_BAD = tuple(
    q for q, code in _TO_STATUS.items() if code == STATUS_QUALITY_BAD
)


# ═════════════════ 质量码字典（契约 1.9，给 GetInfo 用）═════════════════
#
# ★为什么放骨架而不是每个域各报一份（AICloud `C-45 §3.6`，我方定）：
#   质量码是**骨架定义**的，不随域变 —— 每个域回一遍只会重复八份一样的表，
#   还给了它们各自改口径的机会。前端取一次缓存即可。

#: 哪些码**是设备故障**。判据见 `Quality.is_fault` 的文档。
_FAULT_CODES = frozenset({
    Quality.INPUT_BAD,
    Quality.COMPUTE_ERROR,
})

_DISPLAY: dict[Quality, str] = {
    Quality.OK: "正常",
    Quality.INPUT_BAD: "输入坏值",
    Quality.NO_INPUT: "无输入",
    Quality.MODEL_NOT_LOADED: "未启用模型/基线",
    Quality.CONFIG_INCOMPLETE: "配置不全",
    Quality.INSUFFICIENT_SAMPLES: "样本不足",
    Quality.LOW_CONFIDENCE: "置信度低",
    Quality.COMPUTE_ERROR: "计算出错",
}

_HINT: dict[Quality, str] = {
    Quality.OK: "",
    Quality.INPUT_BAD: "采集链路 / 传感器",
    Quality.NO_INPUT: "采集链路（该时段无数据或上游断流）",
    Quality.MODEL_NOT_LOADED: "工件页启用模型，或先采基线",
    Quality.CONFIG_INCOMPLETE: "配置界面：必填参数或采集点没配齐",
    Quality.INSUFFICIENT_SAMPLES: "取数节拍与窗长",
    Quality.LOW_CONFIDENCE: "结论仅供参考；样本或工况可能超出模型适用范围",
    Quality.COMPUTE_ERROR: "我方模块出错，请提工单",
}

# 漏一个就当场炸，而不是等 GetInfo 少回一条（少回的那条前端会当成"这个码不存在"）。
assert set(_DISPLAY) == set(Quality), "质量码字典漏了：%s" % (set(Quality) - set(_DISPLAY))
assert set(_HINT) == set(Quality), "质量码字典漏了：%s" % (set(Quality) - set(_HINT))
