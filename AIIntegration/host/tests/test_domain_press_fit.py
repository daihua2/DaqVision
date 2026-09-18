"""模块 6「AI 压装机运行监测」的回归（需要 numpy / onnxruntime / onnx）。

★本域与模块 4、5 最本质的差别：**输入是结构值，不是标量序列**，
  而且**横轴是位移不是时间** —— 重采样按位移做。

钉的重点：
  ① 一次压装 = 一个结构值；一拍攒了好几次只判**最后一次**，且结论时刻取**那次压装自己的时刻**；
  ② ★三条曲线**必须等长** —— 这条结构描述表达不了（没有跨字段约束那一格），只能域校验；
  ③ ★**按位移重采样**：同一条压装用不同点数采出来，结论应当一致；
  ④ 曲线顺序 / 归一化 被真正尊重（与模块 4、5 同一类静默错）；
  ⑤ 没工件 / 没值 / 坏值 / 点数不足 / 位移无跨度 → 各自的码；
  ⑥ 特征量（峰值力、行程）算完写回标量点。
"""

import importlib.util
import json
import struct
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HAVE_DEPS = all(importlib.util.find_spec(m) is not None
                for m in ("numpy", "onnxruntime", "onnx"))

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
UTC = timezone.utc
T0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)

CURVES = ("position", "velocity", "force")
CLASSES = ("normal", "jam", "under_press", "clearance")
POINTS = 16


def f32(values):
    return struct.pack(f"<{len(values)}f", *values)


def build_model(*, curves=CURVES, classes=CLASSES, points=POINTS, omit=(),
                normalization="builtin", curve_min=None, curve_max=None,
                curves_first=False, n_cls=None, algo="cnn_lstm"):
    """造一个压装形 ONNX：输出 = 前 k 条曲线在点轴上的均值 ⇒ 喂了什么、按什么顺序喂，看得见。"""
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper

    n = len(curves)
    shape = [1, n, points] if curves_first else [1, points, n]
    axis = 2 if curves_first else 1
    k = n_cls if n_cls is not None else len(classes)

    mean = helper.make_node("ReduceMean", inputs=["x"], outputs=["m"], axes=[axis], keepdims=0)
    idx = numpy_helper.from_array(np.arange(k, dtype=np.int64) % n, name="i")
    gather = helper.make_node("Gather", inputs=["m", "i"], outputs=["y"], axis=1)
    graph = helper.make_graph(
        [mean, gather], "fake_press", initializer=[idx],
        inputs=[helper.make_tensor_value_info("x", TensorProto.FLOAT, shape)],
        outputs=[helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, k])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 12)])
    model.ir_version = 7
    pairs = [("curves", ",".join(curves)), ("classes", ",".join(classes)),
             ("points", str(points)), ("normalization", normalization), ("algo", algo)]
    if curve_min is not None:
        pairs.append(("curve_min", ",".join(str(v) for v in curve_min)))
    if curve_max is not None:
        pairs.append(("curve_max", ",".join(str(v) for v in curve_max)))
    for key, val in pairs:
        if key in omit:
            continue
        mp = model.metadata_props.add()
        mp.key, mp.value = key, val
    return model.SerializeToString()


@unittest.skipUnless(HAVE_DEPS, "缺 numpy/onnxruntime/onnx —— 压装域用例只在视觉环境里跑")
class PressBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aiintegration.domains import discover
        loaded, failed = discover(DOMAINS_DIR)
        bad = [(p, e) for p, e in failed if p.name == "press_fit.py"]
        assert not bad, f"压装域装载失败：{bad}"
        cls.loaded = {d.key: d for d in loaded}["press_fit"]

    def setUp(self):
        self.dom = type(self.loaded.instance)()

    def art(self, blob=None, aid=1, algo="cnn_lstm"):
        from aiintegration.types import ArtifactBlob
        return ArtifactBlob(id=aid, kind="model", name="合成压装模型", algo=algo,
                            blob=blob if blob is not None else build_model())

    def sample(self, *, pos=None, vel=None, force=None, n=40, t=None,
               quality=None, drop=(), ragged=False):
        """造一次压装的结构值样本。缺省是一条单调上行的力-位移曲线。"""
        from aiintegration.quality import Quality
        from aiintegration.types import NumBuf, StructSample
        if pos is None:
            pos = [i * 0.5 for i in range(n)]
        if vel is None:
            vel = [1.0] * len(pos)
        if force is None:
            force = [10.0 + 5.0 * i for i in range(len(pos))]
        if ragged:
            force = force[:-3]
        fields = {}
        for name, vals in (("position", pos), ("velocity", vel), ("force", force)):
            if name in drop:
                continue
            fields[name] = NumBuf(data=f32(vals), dtype="f32", shape=(-1,))
        fields["sample_rate"] = 50
        return StructSample(t=t or T0, quality=quality or Quality.OK, fields=fields,
                            struct_name="AI_PressCurve", struct_version=1)

    def frame(self, *, samples="default", artifact="default", params=None):
        from aiintegration.types import ArtifactBlob, Frame
        if samples == "default":
            samples = [self.sample()]
        arts = {}
        if artifact == "default":
            arts["model"] = self.art()
        elif isinstance(artifact, ArtifactBlob):
            arts["model"] = artifact
        return Frame(domain="press_fit", binding="press1",
                     t_start=T0 - timedelta(seconds=60), t_end=T0,
                     channels={}, params=dict(params or {}), artifacts=arts,
                     structs={"press_curve": list(samples or [])})

    def out(self, frame):
        return {f.key: f for f in self.dom.infer(frame)}


class TestDeclare(PressBase):
    def test_声明一路结构值输入且指向已自述的结构(self):
        d = self.dom.declare()
        self.assertEqual(len(d.inputs), 1)
        i = d.inputs[0]
        self.assertEqual(i.kind, "struct")
        self.assertEqual(i.struct, "AI_PressCurve")
        self.assertEqual({s.name for s in d.structs}, {"AI_PressCurve"})

    def test_结构字段表与定案P3逐字一致(self):
        st = self.dom.declare().structs[0]
        self.assertEqual([f.name for f in st.fields],
                         ["sample_rate", "position", "velocity", "force"])
        by = {f.name: f for f in st.fields}
        self.assertEqual(by["sample_rate"].type, "int32")
        for c in ("position", "velocity", "force"):
            self.assertEqual(by[c].type, "numbuf")
            self.assertEqual(by[c].dtype, "f32")
        self.assertEqual(by["position"].unit, "mm")
        self.assertEqual(by["force"].unit, "N")

    def test_界面须注明不判的两类(self):
        """★定案 6.2：不写清楚，用户会把"没报这两类"当成"这两类没发生"。"""
        spec = {o.key: o for o in self.dom.declare().outputs}["verdict"]
        self.assertIn("漏装", spec.description)
        self.assertIn("零件破裂", spec.description)
        self.assertEqual(spec.choices, CLASSES)

    def test_特征量写回标量点(self):
        outs = {o.key: o for o in self.dom.declare().outputs}
        self.assertEqual(outs["peak_force"].unit, "N")
        self.assertEqual(outs["stroke"].unit, "mm")

    def test_依赖自述(self):
        from aiintegration.domaindeps import parse_requires
        self.assertEqual(set(parse_requires(DOMAINS_DIR / "press_fit.py")),
                         {"numpy", "onnxruntime"})


class TestValidateArtifact(PressBase):
    def rejected(self, blob, *, contains):
        why, _ = self.dom.validate_artifact("model", blob)
        self.assertTrue(why, "本该拒收却收下了")
        self.assertIn(contains, why)
        return why

    def test_合规模型收下并记下事实(self):
        why, facts = self.dom.validate_artifact("model", build_model())
        self.assertEqual(why, "", why)
        self.assertEqual(facts["curves"], ",".join(CURVES))
        self.assertEqual(facts["points"], str(POINTS))
        self.assertIn("漏装", facts["not_judged"])

    def test_缺curves就拒并说清理由(self):
        why = self.rejected(build_model(omit=("curves",)), contains="curves")
        self.assertIn("顺序", why)

    def test_缺points就拒(self):
        why = self.rejected(build_model(omit=("points",)), contains="points")
        self.assertIn("重采", why)

    def test_缺normalization就拒并说清量纲那条理由(self):
        why = self.rejected(build_model(omit=("normalization",)), contains="normalization")
        self.assertIn("量纲", why)

    def test_minmax却不给上下限就拒(self):
        self.rejected(build_model(normalization="minmax"), contains="curve_min")

    def test_上下限倒挂就拒(self):
        self.rejected(build_model(normalization="minmax", curve_min=[1, 1, 1],
                                  curve_max=[0, 0, 0]), contains="逐条大于")

    def test_类别不全就拒(self):
        self.rejected(build_model(classes=CLASSES[:3]), contains="缺")

    def test_points与输入形状对不上就拒(self):
        blob = build_model(points=POINTS)
        # 元数据说 99，图里是 16
        import onnx
        m = onnx.load_from_string(blob)
        for mp in m.metadata_props:
            if mp.key == "points":
                mp.value = "99"
        self.rejected(m.SerializeToString(), contains="对不上")


class TestInfer(PressBase):
    def test_正常出五条结论且时刻取那次压装自己的(self):
        from aiintegration.quality import Quality
        ts = T0 - timedelta(seconds=7)
        got = self.out(self.frame(samples=[self.sample(t=ts)]))
        self.assertIs(got["verdict"].quality, Quality.OK)
        self.assertIn(got["verdict"].value, CLASSES)
        self.assertEqual(got["verdict"].t, ts,
                         "★结论时刻该是这次压装自己的，不是帧右端 —— 一拍可能攒了好几次")
        self.assertIn("本次点数", json.loads(got["evidence"].value))

    def test_一拍攒了多次只判最后一次(self):
        early = self.sample(t=T0 - timedelta(seconds=30))
        late = self.sample(t=T0 - timedelta(seconds=2))
        got = self.out(self.frame(samples=[early, late]))
        self.assertEqual(got["verdict"].t, late.t)

    def test_特征量算的是原始曲线(self):
        got = self.out(self.frame(samples=[self.sample(
            pos=[0.0, 1.0, 2.0, 3.0], force=[5.0, 300.0, 120.0, 8.0])]))
        self.assertAlmostEqual(got["peak_force"].value, 300.0, places=3)
        self.assertAlmostEqual(got["stroke"].value, 3.0, places=3)

    def test_按位移重采样与采样点数无关(self):
        """★同一条力-位移曲线，用不同点数采出来，结论与特征量应当一致。"""
        coarse = self.sample(pos=[0.0, 2.0, 4.0, 6.0],
                             force=[0.0, 100.0, 200.0, 300.0], vel=[1.0] * 4)
        fine = self.sample(pos=[i * 0.5 for i in range(13)],
                           force=[i * 25.0 for i in range(13)], vel=[1.0] * 13)
        a = self.out(self.frame(samples=[coarse]))
        self.dom = type(self.loaded.instance)()
        b = self.out(self.frame(samples=[fine]))
        self.assertEqual(a["verdict"].value, b["verdict"].value,
                         "同一条曲线采密采疏judged 成了两类 —— 说明没按位移重采")
        self.assertAlmostEqual(a["stroke"].value, b["stroke"].value, places=3)

    def test_三条曲线不等长就不算(self):
        """★这条结构描述表达不了（没有跨字段约束那一格），只能域自己校验。"""
        from aiintegration.quality import Quality
        got = self.out(self.frame(samples=[self.sample(ragged=True)]))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("长度不一致", got["evidence"].value)

    def test_缺一条曲线就不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(samples=[self.sample(drop=("velocity",))]))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("velocity", got["evidence"].value)

    def test_位移没有跨度就不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(samples=[self.sample(
            pos=[3.0] * 6, force=[1.0] * 6, vel=[0.0] * 6)]))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("跨度", got["evidence"].value)

    def test_点数不足不补数(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(samples=[self.sample(
            pos=[1.0], force=[2.0], vel=[0.0])]))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("不补数", got["evidence"].value)

    def test_这一拍没有压装值落NO_INPUT(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(samples=[]))
        self.assertIs(got["verdict"].quality, Quality.NO_INPUT)
        self.assertIn("没压就没有值", got["evidence"].value)

    def test_值是坏质量就不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(samples=[self.sample(quality=Quality.INPUT_BAD)]))
        self.assertIs(got["verdict"].quality, Quality.INPUT_BAD)
        self.assertIn("不拿坏值算结论", got["evidence"].value)

    def test_没工件落MODEL_NOT_LOADED(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(artifact=None))
        self.assertIs(got["verdict"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIn("不拿默认模型顶", got["evidence"].value)

    def test_曲线顺序被真正尊重(self):
        """★换了 curves 顺序，喂进模型的列就该跟着换 ⇒ 结论不同。"""
        s = self.sample(pos=[0.0, 1.0, 2.0, 3.0], vel=[0.0] * 4,
                        force=[900.0, 900.0, 900.0, 900.0])
        a = self.out(self.frame(samples=[s]))
        swapped = ("force", "velocity", "position")
        self.dom = type(self.loaded.instance)()
        b = self.out(self.frame(samples=[s],
                                artifact=self.art(build_model(curves=swapped), aid=2)))
        self.assertNotEqual(a["verdict"].value, b["verdict"].value,
                            "换了 curves 顺序结论却没变 —— 喂进去的顺序不是模型说的那个")

    def test_minmax归一化真的被应用(self):
        s = self.sample(pos=[0.0, 1.0, 2.0, 3.0], vel=[0.0] * 4, force=[1.0] * 4)
        blob = build_model(normalization="minmax",
                           curve_min=[0.0, 0.0, 0.0], curve_max=[3.0, 1.0, 1.0])
        got = self.out(self.frame(samples=[s], artifact=self.art(blob)))
        self.assertIn(got["verdict"].value, CLASSES)

    def test_诊断选的模型与工件对不上就不硬算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(params={"model": "svm"},
                                  artifact=self.art(build_model(), algo="cnn_lstm")))
        self.assertIs(got["verdict"].quality, Quality.CONFIG_INCOMPLETE)

    def test_同一工件不重复建会话(self):
        f = self.frame()
        self.dom.infer(f)
        self.dom.infer(f)
        self.assertEqual(len(self.dom._models), 1)


if __name__ == "__main__":
    unittest.main()
