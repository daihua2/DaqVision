"""整机装配的回归 —— 起**真的 `Service`**（在线程里），打它自己开的那两个口。

★为什么非有这条不可：`AI-11 §3` 那个缺口就是这么漏的 ——
  "运行期经 API 新加的绑定既不建点也不起线程"，**单测全绿时它一直在**，
  因为每个件单独看都对，错的是"装配"。工作台这轮同样有一处只存在于装配里
  （`service.py` 把 `Workbench` 与回溯判别执行器交给 `ApiService`），
  不起真服务就永远测不到：忘了传，那二十来口会安静地回 UNIMPLEMENTED。

不连实时库：**没有写路径也要能起来**（`service.py` 模块头那条），本用例正好也钉住它。
"""

import tempfile
import threading
import unittest
from pathlib import Path

import grpc

from aiintegration.api import SERVICE
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.config import Config
from aiintegration.service import Service

DOM = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec

class D(Domain):
    key = "boot_probe"
    display = "装配自检域"
    version = "1.0.0"
    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x", unit="mm/s"),),
            outputs=(OutputSpec(key="score", display="分", value_type="float"),),
        )
    def infer(self, frame):
        return []
'''


class TestServiceBoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "domains").mkdir()
        (root / "domains" / "probe.py").write_text(DOM, encoding="utf-8")

        cls.cfg = Config(
            root=root, domains_dir=root / "domains", data_dir=root / "data",
            cert_dir=root / "cert",
            guid_paths=[root / "system.guid", root / "system.guid.bak"],
            hs_read_addr="127.0.0.1:1",      # 故意连不上：本用例不碰实时库
            hs_write_addr="",                 # 无写路径 ⇒ 只读运行，调度不起
            api_listen="127.0.0.1:50931", http_listen="127.0.0.1:50932",
            log_capacity=100,
        )
        # ★这里曾写 `:0`，靠下一行覆盖成固定端口，注释写的是"端口 0 会让 grpc 自己挑、
        #   service 内部拿不到挑中的号"—— 也就是说这个行为我方**早就知道**，只是没把它
        #   当配置错误拦。2026-09-12 起 `:0` 一律拒（见 config.check_addr），这里直接写死端口。
        cls.cfg = _with_ports(cls.cfg, "127.0.0.1:50931", "127.0.0.1:50932")

        cls.svc = Service(cls.cfg)
        cls.thread = threading.Thread(target=cls.svc.run, daemon=True)
        cls.thread.start()
        cls.ch = grpc.insecure_channel("127.0.0.1:50931",
                                       options=[("grpc.enable_http_proxy", 0)])
        # 起来了才继续：连不上就是装配失败，别让后面的断言给出误导性的错。
        grpc.channel_ready_future(cls.ch).result(timeout=15)

    @classmethod
    def tearDownClass(cls):
        cls.ch.close()
        cls.svc.stop()
        cls.thread.join(timeout=15)
        cls._tmp.cleanup()

    def call(self, method, req, res_cls, timeout=8):
        return self.ch.unary_unary(
            f"/{SERVICE}/{method}", request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=res_cls.FromString)(req, timeout=timeout)

    def test_没有写路径也起得来且域装上了(self):
        info = self.call("GetInfo", pb.InfoRequest(), pb.InfoReply)
        self.assertEqual(info.proto_version, "1.6")
        self.assertEqual(info.domain_count, 1)
        self.assertTrue(info.guid, "首启该自己生成 guid")

    def test_工作台真的被装上了(self):
        """★这条就是为"忘了把 Workbench 传给 ApiService"准备的。

        忘了传 ⇒ 那二十来口安静地回 UNIMPLEMENTED，而每个件的单测照样全绿。
        """
        r = self.call("PutDataset", pb.PutDatasetReq(domain="boot_probe", name="装配自检集"),
                      pb.MutateRes)
        self.assertTrue(r.ok, r.message)
        lst = self.call("ListDatasets", pb.ListDatasetsReq(domain="boot_probe"),
                        pb.ListDatasetsRes)
        self.assertEqual([d.name for d in lst.items], ["装配自检集"])

    def test_工作台库落在数据目录下(self):
        # 落错地方 = 重铺目录时被冲掉，而那是平台级资产。
        self.assertTrue((self.cfg.data_dir / "workbench.db").is_file())

    def test_回溯判别执行器也装上了(self):
        """没绑定时该如实说"不知道取哪些点"，**不是**"只读模式"（那意味着执行器没传）。"""
        req = pb.PutSegmentReq(domain="boot_probe", binding="dev1")
        req.t_from.FromDatetime(_t(0))
        req.t_to.FromDatetime(_t(60))
        sid = self.call("PutSegment", req, pb.MutateRes).id
        r = self.call("RediagnoseSegment", pb.IdReq(id=sid), pb.RediagnoseRes)
        self.assertFalse(r.ok)
        self.assertNotIn("只读", r.message, "回了'只读'说明 rediagnose 没传进去")
        self.assertIn("没有绑定", r.message)

    def test_训练执行器也装上了(self):
        """★同 §工作台那条：没传 trainer ⇒ 回"未接"，而每个件的单测照样全绿。

        这里用一个**不支持训练**的域去开训：装上了才会回"能力位里没有 train"，
        没装上会回"未接训练执行器"。两句话区分得开，才证明装配到位。
        """
        ds = self.call("PutDataset", pb.PutDatasetReq(domain="boot_probe", name="训练自检集"),
                       pb.MutateRes)
        r = self.call("StartTraining",
                      pb.StartTrainingReq(domain="boot_probe", dataset_id=ds.id),
                      pb.MutateRes)
        self.assertFalse(r.ok)
        self.assertNotIn("未接", r.message, "回了'未接'说明 trainer 没传进去")
        self.assertIn("能力位", r.message)

    def test_日志两口照常并且能看见启动那几行(self):
        res = self.call("QueryLogs", pb.LogQueryReq(limit=200), pb.QueryLogsRes)
        self.assertGreater(res.TotalCount, 0)
        text = "\n".join(r.Message for r in res.Logs)
        self.assertIn("写路径未就绪", text, "降级要可见：静默降级 = 现场以为在跑其实没结果")


def _with_ports(cfg: Config, api_listen: str, http_listen: str) -> Config:
    import dataclasses
    return dataclasses.replace(cfg, api_listen=api_listen, http_listen=http_listen)


def _t(sec: int):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 9, 11, 8, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=sec)


if __name__ == "__main__":
    unittest.main()
