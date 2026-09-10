"""联机自检：拿**真的 HsClient** 打一个隔离的 historystore 实例。

**不是单测**（`unittest discover` 不会跑到它）—— 它需要一个活的引擎。
用途是证明客户端代码本身能通，而不只是消息构造对。

用法：
    HS_READ=127.0.0.1:5401 HS_WRITE=192.168.1.168:5401 HS_CERTS=/root/hs-aiverify/certs \\
      PYTHONPATH=. python3 tests/live_hs_check.py

★只对**隔离实例**跑。绝不指向现网 —— 它会往库里写点。
"""

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiintegration.hsclient import HsClient, HsConfig
from aiintegration.pointmap import PointMap, default_point_name
from aiintegration.quality import Quality
from aiintegration.types import Finding

CERTS = Path(os.environ.get("HS_CERTS", "/root/hs-aiverify/certs"))
READ = os.environ.get("HS_READ", "127.0.0.1:5401")
WRITE = os.environ.get("HS_WRITE", "192.168.1.168:5401")

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))


def main() -> int:
    cfg = HsConfig(read_addr=READ, write_addr=WRITE,
                   ca_file=CERTS / "ca.cer", cert_file=CERTS / "client.cer",
                   key_file=CERTS / "client.key")
    client = HsClient(cfg)

    try:
        pg = client.ping()
        check("Ping（受限连接探活，不用全量门的 GetServerStatus）", True,
              f"listenAddrs={list(pg.listenAddrs)}")
    except Exception as exc:
        check("Ping", False, repr(exc))
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        pm = PointMap(Path(tmp) / "points.db")
        rows = []
        for domain, key, vt, unit in [
            ("vib", "health_score", "float", "分"),
            ("vib", "fault_type", "string", ""),
            ("vfd", "health_score", "float", "分"),
        ]:
            rows.append(pm.ensure(domain, "dev1", key,
                                  name=default_point_name(domain, "dev1", key),
                                  unit=unit, value_type=vt))
        check("localId 分配", len({r.local_id for r in rows}) == 3,
              f"{[(r.domain, r.key, r.local_id) for r in rows]}")

        try:
            accepted = client.push_snapshot(pm.all())
            # BEGIN + 3×PUT + END = 5
            check("推快照（一条流、全量、不含 IDENTITY）", accepted == 5, f"accepted={accepted}")
        except Exception as exc:
            check("推快照", False, repr(exc))
            return 1

        t = datetime.now(timezone.utc)
        findings = [
            (rows[0].local_id, Finding("health_score", 88.5, Quality.OK, t)),
            (rows[1].local_id, Finding("fault_type", "不平衡", Quality.OK, t)),
            # ★坏值锚点：算不出来也要发，带质量码
            (rows[2].local_id, Finding("health_score", None, Quality.MODEL_NOT_LOADED, t)),
        ]
        try:
            st = client.post_vqt(findings)
            check("写 VQT（含一条坏值锚点）", st.Code == 1, f"Code={st.Code} Msg={st.Message!r}")
        except Exception as exc:
            check("写 VQT", False, repr(exc))
            return 1

        try:
            gids = client.lookup_global([r.local_id for r in rows])
            check("localId→globalId 已铸号", all(g > 0 for g in gids), f"{gids}")
        except Exception as exc:
            check("localId→globalId", False, repr(exc))

        # 中文点名要能原样回来（跨端编码最容易在这里出事）
        try:
            ids = client._unary(client._write_channel(), "ListEntityIdentities",
                                __import__("aiintegration.hsproto.historystore_pb2",
                                           fromlist=["x"]).EntityIdentityReq(),
                                __import__("aiintegration.hsproto.historystore_pb2",
                                           fromlist=["x"]).EntityIdentitiesRes)
            gw = [i for i in ids.identities if i.isGateway]
            check("身份表里没有我方的行（HasIdentity=false 的前提）", len(gw) == 0,
                  f"isGateway 行 {len(gw)} 条")
        except Exception as exc:
            check("身份表自检", False, repr(exc))

        pm.close()

    client.close()
    print("\n===== 汇总")
    for name, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    bad = [r for r in results if not r[1]]
    print("结论：", "全通过" if not bad else f"{len(bad)} 项未通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
