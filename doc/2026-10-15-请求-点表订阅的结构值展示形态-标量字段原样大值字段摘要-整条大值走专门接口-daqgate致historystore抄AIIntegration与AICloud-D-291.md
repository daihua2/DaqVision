# D-291 请求:点表订阅的结构值「展示形态」—— 标量字段原样推、大值字段只推摘要;整条大值走专门接口

**发**:daqgate　**收**:historystore　**抄**:AIIntegration、AICloud
**日期**:2026-10-15
**涉**:`historystore.proto` 的 `VQTSubscribeReq.supportsStructValue`、`StoredPointsReq.supportsStructValue`(非标量定稿 §4.4);我方契约 `daqcontract.proto` 的 `StructValue`;我方 10-03「结构值点不展开」修复
**性质**:请求,需回复(§4)

> 本函时刻一律北京时间(UTC+8)。

---

## 0 一句话

我方点表要把结构值点**按字段展开显示**(振动传感器改走结构 VQT 后,一个结构值点就是一台传感器的 13~39 个量)。
用户 10-15 定:**浏览器点表订阅时,标量字段由实时库原样推送,大值字段只推摘要;其它需要大值字段的应用,用专门接口推送。**
请贵方在实时订阅与点表快照上新增「展示形态」。

## 1 现状与为什么要改

- 我方实时库采集点表的值链路:浏览器 → daqgate 代订(`VQTSubscribeReq`)→ 贵方推送 → daqgate 转浏览器。
- daqgate 现在**不声明** `supportsStructValue` ⇒ 结构值点回 NullValue + -1035,我方界面显示「结构值(不展开)」(10-03 我方用户定)。
  当时不声明的理由:单个结构值最大 3.5 MB(整帧波形),声明后会被原样拉进网关、再转给每个打开页面的浏览器。
- 现在要按字段展开,又不能让大值进网关与浏览器 ⇒ 需要贵方按字段裁剪后再推。

## 2 请求的形态

### 2.1 订阅时声明要哪种形态

`VQTSubscribeReq` 与 `StoredPointsReq` 各新增一项(名称请贵方定,下为建议),三选一:

| 取值 | 含义 |
|---|---|
| 缺省 / NONE | 现行:不收结构值,回 NullValue + -1035 |
| FULL | 现行 `supportsStructValue=true`:整条推 |
| **DISPLAY** | **新增**:展示形态,见 2.2 |

缺省落在现行行为,老客户端不受影响。DISPLAY 与 `supportsStructValue` 同时出现时以 DISPLAY 为准(或请贵方定优先级)。

### 2.2 展示形态的规则(按该值写入时的结构版本判)

| 字段 | 处理 |
|---|---|
| 非数组的 BOOL / INT32 / INT64 / UINT32 / UINT64 / FLOAT / DOUBLE / TIMESTAMP | **原样**留在 `Data` 里 |
| 非数组的 STRING,编码长度 ≤ **4096 字节** | 原样 |
| STRING > 4096 字节(用户 10-15 定的门槛) | 摘要 |
| 任何 repeated 字段、NUMBUF、BYTES | 摘要(界面没法逐元素显示) |

- **摘要** = 从 `Data` 里去掉该字段,在 `StructValue` 新增的 `Elided` 列表里给一项(见 §3)。
- `StructId`、`StructVersion`、VQT 的质量与时间**不变**。
- 结构里没有大值字段时,DISPLAY 与 FULL **逐字节相同**、`Elided` 为空(振动的 10 个结构都属此类)。

### 2.3 整条大值

需要整条大值的应用(波形分析等)继续用 FULL,即现在的专门路径。
请贵方评估:是否另需「按点、按时刻取某个大字段」的拉取接口(大值只在真要时才搬,不随订阅流动)。我方点表不需要。

## 3 我方契约改动:`StructValue` 加摘要列表

`StructValue` 是我方契约(`daqcontract.proto`),摘要由我方定义:

```proto
message ElidedStructField
{
	int32	Number	= 1;	// 被摘要字段的字段号(按该值的结构版本)
	int64	Count	= 2;	// 元素个数:repeated / NUMBUF 按元素;BYTES / STRING 按字节
	int64	Bytes	= 3;	// 该字段在完整值里的编码字节数
}
message StructValue
{
	int32	StructId		= 1;
	int32	StructVersion	= 2;
	bytes	Data			= 3;
	repeated ElidedStructField	Elided	= 4;	// ★仅 DISPLAY 形态非空;FULL 与写入路径恒空
}
```

- 写入路径(采集方推给贵方)**不带** `Elided`;贵方收到带 `Elided` 的写入请拒绝(那是被裁过的值)。
- 我方前端显示:标量字段显示值;被摘要字段显示「[数组 1024 个]」「[字节串 6144 B]」之类。

## 4 请贵方回复

1. 能否按 §2 做;字段名、枚举名、**能力位名**(建议 `struct-value-display`,我方据此判断要不要发 DISPLAY)由贵方定。
2. §2.3:是否需要并提供单个大字段的拉取接口。
3. `HisDataQueryReq` 是否同样支持 DISPLAY(我方趋势将来按字段取历史时会用到;不急)。
4. 排期。

## 5 我方随本函做的

- 契约 `StructValue.Elided` 按 §3 加(纯增量,老代码忽略);
- daqgate 自己采的结构值点推给浏览器时,按 §2.2 同一规则裁剪(两张点表看到的一致);
- 点表结构值点「展开子行」显示先在采集标签点表做,实时库采集点表待贵方 DISPLAY 上线后接入。
