# vision-infer — 视觉推理服务（Python, gRPC Server）

daqgate 的视觉 Sidecar：取流 → RKNPU2 推理 → 判定 → 通过 gRPC 把事件流推给 daqgate。
契约见 [`../proto/vision.proto`](../proto/vision.proto)，设计见《计划/视觉识别/视觉算法gRPC交互方案_v1.md》。

> 当前状态：**阶段 3 STUB**（[app/server.py](app/server.py) 只发假事件），用于先打通 Go⇄Python gRPC 链路。

## 快速开始（空跑骨架）

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 生成 gRPC 桩（产出 app/vision_pb2*.py）
../proto/buildPython.sh        # Windows 下用 WSL/bash 运行

# 3. 启动 stub 服务（默认监听 0.0.0.0:50061）
python app/server.py
# 同机部署可改 Unix socket：
#   VISION_BIND="unix:///var/run/daqgate/vision.sock" python app/server.py

# 4. 验证（任选其一）
#   - grpcurl -plaintext localhost:50061 vision.VisionInference/GetInfo
#   - 由 daqgate 侧 channel/vision 连上来订阅 StreamEvents，看日志收到假事件
```

## 目录

```
vision-infer/
  app/
    server.py          # gRPC 服务入口（现为 STUB）
    vision_pb2*.py      # 由 proto 生成（buildPython.sh 产出，勿手改/勿入库）
    capture/            # (待建) 取流、解码
    infer/              # (待建) RKNN 推理封装(rknn-toolkit-lite2)
    decision/           # (待建) 阈值/ROI/判定规则
  config/
    pipelines.example.yaml
  requirements.txt
```

## 演进路线（对应研发计划阶段 3→6）

1. ✅ STUB：假事件打通链路。
2. 接 `infer/`：载入阶段 2 训练的 `.rknn`，`InferOnce` 先跑通单图。
3. 接 `capture/`：RTSP/USB 取流，`StreamEvents` 推真检测事件 + 心跳。
4. 接 `decision/`：阈值/ROI 判定、命中抓拍存盘、`PIPELINE_STATUS` 上报。
5. 阶段 6：C++ 重写性能版（同一 proto 契约，daqgate 侧零改动）。
