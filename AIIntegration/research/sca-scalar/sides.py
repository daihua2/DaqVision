"""每个案例 train/test 里实际有哪些测点、各多少次测量、采样率与点数。"""
import sys
from pathlib import Path
import numpy as np
import scipy.io as sio
root = Path(sys.argv[1])
for case in range(1, 12):
    for part in ("train", "test"):
        m = sio.loadmat(str(root / str(case) / f"{part}.mat"), squeeze_me=True, struct_as_record=False)
        keys = [k for k in m if not k.startswith("__")]
        sides = [k for k in keys if hasattr(m[k], "_fieldnames") and "rawData" in m[k]._fieldnames]
        desc = []
        for sd in sides:
            s = m[sd]; rd = s.rawData
            n = len(rd) if (isinstance(rd, np.ndarray) and rd.dtype == object) else np.atleast_2d(rd).shape[0]
            fs = sorted(set(np.atleast_1d(s.samplingRate).tolist()))
            lab = np.atleast_1d(s.label)
            desc.append(f"{sd}:n={n} fs={fs} lab={dict(zip(*np.unique(lab, return_counts=True)))}")
        other = [k for k in keys if k not in sides and not hasattr(m[k], "_fieldnames")]
        meta = {k: np.asarray(m[k]).item() for k in other}
        print(case, part, " | ".join(desc), meta if part == "test" else "")
