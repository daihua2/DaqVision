"""看 faultFrequencies 的字段与取值（阶次还是 Hz）。"""
import sys
import numpy as np
import scipy.io as sio
for case in sys.argv[2:]:
    m = sio.loadmat(f"{sys.argv[1]}/{case}/test.mat", squeeze_me=True, struct_as_record=False)
    for side in [k for k in m if not k.startswith("__") and hasattr(m[k], "_fieldnames") and "rawData" in m[k]._fieldnames]:
        ff = m[side].faultFrequencies
        print(case, side, str(m[side].assetName), {f: np.asarray(getattr(ff, f)).tolist() for f in ff._fieldnames},
              "rpm中位", float(np.median(np.atleast_1d(m[side].RPM))))
