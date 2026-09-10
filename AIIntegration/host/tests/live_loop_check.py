"""联机闭环自检：真 historystore + 真调度 + 真域模块，跑一圈。

  写输入点 → 取数 → 推理 → 结论回流 → 回读确认

**不是单测**。只对**隔离实例**跑，绝不指向现网。

用法：
    HS_READ=127.0.0.1:5401 HS_WRITE=192.168.1.168:5401 HS_CERTS=/root/hs-aiverify/certs \\
      PYTHONPATH=. python3 tests/live_loop_check.py
"""

import os
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiintegration.bindings import Binding, BindingStore
from aiintegration.domains import discover
from aiintegration.fetch import Fetcher
from aiintegration.hsclient import HsClient, HsConfig
from aiintegration.pointmap import PointMap, default_point_name
from aiintegration.quality import Quality
from aiintegration.scheduler import Scheduler, aligned_tick
from aiintegration.types import Finding

CERTS = Path(os.environ.get("HS_CERTS", "/root/hs-aiverify/certs"))
READ = os.environ.get("HS_READ", "127.0.0.1:5401")
WRITE = os.environ.get("HS_WRITE", "192.168.1.168:5401")

DEMO_DOMAIN = textwrap.dedent('''
    """联机自检用的最小域：算 x_acc 的均值当健康分。"""
    from aiintegration.domains import Domain
    from aiintegration.quality import Quality
    from aiintegration.types import Declaration, Finding, InputSpec, OutputSpec


    class Demo(Domain):
        key = "demo"
        display = "闭环自检域"
        version = "1.0.0"

        def declare(self):
            return Declaration(
                inputs=(InputSpec(role="x_acc", unit="g"),),
                outputs=(
                    OutputSpec(key="health_score", display="健康分",
                               value_type="float", unit="分"),
                    OutputSpec(key="fault_type", display="故障类型", value_type="string"),
                ),
            )

        def infer(self, frame):
            good = [s for s in frame.channels.get("x_acc", []) if s.quality.is_good()]
            if not good:
                # ★算不出来就落码，不编一个数 —— 这正是类型层强制 V/Q/T 的用意。
                return [Finding("health_score", None, Quality.NO_INPUT, frame.t_end),
                        Finding("fault_type", None, Quality.NO_INPUT, frame.t_end)]
            avg = sum(float(s.value) for s in good) / len(good)
            score = max(0.0, 100.0 - avg * 10.0)
            label = "正常" if score >= 60 else "不平衡"
            return [Finding("health_score", score, Quality.OK, frame.t_end),
                    Finding("fault_type", label, Quality.OK, frame.t_end)]
''')

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


def main() -> int:
    cfg = HsConfig(read_addr=READ, write_addr=WRITE, ca_file=CERTS / "ca.cer",
                   cert_file=CERTS / "client.cer", key_file=CERTS / "client.key")
    client = HsClient(cfg)
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)

    # ── 0. 域装载 ────────────────────────────────────────────────────────
    dom_dir = root / "domains"
    dom_dir.mkdir()
    (dom_dir / "demo.py").write_text(DEMO_DOMAIN, encoding="utf-8")
    loaded, failed = discover(dom_dir)
    check("装载域（丢一个 .py 就多一个域）", len(loaded) == 1 and not failed,
          f"{[d.key for d in loaded]} 失败={len(failed)}")
    if not loaded:
        return 1
    domains = {d.key: d for d in loaded}

    points = PointMap(root / "points.db")
    bindings = BindingStore(root / "bindings.db")

    # ── 1. 造几笔输入数据（当作"上游采集点"）────────────────────────────
    tick = aligned_tick(datetime.now(timezone.utc), 60) - timedelta(minutes=1)
    src = points.ensure("_src", "dev1", "x_acc",
                        name="自检输入.x_acc", unit="g", value_type="float")
    client.push_snapshot(points.all())
    samples = [(tick - timedelta(seconds=s), 1.0 + s * 0.1) for s in (50, 40, 30, 20, 10)]
    client.post_vqt([(src.local_id, Finding("x_acc", v, Quality.OK, t)) for t, v in samples])
    check("写入上游输入点", True, f"localId={src.local_id} 共 {len(samples)} 笔")

    time.sleep(2)
    gids = client.lookup_global([src.local_id])
    check("输入点已铸 globalId", gids and gids[0] > 0, f"localId={src.local_id} → gid={gids[0]}")
    if not gids or gids[0] <= 0:
        return 1

    # ── 2. 绑定：角色 → globalId（取数走全量回环，用 gid）────────────────
    bindings.put(Binding("demo", "dev1", {"x_acc": gids[0]},
                         interval_sec=60, window_sec=120))
    check("建立绑定", True, f"x_acc → gid={gids[0]}")

    # ── 3. 建结论点 + 推全量快照 ────────────────────────────────────────
    sched = Scheduler(client=client, fetcher=Fetcher(client), domains=domains,
                      bindings=bindings, points=points)
    n = sched.ensure_points()
    check("建结论点并推全量快照", n == 3, f"点表共 {n} 个（1 输入 + 2 结论）")

    # ── 4. 跑一拍 ────────────────────────────────────────────────────────
    try:
        findings = sched.run_once(bindings.get("demo", "dev1"), tick)
    except Exception as exc:
        check("跑一拍", False, repr(exc))
        return 1
    good = [f for f in findings if f.quality.is_good()]
    check("跑一拍（取数→推理→回流）", len(findings) == 2,
          "；".join(f"{f.key}={f.value!r}({f.quality.value})" for f in findings))
    check("★结论是真算出来的，不是坏值锚点", len(good) == 2,
          f"OK 的 {len(good)}/2 —— 若为 0 说明取数没取到，闭环断在取数那一段")

    # ── 5. 回读确认结论真的进了库 ────────────────────────────────────────
    time.sleep(2)
    lids = [points.local_id_of("demo", "dev1", k) for k in ("health_score", "fault_type")]
    back = client.query_history_raw(lids, tick - timedelta(minutes=5),
                                    tick + timedelta(minutes=5))
    num_lid, str_lid = lids
    check("回读**数值**结论（受限连接用 localId）", len(back.get(num_lid, [])) >= 1,
          f"lid={num_lid} {len(back.get(num_lid, []))} 笔")

    # ★已知问题（hs 侧，根因已定位，见 doc/已知问题.md §1）：
    #   离散点（字符串/时刻型）用 kRawData 查**恒回空** —— `AggregateDiscretePicks` 漏了
    #   `Method::Raw`。**与落盘无关、不会自愈**。换 kLastValue 查得到，故下面顺带验一次：
    #   数据确实在，只是 Raw 这条路被漏了。
    #   本条**不计入通过与否**，但每次都打出来 —— 别让它悄悄变成"本来就这样"。
    ns = len(back.get(str_lid, []))
    print(f"  [已知] 字符串结论 kRawData 回读：lid={str_lid} {ns} 笔"
          + ("" if ns else " —— 符合已知缺陷（AggregateDiscretePicks 漏了 Raw），见 doc/已知问题.md §1"))
    if not ns:
        from aiintegration.hsproto import historystore_pb2 as _hs
        _req = _hs.HisDataQueryReq()
        _req.hisReq.method = _hs.kLastValue
        _req.hisReq.begTime.FromDatetime(tick - timedelta(minutes=5))
        _req.hisReq.endTime.FromDatetime(tick + timedelta(minutes=5))
        _req.pointsReq.add().id = str_lid
        _res = client._unary(client._write_channel(), "QueryHistory", _req,
                             _hs.VQTArrayRes, timeout=20)
        _n = sum(len(a.VQTs) for a in _res.VQTs)
        _v = [getattr(v, v.WhichOneof("Value")) if v.WhichOneof("Value") else None
              for a in _res.VQTs for v in a.VQTs]
        check("★换 kLastValue 查得到 —— 证明数据在库里，是 Raw 那条路的缺陷",
              _n >= 1, f"{_n} 笔 {_v}")
    for k, vs in back.items():
        for v in vs:
            w = v.WhichOneof("Value")
            print(f"        lid={k} {v.TimStampUtc.ToDatetime()} {w}="
                  f"{getattr(v, w) if w else None} Q={v.Quality.Code}")

    points.close(); bindings.close(); client.close(); tmp.cleanup()

    print("\n===== 汇总")
    for name, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    bad = [r for r in results if not r[1]]
    print("结论：", "闭环通" if not bad else f"{len(bad)} 项未通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
