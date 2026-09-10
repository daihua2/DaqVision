"""联机整机自检：起**真的服务进程**，打它的 gRPC 与 HTTP 两个口。

前面几个 live_*_check 验的是"件"能不能用；这个验"装配起来能不能跑"。

**不是单测**。只对**隔离实例**跑。
    HS_READ=127.0.0.1:5401 HS_WRITE=192.168.1.168:5401 HS_CERTS=/root/hs-aiverify/certs \\
      PYTHONPATH=. python3 tests/live_service_check.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HOST = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOST))

import grpc

from aiintegration.api import SERVICE
from aiintegration.apiproto import aiintegration_pb2 as pb

CERTS = Path(os.environ.get("HS_CERTS", "/root/hs-aiverify/certs"))
READ = os.environ.get("HS_READ", "127.0.0.1:5401")
WRITE = os.environ.get("HS_WRITE", "192.168.1.168:5401")
API = "127.0.0.1:50170"
HTTP = "127.0.0.1:50171"

DEMO = '''
from aiintegration.domains import Domain
from aiintegration.quality import Quality
from aiintegration.types import Declaration, Finding, InputSpec, OutputSpec

class Demo(Domain):
    key = "demo"
    display = "整机自检域"
    version = "1.0.0"
    def declare(self):
        return Declaration(
            inputs=(InputSpec(role="x_acc", unit="g"),),
            outputs=(OutputSpec(key="health_score", display="健康分",
                                value_type="float", unit="分"),),
        )
    def infer(self, frame):
        good = [s for s in frame.channels.get("x_acc", []) if s.quality.is_good()]
        if not good:
            return [Finding("health_score", None, Quality.NO_INPUT, frame.t_end)]
        return [Finding("health_score", 90.0, Quality.OK, frame.t_end)]
'''
BROKEN = "raise RuntimeError('这个域装载就炸')\n"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aii-live-")
    root = Path(tmp)
    (root / "domains").mkdir()
    (root / "domains" / "demo.py").write_text(DEMO, encoding="utf-8")
    (root / "domains" / "boom.py").write_text(BROKEN, encoding="utf-8")
    (root / "cert").mkdir()
    for n, s in (("ca.cer", "ca.cer"), ("client.cer", "client.cer"), ("client.key", "client.key")):
        shutil.copy(CERTS / s, root / "cert" / n)

    env = dict(os.environ,
               AII_ROOT=str(root),
               AII_GUID_BACKUP=str(root / "guid-backup" / "system.guid"),
               AII_HS_READ=READ, AII_HS_WRITE=WRITE,
               AII_API_LISTEN=API, AII_HTTP_LISTEN=HTTP,
               PYTHONPATH=str(HOST), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "-m", "aiintegration.service"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        # 等两个口都起来（不 sleep 蒙）
        ch = grpc.insecure_channel(API, options=[("grpc.enable_http_proxy", 0)])
        try:
            grpc.channel_ready_future(ch).result(timeout=25)
            check("服务进程起来了，gRPC 口在听", True, API)
        except Exception as exc:
            check("服务进程起来了", False, repr(exc))
            return 1

        def call(m, req, cls, timeout=10):
            return ch.unary_unary(f"/{SERVICE}/{m}",
                                  request_serializer=lambda x: x.SerializeToString(),
                                  response_deserializer=cls.FromString)(req, timeout=timeout)

        info = call("GetInfo", pb.InfoRequest(), pb.InfoReply)
        check("GetInfo：身份/契约版本/域数", bool(info.guid) and info.proto_version == "1.0",
              f"guid={info.guid} proto={info.proto_version} domains={info.domain_count} "
              f"runtime={info.runtime}")
        check("★装载失败不藏（boom.py 应出现在 load_errors）",
              any(e.file == "boom.py" for e in info.load_errors),
              "；".join(f"{e.file}: {e.reason[:40]}" for e in info.load_errors))

        doms = call("ListDomains", pb.DomainsRequest(), pb.DomainsReply)
        d = doms.domains[0] if doms.domains else None
        check("ListDomains：带能力位（前端按位渲染的前提）",
              d is not None and "infer" in d.capabilities,
              f"{d.key}/{d.version} caps={list(d.capabilities)} "
              f"inputs={[i.role for i in d.inputs]} outputs={[o.key for o in d.outputs]}"
              if d else "无域")

        # 绑定：先试一个非法的（globalId=0），应被拒且**原样回原因**
        bad = pb.Binding(domain="demo", binding="dev1")
        bad.roles["x_acc"] = 0
        r = call("PutBinding", pb.PutBindingRequest(binding=bad), pb.PutBindingReply)
        check("★非法绑定被拒且回明原因（globalId=0 会静默查到别人的点）",
              (not r.ok) and "0" in r.message, r.message)

        good = pb.Binding(domain="demo", binding="dev1", interval_sec=60,
                          window_sec=120, enabled=True)
        good.roles["x_acc"] = 59
        r = call("PutBinding", pb.PutBindingRequest(binding=good), pb.PutBindingReply)
        check("建绑定", r.ok, r.message)
        lb = call("ListBindings", pb.ListBindingsRequest(), pb.ListBindingsReply)
        check("列绑定", len(lb.bindings) == 1,
              f"{[(b.domain, b.binding, dict(b.roles)) for b in lb.bindings]}")

        # ★运行期加的绑定必须**当场生效**：建点 + 起线程（曾经的缺口：ensure_points 只在启动时跑）
        time.sleep(1.5)
        q0 = call("QueryLogs", pb.LogQueryReq(limit=50, newestFirst=True,
                                              search="调度已同步"), pb.QueryLogsRes)
        synced = [l.Message for l in q0.Logs]
        check("★运行期加绑定后调度当场同步（建点 + 起线程）",
              any("在跑 1 个绑定" in m for m in synced), "；".join(synced[:2]) or "没看到同步日志")

        q = call("QueryLogs", pb.LogQueryReq(limit=5, newestFirst=True), pb.QueryLogsRes)
        check("日志口（形状对齐 hs：真实总数）", q.TotalCount > 0,
              f"TotalCount={q.TotalCount} 本页={len(q.Logs)} 最新一条={q.Logs[0].Message[:40] if q.Logs else ''}")

        with urllib.request.urlopen(f"http://{HTTP}/health", timeout=10) as resp:
            h = json.loads(resp.read())
        check("HTTP /health", h.get("status") == "ok",
              f"guid={h.get('guid')} domains={h.get('domains')} writePath={h.get('writePath')}")
        check("★写路径就绪（否则结论不回流，健康口会明说）",
              h.get("writePath") == "ready", h.get("writePath"))

        ch.close()
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill(); out = ""
        tail = [l for l in (out or "").splitlines() if "WARNING" in l or "ERROR" in l][-6:]
        print("\n--- 服务日志尾部 ---")
        for l in tail:
            print("   ", l[:150])
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 汇总")
    for name, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    bad_n = sum(1 for r in results if not r[1])
    print("结论：", "整机通" if not bad_n else f"{bad_n} 项未通过")
    return 0 if not bad_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
