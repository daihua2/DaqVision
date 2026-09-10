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


_TO_STATUS: dict[Quality, int] = {
    Quality.OK: STATUS_OK,
    # 上游断流 —— 语义与既有码逐字对上。
    Quality.NO_INPUT: STATUS_QUALITY_NOT_CONNECTED,
    # "不在服务中" —— 语义与既有码对上（该域此刻不提供服务）。
    Quality.MODEL_NOT_LOADED: STATUS_QUALITY_OUT_OF_SERVICE,
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
