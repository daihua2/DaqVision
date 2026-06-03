# daqgate 侧回执 · channel/vision 对接

> 发件：daqgate（网关侧）· 日期：2026-06-03
> 回应：`exchange/2026-06-03-channel-vision-对接说明.md`
> 结论：方案与 `vision.proto` 契约我方接受,按你给的 stub 即可联调。以下逐条答复第 10 节 6 问。

---

## 关键前提：部署形态
**现阶段同机(e52c/RK ARM64),未来迁独立高性能主机。** 故 daqgate 侧 `channel/vision` 一律**做成可配置、两形态皆支持**,默认同机。下面 ①③ 据此答。

## 1. 通道地址 / 安全 —— 做成通道配置项,两形态皆支持
- 配置项:`Address`(`unix:///var/run/daqgate/vision.sock` 或 `host:port`)、`TLS`(off / mTLS)、`SameHost`(bool,影响抓拍取图)。
- **现阶段(同机 e52c)**:`unix://` + insecure,与你 stub 默认一致,先通管道。
- **未来(独立主机)**:`TCP` + **mTLS**,复用 daqgate 现有 `cert/` 证书体系(server.pem 等)。
- 你这边 bind 建议:同时支持 unix socket 与 TCP 监听由你启动参数定;我方按配置 dial。

## 2. 配置主源 —— ✅ 确认以 daqgate 为准
- daqgate Web「视觉配置」页统一管理,落 daqgate 配置库(Pebble/SQLite),**启动时与变更时下发 `ApplyPipeline`**,`StopPipeline` 停路。与现有 channel/adapter 配置模型一致。
- 你侧可不持久化 pipeline 配置(或仅缓存),以 daqgate 下发为准;重连后我方会重新握手并重发当前配置。

## 3. 抓拍取图 —— 两路实现,按 SameHost 切换
- **同机**:直读事件里的 `SnapshotRef.path`(省一次 RPC),再经 `webservice/staticassets` 暴露给 ProtocolWeb。
- **独立主机**:走 `GetSnapshot(snapshot_id, with_box)` 取 JPEG。
- 我方抽象一个「取图」接口、两实现,由 `SameHost` 选择。`MaxRecvMsgSize` 调到 ~8MB(与你 send=8MB 对齐)。

## 4. 测点 schema —— ✅ 基本符合,小建议
- 每 `source_id` = 一虚拟设备 + 你列的测点,符合 daqgate tagpoint(VQT 值-质量-时间)习惯;**质量码复用 `protocolgate.proto` 的 `StatusCode`** 我方确认。
- 建议:
  - 多类检测(如同时"未戴帽/未穿反光衣")→ **每类一个 bool/计数测点**(命名 `det_<label>`),而非单一字段,便于组态告警与上云点表。
  - 设备级补 `last_heartbeat_ts`、`stream_error_detail`(string)便于运维定位。
  - `BBox` 归一化 0..1 我方认可,前端画框时乘宽高。
- 具体命名我方在 channel/vision 落地时定模板,不需要你改 proto。

## 5. proto 生成归属 —— ✅ 独立 visionpb 包
- 生成进 `ProtocolGate/proto/visionpb`(用你的 `proto/buildGo.sh`),**源文件以 daqvision/proto/vision.proto 为唯一可信源**;若需改动,我方同步回 daqvision 并升 `proto_version`,不在 daqgate 另起一份。

## 6. vision.proto 增删 —— 暂不需要
- 现有 service/message 够用。待 channel/vision 落地、确定 source_id→设备/测点命名映射后,若需在 proto 里加「设备显示名 / 测点命名模板 / ROI 持久化元数据」等,我再单独提 issue 到 daqvision/exchange。

---

## 下一步(daqgate 侧)
1. 生成 `visionpb` Go 桩。
2. 新增 `channel/vision`(仿 channel/modbus 结构):dial(配置化 unix/TCP)→ `GetInfo` 握手(校 proto_version 1.0)→ `StreamEvents` 分发(HEARTBEAT/DETECTION/ANOMALY/PIPELINE_STATUS)→ 转测点落库 → 指数退避重连。
3. 用你 stub(`:50061`)先通管道:`GetInfo` 成功 + 收到假 DETECTION + 测点可见。
4. 再接 Web「视觉配置」页(ApplyPipeline/StopPipeline)与抓拍取图。

有进展/疑问我会续写到本目录。先通管道,再灌真模型 👍
