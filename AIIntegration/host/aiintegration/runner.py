"""跑一次推理 —— 把模块的异常挡住，并**代它落锚点**。

★这里有一条必须先说死的语义：**模块算不出来时，骨架发坏值锚点，不是什么都不发。**

  "什么都不发"在下游看来是**"这段没数据"**，而真相是**"这段算不出来"**。
  两者对使用者的处置完全相反：前者去查采集链路，后者去查模型/输入。
  混在一起，现场只会看到一条断掉的曲线，然后往错的方向查。
  （与 `fetch.py` 那条"取不到就如实空着、不拿上一帧顶替"是同一条纪律的两端。）

★但**只在"模块没能完成"时代劳**（抛异常 / 超时）。
  模块**正常返回**了什么就是什么 —— 少给一条结论可能正是它的判断（本帧就不该有那条）。
  骨架替它补，等于替它发明语义，与"不替上游发明语义"同一条底线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import dataclasses as _dc

from .domains import LoadedDomain
from .quality import Quality
from .types import Finding, Frame, InferOut

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunResult:
    findings: list[Finding]
    ok: bool
    """模块本身是否正常跑完（`False` = 抛了异常，`findings` 是骨架代落的锚点）。"""

    error: str = ""

    state_error: str = ""
    """跨帧状态**没能存下去**的原因（空 = 没这回事）。

    ★与 `ok` 分开：模块本身跑完了（`ok=True`），是骨架**拒绝**落它给的状态
      （不是 JSON 对象 / 存不成 JSON / 超上限）。把它并进 `error` 会让调用方
      把一次正常推理当成失败，而结论其实是好的、该照写。
    """


def anchor_all(loaded: LoadedDomain, frame: Frame, quality: Quality) -> list[Finding]:
    """为该域**所有声明过的输出**各落一个坏值锚点，时刻取帧的右端。

    ★为什么是"所有输出"而不是"出错的那个"：模块整个没跑起来，谁也不知道
    哪几条本该有值。全落锚点 = 如实说"这一帧我一条都没算出来"。
    """
    return [
        Finding(key=o.key, value=None, quality=quality, t=frame.t_end)
        for o in loaded.declaration.outputs
    ]


def run_domain(loaded: LoadedDomain, frame: Frame, states=None) -> RunResult:
    """跑一次 `infer`，并校验它的产出。

    `states`：跨帧状态的存放处（`Workbench`）。**给了才有状态**：
      骨架在这里把上一拍的状态读出来塞进帧，算完再把模块带回的新状态存回去。

    ★**回溯判别那条路故意不传** —— 它问的是"当时若用现在的模型会怎样"，
      既不写回实时库，也**不许动线上那份状态**：拿历史片段去推一遍就把
      "算到哪儿了"覆盖成几个月前，而线上这条诊断还在跑。与"不写回实时库"同一条理由。
    """
    stateful = bool(getattr(loaded.declaration, "stateful", False))
    if stateful and states is not None:
        # 读不出来当作没有（`get_domain_state` 自己记错）—— 退化成从头攒，
        # 而不是让这条诊断从此一拍都跑不了。
        rec = states.get_domain_state(loaded.key, frame.binding)
        frame = _dc.replace(frame, state=rec.state if rec is not None else {})
    elif frame.state:
        # ★没声明 stateful 却拿到了状态：只可能是调用方塞的。清掉 ——
        #   否则"声明了才给"这条就成了一句空话，而域作者会依赖上一个没声明的偶然。
        frame = _dc.replace(frame, state={})

    try:
        raw = loaded.instance.infer(frame)
    except Exception as exc:  # noqa: BLE001 —— 模块的异常绝不许掀翻骨架
        logger.exception("域 %s 推理抛异常（绑定 %s）：%s", loaded.key, frame.binding, exc)
        # ★状态**原样不动**：这一拍什么都没算出来，凭什么改"算到哪儿了"。
        return RunResult(anchor_all(loaded, frame, Quality.COMPUTE_ERROR), False, repr(exc))

    new_state = None
    if isinstance(raw, InferOut):
        new_state = raw.state
        raw = raw.findings
        if new_state is not None and not stateful:
            # 没声明就不给存。这不是小气：状态每拍写库、跨重启留着，
            # 声明是它与骨架之间唯一的约定，默许等于让代价悄悄长出来。
            logger.error("域 %s 返回了跨帧状态，但它没有声明 stateful=True —— 已丢弃",
                         loaded.key)
            new_state = None

    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        logger.error("域 %s 的 infer() 返回了 %s，应为 Finding 列表或 InferOut",
                     loaded.key, type(raw).__name__)
        return RunResult(anchor_all(loaded, frame, Quality.COMPUTE_ERROR), False,
                         f"infer() 返回类型错误: {type(raw).__name__}")

    declared = loaded.declaration.output_keys()
    kept: list[Finding] = []
    for f in raw:
        if not isinstance(f, Finding):
            # 不接受"看起来像结论"的东西 —— 那正是 V/Q/T 被绕过的方式。
            logger.error("域 %s 返回了非 Finding 对象 %s，已丢弃", loaded.key, type(f).__name__)
            continue
        if f.key not in declared:
            # ★拒收而不是静默丢：未声明的结论没有对应的点，写不进去；
            #   静默丢则模块作者永远不知道自己写错了名字。
            logger.error("域 %s 产出了未声明的结论 %r（已声明的：%s），已拒收",
                         loaded.key, f.key, sorted(declared))
            continue
        if not (frame.t_start <= f.t <= frame.t_end):
            # T 必须落在本帧覆盖的区间内 —— 落在区间外就不是"这一帧的结论"。
            logger.error("域 %s 的结论 %s 时刻 %s 落在帧区间 [%s, %s] 之外，已拒收",
                         loaded.key, f.key, f.t.isoformat(),
                         frame.t_start.isoformat(), frame.t_end.isoformat())
            continue
        kept.append(f)

    # ★骨架硬规则：**这一帧没有任何可信输入，就不许出质量 OK 的结论**。
    #
    #   来由（2026-09-11 对原 v5 真跑实测）：阶次规则诊断在总幅值为 0（=没有数据）时
    #   把 1X 占比写死成 100%，结果判出「正常 0.71」，支持理由里还写着"1X 占比很高"——
    #   **从没有数据里编出一个健康结论**。这类错单看结论完全像真的，事后查不出来。
    #
    #   新写的两个域自己守住了这条，但骨架此前**不查**：下一个域作者照样可能写出来。
    #   所以落在骨架：违规的 OK 结论改落坏质量（值清空），并大声记错。
    #
    #   ★判据只看"整帧有没有可信输入"（任一 OK 样本 / 任一图片）。它挡的是**完全没数据**
    #     这一类，挡不住"必填那一路坏了、别的路还好"——那一类仍要靠域自己看质量码。
    violation = ""
    if not _has_trusted_input(frame):
        offending = [f.key for f in kept if f.quality.is_good()]
        if offending:
            bad_q = Quality.INPUT_BAD if _has_any_sample(frame) else Quality.NO_INPUT
            violation = (f"域 {loaded.key} 在没有任何可信输入时给出了质量 OK 的结论 {offending}，"
                         f"已改落 {bad_q.value} —— 这是域的缺陷（从没有数据里给出了结论）")
            logger.error("%s（绑定 %s，帧 [%s, %s]）", violation, frame.binding,
                         frame.t_start.isoformat(), frame.t_end.isoformat())
            kept = [Finding(key=f.key, value=None, quality=bad_q, t=f.t)
                    if f.quality.is_good() else f for f in kept]

    state_error = ""
    if new_state is not None and stateful and states is not None:
        if violation:
            # ★刚刚才判定这个域**从没有数据里编出了结论**，就不让它把这一拍攒进状态。
            #   状态是会跨重启一直留着的：让一次已证实的缺陷沉淀进去，往后每一拍
            #   都带着它，而且再也看不出是哪一拍坏的。结论照常写（已改成坏质量），
            #   只是"算到哪儿了"停在上一拍。
            state_error = "本帧没有可信输入却出了 OK 结论，跨帧状态未更新（保留上一拍那份）"
            logger.error("域 %s 绑定 %s：%s", loaded.key, frame.binding, state_error)
        else:
            try:
                states.put_domain_state(loaded.key, frame.binding, new_state)
            except Exception as exc:  # noqa: BLE001 —— 存不下状态不该毁掉这一拍的结论
                state_error = str(exc)
                logger.error("域 %s 绑定 %s 的跨帧状态没存下（保留上一拍那份）：%s",
                             loaded.key, frame.binding, exc)

    return RunResult(kept, True, violation, state_error)


def _has_trusted_input(frame: Frame) -> bool:
    """整帧是否有任何可信输入：任一质量 OK 的测点样本，或任一图片类输入。"""
    if frame.blobs:
        return True
    return any(s.quality.is_good() for samples in frame.channels.values() for s in samples)


def _has_any_sample(frame: Frame) -> bool:
    """有样本但全不可信（INPUT_BAD）与根本没样本（NO_INPUT）要分开 —— 处置方向不同。"""
    return any(len(samples) for samples in frame.channels.values())
