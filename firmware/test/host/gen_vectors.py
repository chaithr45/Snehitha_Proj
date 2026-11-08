#!/usr/bin/env python3
"""Generate reference test vectors from phase1_pipeline.py for the C port (safety_core).

    python gen_vectors.py --repo ../../.. --out vectors                 # synthetic data (CI)
    python gen_vectors.py --repo ../../.. --out vec_kfall --kfall DIR   # real KFall test subjects

Writes:
  imu.bin    per recording: 50 Hz sensor input, Python-filtered signal, int8 model
             inputs for every 0.5 s window, int8-model probabilities, alarm decision
  ppg.bin    PPG recordings + Python estimate_hr / estimate_spo2 results
  fsm.bin    random + demo scenarios for SafetyFSM with the expected event per step
KFall-derived vectors must not be committed (KFall licence: no redistribution).
"""
import argparse
import glob
import math
import os
import re
import struct
import sys
import tempfile

import numpy as np

MPU_ACC_LSB_PER_G = 2048.0     # MPU6050 at +-16 g
MPU_GYR_LSB_PER_DPS = 16.4     # MPU6050 at +-2000 deg/s


def load_header(path):
    h = open(path).read()
    arr = lambda name: np.array([float(v.strip().rstrip("f")) for v in
                                 re.search(name + r"\[\d+\]\s*=\s*\{([^}]*)\}", h).group(1).split(",")],
                                np.float32)
    num = lambda name: float(re.search(r"#define " + name + r"\s+(-?[0-9.]+)f?", h).group(1))
    return dict(mean=arr("FALL_MEAN"), std=arr("FALL_STD"), thr=num("FALL_THRESHOLD"),
                in_scale=num("FALL_IN_SCALE"), in_zp=int(num("FALL_IN_ZP")),
                hop=int(num("FALL_HOP")), consec=int(num("FALL_CONSEC")))


def quantize(W, hp, in_scale, in_zp):
    """Int8Model.__call__ input path (float32 maths, round half to even)."""
    Xn = (W - hp["mean"]) / hp["std"]
    q = np.round(Xn / np.float32(in_scale) + np.float32(in_zp))
    return np.clip(q, -128, 127).astype(np.int8)


def imu_records(P, trials_raw50, hp, q8):
    recs = []
    for name, is_fall, raw50 in trials_raw50:
        raw50 = raw50.astype(np.float32)
        filt = P.filter50(raw50.astype(np.float64))          # float32, as in the pipeline
        if len(filt) < P.WIN:
            continue
        W = np.stack([filt[s:s + P.WIN] for s in range(0, len(filt) - P.WIN + 1, P.EVAL_STRIDE)])
        q = quantize(W, hp, q8.in_scale, q8.in_zp)
        prob = np.empty(len(q), np.float32)
        for i in range(len(q)):
            q8.it.set_tensor(q8.inp["index"], q[i:i + 1])
            q8.it.invoke()
            o = q8.it.get_tensor(q8.out["index"]).astype(np.float32)
            prob[i] = (o.ravel()[0] - q8.out_zp) * q8.out_scale
        hit = prob >= np.float32(hp["thr"])
        run = best = 0
        for h in hit:
            run = run + 1 if h else 0
            best = max(best, run)
        recs.append(dict(name=name, is_fall=is_fall, raw=raw50, filt=filt, q=q, prob=prob,
                         alarm=int(best >= hp["consec"])))
    return recs


def write_imu(path, recs, hp, q8):
    with open(path, "wb") as f:
        f.write(b"SCV1")
        f.write(struct.pack("<5I", len(recs), 128, 6, hp["hop"], hp["consec"]))
        f.write(struct.pack("<ffi", hp["thr"], float(q8.in_scale), int(q8.in_zp)))
        for r in recs:
            f.write(struct.pack("<IIBBH", len(r["raw"]), len(r["q"]), r["is_fall"], r["alarm"], 0))
            f.write(r["raw"].astype("<f4").tobytes())
            f.write(r["filt"].astype("<f4").tobytes())
            f.write(r["q"].astype(np.int8).tobytes())
            f.write(r["prob"].astype("<f4").tobytes())


def synthetic_imu(P):
    """Synthetic SisFall-format recordings from the pipeline, as the MPU6050 would deliver
    them: 50 Hz, quantised to int16 counts at +-16 g / +-2000 dps."""
    root = P.make_synthetic(tempfile.mkdtemp(prefix="sc_synth_"), n_subj=2, seed=7)
    out = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.txt"), recursive=True)):
        rows = [[float(v) for v in line.strip().rstrip(";").split(",")[:6]]
                for line in open(path) if line.strip()]
        raw = np.asarray(rows)
        x = np.hstack([raw[:, 0:3] * P.ACC_SCALE, raw[:, 3:6] * P.GYR_SCALE])
        x50 = P.resample_poly(x, P.FS, P.FS_RAW, axis=0)
        lsb = np.array([MPU_ACC_LSB_PER_G] * 3 + [MPU_GYR_LSB_PER_DPS] * 3)
        counts = np.clip(np.round(x50 * lsb), -32768, 32767)
        phys = (counts / lsb).astype(np.float32)
        name = os.path.basename(path)[:-4]
        out.append((name, int(name[0] == "F"), phys))
    return out


def kfall_imu(P, root, test_subjects):
    import pandas as pd
    out = []
    files = sorted(glob.glob(os.path.join(root, "sensor_data_new", "**", "*.csv"), recursive=True))
    for f in files:
        m = P.KFALL_RE.search(os.path.basename(f))
        if not m:
            continue
        subj = f"SA{int(m.group(1)):02d}"
        if subj not in test_subjects:
            continue
        task = int(m.group(2))
        raw = pd.read_csv(f)[["AccX", "AccY", "AccZ", "GyrX", "GyrY", "GyrZ"]].to_numpy(np.float64)
        if len(raw) < P.WIN * 2 + 10 or not np.isfinite(raw).all():
            continue
        x50 = P.resample_poly(raw, P.FS, P.FS_KFALL, axis=0).astype(np.float32)
        out.append((os.path.basename(f)[:-4], int(task in P.KFALL_FALL_TASKS), x50))
    return out


def ppg_cases(P):
    cases = []
    rng = np.random.default_rng(3)
    # the six self-check cases (20 s), then 10 s device-length buffers with random HR/SpO2
    plan = [(hr, sp, 20, hr) for hr, sp in [(55, 98), (72, 97), (95, 95), (120, 93), (150, 90), (170, 88)]]
    plan += [(int(rng.integers(45, 180)), int(rng.integers(85, 100)), 10, 1000 + k) for k in range(40)]
    for hr, sp, dur, seed in plan:
        red, ir = P.synth_ppg(hr, sp, fs=100, dur=dur, seed=seed)
        red = np.round(red).astype(np.float32)          # integer ADC counts, as the sensor gives
        ir = np.round(ir).astype(np.float32)
        r64, i64 = red.astype(np.float64), ir.astype(np.float64)
        cases.append((hr, sp, red, ir, P.estimate_hr(i64, 100), P.estimate_spo2(r64, i64, 100)))
    return cases


def write_ppg(path, cases):
    with open(path, "wb") as f:
        f.write(b"SCP1")
        f.write(struct.pack("<I", len(cases)))
        for hr, sp, red, ir, ehr, esp in cases:
            f.write(struct.pack("<Iffff", len(red), hr, sp, ehr, esp))
            f.write(red.astype("<f4").tobytes())
            f.write(ir.astype("<f4").tobytes())


EV_CODES = [("FALL_SUSPECTED", 1), ("CANCELLED", 2), ("ALERT_FALL (post-fall", 3),
            ("ALERT_FALL (no cancel", 4), ("ALERT_VITALS", 5)]


def ev_code(text):
    for prefix, code in EV_CODES:
        if text.startswith(prefix):
            return code
    raise ValueError(text)


def fsm_phase1_scenarios(P, DM):
    """Random + demo scenarios checked against the original Phase 1 SafetyFSM."""
    rng = np.random.default_rng(11)
    scen = []
    for k in range(300):
        steps, n = [], int(rng.integers(40, 160))
        still = False
        hr, sp = 80.0, 97.0
        for i in range(n):
            t = i * 0.5
            if rng.random() < 0.1:
                still = not still
            hr = float(np.clip(hr + rng.normal(0, 6), 30, 200))
            sp = float(np.clip(sp + rng.normal(0, 1), 80, 100))
            h, s = (math.nan, math.nan) if rng.random() < 0.05 else (hr, sp)
            steps.append((t, bool(rng.random() < 0.04), 0.01 if still else 0.2,
                          bool(rng.random() < 0.03), h, s, 0.0, True))
        scen.append(steps)
    # the four fsm_demo() scenarios, step for step
    demo = {"fall": (5, None, True, 88, 97), "cancel": (5, 8, False, 85, 98),
            "tachy": (None, None, False, 150, 96), "normal": (None, None, False, 90, 98)}
    for fall_at, press_at, still, hr, sp in demo.values():
        steps = []
        for k in range(60):
            t = k * 0.5
            fall = fall_at is not None and abs(t - fall_at) < 1e-6
            moving = 0.01 if (still and fall_at and t > fall_at) else 0.2
            press = press_at is not None and abs(t - press_at) < 1e-6
            steps.append((t, fall, moving, press, float(hr), float(sp), 0.0, True))
        scen.append(steps)
    out = []
    for steps in scen:
        fsm, fsm2 = P.SafetyFSM(), DM.SafetyFSM2.phase1()
        exp = []
        for t, fall, mot, btn, h, s, tilt, worn in steps:
            before = len(fsm.log)
            fsm.step(t, fall, np.float32(mot), btn, np.float32(h), np.float32(s))
            e = ev_code(fsm.log[-1][1]) if len(fsm.log) > before else 0
            e2 = fsm2.step(t, fall, np.float32(mot), btn, np.float32(h), np.float32(s), tilt, worn)
            assert e == e2, "SafetyFSM2.phase1() must equal the Phase 1 SafetyFSM"
            exp.append(e)
        out.append((steps, exp))
    return out


def fsm_v2_scenarios(DM):
    """Long scenarios exercising man-down (posture, stillness), vitals gating, worn flag."""
    rng = np.random.default_rng(23)
    motion_levels = [0.005, 0.015, 0.03, 0.2, 0.6]
    scen = []
    for k in range(400):
        steps, n = [], int(rng.integers(200, 900))
        level, lying, worn = 1, False, True
        hr, sp = 80.0, 97.0
        for i in range(n):
            t = i * 0.5
            if rng.random() < 0.02:
                level = int(rng.choice(len(motion_levels), p=[0.3, 0.25, 0.15, 0.2, 0.1]))
            if rng.random() < 0.01:
                lying = not lying
            if rng.random() < 0.003:
                worn = not worn
            hr = float(np.clip(hr + rng.normal(0, 5), 30, 200))
            sp = float(np.clip(sp + rng.normal(0, 0.8), 80, 100))
            h, s = (math.nan, math.nan) if (not worn or rng.random() < 0.03) else (hr, sp)
            mot = motion_levels[level] * float(rng.uniform(0.7, 1.3))
            tilt = float(rng.uniform(70, 100) if lying else rng.uniform(0, 40))
            steps.append((t, bool(rng.random() < 0.005), mot, bool(rng.random() < 0.004), h, s, tilt, worn))
        scen.append(steps)
    out = []
    for steps in scen:
        fsm = DM.SafetyFSM2()
        exp = [fsm.step(t, fall, np.float32(mot), btn, np.float32(h), np.float32(s), np.float32(tilt), worn)
               for t, fall, mot, btn, h, s, tilt, worn in steps]
        out.append((steps, exp))
    return out


def write_fsm(path, scen, mode):
    """mode 0: Phase 1 feature set (sc_fsm_cfg_phase1), 1: device defaults (sc_fsm_cfg_default)."""
    with open(path, "wb") as f:
        f.write(b"SCF2")
        f.write(struct.pack("<II", mode, len(scen)))
        for steps, exp in scen:
            f.write(struct.pack("<I", len(steps)))
            for (t, fall, mot, btn, h, s, tilt, worn), e in zip(steps, exp):
                f.write(struct.pack("<dBfBfffBB", t, fall, mot, btn, h, s, tilt, worn, e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="repository root (phase1_pipeline.py, fall_model_*)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--kfall", help="KFall folder: use real recordings of the test subjects")
    a = ap.parse_args()
    sys.path.insert(0, os.path.abspath(a.repo))
    import json
    import phase1_pipeline as P

    os.makedirs(a.out, exist_ok=True)
    hp = load_header(os.path.join(a.repo, "fall_model_data.h"))
    q8 = P.Int8Model(open(os.path.join(a.repo, "fall_model_int8.tflite"), "rb").read())
    if a.kfall:
        test_subjects = set(json.load(open(os.path.join(a.repo, "results.json")))["test_subjects"])
        trials = kfall_imu(P, a.kfall, test_subjects)
    else:
        trials = synthetic_imu(P)
    recs = imu_records(P, trials, hp, q8)
    write_imu(os.path.join(a.out, "imu.bin"), recs, hp, q8)
    nf = sum(r["is_fall"] for r in recs)
    print(f"imu.bin: {len(recs)} recordings ({nf} falls), "
          f"python alarms: falls {sum(r['alarm'] for r in recs if r['is_fall'])}/{nf}, "
          f"ADL {sum(r['alarm'] for r in recs if not r['is_fall'])}/{len(recs) - nf}")
    cases = ppg_cases(P)
    write_ppg(os.path.join(a.out, "ppg.bin"), cases)
    print(f"ppg.bin: {len(cases)} PPG recordings")
    sys.path.insert(0, os.path.join(os.path.abspath(a.repo), "firmware", "tools"))
    import device_model as DM
    for name, scen, mode in (("fsm.bin", fsm_phase1_scenarios(P, DM), 0), ("fsm2.bin", fsm_v2_scenarios(DM), 1)):
        write_fsm(os.path.join(a.out, name), scen, mode)
        evs = [e for _, ex in scen for e in ex if e]
        print(f"{name}: {len(scen)} scenarios, {sum(len(s) for s, _ in scen)} steps, {len(evs)} events "
              f"({', '.join(f'{DM.EV_NAMES[c]} {evs.count(c)}' for c in sorted(set(evs)))})")


if __name__ == "__main__":
    main()
