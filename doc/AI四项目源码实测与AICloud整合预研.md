# AI 四项目 —— 源码实测与 AICloud 整合预研

- 编写日期：2026-09-10
- 分析对象：AISERVER（`10.126.126.11` / Tailscale `100.107.230.13`）上在跑的四个 AI 项目
- 分析方式：**现场读源码 + 实测运行态**（进程 / 端口 / InfluxDB 实际数据 / nginx 实配），非纸面推测
- 目的：为"用 daqvision 预案把四个 AI 项目整合进 AICloud 云平台"做前期摸底
- 前置文档：
  - [AI振动诊断v4_数据流分析与整合预研.md](AI振动诊断v4_数据流分析与整合预研.md)（2026-08-12，只覆盖 v4）
  - [AI振动诊断_算法侧改造方案_hs供数daqgate建模.md](AI振动诊断_算法侧改造方案_hs供数daqgate建模.md)（2026-08-12，传感器能力分级）
  - [视觉算法gRPC交互方案_v1.md](视觉算法gRPC交互方案_v1.md) + [`proto/vision.proto`](../proto/vision.proto)（daqvision 预案本体）

> **本文只到"现状 + 待定项"为止。** 方案本身按用户要求**逐项讨论后再写**，
> 不在本文一次性铺开。§7 是讨论清单，讨论出结论的条目回填到本文或另起决策文。

---

## 0. 一句话结论

四个项目在"AI"这一侧差别不大，**差别全在数据面的形态**：v5 已经从 historystore 取真 VQT，
meter/helmet 根本没有数据面，而 v4 与 VFD 各自养着一个自造数据的 InfluxDB。
所以"集成度低"的根因不是页面没挂进菜单——**是这四个服务的输入和输出都不在平台的实体模型里**，
平台看不见它们吃什么点、吐什么结论，于是趋势、报警、租户隔离、上云一样都做不了。

---

## 1. 实测口径

| 项 | 值 |
| --- | --- |
| 采集时刻 | 2026-09-10 16:37~16:47（北京时间），主机 UTC `2026-09-10 08:47:11` |
| 登录方式 | `ssh aiserver`（密钥，走 `100.107.230.13`，见 `运维/主机/AISERVER-10.126.126.11.md` §1.1） |
| 证据来源 | `ps -eo pid,etime,rss,cmd`、`ss -lntp`、`/proc/<pid>/cwd`、InfluxDB `/query`、`/etc/nginx/snippets/`、源码直读 |

⚠️ **一处路径陷阱（先说，免得后面对不上）**：`/home/ruiteng` 与 `/root/ruiteng` 是**两份真实副本**，
不是软链、不是 bind mount（`stat` 出的设备号与 inode 都不同：`/home/…/v4` = `2051:1972045`，
`/root/…/v4` = `2050:1194029`）。

- 在跑的 v4 是 **`/root/ruiteng/2026/ai_diagnosis/v4`**；`/home` 下那份是**陈旧副本**。
- v5、VFD 在 `/home` 下，但都借 **`/root/…/v4/.venv`** 这个虚拟环境启动。

⇒ 动 v4 之前必须先确认改的是哪一份；`/root` 那份还是三个服务共同的 venv 宿主。

---

## 2. 四项目现状总表

| | **v4 高频振动** | **v5 低频振动** | **meter/helmet 视觉** | **VFD 变频器** |
| --- | --- | --- | --- | --- |
| 目录 | `/root/ruiteng/…/ai_diagnosis/v4` | `/home/ruiteng/…/ai_diagnosis/v5` | `/home/ruiteng/2026/meter_service` | `/home/ruiteng/2026/VFD` |
| 端口 | uvicorn `127.0.0.1:8013` | uvicorn `127.0.0.1:8014` | 8101/8102/8103/8096，聚合 `0.0.0.0:7860` → nginx `:8443` | uvicorn `0.0.0.0:18012` |
| 进程 | PID 1075，已跑 14 天 | PID 3654115，已跑 4 天 23 时 | PID 1079/206435/1513047/1077/1513117 | PID 893664，已跑 2 天 |
| 后端规模 | ~18.6k 行（`app.py` 单文件 2254 行） | 7 516 行 | 每服务 157~376 行（另含**两份** vendored ultralytics） | 2 638 行 |
| **数据来源** | 自造波形，`DirectInfluxPublisher` 直写 | **gRPC 读 historystore `127.0.0.1:5400`** | 无数据面（图片进、JSON 出） | 自造波形 `_CodeWaveformGenerator` |
| **时序落点** | 自家 `influxd:8087` / `ai_diagnosis_demo.vibration_raw` | 不落时序，只落 CSV/内存 | 只落 `artifacts/` 图片 | 自家 `influxd:8087` / `vfd_platform.vfd_raw` |
| **业务状态** | `data/mock/*.json`（全量读写） | `local_data/` 17 MB JSON + sklearn 工件 | 无 | SQLite `vfd_platform.db` |
| **结论出口** | 只回自己的页面 | 只回自己的页面 | 只回自己的页面 | 只回自己的页面 |
| 鉴权 | 无，CORS `*` | 无 | 无 | 无，CORS `*` |
| LLM 密钥 | `backend/.env` 明文 | `backend/.env` 明文 | — | — |

**四个都没有把结论写回任何共享存储**，这是本次预研最关键的一条共性。

### 2.1 v4 高频振动

结论与 2026-08-12 的预研一致，不重复；只补一条**今天复查的运行态**：见 §3.1，那条
3 kpts/s 的模拟写**至今仍在跑**，一个月没变。

算法侧另有一条已成文的判断（《算法侧改造方案》§1.5.5）：v4 赖以立身的
FFT 全频谱 / 瀑布图 / 频谱图喂多模态 LLM，在已核实的**六款 Modbus 振动传感器上无一家提供谱线数组**，
即这批能力**在现场没有输入**。这是 v4 去留问题的事实基础（讨论项 §7.1）。

### 2.2 v5 低频振动 —— 唯一已在目标形态上的一个

```
historystored :5400 ──gRPC(VQT)──► v5 backend :8014 ──► 特征/诊断/报告 ──► 自己的前端
```

- 通过 `backend/services/hs_realtime.py`（1 147 行）直连 hs 的 gRPC，桩代码来自
  `v5/hsAPI/api/python/gen`（`daqcontract_pb2` / `historystore_pb2`），
  历史查询方法齐全（`kRawData` / `kTrendData` / `kBeginInterpolated` / `kAverage` …）。
- 点模型 = 每传感器 13 点：`temp` + 三轴 ×(`acc`/`freq`/`disp`/`vel`)，
  与《算法侧改造方案》§1.5 的 **L1 档**吻合。
- ⚠️ **绑定方式是私有 id 表**：`backend/config/sensor_configs.json` 里逐点硬写
  `{"key":"x_acc","local_id":995,"global_id":807}`。
  即 **hs 的内部 globalId 空间被泄到了算法侧**，且这张表靠人工维护。
  （对照我方既有纪律：id 空间是二维的，方向 × 连接类型不同则同一个点的 id 也不同。）
- 还持有一整套**工作台状态**：标注、每传感器模型训练、知识库（实为"标注集 + sklearn 工件"，
  非 RAG 向量库）、归档、PDF 报告 —— 这部分是 daqvision 预案的"无状态 Sidecar"装不下的，见 §6。

### 2.3 meter/helmet 视觉 —— 无历史包袱，最好接

```
浏览器/其它调用方 ──图片──► platform :7860（聚合转发） ──► pointer :8101 / digital :8102 / helmet :8103
                                                     └─► ocr :8096（独立）
nginx :8443  ──►  127.0.0.1:7860
```

- `platform/main.py`（157 行）只做两件事：转发 multipart 上传、把下游返回体里的
  `/artifacts/...` 路径重写成自己的前缀。**没有任何状态**。
- 每个下游都是 `POST /api/read`（或 `/api/detect`、`/api/live-detect`）→ JSON + 标注图。
  helmet 另有 `/api/live-detect` 供前端逐帧推送。
- ⚠️ 仓库卫生：`helmet_service/ultralytics/` 与 `helmet_service/vendor/ultralytics/` 是**两份完全相同的
  vendored 副本**（逐文件行数一致），约 12 万行。
- ⇒ 这一族**天然就是 `vision.proto` 的形状**（`InferOnce` + `StreamEvents`），
  缺的只有"结论→测点"这个出口。

### 2.4 VFD 变频器 —— 模型是真的，数据是假的

- 算法：TCN（`model.py` 的 `TemporalBlock`），**15 通道**
  （`vin_l1l2/l2l3/l3l1`、`iout_u/v/w`、`output_freq`、`motor_power_pct`、`output_torque_pct`、
  `cabinet_temp`、`transformer_temp_a/b/c`、`unit_vdc_mean`、`unit_vdc_spread`），
  **6 类**（正常 / 冷却系统退化 / 功率单元异常 / 输出电流不平衡 / 输入电网异常 / 过载）。
- 数据：`vfd_storage.py` 的 `_CodeWaveformGenerator`，源标记 `code://megavert_vfd_demo_v5`，
  按公式合成 15 路波形，写 InfluxDB + SQLite。**现场设备一个点都没接。**
- ⇒ 要换掉的是数据面，不是算法。

---

## 3. 两条今天现查出来的止血项（与整合方案无关，独立成立）

### 3.1 v4 的 3 kpts/s 模拟写仍在满速跑

```
db=ai_diagnosis_demo  measurement=vibration_raw
最新点   2026-09-10T08:46:51.281584153Z   ← 查询时刻就是 08:47，即"正在写"
保留策略 autogen  duration=12h0m0s  shardGroupDuration=1h0m0s
```

2026-08-12 预研写的"7×24 满速在写、靠 12h 保留自我了断"，**一个月过去完全没变**。

### 3.2 ★ VFD 正在往库里写**未来时间戳**

```
主机当前时间          2026-09-10T08:47:11Z
db=vfd_platform  measurement=vfd_raw
first(iout_u)        2026-09-08T07:40:07.000999936Z
last(iout_u)         2026-09-18T10:13:05.502000128Z   ← 比现在超前 8 天
保留策略 autogen      duration=0s（永不过期）  shardGroupDuration=168h
```

即模拟器自带一条**比墙钟跑得快的时间轴**，进程起来 2 天已经写到 8 天以后，且该库永不过期。

> 这条违反 VQT 铁律里的 **T = 真实采样时刻**。若不改就把 VFD 并进共享时序库，
> 会把整条时间轴污染掉，而且"未来点"会让所有按 `now` 取右边界的查询行为异常。
> **在讨论整合方案之前就该处理。**

---

## 4. 当前的集成方式，以及它为什么脆

现状是 AICloud 的**外部视图**（`ExternalView_Category_Id = 41`，见 AICloud `docs/17`）：
后端内部反代把外部站搬到同源之下，再用同源 iframe 承载；页面跳转靠平台向 iframe
注入脚本 / 发 `postMessage`。

> ## ★ 2026-09-10 订正：本节原写的"v5 是外部视图"是**错的**
>
> 原文写：「v5 走的是「绝对」模式（`mountMode=base`，`publicPath=/ai_diagnosis/v5/`）」，
> 并据此列了**三处**耦合。**这一句没有取证**：我看见 nginx 里 `/ai_diagnosis/v5/` 的 alias，
> 又读了 AICloud `docs/17` 的挂载模式表，**就按模式推断了实体的存在**，没有去查平台的库。
>
> AICloud 2026-09-10（`C-6 §3`）**实查两个库**（`aiportal` / `aiportal_dev`）：
> **都没有 v5 的实体**。v5 由 **nginx 直接静态服务**，根本不在外部视图链路上，
> 那套 `injectJs` / 挂载模式对它不生效。
>
> ⇒ **耦合只有两处（下表 1 与 3），第 2 处对 v5 不成立。**
> ⇒ 教训：**跨项目断言，凡涉及对方库里的状态，一律请对方实查，不按模式推断。**

v5 的嵌入链路涉及**两处独立耦合**：

| # | 耦合点 | 实处 |
| --- | --- | --- |
| 1 | nginx location | `/etc/nginx/snippets/aicloud-locations.conf`：`/ai_diagnosis/v5/` → `alias …/frontend/dist_embed/`；`/ai_diagnosis/v5/api/` → `127.0.0.1:8014` |
| ~~2~~ | ~~平台实体配置（`mountMode`/`publicPath`/`injectJs`，`?aivPage=`）~~ | **对 v5 不成立**，见上方订正。此列适用于 v4（实体 1075 / 1049） |
| 3 | 应用侧页面键 | `v5/frontend/src/main.js`：`externalViewAliases` + `resolveExternalPageKey()`，收 `{__aiv:'page'}` 后回 `page-ack` |

**两者任一漂移，跳转就静默失效**——它不是一个 bug，是一条没有单一可信源的约定。

> 关于本次报告的跳转异常，**我没有复现，不下根因结论**。已排除的一条：
> `dist_embed/` 构建时间 `2026-09-05 15:10` **晚于** `src/main.js` 的 `2026-09-05 13:52`，
> 所以不是"改了没发布"。
>
> AICloud 补充的实据（`C-6 §3`）：那份 nginx 配置**被另一个 AI（codex）反复改过** ——
> `/etc/nginx/snippets/` 下有五份 `codex-*` 备份（`v5-to-direct`、`v5-cache-fix`、`v5-route-fix`…）。
> ⇒ 用户怀疑的"老平台在改"**有实据，但改的不是平台，是 nginx**。
>
> ★按 2026-09-10 裁定（前端做进 AICloud、嵌入层作废），**这一层要拆掉，不值得再在嵌入层修**。

---

## 5. 按数据面形态归类：四个项目其实是三类

| 类 | 成员 | 现状 | 改造成本 |
| --- | --- | --- | --- |
| **A 已在目标形态** | v5 | 从 hs 取真 VQT | 小：绑定改平台下发 + 结论回流 |
| **B 无数据面** | meter / helmet / ocr | 纯 request→response | 小：只需加"结论→测点"出口 |
| **C 数据面是假的** | v4、VFD | 自造数据 + 自养 InfluxDB | 大：数据面整体替换 |

⇒ 整合工作量不是平均分布的，**排期应当按这个分类走，而不是按项目并列推进**。

---

## 6. daqvision 预案的适用范围与缺口

daqvision 预案（`proto/vision.proto` + 《视觉算法gRPC交互方案_v1》）的形状：
**Python 算法 = gRPC Server（Sidecar），daqgate(Go) = Client**，结论经通道变成测点。
契约要素：`GetInfo` / `StreamEvents` / `ApplyPipeline` / `StopPipeline` / `GetSnapshot` / `InferOnce`。
该契约已与 daqgate 在 2026-06-03/04 谈定 6 条约束（见 `exchange/`），**proto_version 1.0，无需改契约**。

**能覆盖的**：

- meter/helmet（B 类）—— 形状完全吻合。
- v4/v5 的"特征 → 证据包 → 结论"这条推理链（B/C 类的推理面）。

**覆盖不了、必须另行安排的**：

| 缺口 | 说明 |
| --- | --- |
| **输入是测点而不是视频流** | `PipelineConfig.stream_url` 装不下"一组 tagpoint 绑定 + 采样节拍" |
| **工作台状态** | v5 的标注 / 训练 / 知识库 / 归档 / 报告是有状态的，"无状态 Sidecar"装不下 |
| **结论回流的落点与语义** | 契约只定义了事件流，没定义"结论作为虚拟测点落到哪、挂在哪个实体下" |
| **Client 归属未定** | 预案假定 Client 是 daqgate；但云侧场景（标量点分析、LLM、报告）AICloud 当 Client 更自然 |

⇒ 结论：**预案的形状可复用、契约不必推翻，但需要泛化**。具体怎么泛化属于方案，按 §7 逐项讨论。

**另需注意的既有事实**：daqgate 侧目前**尚无** `channel/vision` 或 `channel/ai` 实现
（`daqgate/ProtocolGate/channel/` 下只有 `dlt645`、`dlt69845`、`iec60870`、`modbus`、`opc`、`snmp`、`transparent`）。
即预案的 Go 侧从 2026-06 至今没有落地。

---

## 7. 待讨论清单（逐项过，不并发）

按建议的讨论顺序排列；每条都标了"为什么它必须先定"。

| # | 议题 | 为什么先定 | 状态 |
| --- | --- | --- | --- |
| 7.0 | **止血两条**（§3.1 停 v4 模拟写；§3.2 VFD 时间戳与保留策略） | 与方案无关，独立成立，且持续在恶化 | 待议 |
| 7.1 | **v4 的去留** | 决定 C 类是"改造"还是"退役"，直接影响后面所有排期 | 待议 |
| 7.2 | **结论回流的落点与实体归属** | 决定"AI 结论测点"挂在被诊断设备下还是 AI 服务下；直接决定报警按祖先链限段时谁看得见 | 待议 |
| 7.3 | **谁当 gRPC Client**（daqgate / AICloud / 两者） | 决定契约往哪泛化、先做哪一侧 | 待议 |
| 7.4 | **工作台状态的归属**（留 Python 私有存储 / 升为 AICloud 实体） | 决定 AICloud 要不要新增实体类别，是本次整合最大的一块工作量 | 待议 |
| 7.5 | **算力放置**（helmet/meter 留云侧 or 下沉 RK3588） | 决定 `channel/ai` 先做边缘还是先做云 | 待议 |
| 7.6 | **LLM 出网口径与密钥管理** | 现在四个项目各自明文存 key；现场（矿山/工厂）大概率不允许出公网 | 待议 |
| 7.7 | **高频波形要不要进 hs** | 从 2026-08 挂到现在未定；不定则 L4 档（`RAWFIFO` / 25 kHz 重算）一直悬着 | **悬置中** |
| 7.8 | **daqgate 侧由谁实现** | 我方工作边界是 historystore + daqvision，`channel/ai` 落在 daqgate，需走协调文 | 待议 |
| 7.9 | **v5 嵌入跳转异常的根因** | 与整合方案独立；但它是 §4 三方耦合的样本，值得单独取证一次 | 未复现 |

---

## 8. 附：端口与关键文件索引（AISERVER 实测）

### 8.1 端口

| 端口 | 归属 | 绑定 |
| --- | --- | --- |
| 8013 | v4 后端 | `127.0.0.1` |
| 8014 | v5 后端 | `127.0.0.1` |
| 18012 | VFD 后端 | `0.0.0.0` |
| 7860 | meter platform 聚合 | `0.0.0.0`（nginx `:8443` 反代到这里） |
| 8101 / 8102 / 8103 | pointer / digital / helmet | `127.0.0.1` |
| 8096 | ocr | `0.0.0.0` |
| 8087 / 8088 | v4 与 VFD 共用的 InfluxDB 1.x | `127.0.0.1` |
| **5400** | **historystored gRPC** | `127.0.0.1` + `192.168.1.135` + `100.107.230.13`（mTLS） |
| 8086 | historystored InfluxDB v1 兼容查询口 | `127.0.0.1` |
| 5410 / 18090 | protocolgate（daqgate） | `127.0.0.1` |
| 80 / 443 / 8080 / 8443 | nginx | `0.0.0.0` |

### 8.2 关键文件

| 关注点 | 文件 |
| --- | --- |
| v5 ⇄ hs 的 gRPC 取数 | `v5/backend/services/hs_realtime.py` |
| v5 的点位私有绑定表 | `v5/backend/config/sensor_configs.json`、`config/realtime_points.json` |
| v5 的嵌入页面键与 postMessage 协议 | `v5/frontend/src/main.js`（`externalViewAliases` / `syncEmbedRouting`） |
| meter 聚合与 artifact 路径重写 | `meter_service/platform/main.py` |
| VFD 波形合成与双写 | `VFD/app/backend/vfd_storage.py` |
| VFD 的 TCN 模型与标签集 | `VFD/app/backend/model.py` |
| 外部视图的 nginx 挂载 | `/etc/nginx/snippets/aicloud-locations.conf` |
| AICloud 外部视图设计 | `/home/Project/AICloud/docs/17-外部视图与内部代理设计.md` |
| AICloud 实体类别 Id 表 | `/home/Project/AICloud/AIBackend/domain/domain.go` |
| AICloud 实时库/点表/网关/趋势 | `/home/Project/AICloud/AIBackend/irtdb/` |
