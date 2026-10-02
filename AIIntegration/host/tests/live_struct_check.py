"""联机自检：结构值读侧（契约 1.11 按字段绑定）—— **只读**。

★只走明文回环**全量读口**（globalId），**不碰写路径**：不建点、不推快照、不写值、不注册结构。
  所以可以指向一台在用的 hs（缺省 WSL 现网 `127.0.0.1:5400`，daqgate 的联调结构在那上面）。
★不在 discover 里（文件名不是 test_*），按需手动跑：

    HS_READ=127.0.0.1:5400 STRUCT_GIDS=1557 python tests/live_struct_check.py

对每个 gid：① 取最近一条值认出它挂的结构并列字段（同 `DescribeStructPoint`）；
② 按字段绑定、取最近 60 秒（右端留 5 秒）拆成各路样本，报条数、好值占比、头一条；
   最近 60 秒没有值（采集已停）就改取**最后那条值之前的 60 秒** —— 验的是解码，不是现在有没有数据；
③ 拿 `vibration_iso` 的声明去核「x/y/z 按字段绑到这个点」—— 期望被拒还是放行，按结构的单位与物理量而定，照实报。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "domains"))

from aiintegration.bindings import Binding                       # noqa: E402
from aiintegration.fetch import Fetcher                          # noqa: E402
from aiintegration.hsclient import HsClient, HsConfig            # noqa: E402
from aiintegration.scheduler import STRUCT_RIGHT_MARGIN_SEC      # noqa: E402
from aiintegration.structbind import (                           # noqa: E402
    StructBindError, cached_lookup, check_binding, find_point_struct, is_bindable, type_name,
)
from aiintegration.structreg import StructRegistry               # noqa: E402

READ = os.environ.get("HS_READ", "127.0.0.1:5400")
GIDS = [int(x) for x in os.environ.get("STRUCT_GIDS", "1557").split(",") if x.strip()]


def main() -> int:
    client = HsClient(HsConfig(read_addr=READ))       # 无写路径：get_struct 也走读口
    reg = StructRegistry(client)
    print(f"hs 读口 {READ}；struct-value 能力位：{reg.available}")
    bad = 0
    for gid in GIDS:
        print(f"\n== gid {gid} ==")
        try:
            ps = find_point_struct(client, reg, gid)
        except StructBindError as exc:
            print(f"  认不出结构：{exc}")
            bad += 1
            continue
        d = ps.struct
        print(f"  结构 {d.name} id={d.id} v{d.version}（据 {ps.seen_at.isoformat()} 那条值）")
        for f in d.fields:
            print(f"    {f.name:12s} {type_name(f):10s} unit={f.unit or '-':6s} "
                  f"attrs={dict(f.attrs)} bindable={is_bindable(f)}")
        names = [f.name for f in d.fields if is_bindable(f)]
        end = datetime.now(timezone.utc) - timedelta(seconds=STRUCT_RIGHT_MARGIN_SEC)
        if ps.seen_at < end - timedelta(seconds=60):
            end = ps.seen_at + timedelta(milliseconds=1)
            print(f"  （最近 60 秒无值，改取最后那条值之前的 60 秒）")
        b = Binding("probe", f"gid{gid}", {n: gid for n in names}, fields={n: n for n in names},
                    window_sec=60)
        fr = Fetcher(client, reg).fetch(b, end)
        for n in names:
            ss = fr.channels[n]
            good = sum(s.quality.is_good() for s in ss)
            head = f"头一条 t={ss[0].t.isoformat()} v={ss[0].value!r} q={ss[0].quality.name}" if ss else "无"
            print(f"  字段 {n}: {len(ss)} 条，好值 {good}；{head}")
        try:
            from vibration_iso import VibrationIso   # noqa: WPS433
            decl = VibrationIso().declare()
            roles = {r: gid for r in ("x_vel", "y_vel", "z_vel")}
            fields = dict(zip(roles, (names + names[:1] * 3)[:3])) if names else {}
            check_binding(Binding("vibration_iso", "probe", roles, fields=fields),
                          decl.inputs, cached_lookup(client, reg))
            print(f"  vibration_iso x/y/z → {fields}：放行")
        except StructBindError as exc:
            print(f"  vibration_iso x/y/z 按字段绑到它：被拒 —— {exc}")
        except ImportError as exc:
            print(f"  （跳过域核对：{exc}）")
    return 1 if bad == len(GIDS) else 0


if __name__ == "__main__":
    sys.exit(main())
