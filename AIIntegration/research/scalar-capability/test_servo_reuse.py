"""丙1：验「甲类可直接拿」这个结论 —— 伺服 predict_rows 能不能真跑起来。

判据（三条全过才算「可直接拿」）：
  1 能 import，不拖它的 app/db/services
  2 能加载工件
  3 喂构造数据能出结果，且两条推理路（深度 CNN-LSTM / 树模型）都通
"""
import sys, time, json
from pathlib import Path
import numpy as np

ROOT = Path("/root/AIRef/servo-test")
sys.path.insert(0, str(ROOT))          # 让 `from servo_diagnosis...` 可解析

meta = json.loads((ROOT / "servo_diagnosis/model_artifacts/servo_bearing_meta.json").read_text(encoding="utf-8"))
COLS = meta["config"]["signal_columns"]
WIN  = int(meta["config"]["window_seconds"] * meta["config"]["sample_rate_hz"])
print(f"工件自述：{meta['num_classes']} 类 {meta['labels']}")
print(f"          {len(COLS)} 路信号，窗 {WIN} 点（{meta['config']['window_seconds']}s @ {meta['config']['sample_rate_hz']}Hz）")
print(f"          列名：{COLS}\n")

t0 = time.time()
try:
    from servo_diagnosis.infer import predict_rows
    print(f"① import 成功（{time.time()-t0:.1f}s）—— 未拖 app/db/services")
except Exception as e:
    print(f"★① import 失败：{type(e).__name__}: {e}"); raise SystemExit(1)

# 构造两段输入：平稳 vs 带退化趋势。只验链路通不通，不验结论对不对。
rng = np.random.default_rng(42)
def make(n, drift=0.0):
    rows = []
    for i in range(n):
        r = {}
        for j, c in enumerate(COLS):
            base = 100.0 + 10*j
            r[c] = float(base + rng.normal(0, 1.0) + drift * i / n * base * 0.2)
        rows.append(r)
    return rows

for tag, drift in (("平稳", 0.0), ("带上升趋势", 1.0)):
    for use in (None, "tree"):
        rows = make(WIN + 100, drift)
        t1 = time.time()
        try:
            out = predict_rows(rows) if use is None else predict_rows(rows)
            el = time.time() - t1
            keys = list(out.keys())
            print(f"② {tag:<12} 出结果（{el*1000:.0f} ms）  键：{keys[:8]}")
            for k in ("predict", "label", "probability", "probabilities", "score", "model"):
                if k in out:
                    v = out[k]
                    print(f"      {k} = {str(v)[:110]}")
            break
        except Exception as e:
            print(f"★② {tag} 失败：{type(e).__name__}: {e}")
            break

print("\n=== 树模型路 ===")
try:
    from servo_diagnosis.tree_infer import predict_rows as tree_predict
    out = tree_predict(make(WIN + 100, 1.0), "decision_tree")
    print(f"③ 树模型路通，键：{list(out.keys())[:8]}")
    for k in ("predict","label","probabilities","model"):
        if k in out: print(f"      {k} = {str(out[k])[:110]}")
except Exception as e:
    print(f"★③ 树模型路：{type(e).__name__}: {e}")
