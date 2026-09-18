"""模块 4「AI 伺服/机械臂运行监测」的回归（需要 numpy / onnxruntime / onnx —— 视觉环境里跑）。

★不用真模型：原项目 v4 的伺服模型**训练代码与数据都没找到**，我方手上没有权重。
  这里**在测试里现造 ONNX**：输出 = 前两路通道在窗口上的均值 —— 于是"哪一路被喂到了
  第几个位置"变得**可观测**，通道顺序这条最危险的判据才钉得住（不是靠读代码相信）。

钉的重点：
  ① 没工件 / 工件用不了 → MODEL_NOT_LOADED，且判据摘要**本身是好质量**；
  ② 模型不自述 channels / classes / rate_hz → **导入时就拒**，并说清要补什么；
  ③ ★**通道顺序被真正尊重** —— 同一份数据、只有 channels 顺序不同的两个模型，结论相反；
  ④ 样本不足 / 有坏值 / 一路都没有 / 疏密对不上 → 各自的码，**不补数、不拉伸**；
  ⑤ 诊断上选的模型与工件对不上 → CONFIG_INCOMPLETE，不硬算；
  ⑥ T = 帧右端；概率是模型自评。
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

ROLES = ("pos_cmd", "pos_act", "follow_err", "speed", "torque", "iq",
         "motor_temp", "bearing_temp", "igbt_temp")
WIN = 20            # 窗口点数（真现场是 200；用例用小的，判据一样）
RATE = 10.0


def build_model(*, channels=ROLES, classes=("正常", "轴承退化"), rate_hz=RATE,
                window_len=WIN, channels_first=False, omit=(), algo="cnn_lstm",
                out_len=None, time_dynamic=False):
    """造一个伺服形 ONNX：输出 = channels 里**前两路**在时间轴上的均值。

    于是「第 0 类的分」= channels[0] 那一路的均值，「第 1 类的分」= channels[1] 那一路的均值
    ⇒ 喂进去的数据里哪一路大，结论就是哪一类 —— **通道顺序错了当场看得出来**。
    """
    import numpy as np
    from onnx import TensorProto, helper, numpy_helper

    n = len(channels)
    t = "T" if time_dynamic else window_len
    shape = [1, n, t] if channels_first else [1, t, n]
    time_axis = 2 if channels_first else 1
    ch_axis = 1 if channels_first else 1      # Gather 后的轴，见下

    # ReduceMean 掉时间轴 → (1, n)；再 Gather 前两路 → (1, 2)
    mean = helper.make_node("ReduceMean", inputs=["x"], outputs=["m"],
                            axes=[time_axis], keepdims=0)
    k = out_len if out_len is not None else 2
    # ★真取 k 路（不是只把声明的形状写成 k）—— 否则"输出条数与 classes 对不上"那条
    #   用例里模型实际还是吐 2 个数，判据根本没被触发。
    idx = numpy_helper.from_array(np.arange(k, dtype=np.int64), name="idx")
    gather = helper.make_node("Gather", inputs=["m", "idx"], outputs=["y"], axis=ch_axis)
    graph = helper.make_graph(
        [mean, gather], "fake_servo", initializer=[idx],
        inputs=[helper.make_tensor_value_info("x", TensorProto.FLOAT, shape)],
        outputs=[helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, k])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 12)])
    model.ir_version = 7
    for key, val in (("channels", ",".join(channels)),
                     ("classes", ",".join(classes)),
                     ("rate_hz", str(rate_hz)),
                     ("algo", algo)):
        if key in omit:
            continue
        m = model.metadata_props.add()
        m.key, m.value = key, val
    return model.SerializeToString()


@unittest.skipUnless(HAVE_DEPS, "缺 numpy/onnxruntime/onnx —— 伺服域用例只在视觉环境里跑")
class ServoBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aiintegration.domains import discover
        loaded, failed = discover(DOMAINS_DIR)
        bad = [(p, e) for p, e in failed if p.name == "servo_health.py"]
        assert not bad, f"伺服域装载失败：{bad}"
        cls.loaded = {d.key: d for d in loaded}["servo_health"]

    def setUp(self):
        self.dom = type(self.loaded.instance)()      # 每个用例新实例，免得模型缓存串味

    def art(self, blob=None, aid=1, name="合成伺服模型", algo="cnn_lstm"):
        from aiintegration.types import ArtifactBlob
        return ArtifactBlob(id=aid, kind="model", name=name, algo=algo,
                            blob=blob if blob is not None else build_model())

    def frame(self, *, values=None, n=WIN, artifact="default", params=None,
              drop=(), bad=(), rate=RATE, non_numeric=()):
        """造一帧。`values`: role → 常数值（缺省全 0，前两路另给）。"""
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
        return Frame(domain="servo_health", binding="joint1",
                     t_start=T_END - timedelta(seconds=60), t_end=T_END,
                     channels=channels, params=dict(params or {}), artifacts=arts)

    def out(self, frame):
        return {f.key: f for f in self.dom.infer(frame)}


class TestDeclare(ServoBase):
    def test_九路全必填(self):
        d = self.dom.declare()
        self.assertEqual(tuple(i.role for i in d.inputs), ROLES)
        self.assertTrue(all(i.required for i in d.inputs),
                        "9 路是一个整体，缺一路结论就不成立 —— 不该有选填的")
        self.assertTrue(all(i.kind == "point" for i in d.inputs))

    def test_模型参数缺省是CNN_LSTM(self):
        p = {p.key: p for p in self.dom.declare().params}["model"]
        self.assertEqual(p.default, "cnn_lstm")
        self.assertEqual(p.choices, ("cnn_lstm", "decision_tree", "svm"))

    def test_能力位有artifact没有train(self):
        caps = self.dom.capabilities()
        self.assertIn("artifact", caps)
        self.assertNotIn("train", caps, "本域不训练 —— 原项目的训练代码与数据都没找到")

    def test_自述第三方依赖(self):
        """没有这一行，发布件会把本域铺到没有 onnxruntime 的现场，常驻一条装载错误。"""
        from aiintegration.domaindeps import parse_requires
        self.assertEqual(set(parse_requires(DOMAINS_DIR / "servo_health.py")),
                         {"numpy", "onnxruntime"})


class TestValidateArtifact(ServoBase):
    def ok(self, blob):
        why, facts = self.dom.validate_artifact("model", blob)
        self.assertEqual(why, "", f"本该收下却拒了：{why}")
        return facts

    def rejected(self, blob, *, contains):
        why, _ = self.dom.validate_artifact("model", blob)
        self.assertTrue(why, "本该拒收却收下了")
        self.assertIn(contains, why)
        return why

    def test_合规模型收下并记下事实(self):
        facts = self.ok(build_model())
        self.assertEqual(facts["channels"], ",".join(ROLES))
        self.assertEqual(facts["classes"], "正常,轴承退化")
        self.assertEqual(facts["rate_hz"], "10")
        self.assertEqual(facts["window_len"], str(WIN))
        self.assertIn("时间", facts["layout"])

    def test_缺channels就拒并说清为什么非要不可(self):
        """★本域最危险的一处：顺序喂错不报错，只给一个自信的错结论。

        ★这里**不能只断言"拒了"** —— 把这条判据整个删掉，后面"channels 只有 0 路"
          那条照样会拒，用例照样绿。要断言的是**它说清了理由**：导入的人得知道要补什么、
          以及为什么这一项没有缺省。
        """
        why = self.rejected(build_model(omit=("channels",)), contains="channels")
        self.assertIn("顺序", why)
        self.assertIn("缺省", why)

    def test_缺classes就拒(self):
        self.rejected(build_model(omit=("classes",)), contains="classes")

    def test_缺rate_hz就拒而不是当成某个缺省值(self):
        why = self.rejected(build_model(omit=("rate_hz",)), contains="rate_hz")
        self.assertIn("跨度", why, "要说清它是拿来核对疏密的，否则下一个人会给它安个缺省")

    def test_channels有不认得的量就拒(self):
        ch = ("不认得",) + ROLES[1:]
        self.rejected(build_model(channels=ch), contains="不认得")

    def test_channels不全就拒(self):
        self.rejected(build_model(channels=ROLES[:8]), contains="缺")

    def test_channels有重复就拒(self):
        ch = (ROLES[0],) + ROLES[:8]
        self.rejected(build_model(channels=ch), contains="重复")

    def test_classes有不认得的类别就拒(self):
        self.rejected(build_model(classes=("正常", "起火")), contains="起火")

    def test_时间维是动态的就拒(self):
        """"这一窗该取多长"没有答案时只能靠猜 —— 宁可拒收。"""
        self.rejected(build_model(time_dynamic=True), contains="固定")

    def test_通道数与时间维一样长时分不清就拒(self):
        self.rejected(build_model(window_len=9), contains="分不清")

    def test_不是model种类的工件拒收(self):
        why, _ = self.dom.validate_artifact("baseline", build_model())
        self.assertIn("kind=model", why)

    def test_根本不是ONNX就拒(self):
        self.rejected(b"\x00\x01 not an onnx model", contains="不是可用的伺服 ONNX 模型")


class TestInfer(ServoBase):
    def test_正常出结论且T是帧右端(self):
        from aiintegration.quality import Quality
        # channels[0]=pos_cmd → 第 0 类「正常」；让它更大
        got = self.out(self.frame(values={"pos_cmd": 5.0, "pos_act": 1.0}))
        self.assertEqual(got["verdict"].value, "正常")
        self.assertIs(got["verdict"].quality, Quality.OK)
        self.assertEqual(got["verdict"].t, T_END)
        self.assertGreater(got["probability"].value, 0.5)
        self.assertLessEqual(got["probability"].value, 1.0)
        self.assertIn("工件", json.loads(got["evidence"].value))

    def test_另一类也判得出来(self):
        got = self.out(self.frame(values={"pos_cmd": 1.0, "pos_act": 5.0}))
        self.assertEqual(got["verdict"].value, "轴承退化")

    def test_通道顺序被真正尊重(self):
        """★同一份数据、只有 channels 顺序不同的两个模型 ⇒ 结论相反。

        不钉这一条的话，"按模型自述的顺序取"就只是代码里一句好听的话：
        把 `model.channels` 换成本域的固定顺序，所有别的用例照样绿。
        """
        data = {"pos_cmd": 5.0, "pos_act": 1.0}
        a = self.out(self.frame(values=data, artifact=self.art(build_model())))
        self.assertEqual(a["verdict"].value, "正常")

        swapped = ("pos_act", "pos_cmd") + ROLES[2:]
        self.dom = type(self.loaded.instance)()
        b = self.out(self.frame(values=data,
                                artifact=self.art(build_model(channels=swapped), aid=2)))
        self.assertEqual(b["verdict"].value, "轴承退化",
                         "换了 channels 顺序结论却没变 —— 说明喂进去的顺序不是模型说的那个")

    def test_通道在前的排布也对(self):
        got = self.out(self.frame(
            values={"pos_cmd": 5.0, "pos_act": 1.0},
            artifact=self.art(build_model(channels_first=True))))
        self.assertEqual(got["verdict"].value, "正常")

    def test_没工件落MODEL_NOT_LOADED且不顶默认(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(artifact=None))
        self.assertIs(got["verdict"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIsNone(got["verdict"].value)
        self.assertIs(got["evidence"].quality, Quality.OK,
                      "判据摘要本身没算错，它说的就是为什么没给")
        self.assertIn("不拿默认模型顶", got["evidence"].value)

    def test_工件用不了也落MODEL_NOT_LOADED(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(artifact=self.art(build_model(omit=("channels",)))))
        self.assertIs(got["verdict"].quality, Quality.MODEL_NOT_LOADED)
        self.assertIn("channels", got["evidence"].value)

    def test_样本不足不补数(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(n=WIN - 3))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("不补数", got["evidence"].value)

    def test_一路有坏值就不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(bad=("torque",)))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("转矩", got["evidence"].value)
        self.assertIn("一个整体", got["evidence"].value)

    def test_一路都没有落NO_INPUT并点名驱动器通讯口(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(drop=ROLES))
        self.assertIs(got["verdict"].quality, Quality.NO_INPUT)
        self.assertIn("驱动器通讯口", got["evidence"].value)

    def test_缺一路也不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(drop=("igbt_temp",)))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("IGBT", got["evidence"].value)

    def test_疏密对不上就不算且不拉伸时间轴(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(rate=1.0))       # 模型按 10 Hz，实际 1 Hz
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("疏密对不上", got["evidence"].value)

    def test_非数值样本不算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(non_numeric=("speed",)))
        self.assertIs(got["verdict"].quality, Quality.INSUFFICIENT_SAMPLES)
        self.assertIn("转速", got["evidence"].value)

    def test_诊断选的模型与工件对不上就不硬算(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(params={"model": "svm"},
                                  artifact=self.art(build_model(), algo="cnn_lstm")))
        self.assertIs(got["verdict"].quality, Quality.CONFIG_INCOMPLETE)
        self.assertIn("SVM", got["evidence"].value)
        self.assertIn("cnn_lstm", got["evidence"].value)

    def test_工件没记algo时不拦(self):
        """工件来历里没写算法的（老工件），不该因此一条结论都出不来。"""
        got = self.out(self.frame(params={"model": "svm"},
                                  artifact=self.art(build_model(), algo="")))
        self.assertEqual(got["verdict"].value, "正常")

    def test_模型输出条数与classes对不上落COMPUTE_ERROR(self):
        from aiintegration.quality import Quality
        got = self.out(self.frame(artifact=self.art(build_model(out_len=3))))
        self.assertIn(got["verdict"].quality,
                      (Quality.COMPUTE_ERROR, Quality.MODEL_NOT_LOADED))
        self.assertTrue(got["evidence"].value)

    def test_同一工件不重复建会话(self):
        f1 = self.frame(values={"pos_cmd": 5.0})
        self.dom.infer(f1)
        self.dom.infer(f1)
        self.assertEqual(len(self.dom._models), 1, "同一个工件 id 被重复建了会话")


if __name__ == "__main__":
    unittest.main()
