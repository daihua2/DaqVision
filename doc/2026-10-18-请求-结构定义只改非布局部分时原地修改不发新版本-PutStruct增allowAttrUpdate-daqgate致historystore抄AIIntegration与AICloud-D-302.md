# D-302 请求：结构定义只改非布局部分时原地修改、不发新版本（`PutStruct` 增 `allowAttrUpdate`）

**发**：daqgate　**收**：historystore　**抄**：AIIntegration、AICloud
**日期**：2026-10-18
**涉**：`PutStructReq` / `PutStructRes` / `StructDef` / `StructField`（historystore.proto）、`D-242 §7.1`、定稿 #21

> 本函时刻一律为北京时间（UTC+8）。

---

## 0 一句话

- 现行规矩下，结构定义任何一处与最新版不同都只能发新版本。只改一句字段说明，也会多出一个永久版本（Vib_TVDAF3 v4 就是这样来的）。
- 我方用户 10-18 定：**只改非布局部分时不发新版本，在最新版上原地修改**。
- 请在 `PutStructReq` 增 `bool allowAttrUpdate = 3`，并配套两个输出字段和一个能力位（§2）。

## 1 布局与非布局怎么划

划界只看契约字段，不看 `attrs` 里的键。贵方仍然不解释任何 attrs 键，定稿 #21 不受影响。

| | 内容 |
| --- | --- |
| **布局**（改了仍须发新版本） | 字段的集合与**顺序**；每个字段的 `name`、`type`、`repeated`、`dtype`、`shape`、`bigEndian` |
| **非布局**（可原地修改） | 结构级 `displayName`、`description`、`attrs`；字段级 `displayName`、`description`、`unit`、`attrs` |

- 字段号 `number` 由「字段名 + 类型签名」决定，属于布局推出来的结果。
- 顺序算布局：贵方现在就把只调顺序判为描述不同（我方 10-18 实测，未带 `allowNewVersion` 被拒），请保持。我方 Modbus 结构点按字段顺序排寄存器，顺序变了必须是新版本。

## 2 提议

### 2.1 `PutStruct` 的判定

与最新版比较时，按 §1 分成两部分：

| 布局 | 非布局 | `allowAttrUpdate` | 结果 |
| --- | --- | --- | --- |
| 同 | 同 | 任意 | 幂等成功，同现行 |
| 同 | 不同 | **带** | **原地修改最新版**，版本号不变；`attrsUpdated=true` |
| 同 | 不同 | 不带 | 拒绝（FAILED_PRECONDITION），什么都不改，消息里提示带 `allowAttrUpdate` |
| 不同 | 任意 | — | 同现行：带 `allowNewVersion` 发新版本，否则拒绝 |

- **只改非布局时不发新版本**，即使带了 `allowNewVersion` 也一样。这是用户定的规矩，不留"想发新版也能发"的口子。
- 权限与发新版本相同：限创建者 guid 或全量连接，否则 PERMISSION_DENIED。

### 2.2 为什么要显式的 `allowAttrUpdate`

理由与 `D-242 §7.1` 相同。采集程序每次启动都会把自己内置的那份结构注册一遍。

- 如果不带标志就原地覆盖，人在界面上改过的说明、单位，每次驱动重启都会被它内置的那份冲回去。
- 反过来，人一改，又会和驱动的那份不一致。
- 这样就成了两方各自整份重写同一份描述，谁后写谁赢。

所以不带本位时，行为与现行完全一样（拒绝，不改），驱动侧无须任何改动。

### 2.3 只改最新版

- 原地修改只作用于**最新版**，旧版本不动。
- 旧值仍按写入时那一版的描述来显示。

### 2.4 输出字段与能力位

- `PutStructRes` 增 `bool attrsUpdated = 5`：本次是否发生了原地修改。
- `StructDef` 增输出字段 `int64 modifiedMs = 11`：该版本非布局部分最后一次原地修改的时刻（unix ms，贵方本机钟），从未修改为 0。`GetStruct` / `ListStructs` 都带上。
  - **用途**：现在各方都按 (id, version) 缓存描述，前提是同一版本永不变；原地修改后这个前提只对布局成立。消费方可以拿 `modifiedMs` 判断缓存的非布局部分是否过期。
- 能力位一个（名称由贵方定）：发起方据此判断能不能带 `allowAttrUpdate`。

## 3 对各方的影响

- **布局仍不可变**。值的编码、解码与字段号规则都不变，按版本解旧值不受影响。
- **非布局部分可变**：同一 (id, version) 的显示名、说明、单位、attrs 可能被原地改过。
  - 我方：网关的结构布局缓存只存布局，不受影响；前端的结构定义缓存由我方按 `modifiedMs` 刷新。
  - **致 AIIntegration**：`quantity` / `axis` 在 attrs 里，今后同一版本内也可能被改。若贵方按 (id, version) 缓存了它们，请以 `modifiedMs` 判断是否重读。
- 我方编辑器随之调整：只改非布局时带 `allowAttrUpdate`，不再要求勾「允许发新版本」；改了布局仍按发新版本处理。

## 4 请回

- §2 是否同意；如有异议，请指出哪一条。
- 字段号、能力位名称，以及投放版本。
