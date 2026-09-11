"""参照结果：在 AISERVER 上用 helmet_service 现用的 ultralytics，跑**同一个 ONNX、同一批图**。

★只读：不写任何项目目录（predict 默认不存图），结果写 /tmp/aii_vision_ref.json。
★读图方式与它 /api/detect 一致：cv2 按 IMREAD_COLOR 读（PNG 的 alpha 被丢弃，得 BGR 三通道）。
用法（AISERVER）：
  /home/ruiteng/2026/meter_service/digital_meter_service/.venv/bin/python /tmp/ref_ultralytics.py
"""
import json
import sys

import cv2
import ultralytics
from ultralytics import YOLO

# 可指定模型与输出路径：python ref_ultralytics.py [模型路径] [输出 json]
MODEL = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/ruiteng/2026/meter_service/helmet_service/models/yolo11n_safety_640_fp32.onnx"
OUT_PATH = sys.argv[2] if len(sys.argv) > 2 else "/tmp/aii_vision_ref.json"
UP = "/home/ruiteng/2026/meter_service/helmet_service/data/uploads/"
IMAGES = [
    "18f64b6d3f1b42ca8ebc4475a62441ef.png", "56efa01509ea4cde89cf86d7eeb88e15.png",
    "75c06546dddf4c66b054283013e998c8.png", "e272c11c91b0494990adffd68b7fdf78.png",
    "4921b0759ac643d3b52b30eed0272170.png", "cbdd0ca9b73348df897150932c80049f.jpg",
]
CONFS = (0.55, 0.25)      # 0.55 = 它的线上缺省；0.25 出框多，更能考 NMS 与排序
IOU, IMGSZ = 0.45, 640

model = YOLO(MODEL, task="detect")
out = {"ultralytics": ultralytics.__version__, "model": MODEL, "iou": IOU, "imgsz": IMGSZ, "runs": {}}
for conf in CONFS:
    run = {}
    for name in IMAGES:
        img = cv2.imread(UP + name, cv2.IMREAD_COLOR)
        r = model.predict(source=img, conf=conf, iou=IOU, imgsz=IMGSZ, device="cpu", verbose=False)[0]
        dets = []
        if r.boxes is not None:
            for box, cls, score in zip(r.boxes.xyxy.cpu().tolist(), r.boxes.cls.cpu().tolist(),
                                       r.boxes.conf.cpu().tolist()):
                dets.append({"cls": int(cls), "label": r.names[int(cls)], "conf": float(score),
                             "box": [float(v) for v in box]})
        run[name] = {"shape": list(img.shape), "dets": dets}
    out["runs"][str(conf)] = run

with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("ultralytics", ultralytics.__version__)
for conf, run in out["runs"].items():
    print(f"conf={conf}: " + ", ".join(f"{k[:8]}={len(v['dets'])}框{v['shape'][:2]}" for k, v in run.items()))
