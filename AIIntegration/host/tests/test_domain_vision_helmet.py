"""安全帽检测域的回归 —— 需要 onnxruntime / numpy / opencv / onnx（视觉环境里跑；零依赖环境整类跳过）。

★不用真模型：真模型大、且元数据标 AGPL-3.0，不进仓库。这里**在测试里现造一个 ONNX**：
  输出是一个常量张量 `[1, 9, 8400]`，里面按需摆几个候选框 —— 于是解码、阈值、NMS、计数、
  坐标还原、各种坏质量码，每一条都能被**可控地**钉住。
  与真模型逐位对齐的证据在 `research/vision-onnx-parity/`（那边要现场数据，不在单测里）。

钉的重点：
  ① 计数与"是否违规"对；同类重叠被 NMS 压、异类不压；阈值严格大于；
  ② T = 拍照时刻；
  ③ 没模型 / 模型不是安全帽的 / 模型坏了 → MODEL_NOT_LOADED，并说清怎么办；
  ④ 台账阈值写错 → CONFIG_INCOMPLETE，**不替它改成缺省**；
  ⑤ 图片解不开 → INPUT_BAD；没收到图 → NO_INPUT；
  ⑥ 明细超上限要截断且**说出来**。
"""

import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HAVE_DEPS = all(importlib.util.find_spec(m) is not None
                for m in ("numpy", "cv2", "onnxruntime", "onnx"))

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
CN = timezone(timedelta(hours=8))
SHOT = datetime(2026, 9, 11, 9, 30, 0, tzinfo=CN)
HELMET_NAMES = {0: "hat", 1: "nohat", 2: "novest", 3: "person", 4: "vest"}
PERSON, NOHAT, NOVEST, HAT, VEST = 3, 1, 2, 0, 4


def build_model(candidates=(), names=None) -> bytes:
    """造一个输出固定的 YOLO 形 ONNX。candidates: (cx, cy, w, h, 类, 分)。"""
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper
    out = np.zeros((1, 9, 8400), dtype=np.float32)
    for k, (cx, cy, w, h, cls, score) in enumerate(candidates):
        out[0, :4, k] = (cx, cy, w, h)
        out[0, 4 + cls, k] = score
    node = helper.make_node("Constant", inputs=[], outputs=["output0"],
                            value=numpy_helper.from_array(out, name="c"))
    graph = helper.make_graph(
        [node], "fake_yolo",
        inputs=[helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 640, 640])],
        outputs=[helper.make_tensor_value_info("output0", TensorProto.FLOAT, [1, 9, 8400])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 12)])
    model.ir_version = 7
    meta = model.metadata_props.add()
    meta.key, meta.value = "names", str(names if names is not None else HELMET_NAMES)
    return model.SerializeToString()


def png(w=640, h=640) -> bytes:
    import cv2
    import numpy as np
    ok, buf = cv2.imencode(".png", np.zeros((h, w, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


#: 640×640 的图 ⇒ letterbox 不缩放不填充，候选框坐标与原图坐标一一对应，好断言。
SCENE = (
    (125, 140, 50, 80, PERSON, 0.90),   # → [100,100,150,180]
    (300, 300, 40, 40, NOHAT, 0.80),    # → [280,280,320,320]
    (302, 302, 40, 40, NOHAT, 0.70),    # 与上一个同类、IoU≈0.82 ⇒ 被 NMS 压掉
    (302, 302, 40, 40, HAT, 0.60),      # 同位置但**异类** ⇒ 不压
    (500, 500, 30, 30, VEST, 0.30),     # 低于缺省阈值 0.55 ⇒ 不算；阈值 0.25 时算
)


@unittest.skipUnless(HAVE_DEPS, "缺 onnxruntime/numpy/opencv/onnx —— 视觉域用例只在视觉环境里跑")
class HelmetBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aiintegration.domains import discover
        loaded, failed = discover(DOMAINS_DIR)
        helmet_failures = [(p, e) for p, e in failed if p.name == "vision_helmet.py"]
        assert not helmet_failures, f"安全帽域装载失败：{helmet_failures}"
        cls.loaded = {d.key: d for d in loaded}["vision_helmet"]

    def setUp(self):
        # 每个用例一个新实例，免得检测器缓存串味
        self.dom = type(self.loaded.instance)()

    def art(self, blob=None, aid=1, name="合成模型"):
        from aiintegration.types import ArtifactBlob
        return ArtifactBlob(id=aid, kind="model", name=name,
                            blob=blob if blob is not None else build_model(SCENE))

    def frame(self, *, image=True, data=None, params=None, artifact="default"):
        from aiintegration.types import Frame, InputBlob
        blobs = {}
        if image:
            blobs["image"] = InputBlob(t=SHOT, content_type="image/png",
                                       data=data if data is not None else png())
        arts = {}
        if artifact == "default":
            arts["model"] = self.art()
        elif artifact is not None:
            arts["model"] = artifact
        return Frame(domain="vision_helmet", binding="cam1", t_start=SHOT, t_end=SHOT,
                     channels={}, blobs=blobs, params=params or {}, artifacts=arts)

    def infer(self, **kw):
        return {f.key: f for f in self.dom.infer(self.frame(**kw))}


class TestDeclare(HelmetBase):
    def test_能力位与输入形态(self):
        self.assertEqual(self.loaded.caps, frozenset({"infer", "artifact"}))
        (inp,) = self.loaded.declaration.inputs
        self.assertEqual((inp.role, inp.kind), ("image", "image"))

    def test_阈值台账允许缺省且理由写明(self):
        (p,) = self.loaded.declaration.params
        self.assertEqual((p.key, p.default, p.required), ("conf_threshold", "0.55", False))
        self.assertIn("看得见", p.description)


class TestDetect(HelmetBase):
    def test_缺省阈值下计数与违规(self):
        from aiintegration.quality import Quality
        out = self.infer()
        self.assertTrue(all(f.quality is Quality.OK for f in out.values()))
        counts = {k: out[k].value for k in
                  ("person_count", "hat_count", "nohat_count", "vest_count", "novest_count")}
        self.assertEqual(counts, {"person_count": 1, "hat_count": 1, "nohat_count": 1,
                                  "vest_count": 0, "novest_count": 0})
        self.assertIs(out["violation"].value, True)

    def test_同类重叠被压异类不压(self):
        dets = json.loads(self.infer()["detections"].value)
        labels = [d["label"] for d in dets]
        self.assertEqual(labels, ["person", "nohat", "hat"], "按置信度降序；第二个 nohat 被压；hat 不压")

    def test_坐标还原到原图像素(self):
        dets = json.loads(self.infer()["detections"].value)
        self.assertEqual(dets[0]["box"], [100.0, 100.0, 150.0, 180.0])
        self.assertEqual(dets[1]["box"], [280.0, 280.0, 320.0, 320.0])
        self.assertEqual(dets[1]["label_zh"], "未戴安全帽")

    def test_非方图的坐标还原(self):
        # 1280×640 的图 ⇒ gain=0.5、左右不填、上下各填 160。
        # 模型空间框 (cx300,cy300,w40,h40) = [280,280,320,320]
        #   → 减填充 [280,120,320,160] → 除 gain [560,240,640,320]。
        # （第一版把 y 算成 280~360 —— 漏减了填充，是用例算错，被测代码是对的。）
        blob = build_model([(300, 300, 40, 40, PERSON, 0.9)])
        out = self.infer(data=png(1280, 640), artifact=self.art(blob))
        (d,) = json.loads(out["detections"].value)
        self.assertEqual(d["box"], [560.0, 240.0, 640.0, 320.0])

    def test_取整跨界的宽高比与现网一致(self):
        """★research 对齐时查出的那处取整的回归（此前单测没钉住，只有现场数据能暴露）。

        892×564 这类宽高比：缩放后高 404.66，letterbox 取 405、上填 117；
        漏了内层取整会算成 118，框整体偏 1/gain≈1.39px。期望值按 ultralytics 8.4.114 scale_boxes 手算：
          gain=640/892，pad_x=0，pad_y=round((640-405)/2-0.1)=117；
          模型空间 [426,280,466,320] → 减填充 [426,163,466,203] → ×892/640
          = [593.7375, 227.18125, 649.4875, 282.93125] → 保留一位 [593.7, 227.2, 649.5, 282.9]
        """
        blob = build_model([(446, 300, 40, 40, PERSON, 0.9)])
        out = self.infer(data=png(892, 564), artifact=self.art(blob))
        (d,) = json.loads(out["detections"].value)
        self.assertEqual(d["box"], [593.7, 227.2, 649.5, 282.9])

    def test_只有未穿背心也算违规(self):
        blob = build_model([(100, 100, 20, 20, PERSON, 0.9), (100, 140, 30, 40, NOVEST, 0.9)])
        out = self.infer(artifact=self.art(blob))
        self.assertEqual((out["nohat_count"].value, out["novest_count"].value), (0, 1))
        self.assertIs(out["violation"].value, True, "未穿背心同样是违规，不能只认未戴安全帽")

    def test_调低阈值多出低分目标(self):
        out = self.infer(params={"conf_threshold": "0.25"})
        self.assertEqual(out["vest_count"].value, 1)
        self.assertIn("阈值 0.25", out["evidence"].value)

    def test_阈值是严格大于(self):
        blob = build_model([(100, 100, 20, 20, PERSON, 0.5)])
        out = self.infer(params={"conf_threshold": "0.5"}, artifact=self.art(blob))
        self.assertEqual(out["person_count"].value, 0, "分数恰好等于阈值不算")

    def test_没有违规时为假(self):
        blob = build_model([(100, 100, 20, 20, PERSON, 0.9), (100, 60, 20, 20, HAT, 0.9)])
        out = self.infer(artifact=self.art(blob))
        self.assertIs(out["violation"].value, False)
        self.assertIn("未见违规", out["evidence"].value)

    def test_T是拍照时刻(self):
        out = self.infer()
        self.assertTrue(all(f.t == SHOT.astimezone(timezone.utc) for f in out.values()))

    def test_判据摘要写明模型与阈值(self):
        ev = self.infer()["evidence"].value
        self.assertIn("合成模型", ev)
        self.assertIn("阈值 0.55", ev)
        self.assertIn("未戴安全帽 1", ev)

    def test_明细超上限截断并说出来(self):
        cands = [(25 + 50 * (i % 12), 25 + 50 * (i // 12), 30, 30, PERSON, 0.9) for i in range(120)]
        out = self.infer(artifact=self.art(build_model(cands)))
        self.assertEqual(out["person_count"].value, 120)
        self.assertEqual(len(json.loads(out["detections"].value)), 100)
        self.assertIn("共 120 个", out["evidence"].value)

    def test_检测器按工件id缓存(self):
        self.dom.infer(self.frame())
        self.dom.infer(self.frame())
        self.assertEqual(len(self.dom._detectors), 1)
        self.dom.infer(self.frame(artifact=self.art(aid=2)))
        self.assertEqual(len(self.dom._detectors), 2)


class TestBadQuality(HelmetBase):
    def assertAllBad(self, out, q, needle):
        for k, f in out.items():
            self.assertIs(f.quality, q, k)
            if k != "evidence":
                self.assertIsNone(f.value, f"{k} 坏质量下不许有值")
        self.assertIn(needle, out["evidence"].value)

    def test_没有启用模型(self):
        from aiintegration.quality import Quality
        self.assertAllBad(self.infer(artifact=None), Quality.MODEL_NOT_LOADED, "启用")

    def test_模型不是安全帽的(self):
        from aiintegration.quality import Quality
        blob = build_model(SCENE, names={0: "a", 1: "b", 2: "c", 3: "d", 4: "e"})
        self.assertAllBad(self.infer(artifact=self.art(blob)), Quality.MODEL_NOT_LOADED,
                          "不是安全帽模型")

    def test_模型字节坏了(self):
        from aiintegration.quality import Quality
        self.assertAllBad(self.infer(artifact=self.art(b"not an onnx model")),
                          Quality.MODEL_NOT_LOADED, "加载失败")

    def test_台账阈值写错不替它改成缺省(self):
        from aiintegration.quality import Quality
        for bad in ("abc", "0.99", "0", "nan"):
            self.assertAllBad(self.infer(params={"conf_threshold": bad}),
                              Quality.CONFIG_INCOMPLETE, "conf_threshold")

    def test_图片解不开(self):
        from aiintegration.quality import Quality
        self.assertAllBad(self.infer(data=b"definitely not an image"), Quality.INPUT_BAD, "解不开")

    def test_没收到图(self):
        from aiintegration.quality import Quality
        self.assertAllBad(self.infer(image=False), Quality.NO_INPUT, "未收到图片")


class TestThroughSkeleton(HelmetBase):
    def test_走骨架校验路径所有结论都被收下(self):
        from aiintegration.runner import run_domain
        res = run_domain(self.loaded, self.frame())
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(res.findings), len(self.loaded.declaration.outputs))

    def test_经事件入口端到端(self):
        """真域 + 事件入口 + 工件提供者：上传一张图 → 结论。"""
        import tempfile

        from aiintegration.bindings import Binding, BindingStore
        from aiintegration.events import EventRunner
        from aiintegration.pointmap import PointMap

        class Arts:
            def __init__(self, art):
                self.art = art

            def for_binding(self, domain, binding):
                return {"model": self.art}

        with tempfile.TemporaryDirectory() as d:
            b = BindingStore(Path(d) / "b.db")
            p = PointMap(Path(d) / "p.db")
            try:
                b.put(Binding("vision_helmet", "cam1", {}), allow_no_roles=True)
                runner = EventRunner(domains={"vision_helmet": self.loaded}, bindings=b, points=p,
                                     artifacts=Arts(self.art()))
                res = runner.submit(domain="vision_helmet", binding="cam1", data=png(),
                                    content_type="image/png", captured_at=SHOT)
                got = {f.key: f.value for f in res.run.findings}
                self.assertEqual(got["nohat_count"], 1)
                self.assertIs(got["violation"], True)
                self.assertFalse(res.written)
            finally:
                b.close(); p.close()


if __name__ == "__main__":
    unittest.main()
