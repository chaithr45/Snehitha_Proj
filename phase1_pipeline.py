#!/usr/bin/env python3
"""
==============================================================================
 Edge-AI Worker Safety Monitoring System  --  PHASE 1 (software / model stage)
==============================================================================
 Target hardware (Phase 2): ESP32 / ESP32-S3 + MPU6050 (waist) + MAX30102
 Inference engine          : TensorFlow Lite Micro (esp-tflite-micro), int8

 What this script does, end to end:
   1. Loads KFall (the dataset of the previous AIMLBasedFallDetection work:
      32 subjects, 21 ADL + 15 fall types, low-back IMU @ 100 Hz, with
      expert-labelled fall onset/impact frames).  SisFall also supported.
   2. Keeps the 6 channels an MPU6050 actually gives (acc g, gyro deg/s);
      Euler angles are dropped (no fused orientation on the MCU)
   3. Resamples to 50 Hz, causal 4th-order Butterworth LPF @ 5 Hz
      (causal on purpose: identical to what the ESP32 will run)
   4. Windows: 2.56 s (128 samples x 6 ch); impact-aligned fall windows
   5. SUBJECT-INDEPENDENT split (test subjects never seen in training)
   6. Trains a tiny 1D-CNN (TFLM-friendly ops only, no LSTM)
   7. Picks the decision threshold on validation subjects only
   8. int8 full-integer quantisation -> fall_model_int8.tflite
   9. Evaluates float AND int8 models: window-level + trial-level (deployment)
  10. Exports fall_model_data.h  (model bytes + mean/std + filter + quant params)
  11. Vitals reference algorithms (HR, SpO2) for MAX30102 + self-check
  12. Alert state machine (cancel button + post-fall inactivity + vitals)
  13. Saves figures + results.json + results.md for the paper

 Usage:
   python phase1_pipeline.py --data "C:/.../AIMLBasedFallDetection"           (KFall)
   python phase1_pipeline.py --data "C:/.../AIMLBasedFallDetection" --cv 5
   python phase1_pipeline.py --dataset sisfall --data "C:/.../SisFall_dataset"
   python phase1_pipeline.py --synthetic        # smoke test, no dataset needed
==============================================================================
"""
import argparse
import glob
import json
import os
import re
import tempfile
import time

import numpy as np
from scipy.signal import butter, find_peaks, resample_poly, sosfilt, sosfilt_zi
from scipy.spatial.transform import Rotation

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

# ----------------------------------------------------------------------------
# Configuration (everything the firmware must match is here)
# ----------------------------------------------------------------------------
FS_RAW = 200                      # SisFall sampling rate
FS = 50                           # device sampling rate (MPU6050 @ 50 Hz)
WIN = 128                         # 2.56 s window
CH = 6                            # ax, ay, az [g], gx, gy, gz [deg/s]
CH_NAMES = ["ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"]
LPF_CUTOFF = 5.0
LPF_ORDER = 4
SOS = butter(LPF_ORDER, LPF_CUTOFF, btype="low", fs=FS, output="sos")

ADL_STRIDE_TRAIN = 32             # ADL window hop for training
EVAL_STRIDE = 25                  # 0.5 s hop = device inference period
IMPACT_POS_EVAL = 0.6             # impact at 60 % of the test window
IMPACT_POS_TRAIN = np.linspace(0.35, 0.85, 9)   # augmentation positions

# SisFall conversion (dataset readme): ADXL345 +-16 g / 13 bit, ITG3200 +-2000 dps / 16 bit
ACC_SCALE = (2 * 16) / (2 ** 13)
GYR_SCALE = (2 * 2000) / (2 ** 16)

SEED = 42
FNAME_RE = re.compile(r"([DF])(\d{2})_(S[AE]\d{2})_R(\d{2})\.txt$", re.IGNORECASE)


# ----------------------------------------------------------------------------
# 1. Dataset loading + preprocessing
# ----------------------------------------------------------------------------
def preprocess(raw_counts):
    """raw SisFall counts (N x >=6) -> filtered 50 Hz signal (M x 6), float32."""
    acc = raw_counts[:, 0:3] * ACC_SCALE
    gyr = raw_counts[:, 3:6] * GYR_SCALE
    x = np.hstack([acc, gyr]).astype(np.float64)
    x = resample_poly(x, FS, FS_RAW, axis=0)
    # causal filter, state initialised with first sample (do the same on ESP32)
    zi = sosfilt_zi(SOS)[:, :, None] * x[0][None, None, :]
    x, _ = sosfilt(SOS, x, axis=0, zi=zi)
    return x.astype(np.float32)


def load_trial(path):
    rows = []
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            line = line.strip().rstrip(";").strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                rows.append([float(p) for p in parts[:6]])
            except ValueError:
                continue
    if len(rows) < WIN * 4:
        return None
    return preprocess(np.asarray(rows, dtype=np.float64))



# ---------------- KFall (sensor_data_new/ + label_data_new/) ----------------
FS_KFALL = 100
KFALL_RE = re.compile(r"S(\d{2})T(\d{2})R(\d{2})\.csv$", re.IGNORECASE)
KFALL_FALL_TASKS = set(range(20, 35))          # T20..T34 = F01..F15


def filter50(x):
    zi = sosfilt_zi(SOS)[:, :, None] * x[0][None, None, :]
    y, _ = sosfilt(SOS, x, axis=0, zi=zi)
    return y.astype(np.float32)


def read_kfall_labels(label_dir):
    import pandas as pd
    lab = {}
    for f in glob.glob(os.path.join(label_dir, "SA*_label.xlsx")):
        subj = os.path.basename(f)[:4].upper()
        df = pd.read_excel(f)
        df.columns = [str(c).strip() for c in df.columns]
        task_col = [c for c in df.columns if c.lower().startswith("task")][0]
        df[task_col] = df[task_col].ffill()
        for _, r in df.iterrows():
            m = re.search(r"\((\d+)\)", str(r[task_col]))
            if not m or pd.isnull(r["Fall_impact_frame"]):
                continue
            lab[(subj, int(m.group(1)), int(r["Trial ID"]))] = (
                int(r["Fall_onset_frame"]), int(r["Fall_impact_frame"]))
    return lab


def load_kfall(root):
    import pandas as pd
    sdir = os.path.join(root, "sensor_data_new") if os.path.isdir(os.path.join(root, "sensor_data_new")) else root
    ldir = os.path.join(root, "label_data_new")
    labels = read_kfall_labels(ldir) if os.path.isdir(ldir) else {}
    print(f"[kfall] {len(labels)} labelled fall trials")
    trials, data = [], []
    files = sorted(glob.glob(os.path.join(sdir, "**", "*.csv"), recursive=True))
    t0 = time.time()
    for i, f in enumerate(files):
        m = KFALL_RE.search(os.path.basename(f))
        if not m:
            continue
        sid, task, rep = int(m.group(1)), int(m.group(2)), int(m.group(3))
        subj = f"SA{sid:02d}"
        is_fall = task in KFALL_FALL_TASKS
        try:
            df = pd.read_csv(f)
            raw = df[["AccX", "AccY", "AccZ", "GyrX", "GyrY", "GyrZ"]].to_numpy(np.float64)
        except Exception:
            continue
        if len(raw) < WIN * 2 + 10 or not np.isfinite(raw).all():
            continue
        impact = None
        if is_fall:
            lb = labels.get((subj, task, rep))
            if lb is None:
                continue                      # never train on an unlabelled fall
            fc = df["FrameCounter"].to_numpy()
            hit = np.where(fc == lb[1])[0]
            idx100 = int(hit[0]) if len(hit) else lb[1] - 1
            impact = int(round(idx100 * FS / FS_KFALL))
        x = filter50(resample_poly(raw, FS, FS_KFALL, axis=0))
        trials.append(dict(path=f, is_fall=is_fall, activity=f"T{task:02d}",
                           subject=subj, rep=rep, impact=impact))
        data.append(x)
        if (i + 1) % 1000 == 0:
            print(f"   {i + 1}/{len(files)}  ({time.time() - t0:.0f}s)")
    return trials, data


def index_dataset(root):
    trials = []
    for f in glob.glob(os.path.join(root, "**", "*.txt"), recursive=True):
        m = FNAME_RE.search(os.path.basename(f))
        if not m:
            continue
        kind, act, subj, rep = m.groups()
        trials.append(dict(path=f, is_fall=kind.upper() == "F",
                           activity=f"{kind.upper()}{act}", subject=subj.upper(),
                           rep=int(rep)))
    trials.sort(key=lambda t: t["path"])
    return trials


def load_dataset(root, cache_dir, kind="kfall"):
    cache = os.path.join(cache_dir, f"{kind}_cache.npz")
    if os.path.exists(cache):
        z = np.load(cache, allow_pickle=True)
        print(f"[data] loaded cache {cache}")
        return list(z["trials"]), list(z["data"])
    if kind == "kfall":
        keep_t, keep_d = load_kfall(root)
        if not keep_t:
            raise SystemExit(f"No KFall files (SxxTyyRzz.csv) found under {root}")
        data_obj = np.empty(len(keep_d), dtype=object); data_obj[:] = keep_d
        np.savez(cache, trials=np.array(keep_t, dtype=object), data=data_obj)
        return keep_t, keep_d
    trials = index_dataset(root)
    if not trials:
        raise SystemExit(f"No SisFall files found under {root}. "
                         "Expected names like F01_SA01_R01.txt")
    print(f"[data] parsing {len(trials)} trial files (one-time, cached afterwards)...")
    keep_t, keep_d = [], []
    t0 = time.time()
    for i, t in enumerate(trials):
        x = load_trial(t["path"])
        if x is not None:
            keep_t.append(t)
            keep_d.append(x)
        if (i + 1) % 500 == 0:
            print(f"   {i + 1}/{len(trials)}  ({time.time() - t0:.0f}s)")
    data_obj = np.empty(len(keep_d), dtype=object)
    data_obj[:] = keep_d
    np.savez(cache, trials=np.array(keep_t, dtype=object), data=data_obj)
    return keep_t, keep_d


# ----------------------------------------------------------------------------
# 2. Windowing
# ----------------------------------------------------------------------------
def impact_index(x):
    return int(np.argmax(np.linalg.norm(x[:, :3], axis=1)))


def window_at(x, start):
    start = int(np.clip(start, 0, len(x) - WIN))
    return x[start:start + WIN]


def make_windows(trials, data, train):
    X, y, g = [], [], []
    for t, x in zip(trials, data):
        if len(x) < WIN:
            continue
        if t["is_fall"]:
            imp = t.get("impact")
            if imp is None:
                imp = impact_index(x)
            for o in (IMPACT_POS_TRAIN if train else [IMPACT_POS_EVAL]):
                X.append(window_at(x, imp - int(o * WIN)))
                y.append(1)
                g.append(t["subject"])
        else:
            hop = ADL_STRIDE_TRAIN if train else WIN // 2
            for s in range(0, len(x) - WIN + 1, hop):
                X.append(x[s:s + WIN])
                y.append(0)
                g.append(t["subject"])
    return np.stack(X).astype(np.float32), np.array(y, np.float32), np.array(g)


# ----------------------------------------------------------------------------
# 3. Augmentation (mounting-orientation robustness, sensor noise, gain error)
# ----------------------------------------------------------------------------
def augment(xb, rng):
    n = len(xb)
    rot = Rotation.from_rotvec(rng.normal(0, np.deg2rad(10), (n, 3))).as_matrix()
    acc = np.einsum("nij,ntj->nti", rot, xb[:, :, 0:3])
    gyr = np.einsum("nij,ntj->nti", rot, xb[:, :, 3:6])
    out = np.concatenate([acc, gyr], axis=2)
    out *= rng.uniform(0.92, 1.08, (n, 1, CH))
    out[:, :, 0:3] += rng.normal(0, 0.01, out[:, :, 0:3].shape)
    out[:, :, 3:6] += rng.normal(0, 1.0, out[:, :, 3:6].shape)
    return out.astype(np.float32)


# ----------------------------------------------------------------------------
# 4. Model -- TFLM-safe ops only (CONV_2D, MAX_POOL_2D, MEAN, FC, LOGISTIC)
# ----------------------------------------------------------------------------
def build_model():
    import tensorflow as tf
    from tensorflow.keras import layers
    inp = tf.keras.Input(shape=(WIN, CH), name="imu")
    x = layers.Conv1D(16, 7, padding="same", use_bias=False)(inp)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.MaxPooling1D(2)(x)
    x = layers.Conv1D(32, 5, padding="same", use_bias=False)(x)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.MaxPooling1D(2)(x)
    x = layers.Conv1D(32, 3, padding="same", use_bias=False)(x)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.GlobalAveragePooling1D()(x)
    x = layers.Dropout(0.3)(x)
    x = layers.Dense(16, activation="relu")(x)
    out = layers.Dense(1, activation="sigmoid", name="fall_prob")(x)
    model = tf.keras.Model(inp, out, name="fall_cnn")
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3),
                  loss="binary_crossentropy",
                  metrics=["accuracy", tf.keras.metrics.AUC(name="auc")])
    return model


def train_model(Xtr, ytr, Xva, yva, mean, std, epochs, batch, seed, verbose=1):
    import tensorflow as tf
    tf.keras.utils.set_random_seed(seed)
    rng = np.random.default_rng(seed)
    n_pos, n_neg = ytr.sum(), len(ytr) - ytr.sum()
    cw = {0: len(ytr) / (2 * n_neg), 1: len(ytr) / (2 * n_pos)}

    def gen():
        idx = rng.permutation(len(Xtr))
        for s in range(0, len(idx), batch):
            b = idx[s:s + batch]
            xb = (augment(Xtr[b], rng) - mean) / std
            yb = ytr[b]
            wb = np.where(yb == 1, cw[1], cw[0]).astype(np.float32)
            yield xb.astype(np.float32), yb[:, None], wb

    ds = tf.data.Dataset.from_generator(
        gen, output_signature=(
            tf.TensorSpec((None, WIN, CH), tf.float32),
            tf.TensorSpec((None, 1), tf.float32),
            tf.TensorSpec((None,), tf.float32))).prefetch(2)
    val = ((Xva - mean) / std, yva[:, None])

    model = build_model()
    cbs = [tf.keras.callbacks.EarlyStopping(monitor="val_auc", mode="max", patience=8,
                                            restore_best_weights=True),
           tf.keras.callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5,
                                                patience=3, min_lr=1e-5)]
    hist = model.fit(ds, validation_data=val, epochs=epochs, callbacks=cbs,
                     verbose=verbose)
    return model, hist.history


# ----------------------------------------------------------------------------
# 5. Metrics
# ----------------------------------------------------------------------------
def metrics(y, p, thr):
    from sklearn.metrics import roc_auc_score
    yhat = (p >= thr).astype(int)
    tp = int(((yhat == 1) & (y == 1)).sum()); fn = int(((yhat == 0) & (y == 1)).sum())
    tn = int(((yhat == 0) & (y == 0)).sum()); fp = int(((yhat == 1) & (y == 0)).sum())
    sens = tp / max(tp + fn, 1); spec = tn / max(tn + fp, 1)
    prec = tp / max(tp + fp, 1)
    f1 = 2 * prec * sens / max(prec + sens, 1e-9)
    try:
        auc = float(roc_auc_score(y, p))
    except ValueError:
        auc = float("nan")
    return dict(accuracy=(tp + tn) / max(len(y), 1), sensitivity=sens, specificity=spec,
                precision=prec, f1=f1, balanced_acc=(sens + spec) / 2, auc=auc,
                tp=tp, fn=fn, tn=tn, fp=fp)


def pick_threshold(y, p):
    """Max F1 on validation; ties broken toward lower threshold (safety-first)."""
    best_t, best_f = 0.5, -1
    for t in np.linspace(0.05, 0.95, 91):
        f = metrics(y, p, t)["f1"]
        if f > best_f + 1e-9:
            best_t, best_f = float(t), f
    return best_t


def trial_level(predict, trials, data, mean, std, thr, consec=2):
    """Deployment metric: slide over the whole recording every 0.5 s.
    A trial is 'alarmed' if >= `consec` consecutive windows exceed thr."""
    y, yhat = [], []
    for t, x in zip(trials, data):
        if len(x) < WIN:
            continue
        W = np.stack([x[s:s + WIN] for s in range(0, len(x) - WIN + 1, EVAL_STRIDE)])
        hit = predict((W - mean) / std) >= thr
        run = best = 0
        for h in hit:
            run = run + 1 if h else 0
            best = max(best, run)
        y.append(int(t["is_fall"]))
        yhat.append(int(best >= consec))
    y, yhat = np.array(y), np.array(yhat, float)
    m = metrics(y, yhat, 0.5)
    m.pop("auc", None)
    return m


# ----------------------------------------------------------------------------
# 6. int8 conversion + interpreter wrapper
# ----------------------------------------------------------------------------
def to_int8_tflite(model, Xrep):
    import tensorflow as tf

    def rep():
        idx = np.random.default_rng(0).choice(len(Xrep), min(1000, len(Xrep)), replace=False)
        for i in idx:
            yield [Xrep[i:i + 1].astype(np.float32)]

    def configure(c):
        c.optimizations = [tf.lite.Optimize.DEFAULT]
        c.representative_dataset = rep
        c.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
        c.inference_input_type = tf.int8
        c.inference_output_type = tf.int8
        return c

    try:
        return configure(tf.lite.TFLiteConverter.from_keras_model(model)).convert()
    except Exception as e:  # Keras-3 fallback path
        print(f"[tflite] from_keras_model failed ({e}); using concrete function")
        fn = tf.function(lambda x: model(x, training=False),
                         input_signature=[tf.TensorSpec([1, WIN, CH], tf.float32)])
        cf = fn.get_concrete_function()
        return configure(tf.lite.TFLiteConverter.from_concrete_functions([cf], model)).convert()


class Int8Model:
    def __init__(self, tfl_bytes):
        import tensorflow as tf
        self.it = tf.lite.Interpreter(model_content=tfl_bytes)
        self.it.allocate_tensors()
        self.inp = self.it.get_input_details()[0]
        self.out = self.it.get_output_details()[0]
        self.in_scale, self.in_zp = self.inp["quantization"]
        self.out_scale, self.out_zp = self.out["quantization"]

    def __call__(self, Xn):
        q = np.clip(np.round(Xn / self.in_scale + self.in_zp), -128, 127).astype(np.int8)
        res = np.empty(len(q), np.float32)
        for i in range(len(q)):
            self.it.set_tensor(self.inp["index"], q[i:i + 1])
            self.it.invoke()
            o = self.it.get_tensor(self.out["index"]).astype(np.float32)
            res[i] = (o.ravel()[0] - self.out_zp) * self.out_scale
        return res

    def op_names(self):
        try:
            return sorted({d["op_name"] for d in self.it._get_ops_details()} - {"DELEGATE"})
        except Exception:
            return ["(op list unavailable in this TF version)"]


# ----------------------------------------------------------------------------
# 7. C header for ESP32 firmware
# ----------------------------------------------------------------------------
def write_header(path, tfl, mean, std, q, thr, consec, ops):
    def farr(a):
        return ", ".join(f"{v:.8f}f" for v in np.ravel(a))
    lines = [
        "// Auto-generated by phase1_pipeline.py -- DO NOT EDIT BY HAND",
        "#pragma once", "#include <stdint.h>", "",
        f"// TFLM ops to register: {', '.join(ops)}",
        f"#define FALL_FS_HZ          {FS}",
        f"#define FALL_WIN            {WIN}",
        f"#define FALL_CH             {CH}",
        f"#define FALL_HOP            {EVAL_STRIDE}   // run inference every 0.5 s",
        f"#define FALL_CONSEC         {consec}        // consecutive positive windows",
        f"#define FALL_THRESHOLD      {thr:.4f}f",
        f"#define FALL_IN_SCALE       {q['in_scale']:.10f}f",
        f"#define FALL_IN_ZP          {q['in_zp']}",
        f"#define FALL_OUT_SCALE      {q['out_scale']:.10f}f",
        f"#define FALL_OUT_ZP         {q['out_zp']}",
        "// Channel order: ax ay az [g], gx gy gz [deg/s]; MPU6050 at +-16 g, +-2000 dps",
        f"static const float FALL_MEAN[{CH}] = {{{farr(mean)}}};",
        f"static const float FALL_STD[{CH}]  = {{{farr(std)}}};",
        f"// Butterworth LPF order {LPF_ORDER}, fc {LPF_CUTOFF} Hz @ {FS} Hz, SOS rows: b0 b1 b2 a0 a1 a2",
        f"static const float LPF_SOS[{len(SOS)}][6] = {{",
        *[f"  {{{farr(r)}}}," for r in SOS], "};",
        f"static const float LPF_ZI[{len(SOS)}][2] = {{",
        *[f"  {{{farr(r)}}}," for r in sosfilt_zi(SOS)],
        "};  // multiply by first sample of each channel at start-up", "",
        f"alignas(16) const unsigned char g_fall_model[] = {{",
    ]
    for i in range(0, len(tfl), 12):
        lines.append("  " + ", ".join(f"0x{b:02x}" for b in tfl[i:i + 12]) + ",")
    lines += ["};", f"const unsigned int g_fall_model_len = {len(tfl)};", ""]
    with open(path, "w") as f:
        f.write("\n".join(lines))


# ----------------------------------------------------------------------------
# 8. MAX30102 vitals reference algorithms (port 1:1 to C on the ESP32)
# ----------------------------------------------------------------------------
def estimate_hr(ir, fs):
    sos = butter(2, [0.5, 4.0], btype="band", fs=fs, output="sos")
    s = sosfilt(sos, ir - ir[0])[int(fs * 2):]           # drop filter settling
    peaks, _ = find_peaks(s, distance=int(fs * 0.33), prominence=np.std(s) * 0.5)
    if len(peaks) < 3:
        return float("nan")
    return 60.0 / float(np.median(np.diff(peaks)) / fs)


def estimate_spo2(red, ir, fs):
    """Ratio-of-ratios. 110-25R is the common empirical curve; it MUST be
    calibrated against a reference oximeter before reporting absolute SpO2."""
    sos = butter(2, [0.5, 4.0], btype="band", fs=fs, output="sos")
    k = int(fs * 2)
    ac_r, ac_i = np.std(sosfilt(sos, red - red[0])[k:]), np.std(sosfilt(sos, ir - ir[0])[k:])
    r = (ac_r / np.mean(red)) / (ac_i / np.mean(ir))
    return float(np.clip(110.0 - 25.0 * r, 0, 100))


def synth_ppg(hr, spo2, fs=100, dur=20, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(fs * dur)) / fs
    ph = 2 * np.pi * (hr / 60) * t + 0.05 * np.cumsum(rng.normal(0, 1, len(t))) / fs
    pulse = np.sin(ph) + 0.4 * np.sin(2 * ph + 0.8)
    r_target = (110 - spo2) / 25
    dc_ir, dc_red, pi_ir = 50000.0, 40000.0, 0.02
    wander = 0.003 * np.sin(2 * np.pi * 0.25 * t)
    ir = dc_ir * (1 + pi_ir * pulse + wander) + rng.normal(0, 30, len(t))
    red = dc_red * (1 + r_target * pi_ir * pulse + wander) + rng.normal(0, 30, len(t))
    return red, ir


def vitals_selfcheck():
    rows = []
    for hr, sp in [(55, 98), (72, 97), (95, 95), (120, 93), (150, 90), (170, 88)]:
        red, ir = synth_ppg(hr, sp, seed=hr)
        rows.append(dict(true_hr=hr, est_hr=round(estimate_hr(ir, 100), 1),
                         true_spo2=sp, est_spo2=round(estimate_spo2(red, ir, 100), 1)))
    return rows


# ----------------------------------------------------------------------------
# 9. Alert state machine (cancel window + post-fall inactivity + vitals)
# ----------------------------------------------------------------------------
class SafetyFSM:
    NORMAL, FALL_PENDING, ALERT = "NORMAL", "FALL_PENDING", "ALERT"

    def __init__(self, cancel_s=10.0, inactive_s=5.0, motion_thr_g=0.05,
                 hr_lo=45, hr_hi=140, spo2_lo=90, vitals_hold_s=10.0):
        self.cancel_s, self.inactive_s, self.motion_thr = cancel_s, inactive_s, motion_thr_g
        self.hr_lo, self.hr_hi, self.spo2_lo, self.hold = hr_lo, hr_hi, spo2_lo, vitals_hold_s
        self.state, self.t_fall, self.still_since, self.vit_bad_since = self.NORMAL, None, None, None
        self.log = []

    def _emit(self, t, ev):
        self.log.append((round(t, 1), ev))

    def step(self, t, fall, motion_std_g, button, hr, spo2):
        bad = (hr < self.hr_lo or hr > self.hr_hi or spo2 < self.spo2_lo)
        self.vit_bad_since = (t if self.vit_bad_since is None else self.vit_bad_since) if bad else None
        if self.state != self.ALERT and self.vit_bad_since is not None \
                and t - self.vit_bad_since >= self.hold:
            self.state = self.ALERT
            self._emit(t, f"ALERT_VITALS hr={hr:.0f} spo2={spo2:.0f} -> LoRa/Wi-Fi to supervisor")
        if self.state == self.NORMAL and fall:
            self.state, self.t_fall, self.still_since = self.FALL_PENDING, t, None
            self._emit(t, "FALL_SUSPECTED -> buzzer + vibration, cancel window open")
        elif self.state == self.FALL_PENDING:
            if button:
                self.state = self.NORMAL
                self._emit(t, "CANCELLED by worker (false alarm suppressed)")
                return self.state
            self.still_since = (t if self.still_since is None else self.still_since) \
                if motion_std_g < self.motion_thr else None
            inactive = self.still_since is not None and t - self.still_since >= self.inactive_s
            if t - self.t_fall >= self.cancel_s or inactive:
                self.state = self.ALERT
                why = "post-fall inactivity" if inactive else "no cancel in time"
                self._emit(t, f"ALERT_FALL ({why}) -> LoRa/Wi-Fi to supervisor")
        return self.state


def fsm_demo():
    scen = {
        "Real fall, worker motionless": dict(fall_at=5, press_at=None, still=True, hr=88, spo2=97),
        "False alarm, worker presses cancel": dict(fall_at=5, press_at=8, still=False, hr=85, spo2=98),
        "No fall, sustained tachycardia": dict(fall_at=None, press_at=None, still=False, hr=150, spo2=96),
        "Normal work": dict(fall_at=None, press_at=None, still=False, hr=90, spo2=98),
    }
    out = {}
    for name, s in scen.items():
        f = SafetyFSM()
        for k in range(0, 60):
            t = k * 0.5
            fall = s["fall_at"] is not None and abs(t - s["fall_at"]) < 1e-6
            moving = 0.01 if (s["still"] and s["fall_at"] and t > s["fall_at"]) else 0.2
            press = s["press_at"] is not None and abs(t - s["press_at"]) < 1e-6
            f.step(t, fall, moving, press, s["hr"], s["spo2"])
        out[name] = f.log or [("-", "no events (correct)")]
    return out


# ----------------------------------------------------------------------------
# 10. Synthetic SisFall-format data (smoke test only)
# ----------------------------------------------------------------------------
def make_synthetic(root, n_subj=10, seed=0):
    rng = np.random.default_rng(seed)
    g_cnt = 1 / ACC_SCALE
    for s in range(1, n_subj + 1):
        subj = f"SA{s:02d}"
        os.makedirs(os.path.join(root, subj), exist_ok=True)
        for kind, acts in (("F", 4), ("D", 4)):
            for a in range(1, acts + 1):
                for r in range(1, 3):
                    n = FS_RAW * 15
                    t = np.arange(n) / FS_RAW
                    acc = np.zeros((n, 3)); gyr = rng.normal(0, 5, (n, 3))
                    acc[:, 1] = -1.0 + 0.2 * a / 4 * np.sin(2 * np.pi * (1 + a * 0.3) * t)
                    acc += rng.normal(0, 0.03, (n, 3))
                    if kind == "F":
                        i0 = int(n * rng.uniform(0.35, 0.6))
                        acc[i0:i0 + 40, :] += rng.normal(0, 1, 3) * 3 + np.array([0, 2.5, 0])
                        gyr[i0 - 60:i0 + 20] += rng.normal(0, 1, 3) * 250
                        acc[i0 + 40:, 1] = rng.normal(0, 0.02, n - i0 - 40)
                        acc[i0 + 40:, 2] = 1.0 + rng.normal(0, 0.02, n - i0 - 40)
                    raw = np.hstack([acc * g_cnt, gyr / GYR_SCALE, acc * g_cnt / 2]).astype(int)
                    fn = os.path.join(root, subj, f"{kind}{a:02d}_{subj}_R{r:02d}.txt")
                    with open(fn, "w") as f:
                        for row in raw:
                            f.write(",".join(f"{v:5d}" for v in row) + ";\n")
    return root


# ----------------------------------------------------------------------------
# 11. Plots
# ----------------------------------------------------------------------------
def save_plots(out, hist, y, p_float, thr, m_int8):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sklearn.metrics import roc_curve

    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6))
    ax[0].plot(hist["loss"], label="train"); ax[0].plot(hist["val_loss"], label="val")
    ax[0].set_title("Loss"); ax[0].set_xlabel("epoch"); ax[0].legend()
    ax[1].plot(hist["auc"], label="train"); ax[1].plot(hist["val_auc"], label="val")
    ax[1].set_title("AUC"); ax[1].set_xlabel("epoch"); ax[1].legend()
    fig.tight_layout(); fig.savefig(os.path.join(out, "fig_training_curves.png"), dpi=300)

    fpr, tpr, _ = roc_curve(y, p_float)
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.plot(fpr, tpr, lw=2); ax.plot([0, 1], [0, 1], "--", c="gray")
    ax.set_xlabel("False positive rate"); ax.set_ylabel("True positive rate")
    ax.set_title("ROC (unseen test subjects)")
    fig.tight_layout(); fig.savefig(os.path.join(out, "fig_roc.png"), dpi=300)

    cm = np.array([[m_int8["tn"], m_int8["fp"]], [m_int8["fn"], m_int8["tp"]]])
    fig, ax = plt.subplots(figsize=(4, 3.6))
    ax.imshow(cm, cmap="Blues")
    for i in range(2):
        for j in range(2):
            ax.text(j, i, str(cm[i, j]), ha="center", va="center",
                    color="white" if cm[i, j] > cm.max() / 2 else "black", fontsize=12)
    ax.set_xticks([0, 1], ["ADL", "Fall"]); ax.set_yticks([0, 1], ["ADL", "Fall"])
    ax.set_xlabel("Predicted"); ax.set_ylabel("True")
    ax.set_title(f"int8 model, thr={thr:.2f}")
    fig.tight_layout(); fig.savefig(os.path.join(out, "fig_confusion_int8.png"), dpi=300)
    plt.close("all")


# ----------------------------------------------------------------------------
# 12. Subject-wise cross-validation (paper-grade mean +- std)
# ----------------------------------------------------------------------------
def cross_validate(trials, data, k, epochs, batch):
    from sklearn.model_selection import GroupKFold
    subjects = np.array([t["subject"] for t in trials])
    res = []
    for fold, (tr_i, te_i) in enumerate(GroupKFold(k).split(trials, groups=subjects)):
        tr_t = [trials[i] for i in tr_i]; tr_d = [data[i] for i in tr_i]
        te_t = [trials[i] for i in te_i]; te_d = [data[i] for i in te_i]
        tr_subj = sorted({t["subject"] for t in tr_t})
        rng = np.random.default_rng(SEED + fold)
        va_subj = set(rng.choice(tr_subj, max(2, len(tr_subj) // 8), replace=False))
        fit = [(t, d) for t, d in zip(tr_t, tr_d) if t["subject"] not in va_subj]
        va = [(t, d) for t, d in zip(tr_t, tr_d) if t["subject"] in va_subj]
        Xtr, ytr, _ = make_windows(*zip(*fit), train=True)
        Xva, yva, _ = make_windows(*zip(*va), train=False)
        Xte, yte, _ = make_windows(te_t, te_d, train=False)
        mean = Xtr.reshape(-1, CH).mean(0); std = Xtr.reshape(-1, CH).std(0) + 1e-6
        model, _ = train_model(Xtr, ytr, Xva, yva, mean, std, epochs, batch, SEED + fold, 0)
        pf = lambda X: model.predict(X, batch_size=1024, verbose=0).ravel()
        thr = pick_threshold(yva, pf((Xva - mean) / std))
        mw = metrics(yte, pf((Xte - mean) / std), thr)
        mt = trial_level(pf, te_t, te_d, mean, std, thr)
        print(f"  fold {fold + 1}/{k}: window acc={mw['accuracy']:.4f} sens={mw['sensitivity']:.4f} "
              f"spec={mw['specificity']:.4f} | trial sens={mt['sensitivity']:.4f} spec={mt['specificity']:.4f}")
        res.append(dict(window=mw, trial=mt))
    summ = {}
    for lvl in ("window", "trial"):
        for key in ("accuracy", "sensitivity", "specificity", "f1"):
            v = np.array([r[lvl][key] for r in res])
            summ[f"{lvl}_{key}"] = (float(v.mean()), float(v.std()))
    return summ


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="AIMLBasedFallDetection folder (KFall) or SisFall_dataset")
    ap.add_argument("--dataset", choices=["kfall", "sisfall"], default="kfall")
    ap.add_argument("--out", default="phase1_outputs")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--test_frac", type=float, default=0.2)
    ap.add_argument("--consec", type=int, default=2)
    ap.add_argument("--cv", type=int, default=0, help="k for subject-wise k-fold CV (0 = off)")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()

    import tensorflow as tf
    os.makedirs(a.out, exist_ok=True)
    if a.synthetic:
        a.data = make_synthetic(tempfile.mkdtemp(prefix="sisfall_synth_"))
        a.epochs = min(a.epochs, 8)
        a.dataset = "sisfall"   # synthetic files use SisFall format
        print(f"[synthetic] smoke-test data at {a.data}")
    if not a.data:
        raise SystemExit("Give --data <dataset folder> or --synthetic")

    trials, data = load_dataset(a.data, a.out if not a.synthetic else tempfile.mkdtemp(), a.dataset)
    subjects = sorted({t["subject"] for t in trials})
    n_f = sum(t["is_fall"] for t in trials)
    print(f"[data] {len(trials)} trials | {n_f} falls, {len(trials) - n_f} ADLs | {len(subjects)} subjects")

    # ---- subject-independent split: train / val / test ----
    rng = np.random.default_rng(SEED)
    fall_subj = sorted({t["subject"] for t in trials if t["is_fall"]})
    other = [s for s in subjects if s not in fall_subj]
    perm_f, perm_o = list(rng.permutation(fall_subj)), list(rng.permutation(other))
    n_te_f = max(1, round(len(fall_subj) * a.test_frac)); n_va_f = max(1, round(len(fall_subj) * 0.12))
    n_te_o = round(len(other) * a.test_frac); n_va_o = round(len(other) * 0.12)
    te_s = set(perm_f[:n_te_f] + perm_o[:n_te_o])
    va_s = set(perm_f[n_te_f:n_te_f + n_va_f] + perm_o[n_te_o:n_te_o + n_va_o])
    split = lambda S: ([t for t in trials if t["subject"] in S],
                       [d for t, d in zip(trials, data) if t["subject"] in S])
    te_t, te_d = split(te_s); va_t, va_d = split(va_s)
    tr_t, tr_d = split(set(subjects) - te_s - va_s)
    print(f"[split] test subjects: {sorted(map(str, te_s))}")
    print(f"[split] val  subjects: {sorted(map(str, va_s))}")

    Xtr, ytr, _ = make_windows(tr_t, tr_d, train=True)
    Xva, yva, _ = make_windows(va_t, va_d, train=False)
    Xte, yte, _ = make_windows(te_t, te_d, train=False)
    print(f"[win] train {Xtr.shape} ({int(ytr.sum())} fall) | val {Xva.shape} | test {Xte.shape} ({int(yte.sum())} fall)")

    mean = Xtr.reshape(-1, CH).mean(0); std = Xtr.reshape(-1, CH).std(0) + 1e-6

    # ---- train ----
    model, hist = train_model(Xtr, ytr, Xva, yva, mean, std, a.epochs, a.batch, SEED)
    model.save(os.path.join(a.out, "fall_cnn.keras"))
    pf = lambda X: model.predict(X, batch_size=1024, verbose=0).ravel()
    thr = pick_threshold(yva, pf((Xva - mean) / std))
    print(f"[thr] decision threshold (chosen on validation subjects only) = {thr:.3f}")

    # ---- int8 ----
    tfl = to_int8_tflite(model, (Xtr[::7] - mean) / std)
    with open(os.path.join(a.out, "fall_model_int8.tflite"), "wb") as f:
        f.write(tfl)
    q8 = Int8Model(tfl)
    ops = q8.op_names()

    # ---- evaluation on unseen subjects ----
    p_f = pf((Xte - mean) / std)
    p_q = q8((Xte - mean) / std)
    m_float = metrics(yte, p_f, thr)
    m_int8 = metrics(yte, p_q, thr)
    t_float = trial_level(pf, te_t, te_d, mean, std, thr, a.consec)
    t_int8 = trial_level(q8, te_t, te_d, mean, std, thr, a.consec)

    xs = ((Xte[:1] - mean) / std)
    t0 = time.perf_counter()
    for _ in range(200):
        q8(xs)
    host_ms = (time.perf_counter() - t0) / 200 * 1000

    qp = dict(in_scale=float(q8.in_scale), in_zp=int(q8.in_zp),
              out_scale=float(q8.out_scale), out_zp=int(q8.out_zp))
    write_header(os.path.join(a.out, "fall_model_data.h"), tfl, mean, std, qp, thr, a.consec, ops)

    vit = vitals_selfcheck()
    fsm = fsm_demo()
    cv = None
    if a.cv and not a.synthetic:
        print(f"[cv] running {a.cv}-fold subject-wise cross-validation...")
        cv = cross_validate(trials, data, a.cv, a.epochs, a.batch)

    save_plots(a.out, hist, yte, p_f, thr, m_int8)

    res = dict(dataset="synthetic" if a.synthetic else a.dataset,
               n_trials=len(trials), test_subjects=sorted(map(str, te_s)), val_subjects=sorted(map(str, va_s)),
               window_samples=WIN, fs_hz=FS, threshold=thr, consec=a.consec,
               params=int(model.count_params()), tflite_int8_bytes=len(tfl), tflm_ops=ops,
               host_int8_latency_ms=host_ms, window_float=m_float, window_int8=m_int8,
               trial_float=t_float, trial_int8=t_int8, vitals_selfcheck=vit,
               fsm_demo={k: v for k, v in fsm.items()}, cv=cv)
    with open(os.path.join(a.out, "results.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)

    pct = lambda v: f"{100 * v:.2f}"
    md = [f"# Phase 1 Results -- {res['dataset']} (subject-independent test set)", "",
          f"Model: 1D-CNN, {res['params']} parameters, int8 TFLite = {len(tfl) / 1024:.1f} kB, "
          f"host int8 latency {host_ms:.2f} ms/window", "",
          "| Level | Model | Accuracy | Sensitivity | Specificity | Precision | F1 |",
          "|---|---|---|---|---|---|---|"]
    for lvl, mf, mq in (("Window", m_float, m_int8), ("Trial (deployment)", t_float, t_int8)):
        for name, m in (("float32", mf), ("int8", mq)):
            md.append(f"| {lvl} | {name} | {pct(m['accuracy'])} | {pct(m['sensitivity'])} | "
                      f"{pct(m['specificity'])} | {pct(m['precision'])} | {pct(m['f1'])} |")
    if cv:
        md += ["", f"## {a.cv}-fold subject-wise CV (float32, mean ± std)", ""]
        for k2, (mu, sd) in cv.items():
            md.append(f"- {k2}: {pct(mu)} ± {pct(sd)} %")
    md += ["", "## Vitals algorithm self-check (synthetic PPG, NOT clinical validation)", "",
           "| True HR | Est HR | True SpO2 | Est SpO2 |", "|---|---|---|---|"]
    md += [f"| {r['true_hr']} | {r['est_hr']} | {r['true_spo2']} | {r['est_spo2']} |" for r in vit]
    md += ["", "## Alert state machine scenarios", ""]
    for k2, v in fsm.items():
        md.append(f"- **{k2}**: " + "; ".join(f"t={t}s {e}" for t, e in v))
    with open(os.path.join(a.out, "results.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print("\n" + "\n".join(md))
    print(f"\n[done] outputs in {os.path.abspath(a.out)}")
    print("       fall_model_data.h  -> copy into ESP32 project (main/)")


if __name__ == "__main__":
    main()
