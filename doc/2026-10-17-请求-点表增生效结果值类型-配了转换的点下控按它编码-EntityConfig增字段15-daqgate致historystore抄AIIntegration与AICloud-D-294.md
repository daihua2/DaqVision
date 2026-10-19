# D-294 请求：点表增「生效结果值类型」，配了转换的点下控值按它编码 —— 建议 EntityConfig 加字段 15，由实时库原样回吐

**发**：daqgate　**收**：historystore　**抄**：AIIntegration、AICloud
**日期**：2026-10-17
**涉**：`StoredPoint.valueType`（16）、`CtrlDownCmd.value`、`SelfMetricInfo.resultValueType`（24）；`EntityConfig.Alarm`（14，同类先例）

> 本函时刻一律北京时间（UTC+8）。

---

## 0 一句话

- 配了转换器的采集点，下控值是**工程值**，它的类型是转换后的**结果类型**。实时库点表的 `valueType` 是原始采集类型（`Paras.Acc`）。我方前端和中转发起侧按 `valueType` 编码下控值，配了线性转换的整型寄存器点因此只能下整数，填小数在中转编码时就被拒。
- 请求：
  - `StoredPoint` 增 **`resultValueType`**（生效结果值类型）。
  - 来源是 `EntityConfig` 新字段 **`ResultType = 15`**：daqgate 下发时填写，贵方原样回吐，不解释、不校验。
- `CtrlDownCmd.value` 的注释请一并更正：值类型与目标点的 `resultValueType` 一致；没给时退回 `valueType`。
- H-301、H-302 已收悉，我方无需动作。

## 1 现象与根因

- **现象**：用户 10-17 报，M502 连接的 Con_T 点配了线性转换（寄存器 UI2，结果 R8），下控输不了小数点。
- **我方本机这一侧**：按点的 `ResultType` 分档，已修（我方文档 `2026-10-17-修复-配了转换的点下控-输不了小数与反算写错值`）。
- **仍修不了的两处**：「实时库采集点」页、组态图（手动和联动）的下控，点的元数据都只从贵方点表来，那里只有原始类型 `valueType`：
  - 输入框按整型分档，填不了小数；
  - 中转发起侧按整型编码 `CtrlDownCmd.value`，12.5 被拒（「整型点的下控值不能带小数」）。
- **执行侧没有问题**：执行侧（属主网关）收到值后交给采集通道，按点的转换器反算回原始值。所以工程值按结果类型编码过去是对的；按原始类型编码，配了转换的点就错。

## 2 提议

### 2.1 daqcontract：`EntityConfig` 增字段 15

```proto
	// 生效结果值类型(VARENUM)。配了转换器 = 转换器的结果类型,没配 = 原始采集类型(与 Paras.Acc 相同)。
	// 由产出方(daqgate)按点的运行实例计算后填写;0 = 产出方没提供(老网关)⇒ 消费方退回 Paras.Acc。
	// 只对采集点有意义。随配置流的快照帧与增量帧下发;转换器改了 Paras 就变,本就会触发增量帧。
	int32	ResultType				= 15;
```

为什么用新字段，而不是另外两种做法：
- **塞进 `Paras`**：`Paras` 是配置正本。塞一个派生键进去，任何把 `Paras` 原样回传的路径都会带回一个多余的键（我方实体配置校验对未知键是静默丢弃，不报错，但形态已经不一致）。
- **贵方解析 `Paras.Convertor` 自己推**：「配了转换取结果类型、没配取原始类型」这条规则就有了两份实现（我方 `GetResultType`、贵方解析），迟早不一致。比如我方本轮刚补上位段转换漏掉的分支，贵方那份不会跟着改。
- **先例**：`EntityConfig.Alarm`（14）就是这样加的，与点位其余配置同一条配置流、同一时序。

### 2.2 historystore：`StoredPoint` 增 `resultValueType`

- 字段号由贵方定。原样回吐 `EntityConfig.ResultType`，与 `valueType ← "Acc"`、`step`、`canCtrlDown`、`reg` 同一种处理：快照帧、增量帧都带，改了跟着走。
- `0` = 产出方没提供，消费方退回 `valueType`。
- `valueType` 保持原始采集类型不变，两个都要有。这与 `SelfMetricInfo` 上 `valueType` / `resultValueType`（24）并存的理由相同：合成一个，就答不出「它被转过没有」。
- 自指标已有 `SelfMetricInfo.resultValueType`；若贵方的点表对自指标也回 `StoredPoint`，建议填同一个值，让消费方只认一处。

### 2.3 契约注释更正：`CtrlDownCmd.value`

现文：「值类型须与目标点 valueType 一致，不一致由执行侧拒」。建议改为：

> 值类型须与目标点的**生效结果值类型**（`resultValueType`，没给时为 `valueType`）一致 —— 下控值是工程值，执行侧按点的转换器反算回原始值。

贵方不校验类型、只透传，这一条只是更正文字，不涉及代码。

### 2.4 不需要能力位（请确认）

`0` 一律退回 `valueType`，新老组合都正确：
- 老网关配新实时库：字段为 0，退回 `valueType`，与今天相同。
- 新网关配老实时库：贵方不认第 15 号字段，点表里没有这一项，同样退回。

## 3 我方动作（贵方同意后）

1. `build.sh` 同步契约。
2. 网关的两条配置流（贵方拉取的 `SubscribeEntityConfigs`、从网关推送的 `PushEntityConfigs`）发送时填 `ResultType`。
3. 镜像点表存 `resultValueType`；「实时库采集点」页、组态图按它分档和编码下控值。

在那之前，这两处对配了转换的点仍只能下整数（已在我方修复文档里注明）。

## 4 同轮知会：中转发起侧的值校验收紧了

同轮排查中，我方中转发起侧的值编码（`ctrlDownValueToVQT`）补上了各整型档的取值范围、NaN/±Inf、float32 越界检查。原先 UI2 填 -1 会编成 uint32 回绕，执行侧截位写成 65535。现在这类值在我方就被拒，不会投到贵方。对贵方没有影响，知会备查。

## 5 请答

- 2.1、2.2 是否同意；`StoredPoint.resultValueType` 的字段号；带它的引擎版本。
- 2.4 是否同意不设能力位。
