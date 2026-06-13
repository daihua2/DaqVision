# daqgate(Go) ⇄ Python 视觉算法 gRPC 交互方案（v1）

> 配套：《AI视觉识别研发计划.md》第 4 节"daqgate 集成"
> 本文取代研发计划中"先用 MQTT 起步"的说法：**统一走 gRPC，不引入 MQTT。**
> 编写日期：2026-06-03 · 版本：v1（首版，待评审迭代）

---

## 0. 一句话结论

**Python 视觉推理服务做 gRPC Server，daqgate(Go) 做 gRPC Client**，并把这个 client 实现成 daqgate 现有 `channel/` 体系里的一个新通道 **`channel/vision`**——就像 Modbus 通道去"连一个设备"一样，vision 通道去"连一个视觉算法服务"，把推理事件转成虚拟设备的测点(tagpoint)，复用现有 `domain → store → webservice → ProtocolWeb` 全链路。

```
┌──────────────────────────┐         gRPC          ┌──────────────────────────────┐
│  Python 视觉推理服务       │  ◄── 控制(unary) ──   │  daqgate / ProtocolGate (Go)  │
│  = gRPC SERVER            │                       │  = gRPC CLIENT               │
│                           │   事件(server-stream) │  └ channel/vision (新增)      │
│  取流→RKNPU2推理→判定→抓拍 │   ──────────────►     │     ├ 连接/重连/心跳          │
│  暴露 VisionInference 服务 │                       │     ├ 事件→tagpoint 虚拟设备  │
└──────────────────────────┘                       │     └ 复用 store/adapter/web  │
                                                     └──────────────────────────────┘
```

---

## 1. 为什么是"Python 当 Server、Go 当 Client"？

| 维度 | 选定方案：Python=Server / Go=Client | 反向：Go=Server / Python=Client |
|---|---|---|
| 与 daqgate 架构契合 | ✅ `channel/` 本就是"主动去连设备"，视觉服务就是一个"会产数据的设备" | ✗ 要在 daqgate 里再开一个被动接收端，和现有协议通道范式不一致 |
| 控制权 | ✅ daqgate 从 Web 下发"起停某路相机/改阈值/改ROI"，天然是 Go 调 Python(unary) | △ 控制要反向，别扭 |
| 故障隔离 | ✅ Python 崩溃/重启=一个"设备掉线"，Go 带重连退避，主程序不受影响 | △ Python 作为 client，断了 Go 不易感知其内部状态 |
| 启动依赖 | ✅ daqgate 启动不依赖 Python 在不在；连不上就是通道 offline | ✗ 反向时 Go server 等 client 上来，状态模糊 |
| 多路扩展 | ✅ 一个 Python 服务暴露多 pipeline，Go 一个通道管多个虚拟设备 | △ 多 client 反连，连接管理更乱 |

> 数据方向：**事件用 server-streaming 从 Python 持续流向 Go；控制/查询用 unary 从 Go 调 Python。** 不用双向流，简单可靠。

---

## 2. 接口契约：`vision.proto`（首版草案）

新建独立 proto（**不混进** `protocolgate.proto`），package 用 `vision`，与现有 `grpc` 包隔离。

```protobuf
syntax = "proto3";
package vision;
option go_package = ".;visionpb";

import "google/protobuf/timestamp.proto";

// ───────────────────────── 服务定义 ─────────────────────────
service VisionInference {
  // 查询服务能力/版本/已加载模型/各 pipeline 状态（启动握手 + 周期探活）
  rpc GetInfo (InfoRequest) returns (InfoReply);

  // 订阅推理事件流（server-streaming）：检测/异常/心跳/pipeline状态变化
  rpc StreamEvents (StreamRequest) returns (stream VisionEvent);

  // 控制：起/停/改配 一路 pipeline（一路相机=一个 pipeline）
  rpc ApplyPipeline (PipelineConfig) returns (PipelineAck);
  rpc StopPipeline  (PipelineRef)    returns (PipelineAck);

  // 按需取一张抓拍/热力图原图（事件里只带引用，原图按需拉，避免撑爆事件流）
  rpc GetSnapshot (SnapshotRequest) returns (SnapshotReply);

  // 同步单图推理（联调/标定/前端"立即检测一次"用）
  rpc InferOnce (InferOnceRequest) returns (VisionEvent);
}

// ───────────────────────── 能力/版本 ─────────────────────────
message InfoRequest {}
message InfoReply {
  string service_version = 1;   // 推理服务自身版本
  string proto_version   = 2;   // 本 proto 契约版本，如 "1.0"
  string runtime         = 3;   // 如 "rknn-toolkit-lite2 2.3.0 / librknnrt 2.3.0"
  repeated ModelInfo models = 4;
  repeated PipelineStatus pipelines = 5;
}
message ModelInfo {
  string name = 1;              // 如 "helmet-yolo11n"
  string version = 2;
  string task = 3;              // "detection" | "anomaly" | "ocr"
  int32  input_w = 4;
  int32  input_h = 5;
  repeated string labels = 6;   // 检测类别
}

// ───────────────────────── 事件流 ─────────────────────────
message StreamRequest {
  repeated string source_ids = 1; // 空=订阅全部 pipeline
  bool include_heartbeat = 2;      // 是否要心跳事件（建议 true，用于探活）
}

enum EventType {
  EVENT_UNSPECIFIED = 0;
  DETECTION        = 1;   // 检测结果（命中目标）
  ANOMALY          = 2;   // 无监督异常检测结果
  HEARTBEAT        = 3;   // 周期心跳（即使无目标也发，证明活着）
  PIPELINE_STATUS  = 4;   // pipeline 上线/掉线/取流失败等状态变化
}

message VisionEvent {
  string event_id   = 1;
  string source_id  = 2;                       // 哪一路相机/pipeline
  string model      = 3;                        // 用的哪个模型
  EventType type    = 4;
  google.protobuf.Timestamp ts = 5;
  float  infer_ms   = 6;                         // 单帧推理耗时(ms)

  repeated Detection detections = 7;             // type=DETECTION 时有
  AnomalyResult anomaly        = 8;              // type=ANOMALY 时有
  SnapshotRef snapshot         = 9;              // 命中时的抓拍引用(可空)
  PipelineStatus pipeline      = 10;             // type=PIPELINE_STATUS 时有
}

message Detection {
  string label = 1;
  float  score = 2;
  BBox   box   = 3;
}
// 坐标统一用归一化 0..1，跟分辨率解耦
message BBox { float x = 1; float y = 2; float w = 3; float h = 4; }

message AnomalyResult {
  float score      = 1;     // 异常分数
  float threshold  = 2;     // 当前判定阈值
  bool  is_anomaly = 3;
  SnapshotRef heatmap = 4;  // 异常热力图引用(可空)
}

// 抓拍只传"引用"，原图按需用 GetSnapshot 拉
message SnapshotRef {
  string snapshot_id = 1;
  int32  width  = 2;
  int32  height = 3;
  string path   = 4;   // 同机部署时的本地路径(可选，便于 Go 直接读)
}

// ───────────────────────── 控制/配置 ─────────────────────────
message PipelineConfig {
  string source_id     = 1;   // 唯一标识一路
  string stream_url    = 2;   // "rtsp://..." 或 "usb:0" 或 "file:..."
  string model         = 3;   // 用哪个已加载模型
  int32  frame_stride  = 4;   // 每 N 帧推理一次(控制节拍/算力)
  float  score_threshold = 5;
  float  anomaly_threshold = 6;
  repeated ROI rois    = 7;   // 感兴趣区域(归一化)，空=全画面
  int32  heartbeat_sec = 8;   // 心跳周期
  bool   save_snapshot = 9;   // 命中是否抓拍存盘
}
message ROI { string name = 1; float x = 2; float y = 3; float w = 4; float h = 5; }
message PipelineRef { string source_id = 1; }
message PipelineAck { bool ok = 1; string message = 2; }

message PipelineStatus {
  string source_id = 1;
  enum State { OFFLINE = 0; CONNECTING = 1; RUNNING = 2; STREAM_ERROR = 3; }
  State state = 2;
  string detail = 3;          // 错误详情/取流地址等
  float  fps = 4;             // 实际推理帧率
}

// ───────────────────────── 抓拍 / 单图 ─────────────────────────
message SnapshotRequest { string snapshot_id = 1; bool with_box = 2; }
message SnapshotReply {
  bytes  image = 1;           // JPEG 字节
  string mime  = 2;           // "image/jpeg"
  int32  width = 3;
  int32  height = 4;
}
message InferOnceRequest {
  string model = 1;
  bytes  image = 2;           // 直接传一张图(JPEG/PNG字节)
}
```

> 字段编号一旦发布**只增不改不复用**（proto3 兼容铁律）；废弃字段用 `reserved`。`proto_version` 用于 Go/Python 双方握手时校验契约一致。

---

## 3. daqgate(Go) 侧落地：新增 `channel/vision`

参照现有 `channel/modbus`、`channel/snmp` 的结构（不发明新范式），新增一个通道：

1. **连接管理**：dial Python 的 `VisionInference` 服务；带**重连退避**（断线=设备 offline，恢复=重新 `GetInfo` 握手 + 重新 `StreamEvents`）。心跳事件超时即判 offline。
2. **建模为虚拟设备**：每个 `source_id`（一路相机）= 一个虚拟设备；在 `domain/`+`tagpoint/` 建测点，例如：
   - `pipeline_online`(bool)、`fps`(float)
   - `helmet_violation`(bool/计数)、`last_score`(float)
   - `last_event_ts`(time)、`last_snapshot_id`(string)
   - 异常检测：`anomaly_score`、`is_anomaly`、`heatmap_id`
3. **质量码复用**：直接用 `protocolgate.proto` 里现成的 `StatusCode`（`QualityGood/QualityNo/...`）标注测点质量，与其他协议数据一致。
4. **存储**：事件经 `store/`（Pebble/SQLite）落库；抓拍图存文件，测点只存 `snapshot_id`/路径。
5. **抓拍取图**：前端要看图时，Go 调 `GetSnapshot` 拉 JPEG，经 `webservice/staticassets` 暴露给 ProtocolWeb；同机部署也可直接读 `SnapshotRef.path`。
6. **控制下发**：ProtocolWeb 的"视觉"配置页 → daqgate webservice → vision 通道调 `ApplyPipeline/StopPipeline` 下发到 Python。
7. **上云**：视觉测点随其他网关数据走现有 `adapter` 上报，无需单独通道。

> 关键：**推理不进 daqgate 进程**。daqgate 只做"连接、转测点、存储、展示、上报、控制下发"，算力全在 Python 侧。

---

## 4. 传输与安全

| 部署形态 | 建议传输 | 安全 |
|---|---|---|
| Python 与 daqgate **同机**(E52c/同一RK3588盒) | **Unix Domain Socket**：`unix:///var/run/daqgate/vision.sock` | 文件权限即鉴权，免端口免证书，最简最快 |
| Python 在**独立 RK3588 盒**，daqgate 在网关 | TCP + **mTLS** | 复用 daqgate 现有 `cert/` 证书体系（与现有 gRPC 一致） |

补充参数（双方都配）：
- **keepalive**：客户端 ~20s ping，服务端允许；事件流上加心跳事件双保险。
- **max recv size**：`GetSnapshot` 返回 JPEG，把 client 端 `MaxRecvMsgSize` 调到如 8MB；事件流本身只走元数据，保持小。
- **超时**：unary 控制类设 3–5s deadline；`StreamEvents` 长连不设超时，靠心跳判活。

---

## 5. 双端代码生成（codegen）

一份 `vision.proto`，两端各生成：

**Go 端**（仿现有 `proto/build.sh`）：
```bash
protoc vision.proto --go_out=. --go-grpc_out=.
# 产物放入 daqgate，如 proto/visionpb/
```

**Python 端**（推理服务仓库）：
```bash
python -m grpc_tools.protoc -I. \
  --python_out=. --grpc_python_out=. vision.proto
# 产物：vision_pb2.py / vision_pb2_grpc.py
```

> `vision.proto` 作为**契约单一来源**，建议单独放一个 `vision-proto/` 目录，Go 和 Python 仓库都从它生成，避免两份漂移。前端若要直接消费可再生成 TS（参照现有 ProtocolWeb 的 `protos/build.sh`），但首版前端数据走 daqgate 既有 gRPC-web 即可，不必直连 Python。

---

## 6. 交互时序（典型流程）

```
daqgate 启动
  └─ channel/vision dial Python(VisionInference)
       ├─ GetInfo() ──────────────► 校验 proto_version / 读取模型列表
       ├─ ApplyPipeline(相机1配置) ─► Python 起 pipeline → PipelineAck{ok}
       └─ StreamEvents([]) ◄────────  订阅全部事件
                                      │
   ┌──────────────────────────────── 持续 ───────────────────────────────┐
   │ Python: 取流→每N帧RKNPU2推理→命中判定→(抓拍存盘)→push VisionEvent     │
   │ Go: 收到事件 → 更新虚拟设备测点 → 落库 → (命中)前端告警/上云/PLC联动    │
   │ Python: 无命中也定期 push HEARTBEAT；取流失败 push PIPELINE_STATUS    │
   └──────────────────────────────────────────────────────────────────────┘
   前端点开告警缩略图 → daqgate GetSnapshot(snapshot_id) → 返回 JPEG → 展示
   前端改阈值/ROI → daqgate ApplyPipeline(新配置) → Python 热更新该 pipeline
```

---

## 7. 与研发计划的对应 & 落地顺序

本方案落在《AI视觉识别研发计划.md》的**阶段 3→4**，把"推理服务"和"daqgate 集成"都改为 gRPC：

| 步骤 | 做什么 | 验收 |
|---|---|---|
| ① 定契约 | 落定 `vision.proto` v1，双端 codegen 通过 | Go/Python 都能编译生成桩代码 |
| ② Python 最小 Server | 先只实现 `GetInfo` + `StreamEvents`(发假事件) | grpcurl/脚本能拉到事件流 |
| ③ Go 最小 Client | `channel/vision` 连上、收流、打日志 | 日志里看到 Python 推过来的事件 |
| ④ 接真模型 | Python 把阶段2的安全帽 RKNN 接进 `StreamEvents` | 真检测事件流入 Go |
| ⑤ 建模+展示 | 事件→tagpoint 虚拟设备，ProtocolWeb 出告警视图 | Web 看到告警列表+抓拍 |
| ⑥ 控制闭环 | `ApplyPipeline/StopPipeline` + 前端配置页 | 前端改阈值能下发生效 |
| ⑦ 上云 | 视觉测点随现有 adapter 上报 | 云端收到视觉数据 |

> 建议先做①②③打通"空跑"骨架（不接模型），确认 Go⇄Python 链路通了，再接阶段2的真模型——**先通管道，再灌数据**。

---

## 8. 开放问题决议（已与 daqgate 达成，2026-06-03）

> 依据 daqgate 回执 `daqvision/exchange/2026-06-03-daqgate-回执-channel-vision.md`。**结论：proto_version 维持 1.0，无需改契约。**

| # | 问题 | 决议 |
|---|---|---|
| 1 | 抓拍图存储 | **两路按部署形态切换**：同机直读 `SnapshotRef.path`；分机走 `GetSnapshot`。→ 实装时 Python 报的 `path` 必须真实、同机可读 |
| 2 | 多 Python 服务 | **暂定一个服务管多路**（多 pipeline）；多盒子未来再扩为多 target，不影响 v1 契约 |
| 3 | 配置主源 | ✅ **以 daqgate 为准**：Web 统一管理并持久化，启动/变更时下发 `ApplyPipeline`；Python 侧不持久化（仅内存缓存），重连后由 daqgate 重发 |
| 4 | 事件落库粒度 | 心跳**不落库**（仅更新在线/fps）；命中事件（DETECTION/ANOMALY）全量落库 |
| 5 | proto 归属 | ✅ **独立 `visionpb` 包**，源文件唯一可信源 = `daqvision/proto/vision.proto`；改动同步回 daqvision 并升 `proto_version` |
| + | 部署形态 | **现阶段同机(e52c/RK ARM64)，未来迁独立主机**；channel/vision 做成可配置两形态皆支持，默认同机 |
| + | 地址/安全 | 同机：unix socket + insecure；分机：TCP + mTLS（复用 daqgate `cert/`）。Python 服务支持多地址同时监听（`VISION_BIND` 逗号分隔） |
| + | 测点命名 | 多类检测每类一个测点 `det_<label>`；设备级补 `last_heartbeat_ts`/`stream_error_detail`。**均由 daqgate 侧从现有字段派生，无需改 proto** |

后续若需在 proto 增「设备显示名 / 测点命名模板 / ROI 持久化元数据」等，由 daqgate 提 issue 到 `daqvision/exchange/`，再走改契约流程。

---

*v1 为骨架方案，`vision.proto` 字段会随场景细化。每次改契约请升 `proto_version` 并在 `proto/CHANGELOG.md` 记录变更。*
