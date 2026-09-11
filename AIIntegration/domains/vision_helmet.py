"""安全帽与安全背心检测 —— 第一个**图片输入**的域。

用户定（2026-09-11）：视觉域走 **CPU + onnxruntime，不拖 torch**。

---

## 0.1 为什么检测器代码写在这个文件里

分界文档的承诺是"新增一个域 = 丢一个 `.py`"。把 YOLO 前后处理抽成公共库，要么放进骨架
（骨架就开始懂算法了），要么搞一个域间共享包（装载器按文件路径装域，相对 import 不成立）。
⇒ 先内联。等第二个真用 YOLO 的域（数字表）落地时，再按真需求决定怎么共享 —— 与台账参数、
推理时工件那两格同一个做法：**由真需求逼出来，不提前抽象**。

## 0.2 这段检测器与现网逐位一致

它是 `research/vision-onnx-parity/yolo_onnx.py` 的内联版。那里有证据：
与 `helmet_service` 现用的 ultralytics 8.4.114，同 ONNX、同图、同阈值，n/m 两模型 × 两档阈值 × 6 张图，
**24 组框差 0.000px、分差 0.00000**。其中还原坐标那一步的取整（`round(w0*gain)` 先取整）是对着它源码订正过的。

## 0.3 模型从哪来

**不在本文件里、不在发布件里** —— 模型是**工件**：导入工作台、在工件页启用，推理时由骨架放进
`frame.artifacts["model"]`。没启用就没有，本域落 `MODEL_NOT_LOADED` 并说清怎么办，**不拿默认模型顶**。

★模型元数据标注 AGPL-3.0（ultralytics 导出时写入）。商用前是否可用需要有人判断 —— 本域代码不含模型，不受影响。

## 0.4 时刻

结论的 T = **拍照时刻**（`frame.blobs["image"].t`，由上传方给、骨架已校验带时区），不是上传时刻、不是算完的时刻。
"""

from __future__ import annotations

import ast
import json
import math
import threading

import cv2
import numpy as np
import onnxruntime as ort

from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import (
    Declaration, Finding, Frame, InputSpec, OutputSpec, ParamSpec,
)

#: 本域要求模型必须认得的类别。启用了别的模型（比如数字表的）就落码说清，不硬算。
REQUIRED_CLASSES = ("hat", "nohat", "novest", "person", "vest")
LABEL_ZH = {"hat": "已戴安全帽", "nohat": "未戴安全帽", "novest": "未穿安全背心",
            "person": "人员", "vest": "安全背心"}

#: 缺省置信度阈值 —— 与现网 helmet_service 的缺省（HELMET_CONFIDENCE=0.55）一致。
DEFAULT_CONF = 0.55
IOU = 0.45
#: 结论点 `detections` 里最多列多少个框（按置信度降序）。超出的在判据摘要里说明，不静默丢。
MAX_LISTED = 100

_MAX_WH, _MAX_NMS, _MAX_DET = 7680, 30000, 300


class _YoloOnnx:
    """YOLO（v8/v11 无锚头）检测，只用 onnxruntime + numpy + cv2。取整细节见 research 目录的说明。"""

    def __init__(self, model_bytes: bytes) -> None:
        self.sess = ort.InferenceSession(model_bytes, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        shape = inp.shape
        if len(shape) != 4 or not all(isinstance(d, int) for d in shape[2:]):
            raise ValueError(f"只支持固定输入尺寸的导出，收到输入形状 {shape}")
        self.input_name = inp.name
        self.imgsz = (int(shape[2]), int(shape[3]))
        meta = self.sess.get_modelmeta().custom_metadata_map
        # ★names 是 Python 字面量串：literal_eval，**绝不 eval**（模型文件是外来的）。
        names = ast.literal_eval(meta["names"]) if "names" in meta else {}
        self.names = {int(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}
        out = self.sess.get_outputs()[0].shape
        self.nc = int(out[1]) - 4 if len(out) == 3 and isinstance(out[1], int) else -1
        if self.nc <= 0:
            raise ValueError(f"输出形状 {out} 不是 [1, 4+类数, N]")
        if self.names and len(self.names) != self.nc:
            raise ValueError(f"元数据 {len(self.names)} 个类名，输出却是 {self.nc} 类 —— 模型与名表对不上")

    def _letterbox(self, img: np.ndarray) -> np.ndarray:
        h0, w0 = img.shape[:2]
        nh, nw = self.imgsz
        r = min(nh / h0, nw / w0)
        new_unpad = (int(round(w0 * r)), int(round(h0 * r)))
        dw, dh = (nw - new_unpad[0]) / 2, (nh - new_unpad[1]) / 2
        if (w0, h0) != new_unpad:
            img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
        top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
        left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
        return cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT,
                                  value=(114, 114, 114))

    @staticmethod
    def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thres: float) -> np.ndarray:
        order = scores.argsort(kind="stable")[::-1]
        x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        areas = (x2 - x1) * (y2 - y1)
        keep: list[int] = []
        while order.size:
            i = order[0]
            keep.append(int(i))
            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest]); yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest]); yy2 = np.minimum(y2[i], y2[rest])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            iou = inter / (areas[i] + areas[rest] - inter)
            order = rest[iou <= iou_thres]          # 严格大于才压掉，同 torchvision
        return np.asarray(keep, dtype=np.int64)

    def detect(self, img_bgr: np.ndarray, conf: float, iou: float) -> list[dict]:
        h0, w0 = img_bgr.shape[:2]
        x = self._letterbox(img_bgr)[..., ::-1].transpose(2, 0, 1)
        x = np.ascontiguousarray(x, dtype=np.float32)[None] / 255.0
        pred = self.sess.run(None, {self.input_name: x})[0][0].T          # [N, 4+nc]
        cls_scores = pred[:, 4:4 + self.nc]
        best = cls_scores.max(1)
        cand = best > conf                                                 # 严格大于
        if not cand.any():
            return []
        p, scores, classes = pred[cand], best[cand], cls_scores[cand].argmax(1)
        cx, cy, w, h = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
        boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)
        order = scores.argsort(kind="stable")[::-1][:_MAX_NMS]
        boxes, scores, classes = boxes[order], scores[order], classes[order]
        keep = self._nms(boxes + classes[:, None].astype(np.float32) * _MAX_WH, scores, iou)[:_MAX_DET]
        boxes, scores, classes = boxes[keep].astype(np.float64), scores[keep], classes[keep]
        nh, nw = self.imgsz
        gain = min(nh / h0, nw / w0)
        pad_x = round((nw - round(w0 * gain)) / 2 - 0.1)                   # ★内层先取整
        pad_y = round((nh - round(h0 * gain)) / 2 - 0.1)
        boxes[:, [0, 2]] -= pad_x
        boxes[:, [1, 3]] -= pad_y
        boxes /= gain
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w0)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h0)
        return [{"cls": int(c), "label": self.names.get(int(c), str(int(c))),
                 "conf": float(s), "box": [float(v) for v in b]}
                for b, s, c in zip(boxes, scores, classes)]


class HelmetDetection(Domain):
    """安全帽与安全背心检测（图片触发）。"""

    key = "vision_helmet"
    display = "安全帽与安全背心检测"
    version = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 工件 id → 检测器。工件只增不改，同一个 id 就是同一个模型，不必每张图重建会话。
        self._detectors: dict[int, _YoloOnnx] = {}

    def capabilities(self) -> set[str]:
        # 有模型工件可供查看/启用（能力位 artifact）；本域不训练。
        return {"infer", "artifact"}

    def declare(self) -> Declaration:
        return Declaration(
            inputs=(
                InputSpec(role="image", kind="image", required=True,
                          description="现场照片（jpg/png/webp/bmp），由上传触发；须带拍照时刻"),
            ),
            params=(
                ParamSpec(
                    key="conf_threshold", display="置信度阈值", value_type="float",
                    default=str(DEFAULT_CONF), required=False,
                    description=(
                        "低于它的框不算。缺省 0.55，与现网 helmet_service 一致。"
                        "★这一项允许有缺省：每次结论的判据摘要里都写明实际用的阈值，改了看得见。"
                        "调高会漏报未戴安全帽，调低会误报 —— 改之前请拿现场照片试")),
            ),
            outputs=(
                OutputSpec(key="person_count", display="人员数", value_type="int"),
                OutputSpec(key="hat_count", display="已戴安全帽", value_type="int"),
                OutputSpec(key="nohat_count", display="未戴安全帽", value_type="int"),
                OutputSpec(key="vest_count", display="已穿背心", value_type="int"),
                OutputSpec(key="novest_count", display="未穿背心", value_type="int"),
                OutputSpec(key="violation", display="存在违规", value_type="bool",
                           description="检出任一「未戴安全帽」或「未穿安全背心」即为真"),
                OutputSpec(key="detections", display="检出明细", value_type="string",
                           description=f"JSON 数组，按置信度降序，最多 {MAX_LISTED} 个框；"
                                       "box 为原图像素坐标 [x1,y1,x2,y2]，前端据此画框"),
                OutputSpec(key="evidence", display="判据摘要", value_type="string",
                           description="用了哪个模型、什么阈值、检出多少、为什么没给"),
            ),
        )

    # ── 推理 ──────────────────────────────────────────────────────────────
    def infer(self, frame: Frame) -> list[Finding]:
        blob = frame.blobs.get("image")
        if blob is None:
            return self._all(frame.t_end, Quality.NO_INPUT, "未收到图片（本域只由上传触发）")
        t = blob.t

        conf, why = _parse_conf(frame.params.get("conf_threshold", ""))
        if conf is None:
            return self._all(t, Quality.CONFIG_INCOMPLETE, why)

        art = frame.artifacts.get("model")
        if art is None:
            return self._all(t, Quality.MODEL_NOT_LOADED,
                             "没有启用的检测模型 —— 需先导入 ONNX 模型工件，并在工件页启用")
        try:
            det = self._detector(art)
        except Exception as exc:  # noqa: BLE001 —— 坏模型不许掀翻骨架
            return self._all(t, Quality.MODEL_NOT_LOADED,
                             f"模型工件「{art.name}」加载失败：{type(exc).__name__}: {exc}")
        missing = [c for c in REQUIRED_CLASSES if c not in det.names.values()]
        if missing:
            return self._all(t, Quality.MODEL_NOT_LOADED,
                             f"启用的模型「{art.name}」不是安全帽模型：类别表 "
                             f"{sorted(det.names.values())} 缺 {missing}")

        img = cv2.imdecode(np.frombuffer(blob.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return self._all(t, Quality.INPUT_BAD,
                             f"图片解不开（{blob.content_type or '未声明类型'}，{len(blob.data)} 字节）")

        dets = det.detect(img, conf=conf, iou=IOU)
        counts = {c: 0 for c in REQUIRED_CLASSES}
        for d in dets:
            counts[d["label"]] = counts.get(d["label"], 0) + 1
        violation = counts["nohat"] > 0 or counts["novest"] > 0

        listed = [{"label": d["label"], "label_zh": LABEL_ZH.get(d["label"], d["label"]),
                   "conf": round(d["conf"], 4), "box": [round(v, 1) for v in d["box"]]}
                  for d in dets[:MAX_LISTED]]
        h, w = img.shape[:2]
        summary = "、".join(f"{LABEL_ZH[c]} {counts[c]}" for c in REQUIRED_CLASSES)
        evidence = (f"模型「{art.name}」（工件 {art.id}），阈值 {conf}，iou {IOU}，图 {w}×{h}："
                    f"检出 {len(dets)} 个目标 —— {summary}；"
                    + ("★存在未戴安全帽或未穿安全背心" if violation else "未见违规"))
        if len(dets) > MAX_LISTED:
            evidence += f"；检出明细只列了置信度最高的 {MAX_LISTED} 个（共 {len(dets)} 个）"

        ok = Quality.OK
        return [
            Finding(key="person_count", value=counts["person"], quality=ok, t=t),
            Finding(key="hat_count", value=counts["hat"], quality=ok, t=t),
            Finding(key="nohat_count", value=counts["nohat"], quality=ok, t=t),
            Finding(key="vest_count", value=counts["vest"], quality=ok, t=t),
            Finding(key="novest_count", value=counts["novest"], quality=ok, t=t),
            Finding(key="violation", value=violation, quality=ok, t=t),
            Finding(key="detections", value=json.dumps(listed, ensure_ascii=False), quality=ok, t=t),
            Finding(key="evidence", value=evidence, quality=ok, t=t),
        ]

    def _detector(self, art) -> _YoloOnnx:
        with self._lock:
            det = self._detectors.get(art.id)
            if det is None:
                det = _YoloOnnx(bytes(art.blob))
                self._detectors[art.id] = det
            return det

    def _all(self, t, q: Quality, why: str) -> list[Finding]:
        """一条都算不出来：每个输出各落一个坏值锚点，`evidence` 写原因（不是什么都不发）。"""
        keys = [o.key for o in self.declare().outputs if o.key != "evidence"]
        out = [Finding(key=k, value=None, quality=q, t=t) for k in keys]
        out.append(Finding(key="evidence", value=why, quality=q, t=t))
        return out


def _parse_conf(raw: str) -> tuple[float | None, str]:
    """阈值：空 = 缺省；给了就必须是 [0.05, 0.95] 内的数。**给错不替它改成缺省** —— 落码说清。"""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_CONF, ""
    try:
        v = float(raw)
    except ValueError:
        return None, f"台账 conf_threshold={raw!r} 不是数"
    if not math.isfinite(v) or not (0.05 <= v <= 0.95):
        return None, f"台账 conf_threshold={raw!r} 超出 [0.05, 0.95]"
    return v, ""
