# aiintegration.proto 变更记录

> 规矩：**字段编号只增不改不复用**；废弃用 `reserved`；改契约必须升 `proto_version` 并记在这里，
> 且**改即投分发点并发函**（AI-9 §2.3、C-9 §2.1）—— 不投就会出现"我方以为改了、贵方发版还是旧的"，
> 而且不报错。

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
