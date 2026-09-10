# AIIntegration —— AI 项目群接入 AICloud 的集成层

> 新建于 2026-09-10。**这里放新代码；AISERVER 上原有的四个 AI 项目一律不动**（用户定）。

## 1. 为什么另起一个目录，而不是改造原项目

原有四个 AI 项目（v4 高频振动 / v5 低频振动 / meter-helmet 视觉 / VFD 变频器）都在跑、都有人在用，
现状与问题见 [`../doc/AI四项目源码实测与AICloud整合预研.md`](../doc/AI四项目源码实测与AICloud整合预研.md)。

**用户裁定（2026-09-10）：**

1. **原有的不要动** —— AISERVER 上四个项目保持现状，不改造、不停服。
2. **走"乙 · 新的统一 AI 服务"** —— 四类能力在本目录里**重新落地**，
   原项目退化为**参考实现 / demo**（不是被适配的算法后端）。
3. **算法本体不由集成侧从零发明** —— **从原项目拷过来，或由原作者重写后交付**。
   ⇒ 本目录的职责是**骨架、契约、数据面、平台接入**；算法是填进骨架的模块。
   ⇒ 因此"算法模块的边界（进什么、出什么、谁管生命周期）"必须先定，否则原作者无从下手。

```
原有四项目（保持不动，退为参考实现）
                                    ┌──────────────────────────────┐
       平台 / 现场数据 ────────────► │  AIIntegration（本目录）      │ ──► AICloud / historystore
                                    │  统一的新 AI 服务             │
                                    └──────────────────────────────┘
```

> ⚠️ **记在案的后果**：选乙意味着同一套算法会在新旧两处并存。必须在某个时点明确
> "原项目停止演进"，否则两边一定漂 —— v4 与 v5 已经是先例（v5 是 v4 的分叉，
> 两边的 `order_diagnosis` 已经不一致）。时点待定。

## 2. 部署落点

| | 路径 |
| --- | --- |
| 源码（本仓） | `daqvision/AIIntegration/` |
| 运行代码（AISERVER） | **`/home/Project/AIIntegration`** |
| **往来文档**（协调文/回执/知会） | **`AISERVER:/home/Project/AIIntegration/docs/`**（用户定 2026-09-10；命名规范见该目录 README，编号前缀 `AI-`） |

> 注意 AISERVER 上 `/home/ruiteng` 与 `/root/ruiteng` 是两份真实副本（不是软链），
> 原有项目的运行态在 `/root` 那份下；本目录与它们**没有任何路径交集**，别混。

## 3. 与 daqvision 既有件的关系

- [`../proto/vision.proto`](../proto/vision.proto) 是已与 daqgate 谈定的视觉侧契约（proto_version 1.0，
  6 条约束见 [`../exchange/`](../exchange/)）。**本目录不推翻它**；若需要泛化到"输入是测点"的场景，
  走升版流程（CHANGELOG + proto_version），不另起一套。
- [`../vision-infer/`](../vision-infer/) 是视觉推理 Sidecar 的 STUB，属算法侧；本目录属集成侧，两者分工不同。

## 4. 目录规划

待逐项讨论定案后再落代码。当前只建目录与本说明，**不预写投机性代码**。

讨论清单见预研文档 §7；已定的条目会回填到这里。

| 议题 | 状态 |
| --- | --- |
| 原有四项目是否改造 | ✅ **已定：不动**（2026-09-10） |
| 算法本体从哪来 | ✅ **已定：从原项目拷 / 原作者重写后交付**（2026-09-10） |
| 骨架边界 | ✅ **已定：推理 + 训练面**；标注/训练集/模型工件由本目录自持（用正经的库，不重蹈 v5 的 JSON 全量读写）。界面由 AICloud 做（2026-09-10） |
| 结论以什么身份进平台 | ✅ **已定：甲 —— 直连 hs 当一个"源"**（2026-09-10），见 §5 |
| daqvision 主分支命名 | ✅ **已定：保持 `master`**（2026-09-10） |
| `doc/` 二进制原件是否入库 | ✅ **已定：不入库**（2026-09-10） |
| 其余（平台里的身份类别 / 算力放置 / LLM 出网 / 高频波形进不进 hs / 原项目何时停止演进） | 待逐项讨论 |

## 5. 接入 historystore 的既有机制（2026-09-10 现场核实，非纸面）

选了"甲"之后，路径比预期现成得多 —— **不需要求 daqgate 发 guid 或证书**：

| 环节 | 机制 | 出处（AISERVER 实测） |
| --- | --- | --- |
| **写值** | `rpc PostVQT(daq.VQTs) returns (daq.Status)` | `historystore/proto/historystore.proto` |
| **声明结论点** | `rpc PushEntityConfigs(stream EntityConfigPush)` —— 边缘主动推，载荷**不带 guid**，归属由 hs 按连接的 peer cert 定 | 同上 |
| **身份** | mTLS 客户端证书的 SAN URI `urn:daqgate:guid:<uuid>`，hs 的 `GuidFromCert` 从中剥 guid；**证书就是身份，应用层不能自报** | `AICloud/AIBackend/irtdb/cert.go` |
| **签发** | AICloud 用自己 `cert/` 下的 `protocolCA.cer` + `ca.key` 直接签客户端证书，与 daqgate 同一套 CA | 同上；两文件已确认在 `/home/Project/AICloud/AIBackend/cert/`（`ca.key` 600） |
| **自动成为实体** | AICloud 对账器每分钟扫镜像，出现的任何 guid 自动建成实体，落 网关管理(58) 下、默认仅管理员可见、**绝不自动删** | `AICloud/AIBackend/irtdb/gatewayentity.go` |
| **可挂自述属性** | `GatewayIdentity.attrs` 是 `map<string,string>`，hs **只搬运不解释**、原样在 `ListEntityIdentities` 回 | `historystore.proto` |

> ⚠️ 由此引出的下一个待议点：对账器会把 AIIntegration 建成**"网关"实体**。
> 它不是网关（没有采集连接/采集通道），语义错位。`attrs` 那个自由 map 是现成的分流钩子，
> 但要不要用、由谁改对账器，待定。
