# vision.proto 契约变更记录

> 改契约必须：① 字段编号只增不改不复用（废弃用 `reserved`）；② 升 `proto_version`；③ 在此登记。
> Go(daqgate) 与 Python(vision-infer) 双端都从 `vision.proto` 生成，避免漂移。

## proto_version 1.0 — 2026-06-03（首版草案）
- 定义 `VisionInference` 服务：`GetInfo` / `StreamEvents` / `ApplyPipeline` / `StopPipeline` / `GetSnapshot` / `InferOnce`。
- 事件类型：DETECTION / ANOMALY / HEARTBEAT / PIPELINE_STATUS。
- 坐标统一归一化 0..1；抓拍走引用 + 按需 `GetSnapshot`。
- 状态：**待评审**（详见交互方案 v1 第 8 节开放问题）。
