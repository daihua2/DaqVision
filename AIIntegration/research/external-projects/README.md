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

---

## 6. ★`bearing-condition-monitoring` —— 四个里质量最高的，有三条推翻我方现有做法

这个项目小（1.3 MB）但每一条结论都带实测数与"哪里做错过"。**值得整份读一遍。**

### 6.1 它也用合成数据，但目的与原四项目正相反

README 第一句就是：

> **The vibration is synthesised, not measured.** … It is **not CWRU and not IMS**,
> and no number here is comparable to a paper using those.

★**但它说清了为什么要合成**：合成买到的是 **ground truth** —— 故障类型、起始周期、失效周期，
以及**一批真正永不失效的资产**。有了它，"提前 79 周期检出"和"每资产寿命 0.33 次误报"
才成为**可评分的断言**，而不是"一张红点截图"。

⇒ 与我方 `doc/原四项目调查.md §18.7` 记的形成尖锐对照：
**原四项目也全是合成数据，但它们是"因为没有真数据"，这个项目是"为了拿到真标签"。**
同样是合成，一个是退路，一个是设计。★**而且它主动声明，原项目没有。**

### 6.2 ★它解释了"为什么原始频谱判不了轴承"——比我方文档深一层

> A defect impulse is broadband and small; what the accelerometer records is a
> **structural resonance being rung by it**. The fault information is in the
> *amplitude modulation* of a high-frequency carrier, **not in any line at BPFO**,
> and the low-frequency end of the raw spectrum is dominated by 1× imbalance,
> which is **10–100× larger** than anything the bearing is doing.

⇒ 所以必须：**带通共振带 → 希尔伯特包络 → 对包络做 FFT**。

我方文档此前只写"轴承要包络解调"，**没写为什么原始谱不行**。这一段可以直接引。
★它也说明了改造方案把 L3a/L3b 定得那么高不是保守 —— **轴承诊断在原始谱上就是看不见**。

### 6.3 ★★谐波碰撞：主判据 + 决胜判据的分层设计

对 SKF 6205 那个几何：**BPFO×3 = 10.754× 轴频，BPFI×2 = 10.830× 轴频，相差 0.70%** ——
落在任何真实检测器必须允许的滑移容差内。⇒ **单靠谐波能量无法区分外圈与内圈。**

分离靠**边带**，而且理由是**几何不是统计**：

- 外圈缺陷在载荷区**静止** ⇒ 冲击串幅度恒定 ⇒ BPFO 处干净的梳状谱；
- 内圈缺陷**随轴旋转** ⇒ 每转进出载荷区一次 ⇒ 冲击串被轴频调幅 ⇒ **BPFI ± 1× 轴频出现边带**。

★**关键在于它怎么用这条**：边带是**决胜（tie-break）**，不是**否决（override）**。

> An earlier version let any sideband energy above threshold win outright, and on a
> severe outer-race fault **it flipped the diagnosis to BPFI at the very end of life**:
> a strong fault lifts the whole envelope floor, so BPFI's exceedance crosses the
> detection threshold **on leakage alone** and its sideband ratio then gets computed on noise.

⇒ **可直接搬到我方的一条判据设计原则**：
**次级证据用来在两个候选之间分高下，不能用来证明"有两个候选"。**
我方第①层的 `direction_hint`（轴向/径向比判不对中 vs 不平衡）正是同一形状 ——
**比例是决胜，不该单独触发结论**。这一条要写进域注释。

### 6.4 ★★评测：在**匹配的误报预算**下比，不比准确率

它的交付物是一条**工作曲线**，不是一个准确率：

| 健康指数阈值 | 中位提前量（周期） | P05 提前量 | 每资产寿命误报次数 | 漏检 |
| --- | --- | --- | --- | --- |
| 95 | 199 | 178 | 8.00 | 0 |
| 85 | 78 | 74 | 1.33 | 0 |
| **80** | **76** | **70** | **0.00** | **0** |

> A lead time quoted **at** a false-alarm budget is the operational contract;
> a lead time quoted without one is **a number chosen after seeing the answer**.

★**三路检测器的对比也在同一误报预算下做**（Hotelling T² / IsolationForest / Autoencoder），
结论是 **"There is no winner, and that is the result."** —— 9 条轨迹上 1 个周期的差距是采样噪声。

> What I would ship is the **T²**: thirty lines, **no training step and therefore no
> retraining pipeline**, a score that **decomposes into per-feature contributions** so
> the alarm can be explained to the operator… The reason deep does not win is not that
> deep is bad; it is that **the features already contain the physics**.

⇒ ★**这段直接支持我方在 `doc/模块划分.md` 里的取舍**：判据类（①）与学习类（②）分开，
且优先判据。**特征里已经含了物理，剩下的问题只是"这一列是不是异常地大"，那是协方差的活。**
我方"自己实现轻量算法、不引 sklearn"的决定，在这里有了独立佐证。

### 6.5 ★★★三条"我做错过"，每条都打中我方

**① 解调带不能从健康数据里学出来。**

> every asset came back with the same meaningless 500–1062 Hz band… a healthy bearing
> produces **no impulses**, and a kurtogram with nothing impulsive to find returns
> whichever band the noise favoured.

⇒ 解调带是**结构的属性**（壳体共振），应由**投运时的敲击试验**确定 ⇒ **它是配置，不是学出来的**。
★与我方"台账参数没有缺省、必须有人填"是同一条道理的另一个实例。

**② ★绝对阈值不work，而且数字说明了为什么。**

> A healthy bearing's envelope-energy ratio in this simulation is already **~12× the
> broadband floor**… My first threshold of "4× the floor" **fired on everything** and
> classified healthy bearings as inner-race faults **with total confidence**.

⇒ 改法：**每个特征都表达为"相对这台资产自己的健康基线的超出量"，用稳健（IQR）单位**。

★★**这正是我方第②层在做的事**（相对这台机器自己的基线算 z 分数），**但我方用的是 μ/σ，它用的是 IQR**。
IQR 更稳健 —— 我方基线只有 10 帧样本，**一两个离群值就能把 σ 撑大**，从而让后续偏离显得都不显著。
⇒ ★**值得考虑把 `_mean_std` 换成中位数 + IQR**，或至少在样本少时并报两者。

**③ 冷启动要显式说"我不知道"。**

> an asset with too little history gets the `LOW_CONFIDENCE` state **explicitly**,
> not silence and not a number pretending to be calibrated.

⇒ 与我方 `Quality.LOW_CONFIDENCE`（已在 `quality.py` 里，但**目前没有任何域在用**）完全对应。
★**我方有这个码却没用起来** —— 基线样本不足时应当落它，而不是照常出一个 z 分数。

### 6.6 ★滞回 + 持续计数：我方完全没有

> **Alarm flapping: 0** across the whole fleet. **Hysteresis (separate enter and exit
> thresholds per state) plus 3-of-5 persistence** is what produces that. This is the answer
> to *"operators disabled the last vendor's system in six weeks"*: the vendor optimised
> detection and **never measured flapping**.

⇒ 两件我方现在都没有：

1. **进入阈值与退出阈值分开**（滞回）—— 否则值在边界上抖，结论就来回跳；
2. **5 拍里中 3 拍才算数**（持续计数）。

★**而且它把"抖动次数"当成一个要测量的指标**，不是副作用。
我方第①层的 `iso_zone` 是**每拍独立判**的：值在 4.49/4.51 之间抖，区就在 C/D 之间跳，
**趋势图上会看到一条锯齿**，而现场会因此不信任这个结论。

⇒ ★**这条建议列为我方待办**：`iso_zone` 与 `anomaly_score` 的报警态需要滞回与持续计数。
但注意：它要**跨拍状态**，而我方模块是无状态的（`infer(frame)` 只给一个窗口）——
与 `README §11.3` 不做趋势外推是同一个前置。

### 6.7 一条"没扛住测量"的说法，它照实报了

> The textbook says **kurtosis rises before RMS** on a degrading bearing. Measured here:
> median lead **14 cycles**, positive on **8 of 9** assets, **weaker than the textbook implies**,
> and I report it that way rather than trimming the table.

★**这个做法本身值得学**：教科书结论与实测不符时，**照实报并说清自己的局限**（它归因于自己仿真器的退化模型太平滑），
而不是修表。与我方"写错过的东西要承认并订正，不悄悄改掉"同一条。

---

## 7. `bearing-vibration-diagnostics-toolbox`（VibPy）：可做特征清单的对照

博士论文产出的工具箱，`pyvib/` 下 12 个模块。**`features.py` 那份时域特征清单可直接当对照表**：

`rms` / `kurtosis` / `standardmoment` / `absoluteMean` / `peakToPeak` / `squareMeanRoot` /
`waveformLength` / `willsonAmplitude` / `zeroCrossing` / `slopeSignChange` / `shapeFactor` /
`crestFactor` / `impulseFactor` / `clearanceFactor` / `skewnessFactor` / `kurtosisFactor` /
`rootMeanSquareFrequency` / `frequencyCenter`

★我方第②层现在只用了 **峰值** 与 **均值** 两种。这张表说明**同一段波形可派生的时域特征有近二十个**，
其中 `crestFactor` / `impulseFactor` / `clearanceFactor` / `kurtosis` 正是改造方案 **L3a（轴承冲击检出）**
点名要的那几个。

⇒ **但它们都要波形**（我方现在只有传感器算好的标量）⇒ 归 `doc/模块划分.md` 的模块③。
★这条进一步坐实了模块③的边界：**L3a 的特征在标量里拿不到，必须有波形。**

`diagnose.py` 里的 `diagnosefft(..., harmthreshold=3.0, subthreshold=3.0)` ——
**谐波阈值与边带阈值分开给**，与 §6.3 的"主判据 + 决胜判据"是同一结构。

---

## 8. `DL-based-Intelligent-Diagnosis-Benchmark`：三种划分方式的命名

它把数据集加载器按划分方式分成三套目录，**命名本身就是结论**：

| 目录 | 含义 |
| --- | --- |
| `R_A` | **R**andom split **+ A**ugmentation |
| `R_NA` | **R**andom split, **N**o **A**ugmentation |
| ★`O_A` | **O**rder split + Augmentation（`train_test_split_order()`） |

★**"order split"就是按工况/顺序留出**，不是随机切窗 —— 与我方 `AI-41 §1.2` 给 AICloud 提的加强条同源。
它覆盖 7 个公开数据集（CWRU / MFPT / PU / UoC / XJTU-SY / SEU / JNU），
**同一套代码在三种划分下各跑一遍**，这就是"很多论文准确率虚高"那个结论的来源。

⇒ 我方若做可分性实验，**划分方式要在报告里与准确率并列写出**，否则那个准确率无法比较。

---

## 9. 汇总：从四个项目带回来的待办

| # | 事项 | 出处 | 优先级 |
| --- | --- | --- | --- |
| 1 | ★`iso_zone` / `anomaly_score` 的报警态加**滞回 + 持续计数** | §6.6 | 高（现场会看到锯齿） |
| 2 | ★基线统计从 `μ/σ` 改（或并报）**中位数 + IQR** | §6.5② | 高（10 帧样本，σ 易被离群值撑大） |
| 3 | ★样本不足时落 `Quality.LOW_CONFIDENCE`（**码已有、无人用**） | §6.5③ | 高（改动小） |
| 4 | 在域注释里写明"**次级证据只做决胜，不做否决**"（`direction_hint`） | §6.3 | 中 |
| 5 | 评测改为**在匹配误报预算下报提前量**，不报准确率 | §6.4 | 中（做验收时用） |
| 6 | 台账增 `rated_speed_rpm`（a→v 换算 + L2 前置） | §2.1 | 中 |
| 7 | 文档里补"**为什么原始谱判不了轴承**" | §6.2 | 低 |

★**1~3 三条都不依赖新数据、不依赖传感器选型，是现在就能做的**。
但 1 要跨拍状态（与"骨架给不给跨帧状态"那一格绑在一起），2 和 3 纯域内改动。

---
---

# 第二批（2026-09-17，用户指名的 6 个）

| 项目 | 大小 | 值得看什么 |
| --- | --- | --- |
| ★[`LGDiMaggio/predictive-maintenance-mcp`](https://github.com/LGDiMaggio/predictive-maintenance-mcp) | 120.4 MB | **MCP 服务**，ISO 20816-3 判级；★**限值表与我方逐值相同**；三条我方没做的 |
| ★[`VictorBauler/awesome-bearing-dataset`](https://github.com/VictorBauler/awesome-bearing-dataset) | 40.2 MB | **28 个公开数据集**的清单；★**推翻我方"没有真实工业现场数据"的判断** |
| ★[`masha548/bearing-fault-diagnostics`](https://github.com/masha548/bearing-fault-diagnostics) | 40.5 MB | **只用时域标量特征**分轴承故障 —— 与我方处境最接近的一个 |
| [`biswajitsahoo1111/cbm_codes_open`](https://github.com/biswajitsahoo1111/cbm_codes_open) | 98.2 MB | 可复现的 CBM 方法集（notebooks） |
| [`ash-kev/Vibration-Fault_Detection`](https://github.com/ash-kev/Vibration-Fault_Detection) | 14.6 MB | 端到端系统（FastAPI + React + sklearn） |
| [`Xiaohan-Chen/bear_fault_diagnosis`](https://github.com/Xiaohan-Chen/bear_fault_diagnosis) | 0.3 MB | 多尺度 CNN+LSTM 论文基线（Keras→PyTorch） |

---

## 10. ★★`predictive-maintenance-mcp` —— 限值表与我方逐值相同，但它多做了三件

### 10.1 独立佐证：我方 ISO 表是对的

它的 `src/diagnostics/iso20816.py`：

```python
_ZONE_BOUNDARIES = {
    (1, "rigid"):    (2.3, 4.5, 7.1),   # Group 1 (>300 kW) 刚性
    (1, "flexible"): (3.5, 7.1, 11.0),
    (2, "rigid"):    (1.4, 2.8, 4.5),   # Group 2 (15~300 kW) 刚性
    (2, "flexible"): (2.3, 4.5, 7.1),
}
```

★**与我方 `domains/vibration_lowfreq.py` 的 `_ISO_LIMITS` 一字不差。**
一个带 DOI、带测试与覆盖率、明确引标准的项目独立给出同一张表 ⇒ **我方第①层的判据表可以放心。**

（对照 §2.3：node-red 那个项目用的是 Class I~IV，与它注释声称的 10816-3 对不上。**同一个判据，三方里两方一致、一方错。**）

### 10.2 ★我方欠的第一件：**标准版本已更新，我方引的是被取代的那版**

```
Zone boundaries from ISO 10816-3:2009 (four-zone A-D scheme).
ISO 20816-3:2022 supersedes that edition and merges zones A and B;
the A/B boundary is kept here for practitioner familiarity.
```

⇒ **ISO 10816-3:2009 已被 ISO 20816-3:2022 取代**，新版**把 A 区与 B 区合并成一个"可接受区"**。

它的处置值得学：**限值仍用 2009 版（因为从业者熟悉四区方案），但把这条出处说明
（`THRESHOLD_PROVENANCE`）附在每一个评估结果上**。

★我方现在只在代码注释里写"ISO 10816-3"，**结论里不带出处** ——
运维看到 `iso_zone=C` 不知道它依据的是哪一版标准。**这条要补。**

### 10.3 ★我方欠的第二件：**评估频带是判据的一部分，它写死在常量里**

```python
_ISO_BAND_UPPER_HZ = 1000.0        # ISO 20816-3 评估带上沿
_ISO_BAND_CLAMP_FRACTION = 0.95    # 数字滤波器上沿不能坐在奈奎斯特上
_ISO_MIN_FS_HZ = ceil(2*1000/0.95) # ≈ 2106 Hz：低于它就够不到 1000 Hz 带顶
```

★**它把"采样率够不够支撑 ISO 评估带"变成了一个可检查的前置条件。**

⇒ 这正对上我方在 `doc/传感器资料/README.md` 记的那条硬伤：
**有人 SVT10 的频响是 10~1600 Hz，而 ISO 要求 10~1000 Hz 带内的速度 RMS** ——
若传感器在整个响应带内算 RMS，报值偏高、判级偏严。
**我方只是记了这个问题，没有把它变成代码里的判据。**

### 10.4 ★我方欠的第三件：**适用范围下限会被拒绝，不是悄悄照算**

```
Scope: industrial machines with rated power above 15 kW…
When the machine power is declared and falls below 15 kW the assessment is
refused; when it is unknown the scope limit is documented but not enforceable.
```

★**三态处置**：功率已声明且 <15 kW ⇒ **拒绝评估**；功率未知 ⇒ **记录范围限制但不强制**；≥15 kW ⇒ 正常评估。

我方的 `iso_group` 选项里写着"3 组：泵类，独立驱动（**≥15kW**）"，**隐含了这个门槛但从不检查** ——
一台 5 kW 的小泵配成 3 组，我方照样出 `iso_zone`。⇒ **这一条要补，而且补法就照它的三态。**

### 10.5 ★★一条设计纪律，比我方表述得更好

> **the server refuses to guess**. No diagnosis is ever inferred from filenames or
> statistical parameters alone — **a fault indication requires matching spectral evidence**.
> Every severity claim cites ISO 20816-3, and **the evaluative wording in reports is
> authored by the server, not improvised by the model**. The AI orchestrates the analysis
> and presents the evidence… while **the final judgment stays with the engineer**.

三条与我方一致（不猜、要证据、结论带出处），但**第三条我方没有明确写过**：

★★**评价性措辞由服务端写死，不让模型即兴发挥。**

⇒ 这对我方**尚未开工的 LLM 解释层**是个关键原则：LLM 负责**组织与呈现证据**，
**不负责给出评价词**（"严重"、"建议立即停机"这类）。那些词必须来自判据层，
否则同一组数据两次问会得到两种说法。**这条现在就该写进 `doc/模块划分.md` 关于 LLM 那一格。**

---

## 11. ★★`awesome-bearing-dataset` —— 推翻我方"没有真实工业现场数据"的判断

28 个数据集的结构化清单（机构 / 年份 / 任务 / **故障生成方式** / **信号种类**）。

★**我方前两天说过"公开数据集全是实验台，没有真实工业现场的"。这个判断错了**，清单里至少两个直接相关：

### 11.1 ★`SCA Bearing Dataset`（Mittuniversitetet，2024）—— **真实工厂的自然故障**

| | |
| --- | --- |
| 来源 | **运行中的纸浆厂**（operational pulp mill），2019~2022 |
| 故障 | ★**Natural (Industrial)** —— 自然发生，不是人工制造 |
| 内容 | 11 个案例：有记录的轴承失效 + ★**1 个确认的非轴承故障（轴不对中）** |
| 结构 | **train 文件 = 健康数据；test 文件 = 走向失效的数据** |
| 带什么 | 信号、**时间戳**、★**转速**、**故障标签** |
| 工况 | 现场真实工况，**转速与负载变化很大** |

★**这是清单里唯一标注 "Natural (Industrial)" 的**，正是我方一直缺的东西。三点直接有用：

1. **train/test 结构天然适合单类异常检测**（只用健康数据建模）—— 与 §6 那个项目的方法、
   以及我方第②层"对着自己的基线比"是同一形状；
2. ★**带转速** ⇒ 支持 §2.1 学到的 a→v 换算；
3. ★**那 1 个轴不对中案例**对我方 `direction_hint`（轴向/径向比判不对中）有对照价值。

### 11.2 `Politecnico di Torino`（2024）—— **振动 + 温度 + 转速**三样齐全

中大型**球面滚子轴承**（重工业常用），人工缺陷，多工况。
★**清单里唯一同时有温度与转速的**，与我方 13 标量的构成最接近（我方有 `temp`）。

### 11.3 其余按"我方缺什么"筛出来的

| 数据集 | 为什么可能有用 |
| --- | --- |
| `PU Time-Varying Run-to-Failure`（2024） | 振动 + **温度**，自然加速失效 |
| `University of Ottawa`（2018/2023） | 振动 + 声 + **转速**，人工与自然故障都有 |
| `UOS / SDOL`（2022） | 振动 + **温度** |
| `KAIST`（~2023） | **电流** + 振动 + 转矩 —— 若将来做伺服/VFD 模块可对照 |
| `University of Arkansas`（2023） | ★**单故障与双故障** —— 现实里故障常常叠加，这类数据少见 |

⇒ ★**订正我方此前的说法**：不是"没有合适的数据集"，是**我方前两天只搜到了最常被引用的那几个**
（CWRU / MAFAULDA / PU / IMS）。**这份清单本身就是一件该早点找到的东西。**

---

## 12. ★`bearing-fault-diagnostics` —— 与我方处境最接近，而它的结论对我方不利

它**只用时域标量特征**（不碰波形）分四类轴承状态（正常 / 滚珠 / 内圈 / 外圈）：

```
max, min, mean, sd, rms, skewness, kurtosis, crest, form     ← 9 个
RandomForest(n_estimators=80, class_weight="balanced")
train_test_split(test_size=0.2, stratify=y) + StratifiedKFold(3)
```

★**乍看像是"标量也能分开故障"的正面证据，但仔细看恰恰相反**：

| | 它的 9 个标量 | 我方的 13 个标量 |
| --- | --- | --- |
| 来源 | **从波形算的时域统计量** | **传感器内部算好的物理量** |
| 含 `kurtosis`（峭度） | ✅ | ❌ |
| 含 `crest`（波峰因子） | ✅ | ❌ |
| 含 `skewness` / `form` | ✅ | ❌ |
| 含三轴速度/加速度/位移/频率 | ❌ | ✅ |

★★**峭度与波峰因子正是改造方案 L3a（轴承冲击检出）点名要的两个** ——
它们对**冲击**敏感，而轴承故障的本质就是周期性冲击（见 §6.2）。

⇒ **它能用标量分开轴承故障，靠的恰恰是我方没有的那几个标量。**
我方的 13 个里没有任何冲击敏感量 ⇒ **这是对"13 标量能不能分开五类"的一个有力的间接否定证据**，
与 `doc/模块划分.md` 里"第③层已定不做"的结论一致。

★**它的评测比 v5 强但仍不够**：有 `stratify` 与 `StratifiedKFold`（v5 是 `fit` 后自测），
但用的是 **random split 而非 order split** ⇒ 按 §8 的判据，准确率仍可能偏乐观。

---

## 13. 另外三个：扫过，价值有限

| 项目 | 看到什么 |
| --- | --- |
| `cbm_codes_open` | 目标是"**可复现**的 CBM 结果"，全是 notebooks + 项目页。方法学取向与 §8 那个基准一致，但没有可直接搬的工程件 |
| `ash-kev/Vibration-Fault_Detection` | 端到端系统（FastAPI + React + sklearn），形态完整但**没有超出前面几个的判据设计**；可作"系统长什么样"的参考 |
| `Xiaohan-Chen/bear_fault_diagnosis` | 多尺度 CNN+LSTM 的论文基线（已从 Keras 转 PyTorch）。★**它自己在 README 里说"Keras 与 TF 更新后大量 API 不可用，所以重写"** —— 与我方"不引重依赖、保持零依赖"的取舍互为印证：**框架会漂，判据不会** |

---

## 14. 第二批带回的待办（接 §9，编号续）

| # | 事项 | 出处 | 优先级 |
| --- | --- | --- | --- |
| 8 | ★结论里带**判据出处**（哪一版 ISO）；并记 20816-3:2022 已合并 A/B 区 | §10.2 | 高（改动小，运维直接受益） |
| 9 | ★把**评估频带**（10~1000 Hz）变成代码里的可检查前置，而不只是文档里的一条记录 | §10.3 | 高（现有传感器 10~1600 Hz 就踩这条） |
| 10 | ★`iso_group` 增**功率下限三态检查**（<15 kW 拒绝 / 未知记录 / ≥15 kW 正常） | §10.4 | 中 |
| 11 | ★LLM 解释层的原则：**评价性措辞由判据层给，模型只组织与呈现** | §10.5 | 中（现在写进设计，动工前定死） |
| 12 | ★取 `SCA Bearing Dataset` 做可分性/异常检测实验 —— **真实工业现场 + 转速 + 故障标签** | §11.1 | 高（它比 MAFAULDA 更贴近现场） |
| 13 | 台账增 `rated_power_kw`（ISO 适用范围要用） | §10.4 | 中 |

★**8~10 三条都是"把已知的判据前提变成代码里的检查"**，不依赖任何新数据，现在就能做，
与 §9 的 1~3 条合起来是一组**纯内功**的改进。
