#!/usr/bin/env python3
"""
Paper experiments for the worker-safety fall detector on KFall (32 subjects).

Protocol
  5-fold subject-wise cross-validation (GroupKFold on subjects, the same folds as
  phase1_pipeline.cross_validate). Inside each fold 1/8 of the training subjects (at least 2)
  are held out for early stopping and to choose the decision threshold. Every recording of
  all 32 subjects is therefore tested exactly once, by the model of the fold that holds its
  subject out, with the device's decision rule: a window every 0.5 s, an alarm when
  CONSEC consecutive windows are positive.

Methods
  cnn        deployed 1D-CNN: 6 channels, 2.56 s window, rotation/gain/noise augmentation
  cnn_noaug  same network, no augmentation
  cnn_acc    same network, accelerometer only (3 channels)
  cnn_w64    1.28 s window          cnn_w192   3.84 s window
  rf         random forest on 38 hand-crafted window features, same windows and rule
  thr        impact + posture threshold rule (after Bourke et al. 2007), tuned per fold
Extra analyses
  consec     1, 2 or 3 consecutive windows (cnn probabilities, no retraining)
  rotation   falls detected and false alarms per hour when the sensor is mounted rotated
             by 0-45 degrees about a random axis (per recording), for cnn, cnn_noaug, rf, thr
Statistics
  subject-level cluster bootstrap 95 % CIs, exact McNemar tests on paired recording-level
  decisions (cnn against every other method), Poisson 95 % CI for false alarms per hour.

    python research/experiments.py --cache phase1_outputs [--data path/to/kfall]

Writes research/results/summary.json, research/results/*.png and research/RESULTS.md.
Per-fold predictions are cached in research/results/cache/ (KFall-derived: never commit).
"""
import argparse
import json
import os
import sys
import time

import numpy as np
from scipy.spatial.transform import Rotation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import phase1_pipeline as P  # noqa: E402

FS, HOP, CONSEC, K = P.FS, P.EVAL_STRIDE, 2, 5
ANGLES = [0, 10, 20, 30, 45]
ROT_METHODS = ("cnn", "cnn_noaug", "rf", "thr")
UPRIGHT = np.array([0.0, -1.0, 0.0])      # KFall frame: an upright wearer reads ~(0, -0.96, 0.05) g
ACC, ALL = [0, 1, 2], [0, 1, 2, 3, 4, 5]
CNN_CFG = {"cnn": dict(win=128, chans=ALL, aug=True), "cnn_noaug": dict(win=128, chans=ALL, aug=False),
           "cnn_acc": dict(win=128, chans=ACC, aug=True), "cnn_w64": dict(win=64, chans=ALL, aug=True),
           "cnn_w192": dict(win=192, chans=ALL, aug=True)}
METHODS = list(CNN_CFG) + ["rf", "thr"]
LABEL = {"cnn": "1D-CNN (deployed)", "cnn_noaug": "1D-CNN, no augmentation", "cnn_acc": "1D-CNN, accelerometer only",
         "cnn_w64": "1D-CNN, 1.28 s window", "cnn_w192": "1D-CNN, 3.84 s window",
         "rf": "Random forest, 38 features", "thr": "Impact + posture thresholds"}


# ---------------------------------------------------------------------------- data
def make_folds(trials):
    from sklearn.model_selection import GroupKFold
    subj = np.array([t["subject"] for t in trials])
    out = []
    for f, (tr, te) in enumerate(GroupKFold(K).split(trials, groups=subj)):
        tr_subj = sorted({subj[i] for i in tr})
        rng = np.random.default_rng(P.SEED + f)
        va = set(rng.choice(tr_subj, max(2, len(tr_subj) // 8), replace=False))
        out.append(([i for i in tr if subj[i] not in va], [i for i in tr if subj[i] in va], list(te)))
    return out


def pad(x, win):
    """Recordings shorter than one window plus (CONSEC-1) hops are padded at the start with
    their first sample (the device's buffer is pre-filled the same way at boot)."""
    need = win + HOP * (CONSEC - 1)
    if len(x) >= need:
        return x, 0
    k = need - len(x)
    return np.vstack([np.repeat(x[:1], k, 0), x]), k


def win_at(x, start, win):
    start = int(np.clip(start, 0, len(x) - win))
    return x[start:start + win]


def windows(trials, data, win, train):
    X, y = [], []
    for t, x in zip(trials, data):
        x, k = pad(x, win)
        if t["is_fall"]:
            imp = (t["impact"] + k) if t["impact"] is not None else P.impact_index(x)
            for o in (P.IMPACT_POS_TRAIN if train else [P.IMPACT_POS_EVAL]):
                X.append(win_at(x, imp - int(o * win), win)); y.append(1)
        else:
            for s in range(0, len(x) - win + 1, P.ADL_STRIDE_TRAIN if train else win // 2):
                X.append(x[s:s + win]); y.append(0)
    return np.stack(X).astype(np.float32), np.array(y, np.float32)


def stream(x, win):
    """Windows the device evaluates (one every HOP samples) and the time each decision is
    available (end of the window), in seconds from the start of the original recording."""
    x, k = pad(x, win)
    idx = np.arange(0, len(x) - win + 1, HOP)
    return np.stack([x[s:s + win] for s in idx]), (idx + win - k) / FS


def rand_rot(angle, seed):
    a = np.random.default_rng(seed).normal(size=3)
    return Rotation.from_rotvec(a / np.linalg.norm(a) * np.deg2rad(angle)).as_matrix()


def rotate(x, R):
    return np.hstack([x[:, :3] @ R.T, x[:, 3:] @ R.T]).astype(np.float32)


def tilt_deg(v):
    c = (v @ UPRIGHT) / np.maximum(np.linalg.norm(v, axis=-1), 1e-6)
    return np.degrees(np.arccos(np.clip(c, -1, 1)))


# ---------------------------------------------------------------------------- models
def build_cnn(win, nch):
    import tensorflow as tf
    from tensorflow.keras import layers
    inp = tf.keras.Input(shape=(win, nch))
    x = inp
    for f, k in ((16, 7), (32, 5)):
        x = layers.Conv1D(f, k, padding="same", use_bias=False)(x)
        x = layers.ReLU()(layers.BatchNormalization()(x))
        x = layers.MaxPooling1D(2)(x)
    x = layers.Conv1D(32, 3, padding="same", use_bias=False)(x)
    x = layers.ReLU()(layers.BatchNormalization()(x))
    x = layers.Dropout(0.3)(layers.GlobalAveragePooling1D()(x))
    x = layers.Dense(16, activation="relu")(x)
    m = tf.keras.Model(inp, layers.Dense(1, activation="sigmoid")(x))
    m.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss="binary_crossentropy")
    return m


def macs(model):
    import tensorflow as tf
    n = 0
    for layer in model.layers:
        if isinstance(layer, tf.keras.layers.Conv1D):
            n += layer.output.shape[1] * layer.kernel_size[0] * layer.input.shape[-1] * layer.filters
        elif isinstance(layer, tf.keras.layers.Dense):
            n += layer.input.shape[-1] * layer.units
    return int(n)


def fit_cnn(Xtr, ytr, Xva, yva, win, chans, aug, seed, epochs, batch=256):
    import tensorflow as tf
    tf.keras.utils.set_random_seed(seed)
    rng = np.random.default_rng(seed)
    mean = Xtr[..., chans].reshape(-1, len(chans)).mean(0)
    std = Xtr[..., chans].reshape(-1, len(chans)).std(0) + 1e-6
    cw = {0: len(ytr) / (2 * (len(ytr) - ytr.sum())), 1: len(ytr) / (2 * ytr.sum())}

    def gen():
        idx = rng.permutation(len(Xtr))
        for s in range(0, len(idx), batch):
            b = idx[s:s + batch]
            xb = P.augment(Xtr[b], rng) if aug else Xtr[b]
            yield (((xb[..., chans] - mean) / std).astype(np.float32), ytr[b][:, None],
                   np.where(ytr[b] == 1, cw[1], cw[0]).astype(np.float32))

    ds = tf.data.Dataset.from_generator(gen, output_signature=(
        tf.TensorSpec((None, win, len(chans)), tf.float32), tf.TensorSpec((None, 1), tf.float32),
        tf.TensorSpec((None,), tf.float32))).prefetch(2)
    model = build_cnn(win, len(chans))
    cbs = [tf.keras.callbacks.EarlyStopping(monitor="val_loss", mode="min", patience=8, restore_best_weights=True),
           tf.keras.callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=3, min_lr=1e-5)]
    hist = model.fit(ds, validation_data=((Xva[..., chans] - mean) / std, yva[:, None]), epochs=epochs,
                     callbacks=cbs, verbose=0)
    model.epochs_run = len(hist.epoch)

    def predict(W):
        return model.predict(((W[..., chans] - mean) / std).astype(np.float32), batch_size=4096, verbose=0).ravel()
    return model, predict


def features(W):
    acc, gyr = W[..., :3], W[..., 3:]
    am, gm = np.linalg.norm(acc, axis=2), np.linalg.norm(gyr, axis=2)
    q = W.shape[1] // 4
    parts = [W.mean(1), W.std(1), W.min(1), W.max(1), W.max(1) - W.min(1),
             np.c_[am.max(1), am.min(1), am.mean(1), am.std(1), gm.max(1), gm.mean(1),
                   tilt_deg(acc[:, :q].mean(1)), tilt_deg(acc[:, -q:].mean(1))]]
    return np.hstack(parts).astype(np.float32)


def fit_rf(Xtr, ytr, seed):
    from sklearn.ensemble import RandomForestClassifier
    rng = np.random.default_rng(seed)
    Xa = np.concatenate([Xtr, P.augment(Xtr, rng)])          # same augmentation as the CNN, one copy
    ya = np.concatenate([ytr, ytr])
    rf = RandomForestClassifier(n_estimators=300, min_samples_leaf=2, class_weight="balanced_subsample",
                                n_jobs=-1, random_state=seed).fit(features(Xa), ya)
    nodes = int(sum(e.tree_.node_count for e in rf.estimators_))
    return rf, (lambda W: rf.predict_proba(features(W))[:, 1]), nodes


def thr_triggers(x, uft, ang, delay=2.0, settle=1.0, refractory=5.0):
    """Impact (|a| > uft g) followed by a lying posture: mean acceleration over `settle` s,
    starting `delay` s after the impact, tilted more than `ang` degrees from upright.
    Short recordings use whatever follows the impact (at least 0.25 s)."""
    am = np.linalg.norm(x[:, :3], axis=1)
    above = am > uft
    starts = np.flatnonzero(above & ~np.r_[False, above[:-1]])
    out, last = [], -1e9
    for s in starts:
        e = s
        while e + 1 < len(am) and above[e + 1]:
            e += 1
        i = s + int(np.argmax(am[s:e + 1]))
        if i / FS < last + refractory:
            continue
        b = min(i + int((delay + settle) * FS), len(x))
        a = max(i + int(0.5 * FS), b - int(settle * FS))
        if b - a < int(0.25 * FS):
            continue
        if ang <= 0 or tilt_deg(x[a:b, :3].mean(0)) > ang:
            out.append(b / FS)
            last = i / FS
    return out


# ---------------------------------------------------------------------------- scoring
def triggers(prob, t_end, thr, consec=CONSEC):
    out, run = [], 0
    for p, t in zip(prob, t_end):
        run = run + 1 if p >= thr else 0
        if run == consec:
            out.append(float(t))
    return out


def outcomes(recs, trig):
    """Per-recording outcome arrays: detected (falls), latency, alarms (ADL)."""
    det, lat, alarms, early = [], [], [], 0
    for r, tr in zip(recs, trig):
        if r["is_fall"]:
            valid = [t for t in tr if t >= r["impact_s"] - 1.0]
            early += len(tr) - len(valid)
            det.append(bool(valid))
            lat.append(valid[0] - r["impact_s"] if valid else np.nan)
            alarms.append(0)
        else:
            det.append(False); lat.append(np.nan); alarms.append(len(tr))
    return np.array(det), np.array(lat), np.array(alarms), early


def poisson_ci(k, hours):
    from scipy.stats import chi2
    return (float(chi2.ppf(0.025, 2 * k) / 2 / hours) if k else 0.0, float(chi2.ppf(0.975, 2 * k + 2) / 2 / hours))


def score(recs, trig, boot=0, seed=0):
    det, lat, alarms, early = outcomes(recs, trig)
    fall = np.array([r["is_fall"] for r in recs])
    subj = np.array([r["subject"] for r in recs])
    dur = np.array([r["dur_s"] for r in recs])
    tp, fn = int(det[fall].sum()), int((~det[fall]).sum())
    fp_rec = int((alarms[~fall] > 0).sum()); tn = int((~fall).sum()) - fp_rec
    hours = dur[~fall].sum() / 3600
    k = int(alarms.sum())
    sens, spec = tp / max(tp + fn, 1), tn / max(tn + fp_rec, 1)
    prec = tp / max(tp + fp_rec, 1)
    L = lat[fall & det]
    res = dict(falls=int(fall.sum()), detected=tp, sensitivity=sens, adl_recordings=int((~fall).sum()),
               adl_with_alarm=fp_rec, specificity=spec, f1=2 * prec * sens / max(prec + sens, 1e-9),
               false_alarms=k, adl_hours=float(hours), fa_per_h=k / hours, fa_per_h_ci95=poisson_ci(k, hours),
               alarms_before_impact=early,
               latency_s=dict(median=float(np.median(L)), p95=float(np.percentile(L, 95)),
                              within_1s=float((L <= 1.0).mean())) if len(L) else None)
    if boot:
        us = sorted(set(subj))
        S = {s: (det[(subj == s) & fall].sum(), (fall & (subj == s)).sum(),
                 ((alarms > 0) & ~fall & (subj == s)).sum(), (~fall & (subj == s)).sum(),
                 alarms[~fall & (subj == s)].sum(), dur[~fall & (subj == s)].sum()) for s in us}
        rng = np.random.default_rng(seed)
        bs = []
        for _ in range(boot):
            a = np.array([S[s] for s in rng.choice(us, len(us))], float).sum(0)
            bs.append((a[0] / max(a[1], 1), 1 - a[2] / max(a[3], 1), a[4] / max(a[5] / 3600, 1e-9)))
        lo, hi = np.percentile(np.array(bs), [2.5, 97.5], axis=0)
        res["bootstrap_ci95"] = dict(sensitivity=[lo[0], hi[0]], specificity=[lo[1], hi[1]], fa_per_h=[lo[2], hi[2]])
    return res


def correct(recs, trig):
    det, _, alarms, _ = outcomes(recs, trig)
    fall = np.array([r["is_fall"] for r in recs])
    return np.where(fall, det, alarms == 0)


def mcnemar(a, b):
    from scipy.stats import binomtest
    n01, n10 = int((a & ~b).sum()), int((~a & b).sum())
    p = binomtest(min(n01, n10), n01 + n10, 0.5).pvalue if n01 + n10 else 1.0
    return dict(only_first_correct=n01, only_second_correct=n10, p=float(p))


# ---------------------------------------------------------------------------- running
def run_fold(method, fold, trials, data, idx, angles, epochs, cache):
    path = os.path.join(cache, f"{method}_f{fold}.npz")
    if os.path.exists(path):
        return dict(np.load(path, allow_pickle=True))
    fit_i, val_i, te_i = idx
    T = lambda I: [trials[i] for i in I]
    D = lambda I: [data[i] for i in I]
    t0 = time.time()
    info = {}
    if method == "thr":
        recs = T(fit_i + val_i)
        best = None
        for uft in (1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 2.8, 3.2):
            for ang in (0, 30, 45, 60):
                trig = [thr_triggers(x, uft, ang) for x in D(fit_i + val_i)]
                rr = [dict(r, impact_s=(r["impact"] / FS if r["impact"] is not None else None),
                           dur_s=len(x) / FS) for r, x in zip(recs, D(fit_i + val_i))]
                f1 = score(rr, trig)["f1"]
                if best is None or f1 > best[0]:
                    best = (f1, uft, ang)
        info = dict(uft=best[1], ang=best[2], train_f1=best[0])
        out = {}
        for ang_rot in angles:
            out[f"trig_{ang_rot}"] = np.array([thr_triggers(rotate(x, rand_rot(ang_rot, i)) if ang_rot else x,
                                                            best[1], best[2]) for i, x in zip(te_i, D(te_i))],
                                              dtype=object)
    else:
        win = CNN_CFG[method]["win"] if method in CNN_CFG else 128
        Xtr, ytr = windows(T(fit_i), D(fit_i), win, train=True)
        Xva, yva = windows(T(val_i), D(val_i), win, train=False)
        if method == "rf":
            _, predict, nodes = fit_rf(Xtr, ytr, P.SEED + fold)
            info = dict(nodes=nodes)
        else:
            c = CNN_CFG[method]
            model, predict = fit_cnn(Xtr, ytr, Xva, yva, win, c["chans"], c["aug"], P.SEED + fold, epochs)
            info = dict(params=int(model.count_params()), macs=macs(model), epochs_run=model.epochs_run)
            if fold == 0:
                info["tflite_int8_bytes"] = int8_size(model, Xtr[::20][..., c["chans"]])
        info["threshold"] = P.pick_threshold(yva, predict(Xva))
        out = {}
        for ang_rot in angles:
            Ws, te = [], []
            for i, x in zip(te_i, D(te_i)):
                W, t_end = stream(rotate(x, rand_rot(ang_rot, i)) if ang_rot else x, win)
                Ws.append(W); te.append(t_end)
            p = predict(np.concatenate(Ws))
            cut = np.cumsum([len(w) for w in Ws])[:-1]
            out[f"prob_{ang_rot}"] = np.array(np.split(p, cut), dtype=object)
            out[f"tend_{ang_rot}"] = np.array(te, dtype=object)
    info["seconds"] = time.time() - t0
    out["info"] = np.array(json.dumps(info))
    out["test_idx"] = np.array(te_i)
    np.savez(path, **out)
    print(f"  {method} fold {fold}: {info}", flush=True)
    return dict(np.load(path, allow_pickle=True))


def int8_size(model, Xrep):
    import tensorflow as tf
    mean, std = Xrep.reshape(-1, Xrep.shape[-1]).mean(0), Xrep.reshape(-1, Xrep.shape[-1]).std(0) + 1e-6
    Xn = ((Xrep - mean) / std).astype(np.float32)

    def rep():
        for i in range(min(300, len(Xn))):
            yield [Xn[i:i + 1]]
    try:
        c = tf.lite.TFLiteConverter.from_keras_model(model)
        c.optimizations = [tf.lite.Optimize.DEFAULT]
        c.representative_dataset = rep
        c.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
        c.inference_input_type = c.inference_output_type = tf.int8
        return len(c.convert())
    except Exception as e:  # noqa: BLE001
        print(f"  (int8 conversion failed: {e})")
        return None


def plot_robustness(rob, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ink, muted, grid = "#191919", "#666666", "#e3e3e3"
    colors = {"cnn": "#257de0", "cnn_noaug": "#f47230", "rf": "#00b894", "thr": "#f2a600"}
    marker = {"cnn": "o", "cnn_noaug": "s", "rf": "^", "thr": "D"}
    short = {"cnn": "1D-CNN", "cnn_noaug": "1D-CNN, no aug.", "rf": "Random forest", "thr": "Thresholds"}
    fig, ax = plt.subplots(1, 2, figsize=(7.2, 2.8))
    for a, key, title in ((ax[0], "sensitivity", "Falls detected (%)"), (ax[1], "fa_per_h", "False alarms per hour")):
        a.spines[["top", "right"]].set_visible(False)
        a.spines[["left", "bottom"]].set_color(grid)
        a.tick_params(colors=muted, labelsize=8)
        a.grid(axis="y", color=grid, linewidth=0.6)
        a.set_axisbelow(True)
        a.set_title(title, loc="left", color=ink, fontsize=9)
        a.set_xlabel("sensor mounting rotation (degrees)", color=muted, fontsize=8)
        a.set_xticks(ANGLES)
        for m in rob:
            v = [rob[m][str(g)][key] * (100 if key == "sensitivity" else 1) for g in ANGLES]
            a.plot(ANGLES, v, color=colors[m], linewidth=2, marker=marker[m], markersize=5, label=short[m])
    ax[1].set_yscale("symlog", linthresh=1)
    ax[1].legend(frameon=False, fontsize=7, labelcolor=muted, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def plot_latency(lat, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ink, muted, grid = "#191919", "#666666", "#e3e3e3"
    colors = {"cnn": "#257de0", "rf": "#00b894", "thr": "#f2a600"}
    short = {"cnn": "1D-CNN", "rf": "Random forest", "thr": "Thresholds"}
    fig, a = plt.subplots(figsize=(3.5, 2.6))
    a.spines[["top", "right"]].set_visible(False)
    a.spines[["left", "bottom"]].set_color(grid)
    a.tick_params(colors=muted, labelsize=8)
    a.grid(color=grid, linewidth=0.6)
    a.set_axisbelow(True)
    for m in ("cnn", "rf", "thr"):
        v = np.sort(lat[m])
        a.plot(v, np.arange(1, len(v) + 1) / len(v) * 100, color=colors[m], linewidth=2, label=short[m],
               drawstyle="steps-post")
    a.set_xlim(-0.5, 4)
    a.set_xlabel("time from impact to alarm (s)", color=muted, fontsize=8)
    a.set_title("Detected falls alarmed by time t (%)", loc="left", color=ink, fontsize=9)
    a.legend(frameon=False, fontsize=7, labelcolor=muted, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=300)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="phase1_outputs", help="folder with kfall_cache.npz")
    ap.add_argument("--data", help="KFall folder, only needed if the cache does not exist yet")
    ap.add_argument("--out", default=os.path.join(ROOT, "research", "results"))
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--boot", type=int, default=2000)
    a = ap.parse_args()
    cache = os.path.join(a.out, "cache")
    os.makedirs(cache, exist_ok=True)
    trials, data = P.load_dataset(a.data, a.cache, "kfall")
    recs = [dict(subject=t["subject"], task=t["activity"], is_fall=bool(t["is_fall"]),
                 impact_s=(t["impact"] / FS if t["impact"] is not None else None), dur_s=len(x) / FS)
            for t, x in zip(trials, data)]
    folds = make_folds(trials)
    methods = a.methods.split(",")
    print(f"[data] {len(trials)} recordings, {len(set(r['subject'] for r in recs))} subjects; "
          f"{sum(r['is_fall'] for r in recs)} falls; ADL {sum(r['dur_s'] for r in recs if not r['is_fall']) / 3600:.2f} h")

    trig, info, rot = {}, {}, {}
    for m in methods:
        angles = ANGLES if m in ROT_METHODS else [0]
        res = [run_fold(m, f, trials, data, folds[f], angles, a.epochs, cache) for f in range(K)]
        info[m] = [json.loads(str(r["info"])) for r in res]
        for g in angles:
            tr = [None] * len(trials)
            for f, r in enumerate(res):
                for j, i in enumerate(r["test_idx"]):
                    if m == "thr":
                        tr[i] = list(r[f"trig_{g}"][j])
                    else:
                        tr[i] = triggers(r[f"prob_{g}"][j], r[f"tend_{g}"][j], info[m][f]["threshold"])
                        if g == 0 and m == "cnn":
                            for c in (1, 3):
                                trig.setdefault(f"cnn_consec{c}", [None] * len(trials))[i] = triggers(
                                    r["prob_0"][j], r["tend_0"][j], info[m][f]["threshold"], c)
            if g == 0:
                trig[m] = tr
            rot.setdefault(m, {})[str(g)] = score(recs, tr)

    summary = dict(protocol=dict(folds=K, consec=CONSEC, hop_s=HOP / FS, recordings=len(trials),
                                 subjects=len(set(r["subject"] for r in recs))), methods={})
    for m in trig:
        s = score(recs, trig[m], boot=a.boot)
        base = m.split("_consec")[0]
        fold_scores = []
        for f in range(K):
            te = folds[f][2]
            fold_scores.append(score([recs[i] for i in te], [trig[m][i] for i in te]))
        s["per_fold"] = {k: [float(np.mean([fs[k] for fs in fold_scores])), float(np.std([fs[k] for fs in fold_scores]))]
                         for k in ("sensitivity", "specificity", "f1", "fa_per_h")}
        s["fold_info"] = info.get(base)
        summary["methods"][m] = s
    if "cnn" in trig:
        cc = correct(recs, trig["cnn"])
        summary["mcnemar_vs_cnn"] = {m: mcnemar(cc, correct(recs, trig[m])) for m in trig if m != "cnn"}
    summary["rotation"] = {m: rot[m] for m in rot if m in ROT_METHODS}
    # latency distributions of detected falls (for the figure)
    lat = {m: outcomes(recs, trig[m])[1] for m in ("cnn", "rf", "thr") if m in trig}
    lat = {m: v[~np.isnan(v)] for m, v in lat.items()}
    json.dump(summary, open(os.path.join(a.out, "summary.json"), "w"), indent=1, default=float)
    if len(summary["rotation"]) == len(ROT_METHODS):
        plot_robustness(summary["rotation"], os.path.join(a.out, "fig_robustness.png"))
    if len(lat) == 3:
        plot_latency(lat, os.path.join(a.out, "fig_latency.png"))
    write_md(summary, os.path.join(ROOT, "research", "RESULTS.md"))
    print(open(os.path.join(ROOT, "research", "RESULTS.md")).read())


def write_md(S, path):
    M = S["methods"]
    pct = lambda v: f"{100 * v:.2f}"
    ci = lambda m, k, f=100: (f" [{f * M[m]['bootstrap_ci95'][k][0]:.1f}, {f * M[m]['bootstrap_ci95'][k][1]:.1f}]"
                             if "bootstrap_ci95" in M[m] else "")
    L = ["# Research results: 5-fold subject-wise cross-validation on KFall", "",
         f"All {S['protocol']['recordings']} recordings of the {S['protocol']['subjects']} KFall subjects, each tested "
         f"once by the fold model that never saw its subject. Device decision rule: a window every "
         f"{S['protocol']['hop_s']:.1f} s, alarm after {S['protocol']['consec']} consecutive positive windows. "
         "Brackets: 95 % subject-level bootstrap confidence intervals. Generated by `research/experiments.py`.", "",
         "## Methods", "",
         "| Method | Falls detected | Sensitivity % | Specificity % (recordings) | F1 % | False alarms / h | "
         "Median latency | p95 latency | Model size |", "|---|---|---|---|---|---|---|---|---|"]
    for m in [k for k in METHODS if k in M]:
        s = M[m]
        fi = (s.get("fold_info") or [{}])[0]
        size = (f"{fi['params']:,} params, {fi.get('macs', 0) / 1e3:.0f}k MACs"
                + (f", int8 {fi['tflite_int8_bytes'] / 1024:.1f} kB" if fi.get("tflite_int8_bytes") else "")
                if "params" in fi else f"{fi['nodes']:,} tree nodes" if "nodes" in fi else
                f"2 thresholds (|a| > {fi.get('uft')} g, tilt > {fi.get('ang')} deg in fold 1)")
        lat = s["latency_s"] or {}
        L.append(f"| {LABEL[m]} | {s['detected']}/{s['falls']} | {pct(s['sensitivity'])}{ci(m, 'sensitivity')} | "
                 f"{pct(s['specificity'])}{ci(m, 'specificity')} | {pct(s['f1'])} | "
                 f"{s['fa_per_h']:.2f}{ci(m, 'fa_per_h', 1)} | {lat.get('median', float('nan')):+.2f} s | "
                 f"{lat.get('p95', float('nan')):+.2f} s | {size} |")
    if "mcnemar_vs_cnn" in S:
        L += ["", "## Paired comparison with the deployed CNN (exact McNemar test, recording level)", "",
              "| Method | Only CNN correct | Only this method correct | p |", "|---|---|---|---|"]
        for m, r in S["mcnemar_vs_cnn"].items():
            L.append(f"| {LABEL.get(m, m)} | {r['only_first_correct']} | {r['only_second_correct']} | {r['p']:.2g} |")
    cons = [k for k in ("cnn_consec1", "cnn", "cnn_consec3") if k in M]
    if cons:
        L += ["", "## Consecutive positive windows before an alarm (deployed CNN, no retraining)", "",
              "| Windows | Sensitivity % | False alarms / h | Median latency |", "|---|---|---|---|"]
        for k in cons:
            n = {"cnn_consec1": 1, "cnn": 2, "cnn_consec3": 3}[k]
            L.append(f"| {n} | {pct(M[k]['sensitivity'])} | {M[k]['fa_per_h']:.2f} | {M[k]['latency_s']['median']:+.2f} s |")
    if S.get("rotation"):
        L += ["", "## Sensor mounted rotated (random axis per recording)", "",
              "| Method | " + " | ".join(f"{g} deg" for g in ANGLES) + " |", "|---|" + "---|" * len(ANGLES)]
        for m, r in S["rotation"].items():
            L.append(f"| {LABEL[m]} | " + " | ".join(
                f"{pct(r[str(g)]['sensitivity'])} % / {r[str(g)]['fa_per_h']:.1f} per h" for g in ANGLES) + " |")
        L += ["", "Each cell: falls detected / false alarms per hour.", "",
              "![robustness](results/fig_robustness.png)", "", "![latency](results/fig_latency.png)"]
    L += ["", "## Per-fold mean ± std", "", "| Method | Sensitivity % | Specificity % | False alarms / h |",
          "|---|---|---|---|"]
    for m in [k for k in METHODS if k in M]:
        pf = M[m]["per_fold"]
        L.append(f"| {LABEL[m]} | {pct(pf['sensitivity'][0])} ± {pct(pf['sensitivity'][1])} | "
                 f"{pct(pf['specificity'][0])} ± {pct(pf['specificity'][1])} | {pf['fa_per_h'][0]:.2f} ± {pf['fa_per_h'][1]:.2f} |")
    open(path, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
