# 视觉域预研：YOLO 检测只用 onnxruntime + numpy（不拖 torch）—— 与现有流水线逐框对齐

> 2026-09-11。用户定：视觉域走 **CPU + onnxruntime**。本目录是那个决定落地前的**可行性证明**，不是域本体。
> 研究代码，**不进发布件**（`make-release.sh` 只打 `host/` 与 `domains/`）。

## 1. 结论

**可行，且与 `helmet_service` 现用的 ultralytics 流水线逐位一致。**
同一个 ONNX、同一批图、同阈值下，两个模型、两档阈值、6 张图，**24 组全部框差 0.000px、分差 0.00000**。

## 2. 为什么不能"直接换运行时"

`helmet_service` 虽然有现成的 `.onnx`，但它是经 **`ultralytics.YOLO(onnx路径)`** 加载的，
而 ultralytics 一 import 就拖 torch。⇒ 要真不带 torch，得**自己重写 YOLO 的前后处理**：
letterbox、候选过滤、按类 NMS、坐标还原。`yolo_onnx.py` 就是这个，约 150 行。

## 3. 现场事实（AISERVER 只读核实）

| 项 | 值 |
| --- | --- |
| 模型位置 | `/home/ruiteng/2026/meter_service/helmet_service/models/` |
| 拍照检测接口 `/api/detect` 实际用的模型 | **`yolo11m_safety.pt`**（中模型），conf 0.55（环境变量 `HELMET_CONFIDENCE`）、iou 0.45、640 |
| ONNX 的 n 模型 | 只给"实时检测"`/api/live-detect` 用（416/512/640 × fp32/fp16/int8） |
| ONNX 形状 | 输入 `images [1,3,640,640]`，输出 `output0 [1,9,8400]`（4 框 + 5 类），**图内不含 NMS** |
| 类别（嵌在元数据里） | `{0:'hat', 1:'nohat', 2:'novest', 3:'person', 4:'vest'}` |
| 运行环境 | 进程用的是 **`digital_meter_service/.venv`**（ultralytics 8.4.114 / torch 2.13.0+cpu / onnxruntime 1.19.2）—— 又一例多项目共用一个 venv |
| ⚠️ 元数据不一致 | `yolo11n_safety_640_fp32.onnx` 的元数据描述写的是 **"YOLOv5n"**，文件名却是 yolo11n；m 那份写的是 "YOLO11m"（一致）。输出格式相同，不影响解码 |
| ⚠️ **许可** | 两份 ONNX 的元数据都标注 **`license = AGPL-3.0`**（ultralytics 导出时写入） |

## 4. 对齐结果

参照：AISERVER 上用它现用的 ultralytics 8.4.114 跑 `ref_ultralytics.py`。
我方：开发机 WSL 独立环境（Python 3.10.21 / onnxruntime 1.23.2 / numpy 2.2.6 / opencv-headless 5.0）跑 `parity.py`。
判定：框数一致；按顺序逐框类别一致、框差 ≤1.0px、分差 ≤0.005。★按顺序比，不做"找最近的框配对"——配对会把多一个框、少一个框互相抵消。

| 模型 | conf | 6 张图的框数 | 全体最大框差 | 全体最大分差 | 开发机 CPU 单张 |
| --- | --- | --- | --- | --- | --- |
| yolo11n 640 fp32（10 MB） | 0.55 | 5 / 18 / 6 / 9 / 11 / 0 | **0.000px** | **0.00000** | 约 30~100 ms |
| yolo11n 640 fp32 | 0.25 | 6 / 25 / 6 / 9 / 13 / 0 | **0.000px** | **0.00000** | |
| yolo11m fp32（80 MB） | 0.55 | 7 / 23 / 6 / 9 / 12 / 0 | **0.000px** | **0.00000** | 约 240~300 ms |
| yolo11m fp32 | 0.25 | 7 / 30 / 6 / 9 / 13 / 0 | **0.000px** | **0.00000** | |

样例图宽高：768×698、892×564、1000×658、1000×786、840×1000、320×240（最后一张 1.8KB，0 框，作边界情形）。
两边 onnxruntime 版本不同（1.19.2 vs 1.23.2），网络输出仍逐位相同。

## 5. ★第一次没对齐，查出来的是一处取整

第一版 12 组里 **10 组逐位一致、2 组不过**——都是 892×564 那张图：框数、类别、置信度完全一致，**只有框坐标差 1.394px**。

- 分差恰好是 0 ⇒ 网络输入输出完全相同，差异只在"把框还原回原图"；
- 1.394 = 1/0.717489（这张图的缩放比）⇒ 差了**一个 letterbox 像素**的填充量。

推测后**先看源码再改**（ultralytics 8.4.114 `utils/ops.py::scale_boxes`）：

```python
pad_y = round((img1_shape[0] - round(img0_shape[0] * gain)) / 2 - 0.1)
```

内层多一个 `round(img0_shape * gain)`——缩放后尺寸**先取整**（与 letterbox 的 `new_unpad` 一致）。
第一版照老写法漏了它：这张图缩放后高 404.66，letterbox 取 405、上填 117；老公式算出 118。
**只有取整跨界的宽高比才暴露**，所以另 5 张图都对得上。订正后 24/24 逐位一致。

## 6. 本目录有什么、没什么

| 有 | 没有（刻意不入库） |
| --- | --- |
| `yolo_onnx.py` 检测器 | **样例图**：是生产机上 `helmet_service/data/uploads/` 里用户上传的现场照片 |
| `parity.py` 逐框比对 | **ONNX 模型**：体积大（10MB / 80MB），且元数据标注 AGPL-3.0 |
| `ref_ultralytics.py` 参照生成（在 AISERVER 上用它的 venv 跑，只读，结果写 /tmp） | 参照结果 json（可随时重跑生成） |

## 7. 落成视觉域还差什么

1. **非测点输入源**（`AI-13 §3` 许下的）：`Binding` 现在只有"角色 → globalId"，图片输入要另立一类 —— 契约 1.4；
2. **事件驱动**：现有调度是按节拍轮询测点；图片是"来一张算一次"，要一条新入口；
3. **模型作为工件**：已有工作台工件表与"推理时把启用的工件交给模块"（`Frame.artifacts`），
   onnxruntime 可直接从字节建会话 —— 模型走这条路**不需要新机制**；
4. **域依赖的发布件**：onnxruntime + numpy + opencv-headless，py3.10 环境里装好后站点包约 49 + 34 + 72 MB；
5. ★**许可问题要先定**：模型权重的 AGPL-3.0 标注，对商用部署是否构成约束，需要有人判断。
