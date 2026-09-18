"""模块 5「AI 变频器（VFD）运行监测」的回归（需要 numpy / onnxruntime / onnx）。

★同模块 4：不用真模型，在测试里现造 ONNX。本域的模型有**三个头**
（6 类分类 / 当前严重度 / 未来严重度 ×3），造的模型让输出可控且**与输入有关**，
于是"通道顺序""归一化做没做"这两条最容易静默出错的判据才钉得住。

钉的重点：
  ① 15 路与 6 类**与原项目逐字一致**（名字或次序一变就是换了一套语义）；
  ② 未来严重度三条**是模型给的**，本域不外推；
  ③ ★`normalization` 必须自述 —— 少做一次 z-score 不报错，模型照样吐出像样的数；
  ④ 通道顺序被真正尊重；
  ⑤ 没工件 / 点数不足 / 坏值 / 缺一路 / 疏密对不上 → 各自的码，不补数不拉伸；
  ⑥ 三个头的条数对不上 → 拒收或 COMPUTE_ERROR。
"""

import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HAVE_DEPS = all(importlib.util.find_spec(m) is not None
                for m in ("numpy", "onnxruntime", "onnx"))

DOMAINS_DIR = Path(__file__).resolve().parents[2] / "domains"
UTC = timezone.utc
T_END = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)

ROLES = ("vin_l1l2", "vin_l2l3", "vin_l3l1", "iout_u", "iout_v", "iout_w",
         "output_freq", "motor_power_pct", "output_torque_pct", "cabinet_temp",
         "transformer_temp_a", "transformer_temp_b", "transformer_temp_c",
         "unit_vdc_mean", "unit_vdc_spread")
CLASSES = ("normal", "cooling_degradation", "power_unit_anomaly",
           "output_current_imbalance", "input_grid_anomaly", "overload")
WIN = 12            # 真现场是 60；用例用小的，判据一样
RATE = 1.0


def build_model(*, channels=ROLES, classes=CLASSES, rate_hz=RATE, window_len=WIN,
                channels_first=False, omit=(), normalization="builtin",
                scaler_mean=None, scaler_std=None, n_cls=None, n_cur=1, n_fut=3,
                time_dynamic=False, algo="tcn"):
    """造一个 VFD 形 ONNX：三个头都由**输入的通道均值**派生 ⇒ 喂了什么看得见。

      · 分类头 = 前 n_cls 路的均值（哪一路大就判哪一类）
      · 当前严重度 = 第 0 路均值经 Sigmoid
      · 未来严重度 = 第 1/2/3 路均值经 Sigmoid
    """
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper

    n = len(channels)
    t = "T" if time_dynamic else window_len
    shape = [1, n, t] if channels_first else [1, t, n]
    time_axis = 2 if channels_first else 1
    k = n_cls if n_cls is not None else len(classes)

    nodes = [helper.make_node("ReduceMean", inputs=["x"], outputs=["m"],
                              axes=[time_axis], keepdims=0)]
    inits = []

    def gather(name, indices, out):
        init = numpy_helper.from_array(np.asarray(indices, dtype=np.int64), name=name)
        inits.append(init)
        nodes.append(helper.make_node("Gather", inputs=["m", name], outputs=[out], axis=1))

    gather("i_cls", list(range(k)), "logits")
    gather("i_cur", list(range(n_cur)), "cur_raw")
    gather("i_fut", list(range(1, 1 + n_fut)), "fut_raw")
    nodes.append(helper.make_node("Sigmoid", inputs=["cur_raw"], outputs=["current"]))
    nodes.append(helper.make_node("Sigmoid", inputs=["fut_raw"], outputs=["future"]))

    graph = helper.make_graph(
        nodes, "fake_vfd", initializer=inits,
        inputs=[helper.make_tensor_value_info("x", TensorProto.FLOAT, shape)],
        outputs=[helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, k]),
                 helper.make_tensor_value_info("current", TensorProto.FLOAT, [1, n_cur]),
                 helper.make_tensor_value_info("future", TensorProto.FLOAT, [1, n_fut])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 12)])
    model.ir_version = 7
    pairs = [("channels", ",".join(channels)), ("classes", ",".join(classes)),
             ("rate_hz", str(rate_hz)), ("algo", algo),
             ("normalization", normalization)]
    if scaler_mean is not None:
        pairs.append(("scaler_mean", ",".join(str(v) for v in scaler_mean)))
    if scaler_std is not None:
        pairs.append(("scaler_std", ",".join(str(v) for v in scaler_std)))
    for key, val in pairs:
        if key in omit:
            continue
        m = model.metadata_props.add()
        m.key, m.value = key, val
    return model.SerializeToString()


@unittest.skipUnless(HAVE_DEPS, "缺 numpy/onnxruntime/onnx —— VFD 域用例只在视觉环境里跑")
class VfdBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aiintegration.domains import discover
        loaded, failed = discover(DOMAINS_DIR)
        bad = [(p, e) for p, e in failed if p.name == "vfd_health.py"]
        assert not bad, f"VFD 域装载失败：{bad}"
        cls.loaded = {d.key: d for d in loaded}["vfd_health"]

    def setUp(self):
        self.dom = type(self.loaded.instance)()

    def art(self, blob=None, aid=1, name="合成 VFD 模型", algo="tcn"):
        from aiintegration.types import ArtifactBlob
        return ArtifactBlob(id=aid, kind="model", name=name, algo=algo,
                            blob=blob if blob is not None else build_model())

    def frame(self, *, values=None, n=WIN, artifact="default", drop=(), bad=(),
              rate=RATE, non_numeric=()):
        from aiintegration.quality import Quality
        from aiintegration.types import ArtifactBlob, Frame, Sample
        vals = dict(values or {})
        channels = {}
        step = timedelta(seconds=1.0 / rate)
        for role in ROLES:
            if role in drop:
                continue
            v = float(vals.get(role, 0.0))
            samples = []
            for i in range(n):
                q = Quality.INPUT_BAD if (role in bad and i == n - 1) else Quality.OK
                value = "坏" if (role in non_numeric and i == n - 1) else v
                samples.append(Sample(t=T_END - step * (n - 1 - i), value=value, quality=q))
            channels[role] = samples
        arts = {}
        if artifact == "default":
            arts["model"] = self.art()
        elif isinstance(artifact, ArtifactBlob):
            arts["model"] = artifact
        return Frame(domain="vfd_health", binding="vfd1",
                     t_start=T_END - timedelta(seconds=120), t_end=T_END,
                     channels=channels, artifacts=arts)

    def out(self, frame):
        return {f.key: f for f in self.dom.infer(frame)}


class TestDeclare(VfdBase):
    def test_十五路与原项目逐字一致(self):
        """★名字或次序一变就是换了一套语义 —— 与原项目 model.py 的 CHANNELS 对齐。"""
        d = self.dom.declare()
        self.assertEqual(tuple(i.role for i in d.inputs), ROLES)
        self.assertTrue(all(i.required for i in d.inputs), "15 路是一个整体")

    def test_六类与原项目逐字一致(self):
        """★连**次序**一起钉：LABEL_MAP 的次序就是模型输出的下标次序，错位 = 张冠李戴。"""
        spec = {o.key: o for o in self.dom.declare().outputs}["fault_type"]
        self.assertEqual(
            spec.description,
            "正常 / 冷却系统退化 / 功率单元异常 / 输出电流不平衡 / 输入电网异常 / 过载 / 异常负载")

    def test_未来严重度三条都在且说明不是外推(self):
        outs = {o.key: o for o in self.dom.declare().outputs}
        for h in (30, 60, 120):
            self.assertIn(f"severity_{h}s", outs, "定案明确要求保留未来严重度三项")
            self.assertIn("模型给的", outs[f"severity_{h}s"].description)

    def test_单一模型所以不设参数(self):
        self.assertEqual(self.dom.declare().params, (), "定案：单一模型、无待定项")

    def test_能力位与依赖自述(self):
        from aiintegration.domaindeps import parse_requires
        self.assertEqual(self.dom.capabilities(), {"infer", "artifact"})
        self.assertEqual(set(parse_requires(DOMAINS_DIR / "vfd_health.py")),
                         {"numpy", "onnxruntime"})


class TestValidateArtifact(VfdBase):
    def rejected(self, blob, *, contains):
        why, _ = self.dom.validate_artifact("model", blob)
        self.assertTrue(why, "本该拒收却收下了")
        self.assertIn(contains, why)
        return why

    def test_合规模型收下并记下事实(self):
        why, facts = self.dom.validate_artifact("model", build_model())
        self.assertEqual(why, "", why)
        self.assertEqual(facts["channels"], ",".join(ROLES))
        self.assertEqual(facts["classes"], ",".join(CLASSES))
        self.assertEqual(facts["normalization"], "builtin")
        self.assertEqual(facts["window_len"], str(WIN))

    def test_缺normalization就拒并说清为什么(self):
        """★本域最容易静默出错的一处：少做一次 z-score 不会报错。"""
        why = self.rejected(build_model(omit=("normalization",)),
                            contains="normalization")
        self.assertIn("不会报错", why)
        self.assertIn("scaler", why)

    def test_normalization取值不认得就拒(self):
        self.rejected(build_model(normalization="minmax"), contains="normalization")

    def test_zscore却不给scaler就拒(self):
        self.rejected(build_model(normalization="zscore"), contains="scaler_mean")

    def test_scaler个数不对就拒(self):
        self.rejected(build_model(normalization="zscore", scaler_mean=[0.0] * 14,
                                  scaler_std=[1.0] * 15),
                      contains="14 个数")

    def test_scaler标准差有零就拒(self):
        self.rejected(build_model(normalization="zscore", scaler_mean=[0.0] * 15,
                                  scaler_std=[1.0] * 14 + [0.0]),
                      contains="非正数")

    def test_缺channels就拒并说清理由(self):
        why = self.rejected(build_model(omit=("channels",)), contains="channels")
        self.assertIn("顺序", why)

    def test_缺classes就拒并说清理由(self):
        why = self.rejected(build_model(omit=("classes",)), contains="classes")
        self.assertIn("顺序", why)

    def test_缺rate_hz就拒(self):
        why = self.rejected(build_model(omit=("rate_hz",)), contains="rate_hz")
        self.assertIn("跨度", why)

    def test_类别少一个就拒(self):
        self.rejected(build_model(classes=CLASSES[:5]), contains="缺")

    def test_不是三个头就拒(self):
        why = self.rejected(_two_head_model(), contains="三个头")
        self.assertIn("未来严重度", why)

    def test_时间维动态就拒(self):
        self.rejected(build_model(time_dynamic=True), contains="固定")

    def test_不是ONNX就拒(self):
        self.rejected(b"\x00 not onnx", contains="不是可用的 VFD ONNX 模型")


def _two_head_model():
    """只有两个输出头的模型 —— 本域要三个。"""
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper
    n, t = len(ROLES), WIN
    mean = helper.make_node("ReduceMean", inputs=["x"], outputs=["m"], axes=[1], keepdims=0)
    idx = numpy_helper.from_array(np.arange(6, dtype=np.int64), name="i")
    g = helper.make_node("Gather", inputs=["m", "i"], outputs=["logits"], axis=1)
    idx2 = numpy_helper.from_array(np.arange(1, dtype=np.int64), name="i2")
    g2 = helper.make_node("Gather", inputs=["m", "i2"], outputs=["current"], axis=1)
    graph = helper.make_graph(
        [mean, g, g2], "two_head", initializer=[idx, idx2],
        inputs=[helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, t, n])],
        outputs=[helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, 6]),
                 helper.make_tensor_value_info("current", TensorProto.FLOAT, [1, 1])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 12)])
    model.ir_version = 7
    for k, v in (("channels", ",".join(ROLES)), ("classes", ",".join(CLASSES)),
                 ("rate_hz", "1"), ("normalization", "builtin")):
        mp = model.metadata_props.add()
        mp.key, mp.value = k, v
    return model.SerializeToString()


class TestInfer(VfdBase):
    def test_正常出七条结论(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(values={"vin_l1l2": 5.0}))
        self.assertEqual(got["fault_type"].value, "正常")
        self.assertIs(got["fault_type"].quality, Quality.OK)
        self.assertEqual(got["fault_type"].t, T_END)
        for k in ("confidence", "severity", "severity_30s", "severity_60s", "severity_120s"):
            self.assertIsInstance(got[k].value, float, k)
            self.assertGreaterEqual(got[k].value, 0.0)
            self.assertLessEqual(got[k].value, 1.0)
        self.assertIn("工件", json.loads(got["evidence"].value))

    def test_判得出别的类别(self):
        got = self.out(self.frame(values={"vin_l2l3": 9.0}))
        self.assertEqual(got["fault_type"].value, "冷却系统退化")
        got = self.out(self.frame(values={"iout_w": 9.0}))
        self.assertEqual(got["fault_type"].value, "过载 / 异常负载")

    def test_未来严重度来自模型而不是外推(self):
        """三条各由不同通道派生 ⇒ 若本域自己拿当前值外推，这三条会变成同一个数。"""
        got = self.out(self.frame(values={"vin_l1l2": 0.0, "vin_l2l3": 3.0,
                                          "vin_l3l1": -3.0, "iout_u": 1.0}))
        vals = [got[f"severity_{h}s"].value for h in (30, 60, 120)]
        self.assertEqual(len(set(vals)), 3, f"三条未来严重度全一样 {vals} —— 像是外推出来的")

    def test_通道顺序被真正尊重(self):
        data = {"vin_l1l2": 5.0, "vin_l2l3": 1.0}
        a = self.out(self.frame(values=data))
        self.assertEqual(a["fault_type"].value, "正常")
        swapped = ("vin_l2l3", "vin_l1l2") + ROLES[2:]
        self.dom = type(self.loaded.instance)()
        b = self.out(self.frame(values=data,
                                artifact=self.art(build_model(channels=swapped), aid=2)))
        self.assertEqual(b["fault_type"].value, "冷却系统退化",
                         "换了 channels 顺序结论却没变 —— 喂进去的顺序不是模型说的那个")

    def test_zscore归一化真的被应用(self):
        """★用 scaler 把两路的大小关系**倒过来** ⇒ 不做归一化就会判成另一类。"""
        mean = [0.0] * 15
        mean[0] = 10.0           # vin_l1l2 减 10 ⇒ 5-10 = -5
        blob = build_model(normalization="zscore", scaler_mean=mean,
                           scaler_std=[1.0] * 15)
        got = self.out(self.frame(values={"vin_l1l2": 5.0, "vin_l2l3": 1.0},
                                  artifact=self.art(blob)))
        self.assertEqual(got["fault_type"].value, "冷却系统退化",
                         "归一化没被应用 —— 原值 5>1 会判成「正常」")

    def test_没工件落MODEL_NOT_LOADED(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(artifact=None))
        self.assertIs(got["fault_type"].quality, Quality.MODEL_NOT_LOADED)
        for h in (30, 60, 120):
            self.assertIs(got[f"severity_{h}s"].quality, Quality.MODEL_NOT_LOADED)
            self.assertIsNone(got[f"severity_{h}s"].value)
        self.assertIs(got["evidence"].quality, Quality.OK)
        self.assertIn("不拿默认模型顶", got["evidence"].value)

    def test_点数不足不补数(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(n=WIN - 2))
        self.assertIs(got["fault_type"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("不补数", got["evidence"].value)

    def test_一路坏值就不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(bad=("cabinet_temp",)))
        self.assertIs(got["fault_type"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("机柜温度", got["evidence"].value)

    def test_一路都没有落NO_INPUT(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(drop=ROLES))
        self.assertIs(got["fault_type"].quality, Quality.NO_INPUT)
        self.assertIn("一个点都没接", got["evidence"].value)

    def test_缺一路也不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(drop=("unit_vdc_spread",)))
        self.assertIs(got["fault_type"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("极差", got["evidence"].value)

    def test_疏密对不上不拉伸(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(rate=10.0))
        self.assertIs(got["fault_type"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("疏密对不上", got["evidence"].value)

    def test_非数值样本不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(non_numeric=("output_freq",)))
        self.assertIs(got["fault_type"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("输出频率", got["evidence"].value)

    def test_未来严重度条数不对落COMPUTE_ERROR(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(artifact=self.art(build_model(n_fut=2))))
        self.assertIs(got["fault_type"].quality, Quality.COMPUTE_ERROR)
        self.assertIn("未来严重度", got["evidence"].value)

    def test_同一工件不重复建会话(self):
        f = self.frame()
        self.dom.infer(f)
        self.dom.infer(f)
        self.assertEqual(len(self.dom._models), 1)


if __name__ == "__main__":
    unittest.main()
