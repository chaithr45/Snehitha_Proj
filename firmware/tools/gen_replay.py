#!/usr/bin/env python3
"""Generate the firmware's replay + model self-test data (synthetic only, safe to commit).

    python firmware/tools/gen_replay.py --repo .

Writes firmware/main/replay_data.h and firmware/main/selftest_data.h.

Replay: a scripted sequence of synthetic situations delivered to the firmware exactly as
the sensors would (MPU6050 int16 counts at 50 Hz, MAX30102 counts at 100 Hz) plus scripted
button presses. The Python model of the device (device_model.py) runs on the same stream;
its events are stored and the firmware compares its own against them
(CONFIG_SAFETY_REPLAY, used in QEMU).

Self-test: model input windows with the int8 output of TFLite's *reference* kernels, which
TFLite Micro on the ESP32 must reproduce (CONFIG_SAFETY_MODEL_SELFTEST).
"""
import argparse
import glob
import os
import sys
import tempfile

import numpy as np

FS = 50
ACC_LSB, GYR_LSB = 2048.0, 16.4          # MPU6050 at +-16 g, +-2000 dps
PRESS, LONG_PRESS = 1, 2


def load_synthetic(P):
    root = P.make_synthetic(tempfile.mkdtemp(prefix="replay_"), n_subj=1, seed=21)
    recs = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "*.txt"), recursive=True)):
        rows = [[float(v) for v in ln.strip().rstrip(";").split(",")[:6]] for ln in open(path) if ln.strip()]
        raw = np.asarray(rows)
        x = np.hstack([raw[:, 0:3] * P.ACC_SCALE, raw[:, 3:6] * P.GYR_SCALE])
        recs[os.path.basename(path)[:-4]] = P.resample_poly(x, P.FS, P.FS_RAW, axis=0)
    return recs


def still(pose, seconds, rng, acc_noise=0.003, gyr_noise=0.3):
    n = int(seconds * FS)
    return np.asarray(pose, float) + np.hstack([rng.normal(0, acc_noise, (n, 3)), rng.normal(0, gyr_noise, (n, 3))])


def lying_after(x, seconds, rng):
    """Extend a recording with the worker lying still in its final posture."""
    return np.vstack([x, still(x[-FS:].mean(0), seconds, rng, 0.005, 0.5)])


def jog(seconds, rng, cadence=2.75):
    """Synthetic jogging at the waist: vertical bounce, sway, trunk rotation."""
    t = np.arange(int(seconds * FS)) / FS
    w = 2 * np.pi * cadence * t
    ax = 0.25 * np.sin(w / 2)
    ay = -1.0 - 0.7 * np.sin(w) - 0.2 * np.sin(2 * w)
    az = 0.3 * np.sin(w + 0.6)
    g = np.stack([60 * np.sin(w / 2 + 0.3), 40 * np.sin(w / 2), 30 * np.sin(w)], 1)
    x = np.stack([ax, ay, az], 1)
    return np.hstack([x + rng.normal(0, 0.03, x.shape), g + rng.normal(0, 3, g.shape)])


def slow_collapse(seconds_down, seconds_still, rng):
    """Upright -> lying on the back over `seconds_down`, no impact, then lying still."""
    n = int(seconds_down * FS)
    th = np.radians(90) * (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n)))   # smooth 0 -> 90 deg
    acc = np.stack([np.zeros(n), -np.cos(th), np.sin(th)], 1)
    dth = np.gradient(np.degrees(th)) * FS
    gyr = np.stack([dth, np.zeros(n), np.zeros(n)], 1)
    down = np.hstack([acc + rng.normal(0, 0.01, acc.shape), gyr + rng.normal(0, 1, gyr.shape)])
    return np.vstack([down, still(np.r_[acc[-1], 0, 0, 0], seconds_still, rng)])


def crossfade(a, b, n=100):
    """Join two recordings with an n-sample linear blend (avoids an artificial step)."""
    w = np.linspace(0, 1, n)[:, None]
    return np.vstack([a[:-n], a[-n:] * (1 - w) + b[:n] * w, b[n:]])


def build_stream(recs):
    rng = np.random.default_rng(4)
    lie_pose = np.array([0.0, 0.05, 1.0, 0, 0, 0])
    stand_pose = np.array([0.0, -1.0, 0.05, 0, 0, 0])
    # name, signal, true HR, SpO2, PPG motion artefact (cadence Hz or 0)
    segs = [
        ("normal work", recs["D01_SA01_R01"], 72, 97, 0),
        ("jogging, PPG motion artefacts read ~165 bpm", jog(20, rng), 120, 97, 2.75),
        ("fall, worker motionless", lying_after(recs["F01_SA01_R01"], 10, rng), 88, 97, 0),
        ("fall, worker presses cancel", lying_after(recs["F02_SA01_R01"], 10, rng), 85, 98, 0),
        ("sustained tachycardia", np.vstack([recs["D02_SA01_R01"], recs["D01_SA01_R02"]]), 150, 96, 0),
        ("slow collapse without impact, lying still", slow_collapse(3, 60, rng), 64, 96, 0),
        ("lying still, then moves", np.vstack([still(lie_pose, 33, rng), crossfade(still(lie_pose, 2, rng),
                                               recs["D03_SA01_R01"][:6 * FS], 50)]), 66, 97, 0),
        ("standing still upright", still(stand_pose, 40, rng), 70, 98, 0),
        ("motion sensor frozen", None, 70, 98, 0),
    ]
    imu, starts, frozen_at = None, [], None
    for name, x, *_ in segs:
        if x is None:   # frozen sensor: the last sample repeated for 3 s, then real data again
            frozen_at = len(imu)
            x = np.vstack([np.repeat(imu[-1:], 3 * FS, 0), recs["D02_SA01_R02"][:3 * FS]])
            starts.append(len(imu))
            imu = np.vstack([imu, x])
            continue
        starts.append(0 if imu is None else len(imu) - 100)
        imu = x if imu is None else crossfade(imu, x)
    return segs, starts, imu, frozen_at


def to_counts(imu, frozen_at):
    lsb = np.array([ACC_LSB] * 3 + [GYR_LSB] * 3)
    c = np.clip(np.round(imu * lsb), -32768, 32767).astype(np.int16)
    c[frozen_at:frozen_at + 3 * FS] = c[frozen_at - 1]   # exactly identical raw samples
    return c, (c / lsb).astype(np.float32)


def make_ppg(segs, starts, n_imu, seed=5):
    """Continuous-phase synthetic PPG (same model as synth_ppg) following the segments,
    plus a motion artefact at the running cadence where the segment has one."""
    rng = np.random.default_rng(seed)
    n = 2 * n_imu
    seg_of = np.searchsorted(np.array(starts) * 2, np.arange(n), side="right") - 1
    hr = np.array([segs[s][2] for s in seg_of], float)
    sp = np.array([segs[s][3] for s in seg_of], float)
    art = np.array([segs[s][4] for s in seg_of], float)
    t = np.arange(n) / 100
    ph = np.cumsum(2 * np.pi * hr / 60 / 100)
    pulse = np.sin(ph) + 0.4 * np.sin(2 * ph + 0.8)
    motion = np.where(art > 0, 3.0 * np.sin(2 * np.pi * art * t), 0.0)   # 3x the pulse amplitude
    r_target = (110 - sp) / 25
    wander = 0.003 * np.sin(2 * np.pi * 0.25 * t)
    ir = 50000.0 * (1 + 0.02 * (pulse + motion) + wander) + rng.normal(0, 30, n)
    red = 40000.0 * (1 + 0.02 * (r_target * pulse + motion) + wander) + rng.normal(0, 30, n)
    red, ir = np.round(red), np.round(ir)
    assert red.min() >= 0 and ir.max() < 65536, "PPG must fit uint16"
    return red.astype(np.uint16), ir.astype(np.uint16)


def c_array(name, ctype, arr, per_line=16):
    """Flat C array, `per_line` values per line."""
    vals = [str(int(v)) for v in np.ravel(arr)]
    return ([f"static const {ctype} {name}[{len(vals)}] = {{"] +
            ["  " + ", ".join(vals[i:i + per_line]) + "," for i in range(0, len(vals), per_line)] + ["};"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "firmware", "test", "host"))
    sys.path.insert(0, os.path.join(repo, "firmware", "tools"))
    import tensorflow as tf
    import device_model as DM
    import phase1_pipeline as P
    from gen_vectors import load_header

    hp = load_header(os.path.join(repo, "fall_model_data.h"))
    tfl = open(os.path.join(repo, "fall_model_int8.tflite"), "rb").read()
    q8 = P.Int8Model(tfl)

    recs = load_synthetic(P)
    segs, starts, imu, frozen_at = build_stream(recs)
    counts, phys = to_counts(imu, frozen_at)
    red, ir = make_ppg(segs, starts, len(imu))
    seg_end = starts[1:] + [len(imu)]
    sim = lambda actions: DM.simulate(P, hp, q8, counts, phys, red, ir, actions)

    # Long presses (reset after an alert) 3 s before the end of the alert segments
    actions = [(seg_end[s] - 150, LONG_PRESS) for s, (name, *_r) in enumerate(segs)
               if name in ("fall, worker motionless", "sustained tachycardia",
                           "slow collapse without impact, lying still")]
    # Pass 1: find when the cancel-segment fall is detected, press 2 s later
    dev = sim(actions)
    for s, (name, *_r) in enumerate(segs):
        lo, hi = starts[s] / FS, seg_end[s] / FS
        sus = [t for t, c in dev.events if c == DM.EV_FALL_SUSPECTED and lo <= t < hi]
        if name == "fall, worker presses cancel" and sus:
            actions.append((int(round((sus[0] + 2.0) * FS)), PRESS))
    actions.sort()
    # Pass 2 with all scripted presses: these are the expected events
    dev = sim(actions)
    print("segments:")
    for s, (name, *_r) in enumerate(segs):
        evs = [f"{DM.EV_NAMES[c]} @{t:.2f}" for t, c in dev.events if starts[s] / FS <= t < seg_end[s] / FS]
        print(f"  {starts[s] / FS:6.2f}s  {name}: {', '.join(evs) or 'no events'}")
    print("actions:", [(round(k / FS, 2), "press" if v == PRESS else "long press") for k, v in actions])

    # Evidence that the jogging segment really exercises the vitals gating
    ungated = DM.simulate(P, hp, q8, counts, phys, red, ir, actions,
                          dev=DM.DeviceModel(P, hp, q8, DM.SafetyFSM2(vit_motion_g=0.0)))
    jog = next(s for s, (n, *_r) in enumerate(segs) if n.startswith("jogging"))
    extra = [round(t, 2) for t, c in ungated.events if c == DM.EV_ALERT_VITALS and
             starts[jog] / FS <= t < seg_end[jog] / FS + 5]
    print(f"check: without motion-aware vitals the jogging segment raises ALERT_VITALS at {extra}")

    fw_main = os.path.join(repo, "firmware", "main")
    L = ["// Auto-generated by firmware/tools/gen_replay.py -- DO NOT EDIT BY HAND",
         "// Synthetic sensor stream for CONFIG_SAFETY_REPLAY (no KFall data).",
         "#pragma once", "#include <stdint.h>", "",
         f"#define REPLAY_IMU_N {len(counts)}   // MPU6050 samples at 50 Hz ({len(counts) / FS:.0f} s)",
         f"#define REPLAY_PPG_N {len(red)}   // MAX30102 samples at 100 Hz (2 per IMU sample)",
         "#define REPLAY_PRESS 1", "#define REPLAY_LONG_PRESS 2", "",
         "typedef struct { uint32_t imu_index; const char *name; } replay_segment_t;",
         "typedef struct { uint32_t imu_index; uint8_t action; } replay_action_t;",
         "typedef struct { float t; uint8_t event; } replay_expected_t;", "",
         "static const replay_segment_t REPLAY_SEGMENTS[] = {"]
    L += [f'  {{{st}, "{name}"}},' for st, (name, *_r) in zip(starts, segs)] + ["};"]
    L += ["static const replay_action_t REPLAY_ACTIONS[] = {"]
    L += [f"  {{{k}, {v}}}," for k, v in actions] + ["};"]
    L += ["// Events of the Python model of the device on this stream (sc_event_t codes)",
          "static const replay_expected_t REPLAY_EXPECTED[] = {"]
    L += [f"  {{{t:.2f}f, {c}}},  // {DM.EV_NAMES[c]}" for t, c in dev.events] + ["};", ""]
    L += ["// ax ay az gx gy gz per sample, MPU6050 counts (2048 LSB/g, 16.4 LSB/dps)"]
    L += c_array("REPLAY_IMU", "int16_t", counts, 18)
    L += ["// red, ir per sample (ADC counts)"]
    L += c_array("REPLAY_PPG", "uint16_t", np.stack([red, ir], 1), 16)
    open(os.path.join(fw_main, "replay_data.h"), "w").write("\n".join(L) + "\n")

    # ---- model self-test: windows covering falls, ADL, random and mid-range inputs ----
    rng = np.random.default_rng(9)
    filt = P.filter50(phys.astype(np.float64))
    probs = np.array(dev.probs)
    starts_w = np.arange(len(probs)) * P.EVAL_STRIDE      # window k covers [25k, 25k + 128)
    pick = list(starts_w[np.argsort(probs)[-6:]]) + list(rng.choice(starts_w, 6, replace=False))
    quant = lambda x: np.clip(np.round(((x - hp["mean"]) / hp["std"]) / np.float32(q8.in_scale)
                                       + np.float32(q8.in_zp)), -128, 127).astype(np.int8)
    Q = [quant(filt[s:s + P.WIN]) for s in pick]
    Q += [rng.integers(-128, 128, (P.WIN, P.CH)).astype(np.int8) for _ in range(4)]
    ref = tf.lite.Interpreter(model_content=tfl,
                              experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_REF)
    ref.allocate_tensors()
    ii, oo = ref.get_input_details()[0]["index"], ref.get_output_details()[0]["index"]

    def run_ref(q):
        ref.set_tensor(ii, q[None])
        ref.invoke()
        return int(ref.get_tensor(oo).ravel()[0])

    # blends between a fall window and an ADL window: keep 12 whose output is mid-range,
    # so the self-test also covers the outputs between the two saturated ends
    blend = lambda al: np.round(al * Q[0].astype(float) + (1 - al) * Q[6].astype(float)).astype(np.int8)
    grid = np.linspace(0, 1, 401)
    mid = [al for al in grid if -124 < run_ref(blend(al)) < 124]
    mids = [mid[i] for i in np.linspace(0, len(mid) - 1, min(12, len(mid))).round().astype(int)] if mid else []
    Q += [blend(al) for al in mids]
    outs = [run_ref(q) for q in Q]
    S = ["// Auto-generated by firmware/tools/gen_replay.py -- DO NOT EDIT BY HAND",
         "// Model inputs + int8 outputs of TFLite's reference kernels (CONFIG_SAFETY_MODEL_SELFTEST).",
         "#pragma once", "#include <stdint.h>", "", f"#define SELFTEST_N {len(Q)}",
         f"#define SELFTEST_LEN {P.WIN * P.CH}"]
    S += c_array("SELFTEST_IN", "int8_t", np.stack([q.ravel() for q in Q]), 24)
    S += c_array("SELFTEST_OUT", "int8_t", np.array(outs, np.int8), 16)
    open(os.path.join(fw_main, "selftest_data.h"), "w").write("\n".join(S) + "\n")
    print(f"self-test outputs: {outs}")
    print("wrote firmware/main/replay_data.h and firmware/main/selftest_data.h")


if __name__ == "__main__":
    main()
