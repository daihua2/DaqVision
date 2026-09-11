# aiintegration.proto 变更记录

> 规矩：**字段编号只增不改不复用**；废弃用 `reserved`；改契约必须升 `proto_version` 并记在这里，
> 且**改即投分发点并发函**（AI-9 §2.3、C-9 §2.1）—— 不投就会出现"我方以为改了、贵方发版还是旧的"，
> 而且不报错。

## 1.4 —— 2026-09-11

**动机**：视觉域（安全帽检测）落地。它的输入是**图片**，不是测点：不绑 globalId、
不在实时库里、什么时候来由现场决定。原有契约只有"角色 → globalId"一种输入形状（`AI-13 §3` 许下的那一类，现在由真域逼出来了）。

| 改动 | 说明 |
| --- | --- |
| `InputSpec.kind = 5`（新增 string） | `point`（测点）/ `image`（图片）。老版本空串按 `point` 理解；**不是封闭枚举** |
| 纯图片域的 `Binding.roles` 可以为空 | 只有**全部输入都是 `image` 类**的域才放行；测点域少绑一个角色照旧拒 |
| HTTP `POST /infer/{domain}/{binding}`（新增，不在 proto service 里） | 正文图片原始字节；**`X-Captured-At` 拍照时刻必填且带时区**；详见 proto 里 InputSpec 下方的说明 |

**三条使用说明**：

1. **绑定表单**：`image` 类角色不画"选测点"；但**绑定照样要建** —— 台账参数（如置信度阈值）与结论点都挂在绑定上，没有绑定的上传回 404。
2. **拍照时刻没有缺省**：巡检拍完可能半小时后才上传，拿上传时刻当结论时刻就是把"半小时前的违规"记成"现在的违规"。缺了 / 不带时区一律 400。★`Z` 结尾的写法我方已兼容（现场 Python 3.10 的标准库本身不认 `Z`）。
3. **`written=false` 请显示 `write_note`**：只读运行时结论只回给调用方、不进实时库，界面不该让人以为平台上已有这条记录。

**兼容性**：纯增量。老客户端看不到 `kind`，会把图片输入当测点角色画出来 —— 所以**贵方要按 `kind` 渲染绑定表单**，新域才可用。

## 1.3 —— 2026-09-11

**动机**：1.2 里说好"训练执行还没落，故意不给一个永远 pending 的 `StartTraining`"。现在落了。

| 新增 | 说明 |
| --- | --- |
| `rpc StartTraining(StartTrainingReq) returns (MutateRes)` | 建一条待跑任务**立刻返回** id，后台串行跑 |
| `rpc CancelTrainJob(IdReq) returns (MutateRes)` | 取消。★回执**如实说**实际发生了什么，见下 |
| `message StartTrainingReq` | domain / dataset_id / binding（空 = 全域通用模型）/ algo（**由域解释，骨架不枚举**） |

**三条使用说明**（写进 proto 注释了）：

1. **只对声明了 `train` 能力位的域成立**，其余当场拒 ⇒ 请按能力位渲染，别出训练入口；
2. **新工件不自动启用** —— 自动启用等于"训一次就换一次现场模型"，而训练常常只是试试看；
3. ★**"用了多少条样本"要显示出来**：样本记的是时间范围，训练时那段可能已被滚存删掉。
   取不到的不进训练集，但**丢了几条、为什么丢**写在 `TrainJob.message` 与工件 `meta_json` 里。
   「用 200 条训出来的」与「以为用 200 条、实际只用了 3 条」是两个模型，界面上却长得一样。

**`CancelTrainJob` 的回执分三种**，我方不笼统回"已取消"：排队中的当场取消；
**正在跑的只是"请求取消"**（算法不检查取消标志就会跑完当前这次训练才停）；已终态的回"不适用"。

**仍未开**：报告生成（等 LLM 出网定；存储与下载已就位）。

## 1.2 —— 2026-09-11

**动机**：AICloud `C-11` 看完 v4/v5/meter/VFD 四套界面后，给出**七类页面**清单，其中
4~7 四类（标注工作台 / 训练集与知识库 / 训练任务与模型工件 / 片段归档与报告）要我方出端点。

**新增 23 个方法**（纯新增，1.1 的一个字没动）：

| 组 | 方法 |
| --- | --- |
| 标注 | `ListAnnotations` `PutAnnotation` `AnnotateRange` `DeleteAnnotations` `ListLabels` |
| 训练集 | `ListDatasets` `PutDataset` `DeleteDataset` `CopyDataset` |
| 样本 | `ListSamples` `AddSamples` `RemoveSamples` `CopySamples` |
| 工件 | `ListArtifacts` `ActivateArtifact` `DeleteArtifact` |
| 训练任务 | `ListTrainJobs` `GetTrainJob` |
| 片段与报告 | `ListSegments` `PutSegment` `DeleteSegment` `ListReports` `RediagnoseSegment` |

**四条语义在库层落死**（不是靠调用方自觉）：① 标注 ≠ 样本；② 移出 ≠ 删除（`RemoveSamples`
只解关系，原始标注一动不动）；③ 标签不枚举（没有标签表，`ListLabels` 从数据聚合）；
④ 训练是可查询状态的任务，且**终态不可覆盖**。
外加我方一条：**样本上的标签是入集那一刻的快照**，不跟随标注改动。

**质量码订正**：`CONFIG_INCOMPLETE` 出向从 `-1007 QualityOutofService` 改为
**`-1001 QualityConfigError`**。理由见 `C-10 §2`（`-1007` 在 hs 读路径里已是"采集中断"，
与"去补台账"的处置方向正相反）。我方已核：hs 源码从没往数据面写过 `-1001`，这个码是干净的。

**本版故意没开的两件**（不是遗漏）：`StartTraining`（训练执行未落，
**不给一个永远 pending 的口** —— 那比没有更坏）、报告**生成**（等 LLM 出网定；
存储与下载已就位）。

**大对象 HTTP**：新增 `GET /reports/<path>`，与 `GET /artifacts/<path>` **走同一段代码**
（含路径穿越防护 —— 复制一份就等于给它一次退化的机会）。

## 1.1 —— 2026-09-11

**动机**：落第一个算法域（低频振动）时发现的真缺口 —— ISO 10816-3 烈度判级要知道
「机组功率等级 / 刚性还是柔性支承 / 哪一轴是轴向 / 速度口径是不是 RMS」。
这些是**每台机器一份的静态台账**，既没有时刻也没有质量码，**绑不到测点上**；
往实时库里塞一个恒定值的点，就是把台账伪装成测量。

| 改动 | 说明 |
| --- | --- |
| `Binding.params = 8`（新增 `map<string,string>`） | 台账参数的**取值**。骨架只搬运不解释（照 hs 的 `GatewayIdentity.attrs`） |
| `DomainInfo.params = 7`（新增 `repeated ParamSpec`） | 台账参数的**自述**。贵方按它渲染绑定表单 |
| `ParamSpec`（新增消息） | key/display/value_type(含 `enum`)/choices/choice_displays/default/required/unit/description |

**兼容性**：纯新增，老客户端忽略这三处即可正常工作 —— 但**绑定表单会缺字段**，
于是该域的部分结论会落坏质量码（不是猜一个缺省值）。⇒ 贵方要渲染 `params` 才能让新域真正可用。

★与能力位同一条道理：**前端按 `param_specs` 渲染，不按域名写死字段** —— 新增域两侧都不改。

## 1.0 —— 2026-09-10

首版（`AI-11`）。服务名 `aiintegration.AIIntegrationService`，7 个方法：
`GetInfo` / `ListDomains` / `ListBindings` / `PutBinding` / `DeleteBinding` / `SubscribeLogs` / `QueryLogs`。
