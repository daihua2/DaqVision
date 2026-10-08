# 135 个振动诊断仓普查 —— 业内算法现状与「低频标量数据集」的结论

> 2026-09-17。对象：`D:\AIRef\projects\vibration-analysis-repos-all`（25 GB，137 条目 = 136 仓 + 1 索引）。
> 方法：全量扫 README 做关键词命中统计 → 再对候选逐个核实**实际数据文件**（关键词命中 ≠ 真有数据）。
> 标注：【实测】有出处可复核　【推断】未验证

## 0 两个问题的答案（先行）

| 问题 | 答案 |
| --- | --- |
| 业内振动诊断算法现状 | ★**频谱是绝对主流（56%）**，深度学习并不占优；ISO 绝对判级极少（6%） |
| ★**有没有低频标量数据集** | ★**没有**。但找到**一个低频三轴原始加速度数据集**（31.25 Hz，带标签），见 §3.1 |

## 1 业内算法现状【实测：135 个仓的 README 关键词命中】

| 算法 | 仓数 | 占比 |
| --- | --- | --- |
| **FFT / 频谱** | **76** | **56%** |
| 统计特征（RMS / 峭度 / 峰值因子 / 偏度） | 60 | 44% |
| **包络解调**（Hilbert） | **28** | 21% |
| 模态 / SHM | 25 | 19% |
| RandomForest | 23 | 17% |
| 物理模型（PINN / FEM / 转子动力学） | 20 | 15% |
| RUL / 预测性 | 16 | 12% |
| CNN | 15 | 11% |
| XGBoost / LightGBM | 14 | 10% |
| SVM | 13 | 10% |
| IsolationForest / 单类 | 10 | 7% |
| 阶次分析 | 9 | 7% |
| ★**ISO 标准判级** | **8** | **6%** |
| Transformer | 8 | 6% |
| 小波 / LSTM / AutoEncoder | 7 / 7 / 6 | ~5% |
| EMD / VMD | 1 | <1% |

数据集引用：**CWRU 29（21%，一家独大）**、IMS 8、Paderborn 7、XJTU 5、NREL/风电 5、
PRONOSTIA 3、MAFAULDA 3、MFPT 3、SEU/JNU 2、风机 SCADA 2、Z24 桥 1。

### ⇒ 三条对我方有意义的

1. ★**频谱 56% + 包络解调 21%，都要波形。** 这从业内分布的角度印证了我方的判断：
   **没有波形，可做的算法就只剩那一小块**（统计特征 + 传统 ML + 单类异常检测）。
2. ★**ISO 绝对判级只有 6%（8 个）**，而我方模块 1 的主体正是它。
   这不一定是坏事（我方是工程落地、不是发论文），但意味着**可借鉴的同类实现极少**，
   我方基本要自己走 —— 与 `原项目功能集成清单` 记的「模块 1 原项目实现度为零」互相印证。
3. ★**深度学习并不占优**：CNN 11% + LSTM 5% + Transformer 6%，加起来不如「统计特征 + 传统 ML」。
   与我方 RUL 实验的观察一致：**特征里已经含了物理，剩下的问题是「这一列是不是异常地大」**。

## 2 数据普查【实测】

135 个仓里**只有 27 个含 ≥1MB 的数据文件**，其余全是代码仓。按体量：

| 仓 | 体量 | 内容 | 对我方 |
| --- | --- | --- | --- |
| `MHFL-MCA-Codes-Datasets-and-Results` | **4.2 GB**（227 文件） | KAIST：`.tdms` **电流** + `.mat` **振动** | ★**多模态（电流+振动）**，但是波形 |
| `Real-Time-Anomaly-Detection` | 624 MB | CWRU `.mat` | 波形，已有 |
| `permas4edu` / `Micro-motion-SAR` | 322 / 174 MB | FEM 教学 / SAR 雷达 | 不相关 |
| `predictive-maintenance-mcp` | 101 MB | 我方已看过（ISO 表逐值对过） | — |
| `edge-ai-pdm` | 53 MB | ★名为 edge，实为 **CWRU `.mat`** | 波形 |
| **`gearbox`** | 53 MB（2 文件） | ★**三轴** `GVX/GVY/GVZ`，**12.8 kHz**，768000 行 = 60 s，健康 vs 齿断，2000 rpm / 20 Nm | 高频，但**三轴 + 明确工况 + 健康/故障对照** |
| ★**`AIoT_Vibration_Prediction_EdgeDL`** | 48 MB | ★★**见 §3.1** | ★**唯一的低频** |
| `bearing-fault-diagnostics` | 40 MB | 我方已看过 | — |

★**注意一条**：`edge-ai-pdm` 名字带 edge，实际数据是 CWRU 的 12k/48k 波形 ——
**"边缘/嵌入式"这个词不代表它吃标量**。我方第一轮扫描把它当候选，是关键词命中造成的假阳性。

## 3 ★低频标量数据集：结论

### 3.1 唯一的低频数据集：`AIoT_Vibration_Prediction_EdgeDL`

【实测】

| 项 | 值 |
| --- | --- |
| 文件 | `XYZ_ACC_Labeled_Data.txt`（22 MB）、`Accl_Labeled_Data.txt`（同样 22 MB） |
| 格式 | `机器id, 标签, 时间戳(ms), x, y, z` |
| ★**采样率** | ★**31.25 Hz**（时间戳间隔 249/249 全为 32 ms，完全均匀） |
| 行数 | **494,801** |
| 标签 | `Normal` 124051 / `Fault1` 113236 / `Fault2` 134414 / `Fault3` 123100 —— ★**四类均衡** |
| 量程 | 值域约 ±20（看着像 ±20 g 的 MEMS 加速度计） |
| 项目 | STM32 + X-CUBE-AI 边缘部署（`app_x-cube-ai.c`、`data_Processor.c`、`model.h5` 仅 75 KB） |
| 模型 | CNN：maxpool → flatten(288) → dense(12) → dense(6)，自称准确率 >90% |

★**为什么它对我方特别**：31.25 Hz 的奈奎斯特频率只有 **15.6 Hz**，
**传统频谱诊断在这个采样率下根本做不了**（连 1× 转频都可能超出）。
⇒ 该项目必然只能靠**统计特征 + 学习** —— **这正是我方的处境**。
而且它**做成了并部署到了 MCU 上**，说明「低频不是死路」。

★**但它的局限必须说清**【实测，PDF 原文】：
项目是 **DC 电机**振动状态分类，六类为 `Normal / Idle / Fault1..Fault4`（数据文件里只有 4 类），
★**Fault1/2/3 没有任何物理定义** —— 文档里只当作「电机状态」，不是「不平衡 / 不对中 / 轴承」。

⇒ **它能验证的**：低频三轴信号有没有**可分性**（泛泛的状态区分）。
⇒ **它验证不了的**：**具体故障类型**能不能判 —— 而这正是我方 L2 要回答的。

### 3.2 ⇒ 严格意义的「低频标量数据集」：没有

我方要找的是「**传感器算好的标量**（每轴 加速度/速度/位移/主频 + 温度）+ 故障标签」。
135 个仓里**一个都没有**。原因也清楚：

★**公开数据集都由研究者采集，而研究者会采原始波形** —— 标量是**工业传感器为了省带宽在内部算掉的**，
这个环节发生在现场，不发生在实验室。⇒ **这类数据在公开世界里基本不存在**，
要么自己采，要么像我方在 FEMTO / MAFAULDA 上做的那样**从波形降维模拟**。

## 4 带回的两条可借鉴（都有实测数字）

### 4.1 ★★持续计数：第二份独立实测，且数字比第一份更硬

`acousticpinn-machine-failure-prediction` 的对照表【实测，README 原文】：

| 阈值 | 连续次数 | **误报 / 月** | 失效前中位预警 |
| --- | --- | --- | --- |
| 95th 百分位 | 1 | **315** | 46.7 h |
| 95th 百分位 | ★**3** | ★**0** | **29 h**（48 h 中的 29 h） |

> "requiring consecutive exceedances **works, and cheaply**: three in a row removed every false alarm in 1,728…"

★**而且它验证了另一条路走不通**：单纯提高阈值不管用 —— 从 95th 提到 99.9th 百分位，
**分位数已经超出数据范围**。

另有 `Tier3_PdM_System` 独立实现了同一思路（`temporal_persistence_gate.py`，
"Suppresses transient false alarms by requiring repeated anomaly confirmation"）。

⇒ 连同 `bearing-condition-monitoring`（滞回 + 3-of-5 → alarm flapping 归零），
**现在有三份独立来源指向同一条**，而我方 `external-projects §9` 的待办 1 **至今没做**。
★**这已不是"可以考虑的优化"，是代价极小、三方验证过的必做项。**

### 4.2 ★ISO 静态阈值的局限：有专门研究，结论对我方模块 1 直接适用

`Anomaly_Detection_in_Wind_Turbines_using_VAE_and_IsolationForest`【实测，README 原文】：

> - ISO thresholds performed well **under ideal conditions**.
> - VAE-IF detected **significantly more faults** than ISO thresholds,
>   showing better adaptability to **noise and real-world variability**.

它用 NREL 750 kW 风机齿轮箱（40 kHz）与 Nordex N131 实机（12.8~25.6 kHz）对照，
专门评估 **ISO 10816-21** 静态 RMS 阈值作为异常检测的效果，并有 `iso_evaluation.py`。

⇒ ★**这正是我方「①判据（ISO）+ ②学习（基线偏离）」两层并存的独立佐证** ——
而且它说明**②不是可选项**：静态阈值在真实工况下**漏检明显**。
（该仓只有代码，`.parquet` 数据未随仓下载。）

★另记一条：该项目的实机传感器是「**每 6~12 小时采一个短快照**」，
与我方 `AI-43 §3` 对 historystore 说的「波形不是连续采的，是按节拍采一帧」是同一形态。

## 5 ⇒ 给我方的三条

1. ★**要验「标量能不能判故障类型」，仍应用 MAFAULDA，不是 AIoT 那个** ——
   AIoT 虽是低频，但 `Fault1/2/3` 无物理定义；MAFAULDA 的 6 类（不平衡 / 水平不对中 /
   垂直不对中 / 欠悬轴承 / 过悬轴承 / 正常）**有明确物理定义**，才答得了 L2 那个问题。
   代价是 MAFAULDA 为 51.2 kHz，需按我方口径降维 —— 而这套降维方法我方在 FEMTO 上已跑通。
2. ★**AIoT 那份可作为「低频可行性」的旁证**：31.25 Hz、奈奎斯特 15.6 Hz、频谱诊断完全做不了，
   而它做成了 4 类分类并上了 MCU。⇒ **低频不是死路**，但它证明的是"能分状态"，不是"能判故障类型"。
3. ★**持续计数（§4.1）现在就该做** —— 三份独立来源、代价极小、不依赖任何新数据、不依赖任何人。

## 6 ★ISO 判级那 8 个仓：名单与逐个核实（2026-10-08 补）

> 原普查只留了计数、没留名单。目录已挪到 `D:\download\AIRef\projects\vibration-analysis-repos-all`（现为 136 个仓），
> 按 `scan_repos.py` 同口径（同样的 README 选取、同一条正则 `ISO ?10816|ISO ?20816|ISO ?13373|ISO ?2372`）重扫，
> README 命中仍是 **8 个**，与 §1 对得上；再扫各仓源码，看是不是真的判级。【实测】

| 仓 | README 提到 | 源码里实际做的 | 对我方 |
| --- | --- | --- | --- |
| ★`rotordynamics-vibration-toolkit` | ISO 10816 | `data/standards/iso10816-3_zone_limits.csv` + `vibtool/io.py::classify_vibration`：**按组 × 支承查表判 A/B/C/D**，宽带速度 RMS | ★**12 个边界值与我方 `_MACHINE_LIMITS` 逐值相同** —— 继 `predictive-maintenance-mcp` 之后第二份独立佐证 |
| `predictive-maintenance-mcp` | ISO 20816 | `iso20816.py`，组 × 支承查表 | 早已逐值对过，一致（external-projects §10.1） |
| `PRISM` | ISO 20816 | `physics/iso_levels.py`：带通 10~1000 Hz 后算速度 RMS 判 A/B/C/D；边界在 `config.yaml` 里**只有一组** `2.8 / 7.1 / 18.0`，自注「provisional，待按机型与版本确认」 | ⚠️ 不分组与支承，那组数是 ISO 2372 / 10816-1 Class Ⅳ 的值（【推断】），不是 10816-3。可借鉴的是**先带通到 10~1000 Hz 再算 RMS** |
| `maintenance-os` | ISO 20816 | `inference/iso.py`：**由模型预测的严重度映射成区**，不看振动值 | ✗ 名为 ISO 区，实为分类结果改名 |
| `Anomaly_Detection_in_Wind_Turbines…` | ISO 10816 | `iso_evaluation.py`：写死 B/C = 0.765、C/D = 1.223（ISO 10816-21 风电），拿来**评估**静态阈值的漏检 | 结论见 §4.2：理想工况好、真实工况漏检明显 |
| `Anomaly-detection-bearing-faults-PolitoTestRig` | ISO 20816 | 源码里没有 ISO 字样；README 称拿「ISO 20816 固定阈值法」与 ML 对比 | 只是对照组，未见限值表 |
| `industrial-machinery-vibration-fault-diagnosis-lstm` | ISO 10816 | 源码里没有；README 只是行文提到 | ✗ |
| `predictive-maintenance-demo` | ISO 13373 | `features.py` 引它说明包络解调流程 | ✗ 13373 是监测程序，不是限值 |

README 没提、源码里出现 ISO 的另有 3 个：`Advanced-Jeffcott-Rotor-Dynamics-Simulator`（打印一句「会超 ISO 10816 报警限」）、
`abla-rotor-analytics`（★**明确不做绝对判级**：「无通用限值，真实报警按机按点由工程师定」，改为对自身基线趋势 —— 与我方第 ② 层同一思路）、
`signalmap`（注释里一句「ISO 10816 建立在 RMS 上」）。

⇒ **真正按标准查表判级的只有 2 个**（`rotordynamics-vibration-toolkit`、`predictive-maintenance-mcp`），**两个的表都与我方逐值相同**；
**泵（ISO 10816-7 / GB/T 6075.7）一个实现都没有** —— 我方泵限值仍只有 Europump 转引一个来源（`C-43 §4`），原文未核。
另：上一级 `projects/` 下的 `node-red-contrib-condition-monitoring`（Class Ⅰ~Ⅳ 冒充 10816-3，external-projects §2.3）不在这 136 个里。

## 7 ★按标准号搜 GitHub 与网上（2026-10-08）

> 关键词：`10816-3` / `20816-3` / `10816-7` / `20816-7` / `GB/T 6075` / `iso10816` / `iso20816` / `pump vibration iso 10816`。
> 走 GitHub 公开仓库搜索 API（未登录，**代码搜索未做** —— 要登录，待用户 `gh auth login` 后补）+ 网页搜索。
> 候选浅克隆到 `D:\download\AIRef\projects\iso-grading-repos\`，**只读不执行**，逐个扫源码；无实质的已删，留 5 个。【实测】

### 7.1 结论

| 问题 | 答案 |
| --- | --- |
| 有没有泵判级（ISO 10816-7 / 20816-7 / GB/T 6075.7）的开源实现 | ★**没有**。`10816-7`、`20816-7`、`GB/T 6075` 仓库搜索均 0 条；网页搜索同样没有 |
| 工业机器表（10816-3 组 × 支承 × 速度） | 完整实现的 4 个（下表）**全部与我方 `_MACHINE_LIMITS` 逐值相同**；另有十来个只写死其中一行 |
| 泵限值出处 | ★**Europump《Pump Vibration Standards Guidelines》2013 原文已读**（第 9、14 页）：12 个值与我方 `_PUMP_LIMITS` **逐值相同**，含 >200 kW 的 C/D 7.6 / 9.5。其致谢页称标准摘录经 BSI 授权取自 BS EN ISO 版。**仍非标准正文** |

### 7.2 留下的 5 个

| 仓 | 做了什么 | 对我方 |
| --- | --- | --- |
| ★`jmrplens/phonometry`（`src/phonometry/vibration/machinery/evaluation.py`） | ISO 10816-3:2009 表 A.1/A.2 **速度 + 位移两套**；§5.2.3 两者都测取较严的一档；§5.3 显著变化 = 超过 B 区上限的 25%；§5.4.1 ALARM = min(基线 + 0.25 × B 上限, 1.25 × B 上限)；§5.4.2 TRIP ≤ 1.25 × C 上限；边界值归低一档；731 条符合性检查 | 速度表与我方相同。★**位移有效值表（µm）**我方没有：第 1 组刚性 29/57/90、柔性 45/90/140；第 2 组刚性 22/45/71、柔性 37/71/113（【引】该库转录，标准原文未核）。★**准则 II（变化量）是有条款依据的偏离判据**，我方自训「偏离 ≥ 3」无标定 |
| `kazubazoo/smartpulse365`（`pdm-frontend/src/lib/standards.js`） | 10816-1 Class Ⅰ~Ⅳ 与 20816-3 组 × 支承两套可选，另可按设备填自定义限值 | 20816-3 四行与我方相同 |
| `davidsanvel88-sys/Analizador-Motores-Siemens-ISO10816`（`src/analyzer.py`） | 组 × 支承四行，组合非法即报错 | 相同；非法组合不猜，与我方同 |
| `Firercrow/PROJECT-Farseer`（`src/normas.py`） | 按功率 300 kW 分组 × 基础四行 | 相同 |
| ✗ `ggkjyai04-oss/Vibration_analysis_260922_v2.0`（`src/utils/vibrationAnalysis.ts`） | 组 1/2 速度四行正确；**另有「加速度限值」和「Group 3/4 泵」两套数** | ★**反例留档**：10816-3 没有加速度限值；Group 3/4 泵是 1998 版旧分组，所给数值与 10816-3、10816-7 都对不上 —— 疑为生成内容。拿网上的表别不核就用 |

已删（无实质）：只写死一行的 `conveyor-digital-twin`、`MechPulse`、`iot-predictive-maintenance` 等；用旧 Class Ⅰ~Ⅳ 的 `VibraGuard-Pro`；`biw00/predictive-maintenance-pump`（622 MB 多为自带 venv，只一个 notebook 一行限值）；空仓 `IS0-10816-Vibration-Sensor`；其余 README 提及而源码无表的。

### 7.3 ISO 10816-7:2009 官方样章（iTeh，第 1~3.4 条，无附录 A）里对我方有用的原文

- §3.1 / §3.2.1：★**转速 < 600 r/min 的泵，另须测峰峰值位移（µm）**，测量下限 2 Hz。⇒ 我方 `vibration.py` 判泵时注释「泵标准不限转速」、不出低速提示 —— **与原文不符**（待用户定改）。
- §1：不适用于固体输送、渣浆、潜水泵；整体式电机泵按整机评价，柔性联轴的电机归 10816-3。
- §3.3.2：表 A.1、A.2 适用于所有轴承的径向振动与推力轴承的轴向振动。
- 引言：限值来自约 1500 台泵的统计；刚性 / 柔性支承、卧式 / 立式**无显著差别**，故不分；按 200 kW 分档。
- 目录：附录 C（资料性）给 ALARM / TRIP 设定示例。
