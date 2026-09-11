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
STATUS_QUALITY_BAD = -1000
STATUS_QUALITY_CONFIG_ERROR = -1001
STATUS_QUALITY_NOT_CONNECTED = -1002
STATUS_QUALITY_OUT_OF_SERVICE = -1007


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

    def to_status_code(self) -> int:
        """映射到 `daq.StatusCode`。见模块头：对不上的一律折叠成 QualityBad，不挪用。"""
        return _TO_STATUS[self]

    @staticmethod
    def from_status_code(code: int) -> "Quality":
        """**入向**：上游点值的状态码 → AI 侧质量。

        ★只分"好/不好"两档，**不试图把 hs 那四十来个码映射进我方这七档** ——
        那等于替上游发明语义（"设备故障"和"通讯失败"到了我方都只影响一件事：
        这笔输入不可信）。原始码由 `Sample.status_code` 原样留着，要细分的模块自己看。

        判据与 hs 一致：**只有 `Ok(1)` 算好**，其余一律不可信。
        """
        return Quality.OK if code == STATUS_OK else Quality.INPUT_BAD


_TO_STATUS: dict[Quality, int] = {
    Quality.OK: STATUS_OK,
    # 上游断流 —— 语义与既有码逐字对上。
    Quality.NO_INPUT: STATUS_QUALITY_NOT_CONNECTED,
    # "不在服务中" —— 语义与既有码对上（该域此刻不提供服务）。
    Quality.MODEL_NOT_LOADED: STATUS_QUALITY_OUT_OF_SERVICE,
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
