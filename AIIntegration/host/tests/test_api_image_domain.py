"""契约 1.4：图片类输入在**控制面**上的三件事 —— 起真 gRPC server、用真 channel 打。

  ① `ListDomains` 带出 `InputSpec.kind`（贵方据此决定画不画"选测点"）；
  ② 纯图片域的绑定**放行空 roles**，测点域 / 混合域照旧拒；
  ③ `missing_required` **只算测点类**必填角色 —— 图片角色不存在"没绑"。

★为什么单独补这个文件：api.py 为图片域改的这三处，第一轮落码后**一条控制面用例都没有**
  （库层与事件入口各有用例，但贵方看得见的只有控制面）。自查时发现，补上。
"""

import tempfile
import unittest
from concurrent import futures
from pathlib import Path

import grpc

from aiintegration.api import SERVICE, ApiService, build_handler
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import BindingStore
from aiintegration.domains import discover
from aiintegration.logstore import LogStore

_HEAD = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec

class D(Domain):
    version = "1.0.0"
    def infer(self, frame):
        return []
'''

IMG = _HEAD + '''
    key = "img"
    display = "图"
    def declare(self):
        return Declaration(inputs=(InputSpec(role="image", kind="image"),),
                           outputs=(OutputSpec(key="n", display="n", value_type="int"),))
'''

POINT = _HEAD + '''
    key = "pt"
    display = "点"
    def declare(self):
        return Declaration(inputs=(InputSpec(role="x", unit="mm/s"),),
                           outputs=(OutputSpec(key="v", display="v", value_type="float"),))
'''

MIXED = _HEAD + '''
    key = "mixed"
    display = "混合"
    def declare(self):
        return Declaration(inputs=(InputSpec(role="x", unit="mm/s"),
                                   InputSpec(role="snap", kind="image")),
                           outputs=(OutputSpec(key="v", display="v", value_type="float"),))
'''


class ImageDomainApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        dom_dir = root / "domains"; dom_dir.mkdir()
        for name, src in (("img.py", IMG), ("pt.py", POINT), ("mixed.py", MIXED)):
            (dom_dir / name).write_text(src, encoding="utf-8")
        loaded, failed = discover(dom_dir)
        assert not failed, failed
        self.bindings = BindingStore(root / "b.db")
        self.svc = ApiService(guid="g", version="0.1.0", logstore=LogStore(capacity=50),
                              domains={d.key: d for d in loaded}, bindings=self.bindings)
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4),
                                  handlers=(build_handler(self.svc),))
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.ch = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)])

    def tearDown(self):
        self.ch.close(); self.server.stop(0)
        self.bindings.close(); self._tmp.cleanup()

    def call(self, method, req, res_cls):
        return self.ch.unary_unary(
            f"/{SERVICE}/{method}", request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=res_cls.FromString)(req, timeout=5)

    def put(self, domain, binding, roles=None):
        b = pb.Binding(domain=domain, binding=binding, enabled=True)
        for k, v in (roles or {}).items():
            b.roles[k] = v
        return self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)

    def test_域清单带出输入形态(self):
        r = self.call("ListDomains", pb.DomainsRequest(), pb.DomainsReply)
        kinds = {d.key: {i.role: i.kind for i in d.inputs} for d in r.domains}
        self.assertEqual(kinds["img"], {"image": "image"})
        self.assertEqual(kinds["pt"], {"x": "point"})
        self.assertEqual(kinds["mixed"], {"x": "point", "snap": "image"})

    def test_纯图片域放行空roles(self):
        r = self.put("img", "cam1")
        self.assertTrue(r.ok, r.message)

    def test_测点域空roles照旧拒(self):
        r = self.put("pt", "dev1")
        self.assertFalse(r.ok)
        self.assertIn("一个角色都没有", r.message)

    def test_混合域有测点输入所以空roles也拒(self):
        r = self.put("mixed", "m1")
        self.assertFalse(r.ok, "混合域有测点类输入，不该因为也有图片输入就放行空 roles")

    def test_必填角色只算测点类(self):
        self.assertTrue(self.put("mixed", "m1", {"x": 5}).ok)
        self.assertTrue(self.put("img", "cam1").ok)
        got = {b.domain: list(b.missing_required) for b in
               self.call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply).bindings}
        self.assertEqual(got["mixed"], [], "图片角色 snap 不该被报成'没绑'")
        self.assertEqual(got["img"], [])


if __name__ == "__main__":
    unittest.main()
