"""对齐比对：我方 numpy+onnxruntime 流水线 vs helmet_service 现用 ultralytics（同 ONNX、同图、同阈值）。

判定（逐图、逐阈值）：
  · 框数一致；
  · 按顺序逐框：类别一致、框坐标最大偏差 ≤ 1.0 像素、置信度偏差 ≤ 0.005。
★顺序比对而不是"找最近的框配对"：配对式比对会把"多一个框、少一个框"互相抵消掉，看不出来。
"""
import json
import pathlib
import sys
import time

import cv2

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from yolo_onnx import YoloOnnxDetector  # noqa: E402

BOX_TOL, CONF_TOL = 1.0, 0.005

# 用法：parity.py [模型文件名] [参照 json 文件名]（都相对本目录）
MODEL_NAME = sys.argv[1] if len(sys.argv) > 1 else "yolo11n_safety_640_fp32.onnx"
REF_NAME = sys.argv[2] if len(sys.argv) > 2 else "aii_vision_ref.json"
ref = json.loads((HERE / REF_NAME).read_text(encoding="utf-8"))
det = YoloOnnxDetector(str(HERE / MODEL_NAME))
print(f"模型 {MODEL_NAME} | 参照 {REF_NAME}")
print(f"参照 ultralytics {ref['ultralytics']} | 我方 names={det.names} imgsz={det.imgsz} nc={det.nc}")

bad = 0
worst_box = worst_conf = 0.0
for conf_s, run in ref["runs"].items():
    conf = float(conf_s)
    for name, r in run.items():
        img = cv2.imread(str(HERE / "imgs" / name), cv2.IMREAD_COLOR)
        if list(img.shape) != r["shape"]:
            print(f"  ✗ {name[:8]} 读出尺寸 {img.shape} 与参照 {r['shape']} 不同（读图方式不一致）")
            bad += 1
            continue
        t = time.perf_counter()
        mine = det.detect(img, conf=conf, iou=ref["iou"])
        ms = (time.perf_counter() - t) * 1000
        theirs = r["dets"]
        if len(mine) != len(theirs):
            print(f"  ✗ conf={conf} {name[:8]} 框数 我方{len(mine)} vs 参照{len(theirs)}")
            bad += 1
            continue
        img_box = img_conf = 0.0
        ok = True
        for a, b in zip(mine, theirs):
            if a["cls"] != b["cls"]:
                ok = False
                break
            img_box = max(img_box, max(abs(x - y) for x, y in zip(a["box"], b["box"])))
            img_conf = max(img_conf, abs(a["conf"] - b["conf"]))
        worst_box, worst_conf = max(worst_box, img_box), max(worst_conf, img_conf)
        if not ok or img_box > BOX_TOL or img_conf > CONF_TOL:
            print(f"  ✗ conf={conf} {name[:8]} {len(mine)}框 类别{'一致' if ok else '不一致'} "
                  f"最大框差{img_box:.3f}px 最大分差{img_conf:.5f}")
            bad += 1
        else:
            print(f"  ✓ conf={conf} {name[:8]} {len(mine)}框 最大框差{img_box:.3f}px "
                  f"最大分差{img_conf:.5f}  {ms:.0f}ms  {r['shape'][1]}x{r['shape'][0]}")

print(f"\n全体最大框差 {worst_box:.3f}px，最大分差 {worst_conf:.5f}")
print("结论：" + ("全部对齐" if bad == 0 else f"{bad} 处不对齐"))
sys.exit(1 if bad else 0)
