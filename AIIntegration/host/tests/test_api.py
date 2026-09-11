"""对外口的回归 —— 起**真的 gRPC server**，用真 channel 打，只是不连 hs。

钉的几条：
  · 域清单**带能力位**（前端按位渲染的前提）；
  · 装载失败**不藏**；
  · 绑定校验失败**原样回**，不吞；
  · 日志两口与 hs 形状对齐（真实总数、ReachedOldest、背压计数）；
  · 时间戳"不设 = 不限"，不许当成 1970。
"""

import tempfile
import unittest
from concurrent import futures
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grpc

from aiintegration.api import ApiService, SERVICE, build_handler
from aiintegration.apiproto import aiintegration_pb2 as pb
from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import discover
from aiintegration.logstore import LogLevel, LogStore

UTC = timezone.utc
T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

DOM = '''
from aiintegration.domains import Domain
from aiintegration.types import Declaration, InputSpec, OutputSpec, ParamSpec

class D(Domain):
    key = "vib"
    display = "振动"
    version = "2.0.0"
    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x_acc", unit="g"),
                    InputSpec(role="temp", unit="℃", required=False)),
            outputs=(OutputSpec(key="health_score", display="健康分",
                                value_type="float", unit="分"),),
            params=(ParamSpec(key="iso_group", display="机组类别", value_type="enum",
                              choices=("1", "2"), choice_displays=("大型", "中型"),
                              description="决定边界值"),
                    ParamSpec(key="note", display="备注", value_type="string",
                              required=False),),
        )
    def infer(self, frame):
        return []
'''
BROKEN = "raise RuntimeError('装载就炸')\n"


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        dom_dir = root / "domains"; dom_dir.mkdir()
        (dom_dir / "vib.py").write_text(DOM, encoding="utf-8")
        (dom_dir / "boom.py").write_text(BROKEN, encoding="utf-8")
        loaded, failed = discover(dom_dir)
        self.domains = {d.key: d for d in loaded}
        self.logs = LogStore(capacity=100)
        self.bindings = BindingStore(root / "b.db")
        self.svc = ApiService(
            guid="11111111-2222-3333-4444-555555555555", version="0.1.0",
            logstore=self.logs, domains=self.domains, bindings=self.bindings,
            load_errors=[(p.name, str(e)) for p, e in failed])

        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4),
                                  handlers=(build_handler(self.svc),))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.ch = grpc.insecure_channel(f"127.0.0.1:{self.port}",
                                        options=[("grpc.enable_http_proxy", 0)])

    def tearDown(self):
        self.ch.close(); self.server.stop(0)
        self.bindings.close(); self._tmp.cleanup()

    def call(self, method, req, res_cls, timeout=5):
        return self.ch.unary_unary(
            f"/{SERVICE}/{method}", request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=res_cls.FromString)(req, timeout=timeout)


class TestInfoAndDomains(ApiTestBase):
    def test_GetInfo_带身份与契约版本(self):
        r = self.call("GetInfo", pb.InfoRequest(), pb.InfoReply)
        self.assertEqual(r.guid, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(r.proto_version, "1.2")
        self.assertEqual(r.domain_count, 1)

    def test_装载失败不藏(self):
        # 静默跳过会变成"某个域莫名其妙不见了"。
        r = self.call("GetInfo", pb.InfoRequest(), pb.InfoReply)
        self.assertEqual(len(r.load_errors), 1)
        self.assertEqual(r.load_errors[0].file, "boom.py")
        self.assertIn("装载就炸", r.load_errors[0].reason)

    def test_域清单带能力位与进出声明(self):
        # 前端按能力位渲染的前提：这个字段必须在。
        r = self.call("ListDomains", pb.DomainsRequest(), pb.DomainsReply)
        self.assertEqual(len(r.domains), 1)
        d = r.domains[0]
        self.assertEqual(d.key, "vib")
        self.assertEqual(list(d.capabilities), ["infer"])
        self.assertEqual([i.role for i in d.inputs], ["x_acc", "temp"])
        self.assertFalse(d.inputs[1].required)
        self.assertEqual([o.key for o in d.outputs], ["health_score"])
        self.assertEqual(d.outputs[0].value_type, "float")


class TestBindings(ApiTestBase):
    def put(self, domain="vib", binding="dev1", roles=None, **kw):
        b = pb.Binding(domain=domain, binding=binding, interval_sec=60, window_sec=60,
                       enabled=True, **kw)
        for k, v in (roles or {"x_acc": 101}).items():
            b.roles[k] = v
        return self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)

    def test_存取往返(self):
        self.assertTrue(self.put().ok)
        r = self.call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply)
        self.assertEqual(len(r.bindings), 1)
        self.assertEqual(dict(r.bindings[0].roles), {"x_acc": 101})

    def test_指向未装载的域被拒而不是存下来(self):
        # 存下来的话调度器每拍都跳过并告警，而配置者以为配好了。
        r = self.put(domain="nosuch")
        self.assertFalse(r.ok)
        self.assertIn("未装载", r.message)

    def test_校验失败原样回不吞(self):
        r = self.put(roles={"x_acc": 0})   # 0 = hs 尚未分配映射
        self.assertFalse(r.ok)
        self.assertIn("0", r.message)

    def test_缺必填角色会回给前端(self):
        self.put(roles={"temp": 102})      # 只绑了选填的
        r = self.call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply)
        self.assertEqual(list(r.bindings[0].missing_required), ["x_acc"])

    def test_删绑定(self):
        self.put()
        r = self.call("DeleteBinding", pb.DeleteBindingRequest(domain="vib", binding="dev1"),
                      pb.DeleteBindingReply)
        self.assertTrue(r.ok)
        r2 = self.call("DeleteBinding", pb.DeleteBindingRequest(domain="vib", binding="dev1"),
                       pb.DeleteBindingReply)
        self.assertFalse(r2.ok)


class TestLogs(ApiTestBase):
    def wait_subscribed(self, timeout=5.0) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.logs.subscriber_count() >= 1:
                return True
            time.sleep(0.02)
        return False

    def fill(self, n=30):
        for i in range(n):
            self.logs.append(LogLevel.INFO, "cat", f"msg{i}",
                             timestamp=T0 + timedelta(seconds=i))

    def test_查询回真实总数而不是本页条数(self):
        self.fill(30)
        r = self.call("QueryLogs", pb.LogQueryReq(limit=5, newestFirst=True), pb.QueryLogsRes)
        self.assertEqual(len(r.Logs), 5)
        self.assertEqual(r.TotalCount, 30)

    def test_时间戳不设等于不限而不是1970(self):
        # 当成 1970：fromTime 上看不出区别，toTime 上会把整个窗口掐掉。
        self.fill(5)
        r = self.call("QueryLogs", pb.LogQueryReq(), pb.QueryLogsRes)
        self.assertEqual(r.TotalCount, 5)

    def test_时间窗生效(self):
        self.fill(10)
        req = pb.LogQueryReq()
        req.fromTime.FromDatetime(T0 + timedelta(seconds=5))
        r = self.call("QueryLogs", req, pb.QueryLogsRes)
        self.assertEqual(r.TotalCount, 5)

    def test_环挤掉过就不报到底并给出挤掉数(self):
        self.fill(150)   # capacity=100
        r = self.call("QueryLogs", pb.LogQueryReq(limit=200, newestFirst=False), pb.QueryLogsRes)
        self.assertFalse(r.ReachedOldest)
        self.assertGreater(r.EvictedTotal, 0)

    def test_订阅拿得到实时日志(self):
        stream = self.ch.unary_stream(
            f"/{SERVICE}/SubscribeLogs",
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=pb.LogStreamItem.FromString)
        it = stream(pb.LogSubscribeReq(minLevel=pb.Warn_LogLevel), timeout=10)
        # ★等订阅**真正注册**再写，不用 sleep 蒙：客户端拿到迭代器 ≠ 服务端已经订上，
        #   先写就会漏掉，表现为超时 —— 而那是测试的竞态，不是实现的问题。
        self.assertTrue(self.wait_subscribed(), "服务端未在预期时间内注册订阅")
        self.logs.append(LogLevel.INFO, "c", "低级别不该出现")
        self.logs.append(LogLevel.ERROR, "c", "这条要出现")
        item = next(it)
        self.assertEqual(item.record.Message, "这条要出现")
        self.assertEqual(item.record.LogLevel, pb.Error_LogLevel)
        it.cancel()

    def test_补发历史与实时同一过滤(self):
        self.logs.append(LogLevel.ERROR, "c", "旧的命中")
        self.logs.append(LogLevel.INFO, "c", "旧的不中")
        stream = self.ch.unary_stream(
            f"/{SERVICE}/SubscribeLogs",
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=pb.LogStreamItem.FromString)
        it = stream(pb.LogSubscribeReq(minLevel=pb.Warn_LogLevel, backlog=10), timeout=5)
        item = next(it)
        self.assertEqual(item.record.Message, "旧的命中")
        it.cancel()


class TestBindingParamsOverWire(unittest.TestCase):
    """契约 1.1 的台账参数 —— 这是 AICloud **唯一看得见**的那一面。"""

    def setUp(self):
        ApiTestBase.setUp(self)

    def tearDown(self):
        ApiTestBase.tearDown(self)

    call = ApiTestBase.call

    def test_域清单带台账自述供前端渲染表单(self):
        r = self.call("ListDomains", pb.DomainsRequest(), pb.DomainsReply)
        d = next(x for x in r.domains if x.key == "vib")
        specs = {p.key: p for p in d.params}
        self.assertEqual(set(specs), {"iso_group", "note"},
                         "自述漏一项，前端表单就少一格，而域会因此永远落坏码")
        self.assertEqual(list(specs["iso_group"].choices), ["1", "2"])
        self.assertEqual(list(specs["iso_group"].choice_displays), ["大型", "中型"])
        self.assertTrue(specs["iso_group"].required)
        self.assertFalse(specs["note"].required)

    def test_台账原样往返(self):
        b = pb.Binding(domain="vib", binding="dev1", enabled=True)
        b.roles["x_acc"] = 101
        b.params["iso_group"] = "2"
        b.params["note"] = "1# 主泵 驱动端"
        r = self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)
        self.assertTrue(r.ok, r.message)

        got = self.call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply)
        one = got.bindings[0]
        self.assertEqual(dict(one.params), {"iso_group": "2", "note": "1# 主泵 驱动端"})

    def test_不填台账也能存但域会自己落码(self):
        # 骨架**不校验台账语义**（那会让"新增域不改骨架"当场不成立）。
        b = pb.Binding(domain="vib", binding="dev2", enabled=True)
        b.roles["x_acc"] = 102
        r = self.call("PutBinding", pb.PutBindingRequest(binding=b), pb.PutBindingReply)
        self.assertTrue(r.ok, r.message)
        got = self.call("ListBindings", pb.ListBindingsRequest(domain="vib"),
                        pb.ListBindingsReply)
        self.assertEqual(dict(got.bindings[0].params), {})


if __name__ == "__main__":
    unittest.main()
