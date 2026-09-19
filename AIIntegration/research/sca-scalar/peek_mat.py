"""看一个 SCA .mat 里有什么（只读，打印结构）。

用法：~/research-venv/bin/python peek_mat.py /mnt/d/download/AIRef/datasets/sca/1/test.mat
"""
import sys

import numpy as np
import scipy.io as sio

m = sio.loadmat(sys.argv[1], squeeze_me=True, struct_as_record=False)
for k, v in m.items():
    if k.startswith("__"):
        continue
    a = np.asarray(v)
    if a.dtype == object:
        print(k, "object", a.shape, "首元素:", type(a.ravel()[0]).__name__,
              np.asarray(a.ravel()[0]).shape)
    elif hasattr(v, "_fieldnames"):
        print(k, "struct", v._fieldnames)
    else:
        flat = a.ravel()
        print(k, a.dtype, a.shape, "前 5:", flat[:5], "唯一值数:",
              len(np.unique(flat)) if flat.size < 200000 else "-")

for side in ("DS", "FS"):
    s = m.get(side)
    if s is None or not hasattr(s, "_fieldnames"):
        continue
    print(f"== {side}")
    for f in s._fieldnames:
        a = np.asarray(getattr(s, f))
        if a.dtype == object:
            e = a.ravel()[0] if a.size else None
            print(" ", f, "object", a.shape, "首元素", type(e).__name__, np.asarray(e).shape,
                  np.asarray(e).dtype if e is not None else "")
        else:
            flat = a.ravel()
            print(" ", f, a.dtype, a.shape, "前 5:", flat[:5])
