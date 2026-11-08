#!/usr/bin/env python3
"""End-to-end check of the C pre-processing: run the int8 model on the windows the C code
produced (test_host --dump-windows) and compare the per-recording fall decisions with the
Python reference stored in imu.bin.

    test_host vec --dump-windows vec/c_windows.bin
    python eval_c_windows.py --repo . --vectors vec
"""
import argparse
import os
import struct
import sys

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--vectors", required=True)
    a = ap.parse_args()
    sys.path.insert(0, os.path.abspath(a.repo))
    import phase1_pipeline as P

    q8 = P.Int8Model(open(os.path.join(a.repo, "fall_model_int8.tflite"), "rb").read())
    d = open(os.path.join(a.vectors, "imu.bin"), "rb").read()
    cw = np.frombuffer(open(os.path.join(a.vectors, "c_windows.bin"), "rb").read(), np.int8)
    cw = cw.reshape(-1, P.WIN, P.CH)
    assert d[:4] == b"SCV1"
    n_rec, _, _, _, consec = struct.unpack_from("<5I", d, 4)
    thr, _, _ = struct.unpack_from("<ffi", d, 24)
    p, wi = 36, 0
    conf = {"py": np.zeros((2, 2), int), "c": np.zeros((2, 2), int)}
    disagree = 0
    for _ in range(n_rec):
        n, nwin, is_fall, py_alarm, _ = struct.unpack_from("<IIBBH", d, p)
        p += 12 + n * 6 * 4 * 2 + nwin * P.WIN * P.CH + nwin * 4
        prob = np.empty(nwin, np.float32)
        for i in range(nwin):
            q8.it.set_tensor(q8.inp["index"], cw[wi + i][None])
            q8.it.invoke()
            o = q8.it.get_tensor(q8.out["index"]).astype(np.float32)
            prob[i] = (o.ravel()[0] - q8.out_zp) * q8.out_scale
        wi += nwin
        run = best = 0
        for h in prob >= np.float32(thr):
            run = run + 1 if h else 0
            best = max(best, run)
        c_alarm = int(best >= consec)
        conf["py"][is_fall, py_alarm] += 1
        conf["c"][is_fall, c_alarm] += 1
        disagree += c_alarm != py_alarm
    assert wi == len(cw), "window count mismatch"
    for k, m in conf.items():
        print(f"{k:>2}: falls detected {m[1, 1]}/{m[1].sum()}, false alarms {m[0, 1]}/{m[0].sum()} recordings")
    print(f"recordings where C and Python decide differently: {disagree}")
    sys.exit(1 if disagree else 0)


if __name__ == "__main__":
    main()
