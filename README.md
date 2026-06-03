# daqvision

daqgate 的 **AI 视觉**配套工程：以瑞芯微 RK3588(RKNN) 为算力，把工业相机/视频流的检测与异常识别，
通过 gRPC 汇入 daqgate 网关，复用其数据建模、存储、Web 展示与上云链路。

> 计划与设计文档在 [`../计划/视觉识别/`](../计划/视觉识别/)：
> - 《AI视觉识别研发计划.md》——总路线图（阶段 0→6）
> - 《视觉算法gRPC交互方案_v1.md》——Go⇄Python gRPC 契约设计

## 架构（一句话）

**Python 视觉服务 = gRPC Server，daqgate(Go) = Client**（实现为 daqgate 的 `channel/vision` 通道，像 Modbus 通道连设备一样连视觉服务）。

```
工业相机/RTSP ─► [vision-infer (Python, gRPC Server)] ─事件流─► [daqgate channel/vision (Go Client)]
                  取流→RKNPU2推理→判定→抓拍          ◄─控制─    →tagpoint虚拟设备→store/web/上云
```

## 仓库结构

```
daqvision/
  proto/                # ★ 契约单一可信源（Go 与 Python 都从这里生成）
    vision.proto
    buildPython.sh      # 生成 Python 桩 -> vision-infer/app/
    buildGo.sh          # 生成 Go 桩（daqgate 侧 agent 在 WSL 指向 daqgate/proto 执行）
    CHANGELOG.md        # 契约版本记录
  vision-infer/         # Python 推理 Sidecar（现为阶段3 STUB）
  data/                 # 数据/模型/抓拍（不入库，见其 README）
```

## 职责边界

| 部分 | 归属 | 环境 |
|---|---|---|
| `proto/` 契约 | 本工程（单一可信源） | Windows / 跨端共用 |
| `vision-infer/` Python 服务 | 本工程 | 开发 Windows/WSL，部署 RK3588(aarch64) |
| daqgate `channel/vision` (Go) | **另一 agent（WSL/Debian12）** | 从本 `vision.proto` 生成 Go 桩 |

## 文档交换约定

两个 agent 协作，文档互投对方的 `exchange/`：
- **daqvision → daqgate**：放 `daqgate/exchange/`（已投：`channel/vision 对接说明`）
- **daqgate → daqvision**：放 `daqvision/exchange/`（我从这里读回执/问题）

契约仍以 `proto/vision.proto` 为单一可信源；exchange 只放说明/问答/决策。

## 现在做什么（阶段 3：先通管道）

1. `cd vision-infer && pip install -r requirements.txt`
2. `../proto/buildPython.sh` 生成桩
3. `python app/server.py` 起 stub，发假事件
4. daqgate 侧 `channel/vision` 连上订阅 `StreamEvents`，确认收到事件 → 链路打通
5. 之后按计划接阶段 2 的真 `.rknn` 模型

硬件就位（RK3588 + 工业相机）后，进入 vision-infer 的 capture/infer/decision 实装。
