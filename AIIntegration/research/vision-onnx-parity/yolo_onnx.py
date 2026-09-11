"""YOLO（v8/v11 无锚头）检测 —— **只用 onnxruntime + numpy + cv2，不 import torch / ultralytics**。

对齐目标：ultralytics `YOLO(onnx).predict()` 的结果（同模型、同图、同阈值）逐框一致。
每一处取整与比较都照它的实现写，差一个 `>` 与 `>=`、一个 `round` 与 `int`，框就会漂一个像素或多一个框：

  · letterbox：r=min(新/旧)，new_unpad 用 Python round；居中填充，top/bottom 用 round(dh∓0.1)，填 114；
    尺寸不变时不 resize；resize 用 INTER_LINEAR。
  · 前处理：BGR→RGB、HWC→CHW、/255、float32。
  · 候选：各类最大分 **严格大于** conf。
  · NMS：按类偏移（box + cls*7680）后做一次贪心 NMS；IoU **严格大于** 阈值才压掉（同 torchvision）；
    先按分数降序取前 30000，结果最多 300 个。
  · 还原：gain=min(640/h0, 640/w0)，pad=round((640 - w0*gain)/2 - 0.1)，减 pad、除 gain、裁到图内。
"""
from __future__ import annotations

import ast

import cv2
import numpy as np
import onnxruntime as ort

MAX_WH = 7680      # 按类偏移量（ultralytics 同名常量）
MAX_NMS = 30000
MAX_DET = 300


class YoloOnnxDetector:
    def __init__(self, model_path: str, providers: tuple[str, ...] = ("CPUExecutionProvider",),
                 intra_threads: int = 0) -> None:
        so = ort.SessionOptions()
        if intra_threads:
            so.intra_op_num_threads = intra_threads
        self.sess = ort.InferenceSession(model_path, sess_options=so, providers=list(providers))
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        shape = inp.shape                     # [1, 3, H, W]
        if not all(isinstance(d, int) for d in shape[2:]):
            raise ValueError(f"只支持固定输入尺寸的导出，收到 {shape}")
        self.imgsz = (int(shape[2]), int(shape[3]))
        meta = self.sess.get_modelmeta().custom_metadata_map
        # ★names 是 Python 字面量串（"{0: 'hat', ...}"）：用 literal_eval，**绝不用 eval**。
        self.names: dict[int, str] = ast.literal_eval(meta["names"]) if "names" in meta else {}
        out = self.sess.get_outputs()[0].shape   # [1, 4+nc, N]
        self.nc = int(out[1]) - 4
        if self.names and len(self.names) != self.nc:
            raise ValueError(f"元数据里 {len(self.names)} 个类名，输出却是 {self.nc} 类 —— 模型与名表对不上")

    # ── 前处理 ────────────────────────────────────────────────────────────
    def letterbox(self, img: np.ndarray) -> np.ndarray:
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

    def preprocess(self, img_bgr: np.ndarray) -> np.ndarray:
        x = self.letterbox(img_bgr)[..., ::-1].transpose(2, 0, 1)   # BGR→RGB, HWC→CHW
        return np.ascontiguousarray(x, dtype=np.float32)[None] / 255.0

    # ── 后处理 ────────────────────────────────────────────────────────────
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
            order = rest[iou <= iou_thres]          # ★严格大于才压掉，同 torchvision
        return np.asarray(keep, dtype=np.int64)

    def detect(self, img_bgr: np.ndarray, conf: float = 0.25, iou: float = 0.45,
               agnostic: bool = False) -> list[dict]:
        if img_bgr is None or img_bgr.ndim != 3 or img_bgr.shape[2] != 3:
            raise ValueError("需要 BGR 三通道图像")
        h0, w0 = img_bgr.shape[:2]
        pred = self.sess.run(None, {self.input_name: self.preprocess(img_bgr)})[0][0]   # [4+nc, N]
        pred = pred.T                                                                    # [N, 4+nc]
        cls_scores = pred[:, 4:4 + self.nc]
        best = cls_scores.max(1)
        cand = best > conf                         # ★严格大于
        if not cand.any():
            return []
        p = pred[cand]
        scores = best[cand]
        classes = cls_scores[cand].argmax(1)
        cx, cy, w, h = p[:, 0], p[:, 1], p[:, 2], p[:, 3]
        boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)

        order = scores.argsort(kind="stable")[::-1][:MAX_NMS]
        boxes, scores, classes = boxes[order], scores[order], classes[order]
        offset = 0 if agnostic else classes[:, None].astype(np.float32) * MAX_WH
        keep = self._nms(boxes + offset, scores, iou)[:MAX_DET]
        boxes, scores, classes = boxes[keep], scores[keep], classes[keep]

        # 还原到原图坐标
        nh, nw = self.imgsz
        gain = min(nh / h0, nw / w0)
        # ★缩放后的尺寸**先取整**再算填充，与 letterbox 的 new_unpad 一致。
        #   曾照老版本写成 (nw - w0*gain)/2：多数宽高比下结果相同，但取整跨界时（如 892×564，
        #   缩放后高 404.66 → letterbox 取 405、上填 117；老公式算出 118）框整体偏一个 letterbox 像素，
        #   还原到原图是 1/gain≈1.39px。对照 ultralytics 8.4.114 utils/ops.py::scale_boxes 源码订正。
        pad_x = round((nw - round(w0 * gain)) / 2 - 0.1)
        pad_y = round((nh - round(h0 * gain)) / 2 - 0.1)
        boxes = boxes.astype(np.float64)
        boxes[:, [0, 2]] -= pad_x
        boxes[:, [1, 3]] -= pad_y
        boxes /= gain
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w0)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h0)

        return [{"cls": int(c), "label": self.names.get(int(c), str(int(c))),
                 "conf": float(s), "box": [float(v) for v in b]}
                for b, s, c in zip(boxes, scores, classes)]
