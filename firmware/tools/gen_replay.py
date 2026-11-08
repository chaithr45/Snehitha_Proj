#!/usr/bin/env python3
"""Generate the firmware's replay + model self-test data (synthetic only, safe to commit).

    python firmware/tools/gen_replay.py --repo .

Writes firmware/main/replay_data.h and firmware/main/selftest_data.h.

Replay: a scripted sequence of synthetic recordings from phase1_pipeline.make_synthetic(),
delivered to the firmware exactly as the sensors would (MPU6050 int16 counts at 50 Hz,
MAX30102 18-bit counts at 100 Hz) plus scripted button presses. This script also runs a
Python model of the whole device (same pipeline functions, SafetyFSM, int8 model) on the
same stream and stores the events it produces; the firmware compares its own events with
them (CONFIG_SAFETY_REPLAY, used in QEMU).

Self-test: model input windows with the int8 output of TFLite's *reference* kernels, which
TFLite Micro on the ESP32 must reproduce (CONFIG_SAFETY_MODEL_SELFTEST).
"""
import argparse
import glob
import math
import os
import sys
import tempfile

import numpy as np

ACC_LSB, GYR_LSB = 2048.0, 16.4          # MPU6050 at +-16 g, +-2000 dps
PPG_FS, PPG_WINDOW_S, FINGER_IR_MIN = 100, 10, 30000.0   # must match Kconfig defaults
MOTION_N = 50                            # 1 s of samples for the inactivity check
PRESS, LONG_PRESS = 1, 2
EV_CODES = [("FALL_SUSPECTED", 1), ("CANCELLED", 2), ("ALERT_FALL (post-fall", 3),
            ("ALERT_FALL (no cancel", 4), ("ALERT_VITALS", 5)]


def ev_code(text):
    return next(c for p, c in EV_CODES if text.startswith(p))


def load_synthetic(P):
    root = P.make_synthetic(tempfile.mkdtemp(prefix="replay_"), n_subj=1, seed=21)
    recs = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "*.txt"), recursive=True)):
        rows = [[float(v) for v in ln.strip().rstrip(";").split(",")[:6]] for ln in open(path) if ln.strip()]
        raw = np.asarray(rows)
        x = np.hstack([raw[:, 0:3] * P.ACC_SCALE, raw[:, 3:6] * P.GYR_SCALE])
        recs[os.path.basename(path)[:-4]] = P.resample_poly(x, P.FS, P.FS_RAW, axis=0)
    return recs


def crossfade(a, b, n=100):
    """Join two recordings with an n-sample linear blend (avoids an artificial step)."""
    w = np.linspace(0, 1, n)[:, None]
    return np.vstack([a[:-n], a[-n:] * (1 - w) + b[:n] * w, b[n:]])


def lying_still(x, seconds, fs, seed):
    """Extend a fall recording with the worker lying still in the final posture."""
    rng = np.random.default_rng(seed)
    pose = x[-fs:].mean(0)
    n = int(seconds * fs)
    noise = np.hstack([rng.normal(0, 0.005, (n, 3)), rng.normal(0, 0.5, (n, 3))])
    return np.vstack([x, pose + noise])


def build_stream(recs):
    walk1, walk2, walk3 = recs["D01_SA01_R01"], recs["D02_SA01_R01"], recs["D01_SA01_R02"]
    fall1 = lying_still(recs["F01_SA01_R01"], 10, 50, 1)
    fall2 = lying_still(recs["F02_SA01_R01"], 10, 50, 2)
    segs = [("normal work", walk1, 72, 97), ("fall, worker motionless", fall1, 88, 97),
            ("fall, worker presses cancel", fall2, 85, 98),
            ("sustained tachycardia", np.vstack([walk2, walk3]), 150, 96)]
    imu, starts = None, []
    for name, x, hr, sp in segs:
        starts.append(0 if imu is None else len(imu) - 100)
        imu = x if imu is None else crossfade(imu, x)
    return segs, starts, imu


def to_counts(imu):
    lsb = np.array([ACC_LSB] * 3 + [GYR_LSB] * 3)
    c = np.clip(np.round(imu * lsb), -32768, 32767).astype(np.int16)
    return c, (c / lsb).astype(np.float32)


def make_ppg(segs, starts, n_imu, seed=5):
    """Continuous-phase synthetic PPG (same model as synth_ppg) whose HR / SpO2 follow the segments."""
    rng = np.random.default_rng(seed)
    n = 2 * n_imu
    seg_of = np.searchsorted(np.array(starts) * 2, np.arange(n), side="right") - 1
    hr = np.array([segs[s][2] for s in seg_of], float)
    sp = np.array([segs[s][3] for s in seg_of], float)
    ph = np.cumsum(2 * np.pi * hr / 60 / PPG_FS)
    t = np.arange(n) / PPG_FS
    pulse = np.sin(ph) + 0.4 * np.sin(2 * ph + 0.8)
    r_target = (110 - sp) / 25
    wander = 0.003 * np.sin(2 * np.pi * 0.25 * t)
    ir = 50000.0 * (1 + 0.02 * pulse + wander) + rng.normal(0, 30, n)
    red = 40000.0 * (1 + r_target * 0.02 * pulse + wander) + rng.normal(0, 30, n)
    return np.round(red).astype(np.uint32), np.round(ir).astype(np.uint32)


class DeviceModel:
    """Python model of firmware/main/pipeline.c (same order of operations)."""

    def __init__(self, P, hp, q8):
        self.P, self.hp, self.q8 = P, hp, q8
        self.raw = []
        self.ppg_red, self.ppg_ir = [], []
        self.hr = self.spo2 = math.nan
        self.run = 0
        self.fsm = P.SafetyFSM()
        self.press = self.reset = False
        self.events = []
        self.probs = []

    def push_ppg(self, red, ir):
        self.ppg_red.append(float(red))
        self.ppg_ir.append(float(ir))
        n, N = len(self.ppg_ir), PPG_FS * PPG_WINDOW_S
        if n >= N and n % PPG_FS == 0:
            r, i = np.array(self.ppg_red[-N:]), np.array(self.ppg_ir[-N:])
            if i.mean() < FINGER_IR_MIN:
                self.hr = self.spo2 = math.nan
            else:
                self.hr = self.P.estimate_hr(i, PPG_FS)
                self.spo2 = self.P.estimate_spo2(r, i, PPG_FS)

    def step_window(self, filt_win, t):
        q = np.clip(np.round(((filt_win - self.hp["mean"]) / self.hp["std"]) / np.float32(self.q8.in_scale)
                             + np.float32(self.q8.in_zp)), -128, 127).astype(np.int8)
        self.q8.it.set_tensor(self.q8.inp["index"], q[None])
        self.q8.it.invoke()
        o = self.q8.it.get_tensor(self.q8.out["index"]).astype(np.float32)
        prob = (o.ravel()[0] - self.q8.out_zp) * self.q8.out_scale
        self.probs.append(prob)
        if self.reset:
            self.reset = False
            self.fsm = self.P.SafetyFSM()
            self.run = 0
        if prob >= np.float32(self.hp["thr"]):
            self.run += 1
            fall = self.run == self.hp["consec"]
        else:
            self.run = 0
            fall = False
        button, self.press = self.press, False
        acc = filt_win[-MOTION_N:, :3].astype(np.float64)
        motion = float(np.linalg.norm(acc, axis=1).std())
        before = len(self.fsm.log)
        self.fsm.step(t, fall, motion, button, self.hr, self.spo2)
        if len(self.fsm.log) > before:
            self.events.append((t, ev_code(self.fsm.log[-1][1]), self.fsm.log[-1][1]))
        return fall


def simulate(P, hp, q8, phys, red, ir, actions):
    dev = DeviceModel(P, hp, q8)
    filt = P.filter50(phys.astype(np.float64))   # causal: identical to sample-by-sample
    act = dict(actions)
    for k in range(len(phys)):
        dev.push_ppg(red[2 * k], ir[2 * k])
        dev.push_ppg(red[2 * k + 1], ir[2 * k + 1])
        a = act.get(k)
        if a == PRESS:
            dev.press = True
        elif a == LONG_PRESS:
            dev.reset = True
        count = k + 1
        if count >= P.WIN and (count - P.WIN) % hp["hop"] == 0:
            dev.step_window(filt[count - P.WIN:count], count / P.FS)
    return dev


def c_array(name, ctype, arr, per_line=16):
    """2-D arrays: one row per line. 1-D arrays: `per_line` values per line."""
    dims = "".join(f"[{d}]" for d in arr.shape)
    out = [f"static const {ctype} {name}{dims} = {{"]
    if arr.ndim > 1:
        out += ["  {" + ", ".join(str(int(v)) for v in row.ravel()) + "}," for row in arr]
    else:
        vals = [str(int(v)) for v in arr]
        out += ["  " + ", ".join(vals[i:i + per_line]) + "," for i in range(0, len(vals), per_line)]
    return out + ["};"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "firmware", "test", "host"))
    import tensorflow as tf
    import phase1_pipeline as P
    from gen_vectors import load_header

    hp = load_header(os.path.join(repo, "fall_model_data.h"))
    tfl = open(os.path.join(repo, "fall_model_int8.tflite"), "rb").read()
    q8 = P.Int8Model(tfl)

    recs = load_synthetic(P)
    segs, starts, imu = build_stream(recs)
    counts, phys = to_counts(imu)
    red, ir = make_ppg(segs, starts, len(imu))

    # Long presses (reset after an alert) 3 s before the end of the alert segments
    seg_end = starts[1:] + [len(imu)]
    actions = [(seg_end[s] - 150, LONG_PRESS) for s, (name, *_r) in enumerate(segs)
               if name in ("fall, worker motionless", "sustained tachycardia")]
    # Pass 1: find when the cancel-segment fall is detected, press 2 s later
    dev = simulate(P, hp, q8, phys, red, ir, actions)
    for s, (name, *_rest) in enumerate(segs):
        lo, hi = starts[s] / P.FS, seg_end[s] / P.FS
        sus = [t for t, c, _ in dev.events if c == 1 and lo <= t < hi]
        if name.endswith("presses cancel") and sus:
            actions.append((int(round((sus[0] + 2.0) * P.FS)), PRESS))
    actions.sort()
    # Pass 2 with all scripted presses: these are the expected events
    dev = simulate(P, hp, q8, phys, red, ir, actions)
    print("segments:")
    for s, (name, *_r) in enumerate(segs):
        print(f"  {starts[s] / P.FS:6.2f}s  {name}")
    print("actions:", [(round(k / P.FS, 2), "press" if v == PRESS else "long press") for k, v in actions])
    print("expected events:")
    for t, c, txt in dev.events:
        print(f"  t={t:6.2f}s  {txt}")

    fw_main = os.path.join(repo, "firmware", "main")
    L = ["// Auto-generated by firmware/tools/gen_replay.py -- DO NOT EDIT BY HAND",
         "// Synthetic sensor stream for CONFIG_SAFETY_REPLAY (no KFall data).",
         "#pragma once", "#include <stdint.h>", "",
         f"#define REPLAY_IMU_N {len(counts)}      // MPU6050 samples at 50 Hz",
         f"#define REPLAY_PPG_N {len(red)}     // MAX30102 samples at 100 Hz (2 per IMU sample)",
         "#define REPLAY_PRESS 1", "#define REPLAY_LONG_PRESS 2", "",
         "typedef struct { uint32_t imu_index; const char *name; } replay_segment_t;",
         "typedef struct { uint32_t imu_index; uint8_t action; } replay_action_t;",
         "typedef struct { float t; uint8_t event; } replay_expected_t;", "",
         "static const replay_segment_t REPLAY_SEGMENTS[] = {"]
    L += [f'  {{{st}, "{name}"}},' for st, (name, *_r) in zip(starts, segs)] + ["};"]
    L += ["static const replay_action_t REPLAY_ACTIONS[] = {"]
    L += [f"  {{{k}, {v}}}," for k, v in actions] + ["};"]
    L += ["// Events produced by the Python model of the device on this stream (sc_event_t codes)",
          "static const replay_expected_t REPLAY_EXPECTED[] = {"]
    L += [f"  {{{t:.2f}f, {c}}},  // {txt}" for t, c, txt in dev.events] + ["};", ""]
    L += ["// ax ay az gx gy gz, MPU6050 counts (+-16 g: 2048 LSB/g, +-2000 dps: 16.4 LSB/dps)"]
    L += c_array("REPLAY_IMU", "int16_t", counts)
    L += ["// red, ir (18-bit ADC counts)"]
    L += c_array("REPLAY_PPG", "uint32_t", np.stack([red, ir], 1))
    open(os.path.join(fw_main, "replay_data.h"), "w").write("\n".join(L) + "\n")

    # ---- model self-test: windows covering falls, ADL and random inputs ----
    rng = np.random.default_rng(9)
    filt = P.filter50(phys.astype(np.float64))
    probs = np.array(dev.probs)
    starts_w = np.arange(len(probs)) * P.EVAL_STRIDE      # window k covers [25k, 25k + 128)
    pick = list(starts_w[np.argsort(probs)[-6:]]) + list(rng.choice(starts_w, 6, replace=False))
    X = [filt[s:s + P.WIN] for s in pick]
    Q = [np.clip(np.round(((x - hp["mean"]) / hp["std"]) / np.float32(q8.in_scale) + np.float32(q8.in_zp)),
                 -128, 127).astype(np.int8) for x in X]
    Q += [rng.integers(-128, 128, (P.WIN, P.CH)).astype(np.int8) for _ in range(4)]
    # blends between a fall window and an ADL window give mid-range outputs too
    Q += [np.round(al * Q[0].astype(float) + (1 - al) * Q[6].astype(float)).astype(np.int8)
          for al in np.linspace(0.28, 0.42, 12)]
    ref = tf.lite.Interpreter(model_content=tfl,
                              experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_REF)
    ref.allocate_tensors()
    ii, oo = ref.get_input_details()[0]["index"], ref.get_output_details()[0]["index"]
    outs = []
    for q in Q:
        ref.set_tensor(ii, q[None])
        ref.invoke()
        outs.append(int(ref.get_tensor(oo).ravel()[0]))
    S = ["// Auto-generated by firmware/tools/gen_replay.py -- DO NOT EDIT BY HAND",
         "// Model inputs + int8 outputs of TFLite's reference kernels (CONFIG_SAFETY_MODEL_SELFTEST).",
         "#pragma once", "#include <stdint.h>", "", f"#define SELFTEST_N {len(Q)}"]
    S += c_array("SELFTEST_IN", "int8_t", np.stack([q.ravel() for q in Q]))
    S += c_array("SELFTEST_OUT", "int8_t", np.array(outs, np.int8))
    open(os.path.join(fw_main, "selftest_data.h"), "w").write("\n".join(S) + "\n")
    print(f"self-test outputs: {outs}")
    print("wrote firmware/main/replay_data.h and firmware/main/selftest_data.h")


if __name__ == "__main__":
    main()
