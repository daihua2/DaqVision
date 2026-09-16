# 外部振动诊断项目参考

> 2026-09-17 拉取。目的：看别人怎么做，找可借鉴的与该引以为戒的。
> **落点**：`D:\AIRef\projects\`（★**不入库** —— 别人的代码不进我方仓，只把结论写在这里）。

## 0. 清单

| 项目 | 大小 | 性质 | 值得看什么 |
| --- | --- | --- | --- |
| [`node-red-contrib-condition-monitoring`](https://github.com/blanpa/node-red-contrib-condition-monitoring) | 111.5 MB | ★**工程件**（Node-RED 节点包，15 个节点，约 1.5 万行 JS） | 与我方处境最像：标量输入 + ISO 判级 + 健康指数 + 异常检测 + LLM |
| [`bearing-vibration-diagnostics-toolbox`](https://github.com/andrek10/bearing-vibration-diagnostics-toolbox) | 5.2 MB | 博士论文产出的工具箱（VibPy） | 轴承诊断的算法实现 |
| [`DL-based-Intelligent-Diagnosis-Benchmark`](https://github.com/ZhaoZhibin/DL-based-Intelligent-Diagnosis-Benchmark) | 1.1 MB | 学术基准（252 个 py） | ★**揭示论文准确率虚高**的那套评测纪律 |
| [`bearing-condition-monitoring`](https://github.com/riya0920/bearing-condition-monitoring) | 1.3 MB | 小而专 | 包络解调 + BPFO/BPFI + 健康指数 + **滞回** |
| [`Vibration-Based-Fault-Diagnosis-with-Low-Delay`](https://github.com/Western-OC2-Lab/Vibration-Based-Fault-Diagnosis-with-Low-Delay) | 0.5 MB | IEEE TIM 2022 论文代码（全是 notebook） | 小波包 + FFT |

★**许可各不相同，商用前要逐个核**。本文只记"我方看到什么、学到什么"，不拷代码。

---

## 1. 模块划分：它按算法切，我方按输入切

`node-red-contrib-condition-monitoring` 的 15 个节点：

```
signal-analyzer   anomaly-detector   isolation-forest-anomaly   pca-anomaly
health-index      trend-predictor    ml-inference               training-data-collector
llm-analyzer      condition-monitoring-source   multi-value-processor
image-source      image-preprocess   vision-annotator           json-source
```

**三处对照**：

| | 它 | 我方（`doc/模块划分.md`） |
| --- | --- | --- |
| 异常检测 | ★**按算法切成三个节点**（通用 / 孤立森林 / PCA） | **按输入切**，算法是模块内的选择 |
| 健康指数 | `health-index` **独立节点** | 在标量判据模块里（第①层 `iso_zone` + 第②层 `anomaly_score`） |
| LLM | ★**`llm-analyzer` 是独立节点** | **不是模块**，是跨模块解释层 |

**我方仍坚持按输入切**，理由在 `doc/模块划分.md §20.1`：`declare()` 是静态自述，
角色集决定前端渲染的绑定表单；按算法切会让**同一组输入配出三条绑定、台账填三遍**。
★ 但要承认：在 Node-RED 里"按算法切"是自然的 —— **它的节点是流程图里的一个框，本来就没有"绑定"和"台账"这两层**。
⇒ **划分方式跟着宿主形态走，不能照抄。**

★**`llm-analyzer` 那条值得再想一遍**：它把 LLM 做成节点，是因为在流程图里"把上一步的结果喂给 LLM"就是一个框。
我方判定 LLM 不是模块（不吃测点、不绑设备），**这个判断不变**，但它提示了一件事：
LLM 那一层将来要有**自己的配置入口**，而不是塞进某个模块的参数里。

---

## 2. ★ISO 判级：三处对照，两处我方更严，一处该借鉴

`nodes/signal-analyzer.js` 有完整的 ISO 实现，逐条比：

### 2.1 ★该借鉴：用**已知转速**做加速度→速度换算

```js
// v = a / (2πf)，f 由配置的轴转速换算（RPM → Hz）
const convFreq = node.shaftSpeed > 0 ? node.shaftSpeed / 60 : 50; // Hz
case "g":  return (rmsValue * 9810) / (2 * Math.PI * convFreq);
```

★**这与我方前两天走过的弯路正好反向**：我方曾设想用 `a/v`、`v/d` **从数据反推**等效频率，
在北自所实测数据上验证不成立（`a/v` 推出 3572 Hz，而 `freq` 通道报 25.3 Hz），已撤回。

**它的做法是对的**：转速是**台账里的已知量**，不是从数据推的量。
⇒ **我方若将来要支持"只有加速度、没有速度"的传感器，这条换算是可行路径，但前提是台账里要有 `rated_speed_rpm`。**
（改造方案 §1.5 的 L2 门槛本来就要"可信转速"，与此一致。）

### 2.2 我方更严：**它给了一个物理上错误的缺省值**

```js
const convFreq = node.shaftSpeed > 0 ? node.shaftSpeed / 60 : 50; // Hz
// 注释自陈：only when no shaft speed is configured do we fall back to a generic
// 50 Hz, which is physically wrong for other speeds.
```

★**它自己在注释里承认这个缺省是物理上错的，但还是给了。**

我方的规矩相反（`domains/vibration_lowfreq.py` 的 `ParamSpec`）：
`iso_group` / `mount_type` / `axial_axis` **一律没有缺省**，理由写在代码里 ——
「猜错会把**该停机**说成**可长期运行**，而且从数值上看不出来」。

⇒ **这条对照值得留着**：同一个位置，一边选了"给个能跑的缺省"，一边选了"宁可不出结论"。
我方选后者，并且现在有了一个现成的反面例子。

### 2.3 ★注意：它的分级表与注释对不上

代码注释写：

> Based on ISO 10816-3:2009 for industrial machines with rated power > 15 kW

但表是按 **Class I~IV** 给的：

| | A/B | B/C | C/D |
| --- | --- | --- | --- |
| class1（≤15 kW） | 0.71 | 1.8 | 4.5 |
| class2（15~75 kW） | 1.12 | 2.8 | 7.1 |
| class3（>75 kW 刚性） | 1.8 | 4.5 | 11.2 |
| class4（大机器柔性基础） | 2.8 | 7.1 | 18.0 |

★**Class I~IV 是 ISO 2372 / 10816-1 的分类**；**ISO 10816-3 用的是 Group 1/2 × 刚性/柔性**
（我方表就是后者：`("2","rigid") = 1.4/2.8/4.5`）。
⇒ **注释声称的标准与实际用的表不是同一份。** 判据的出处写错，比判据本身错更难查 ——
因为看代码的人会以为它对过标准。

★**我方对照自查过**：`_ISO_LIMITS` 的表与注释都是 10816-3 的 Group × 支承方式，一致。

### 2.4 一处一致：**不适用就说不适用**

```js
if (inputUnit === "raw") {
    return { zone: "N/A", isValid: false,
             recommendation: "ISO 10816 not applicable - input unit is raw/dimensionless..." };
}
```

⇒ 与我方"缺什么就落什么码、并在 `evidence` 里写清为什么"是同一个做法。**这一条两边想到一块儿了。**

---

## 3. ★健康指数：一个我方没有的想法 —— **按传感器可信度动态加权**

`nodes/health-index.js` 的 `calculateDynamicWeight()`：合成健康指数时，**每一路的权重不是固定的**，
而是按这一路自身的"可信度"动态调整，三个因子：

| 因子 | 做法 |
| --- | --- |
| **异常率过高** | 近 N 拍里这一路报异常超过 30% ⇒ 降权（`×(1-(rate-0.3))`） |
| **噪声过大** | 变异系数 `CV = σ/|μ|` > 0.5 ⇒ 降权（最多降到 0.5） |
| **上游给的置信度** | 直接乘 |

权重下限钳到 0.1，不会归零。

★**这个想法我方没有**。我方第②层的 `anomaly_score` 是**人工定的加权和**，每一路权重固定。

**值不值得学，要分两面看**：

- ✅ **合理的内核**：一路传感器如果一直在报异常、或噪声大得离谱，**它多半是坏了，不是设备坏了**。
  拿它去拉高整机健康分，等于让坏传感器决定停不停机。
- ⚠️ **但它绕过了我方一条纪律**：我方的做法是**坏值整笔剔除、并落坏质量码说清楚**，
  而不是"降权让它少说话"。降权是**把"这一路可能坏了"这个事实藏进了一个连续系数里** ——
  界面上看不出来，只是分数变了一点。

⇒ **我方的取法**：内核可以学，形式不学。若将来要做，应当是
**"判定这一路不可信 ⇒ 落坏质量码 + 在 `evidence` 里点名是哪一路"**，
而不是悄悄降权。★**可见的降级优于不可见的加权。**

★另注：这个动态权重要**跨拍攒状态**（近 50 个值、异常计数），而我方模块是**无状态**的
（`infer(frame)` 每拍只给一个窗口）。要做就得先有"跨帧运行状态"那一格 ——
与 `README §11.3` 不做趋势外推是同一个前置。

---

## 4. 学术基准那条：评测纪律

`DL-based-Intelligent-Diagnosis-Benchmark` 的价值不在模型，在**它把几十篇论文的方法放到统一划分下重跑**，
结论是很多"高准确率"是划分方式造成的。

⇒ 与我方在 `AI-41 §1.2` 对 AICloud 提的加强条（**第③层不能只报准确率，要每类精确率/召回率与混淆矩阵**）
是同一条纪律。**我方那条不是自创，是这条线上的共识。**

★ 若将来做可分性实验（MAFAULDA 那条），**划分方式必须按转速/工况留出，不能随机切窗** —— 出处就是这里。

---

## 5. 小结：拿走什么、不拿什么

| | 结论 |
| --- | --- |
| ★**拿**：用**已知转速**做 a→v 换算 | 前提是台账要有 `rated_speed_rpm`；与改造方案 L2 门槛一致（§2.1） |
| ★**拿**：传感器可信度的**内核** | 但形式改成"落坏码 + 点名"，不做隐形降权（§3） |
| ★**拿**：统一划分下的评测纪律 | 按工况/转速留出，报每类指标（§4） |
| **不拿**：按算法切模块 | 它的宿主是流程图，没有绑定与台账两层；我方按输入切的理由不变（§1） |
| **不拿**：物理上错误的缺省值 | 我方"没有缺省"的规矩不动（§2.2） |
| ★**引以为戒**：注释声称的标准与实际用的表不一致 | 判据的出处写错比判据错更难查（§2.3） |
