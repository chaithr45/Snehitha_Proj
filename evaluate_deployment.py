#!/usr/bin/env python3
"""
Deployment-level evaluation of the int8 fall model, exactly as the firmware runs it
(fall_model_data.h constants, int8 model, window every 0.5 s, alarm after
FALL_CONSEC consecutive positive windows).

Answers the questions window/recording accuracy does not:
  * false alarms per hour of normal activity
  * how long after the impact the alarm is raised
  * which activities cause the false alarms
  * how sensitive the results are to the decision threshold, and which threshold the
    validation subjects actually support

    python evaluate_deployment.py --data path/to/kfall          (writes deployment_eval.*)

Uses the validation and test subjects listed in results.json (never the training subjects).
"""
import argparse
import glob
import json
import os
import re

import numpy as np

import phase1_pipeline as P

THRESHOLDS = np.round(np.arange(0.05, 0.951, 0.05), 2)


def load_header(path):
    h = open(path).read()
    arr = lambda n: np.array([float(v.strip().rstrip("f")) for v in
                              re.search(n + r"\[\d+\]\s*=\s*\{([^}]*)\}", h).group(1).split(",")], np.float32)
    num = lambda n: float(re.search(r"#define " + n + r"\s+(-?[0-9.]+)f?", h).group(1))
    return dict(mean=arr("FALL_MEAN"), std=arr("FALL_STD"), thr=num("FALL_THRESHOLD"),
                hop=int(num("FALL_HOP")), consec=int(num("FALL_CONSEC")))


def load_recordings(root, subjects):
    """Raw KFall recordings -> 50 Hz filtered signal + impact sample (falls)."""
    import pandas as pd
    labels = P.read_kfall_labels(os.path.join(root, "label_data_new"))
    recs = []
    for f in sorted(glob.glob(os.path.join(root, "sensor_data_new", "**", "*.csv"), recursive=True)):
        m = P.KFALL_RE.search(os.path.basename(f))
        if not m:
            continue
        subj, task, rep = f"SA{int(m.group(1)):02d}", int(m.group(2)), int(m.group(3))
        if subj not in subjects:
            continue
        df = pd.read_csv(f)
        raw = df[["AccX", "AccY", "AccZ", "GyrX", "GyrY", "GyrZ"]].to_numpy(np.float64)
        if len(raw) < P.WIN * 2 + 10 or not np.isfinite(raw).all():
            continue
        is_fall = task in P.KFALL_FALL_TASKS
        impact = None
        if is_fall:
            lb = labels.get((subj, task, rep))
            if lb is None:
                continue                                # same rule as training
            hit = np.where(df["FrameCounter"].to_numpy() == lb[1])[0]
            impact = (int(hit[0]) if len(hit) else lb[1] - 1) * P.FS / P.FS_KFALL
        x = P.filter50(P.resample_poly(raw, P.FS, P.FS_KFALL, axis=0))
        recs.append(dict(subject=subj, task=task, is_fall=is_fall, impact_s=None if impact is None
                         else impact / P.FS, dur_s=len(x) / P.FS, x=x))
    return recs


def add_probs(recs, hp, q8):
    for r in recs:
        x = r.pop("x")
        W = np.stack([x[s:s + P.WIN] for s in range(0, len(x) - P.WIN + 1, hp["hop"])])
        r["prob"] = q8((W - hp["mean"]) / hp["std"])
        # decision time of window k = end of that window
        r["t_end"] = (np.arange(len(W)) * hp["hop"] + P.WIN) / P.FS


def triggers(prob, t_end, thr, consec):
    """Times at which the device raises FALL_SUSPECTED (run of positives reaches consec)."""
    out, run = [], 0
    for p, t in zip(prob, t_end):
        run = run + 1 if p >= np.float32(thr) else 0
        if run == consec:
            out.append(float(t))
    return out


def evaluate(recs, thr, consec):
    falls = [r for r in recs if r["is_fall"]]
    adl = [r for r in recs if not r["is_fall"]]
    lat, missed, early = [], 0, 0
    for r in falls:
        tr = triggers(r["prob"], r["t_end"], thr, consec)
        if not tr:
            missed += 1
            continue
        # first alarm from 1 s before the impact onwards (anything earlier is a false alarm
        # during the pre-fall activity, not a detection of this fall)
        valid = [t for t in tr if t >= r["impact_s"] - 1.0]
        early += len(tr) - len(valid)
        if valid:
            lat.append(valid[0] - r["impact_s"])
        else:
            missed += 1
    adl_tr = [len(triggers(r["prob"], r["t_end"], thr, consec)) for r in adl]
    hours = sum(r["dur_s"] for r in adl) / 3600
    per_task = {}
    for r, n in zip(adl, adl_tr):
        if n:
            per_task[f"T{r['task']:02d}"] = per_task.get(f"T{r['task']:02d}", 0) + n
    lat = np.array(lat)
    from scipy.stats import chi2
    k = int(sum(adl_tr))
    ci = (chi2.ppf(0.025, 2 * k) / 2 / hours if k else 0.0, chi2.ppf(0.975, 2 * k + 2) / 2 / hours)
    return dict(
        threshold=float(thr), falls=len(falls), detected=len(falls) - missed,
        sensitivity=(len(falls) - missed) / max(len(falls), 1),
        adl_recordings=len(adl), adl_recordings_with_alarm=int(sum(n > 0 for n in adl_tr)),
        adl_hours=hours, false_alarms=int(sum(adl_tr)), false_alarms_per_hour=sum(adl_tr) / hours,
        false_alarms_per_hour_ci95=[float(ci[0]), float(ci[1])],
        alarms_before_fall=early,
        latency_s=dict(median=float(np.median(lat)), p5=float(np.percentile(lat, 5)),
                       p95=float(np.percentile(lat, 95)), max=float(lat.max()),
                       within_1s=float((lat <= 1.0).mean())) if len(lat) else None,
        false_alarms_by_task=dict(sorted(per_task.items(), key=lambda kv: -kv[1])),
    )


def plateau(recs_val, data, hp, q8):
    """The pipeline's own rule (max window-level F1 on validation windows): which
    thresholds tie for the maximum, and the middle of that range."""
    trials = [dict(is_fall=r["is_fall"], impact=None if r["impact_s"] is None else int(r["impact_s"] * P.FS),
                   subject=r["subject"]) for r in recs_val]
    X, y, _ = P.make_windows(trials, data, train=False)
    p = q8((X - hp["mean"]) / hp["std"])
    f1 = np.array([P.metrics(y, p, t)["f1"] for t in THRESHOLDS])
    best = THRESHOLDS[np.isclose(f1, f1.max())]
    return dict(max_f1=float(f1.max()), tied_from=float(best.min()), tied_to=float(best.max()),
                midpoint=float(np.round((best.min() + best.max()) / 2, 2)))


def plot(sweep_val, sweep_test, deployed, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ink, muted, grid = "#1f2328", "#59636e", "#d8dee4"
    blue, orange = "#2a78d6", "#eb6834"   # validated categorical slots 1-2
    fig, ax = plt.subplots(1, 2, figsize=(9.6, 3.4))
    for a in ax:
        a.spines[["top", "right"]].set_visible(False)
        a.spines[["left", "bottom"]].set_color(grid)
        a.tick_params(colors=muted, labelsize=9)
        a.grid(axis="y", color=grid, linewidth=0.6)
        a.set_axisbelow(True)
        a.set_xlabel("decision threshold", color=muted, fontsize=9)
        a.axvline(deployed, color=muted, linestyle=":", linewidth=1)
    for sw, c, name in ((sweep_val, orange, "validation subjects"), (sweep_test, blue, "test subjects")):
        th = [s["threshold"] for s in sw]
        # sampled every 0.05: draw steps, not slopes between samples
        ax[0].plot(th, [s["latency_median_s"] for s in sw], color=c, linewidth=2, label=name,
                   drawstyle="steps-post")
        ax[1].plot(th, [s["false_alarms_per_hour"] for s in sw], color=c, linewidth=2, label=name,
                   drawstyle="steps-post")
    ax[0].set_title("Median time from impact to alarm (s)", loc="left", color=ink, fontsize=10)
    ax[1].set_title("False alarms per hour of activity", loc="left", color=ink, fontsize=10)
    ax[0].set_ylim(bottom=0)
    ax[1].set_ylim(bottom=-0.05)
    ax[1].legend(frameon=False, fontsize=8, labelcolor=muted, loc="upper right")
    for a in ax:
        a.annotate("deployed", (deployed, 0.3), xycoords=("data", "axes fraction"), xytext=(4, 0),
                   textcoords="offset points", color=muted, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="KFall folder (sensor_data_new/, label_data_new/)")
    ap.add_argument("--out", default="deployment_eval")
    a = ap.parse_args()

    hp = load_header("fall_model_data.h")
    q8 = P.Int8Model(open("fall_model_int8.tflite", "rb").read())
    split = json.load(open("results.json"))
    recs = {}
    for name in ("val", "test"):
        subj = set(split[f"{name}_subjects"])
        recs[name] = load_recordings(a.data, subj)
        if name == "val":
            val_signals = [r["x"] for r in recs[name]]
        add_probs(recs[name], hp, q8)
        print(f"[{name}] {len(recs[name])} recordings from {len(subj)} subjects")

    plat = plateau(recs["val"], val_signals, hp, q8)
    sweep = {n: [evaluate(recs[n], t, hp["consec"]) for t in THRESHOLDS] for n in recs}
    dep = {n: evaluate(recs[n], hp["thr"], hp["consec"]) for n in recs}
    mid = {n: evaluate(recs[n], plat["midpoint"], hp["consec"]) for n in recs}

    out = dict(deployed_threshold=hp["thr"], consec=hp["consec"], validation_plateau=plat,
               deployed=dep, at_plateau_midpoint=mid,
               sweep={n: [dict({k: s[k] for k in ("threshold", "sensitivity", "false_alarms_per_hour",
                                                   "adl_recordings_with_alarm")},
                                latency_median_s=s["latency_s"]["median"]) for s in sweep[n]] for n in sweep})
    json.dump(out, open(a.out + ".json", "w"), indent=2)
    plot(out["sweep"]["val"], out["sweep"]["test"], hp["thr"], a.out + ".png")

    def row(label, e):
        L = e["latency_s"]
        lo, hi = e["false_alarms_per_hour_ci95"]
        return (f"| {label} | {e['detected']}/{e['falls']} | {e['false_alarms']} in {e['adl_hours']:.2f} h: "
                f"{e['false_alarms_per_hour']:.1f}/h (95% CI {lo:.1f}-{hi:.1f}) | "
                f"{e['adl_recordings_with_alarm']}/{e['adl_recordings']} | "
                f"{L['median']:+.2f} s | {L['p95']:+.2f} s | {100 * L['within_1s']:.0f} % |")

    md = ["# Deployment evaluation (int8 model, firmware decision rule)", "",
          f"Every recording of the validation and test subjects is replayed exactly as the device "
          f"runs it: a 2.56 s window every 0.5 s, alarm after {hp['consec']} consecutive windows "
          f"with probability >= threshold. Latency = alarm time minus the labelled impact.", "",
          "| Subjects, threshold | Falls detected | False alarms (normal activity) | ADL recordings with an alarm | "
          "Median latency | 95th pct latency | Alarm within 1 s |", "|---|---|---|---|---|---|---|"]
    for n, lab in (("test", "Test"), ("val", "Validation")):
        md.append(row(f"{lab}, {hp['thr']:.2f} (deployed)", dep[n]))
        md.append(row(f"{lab}, {plat['midpoint']:.2f} (plateau middle)", mid[n]))
    md += ["", "KFall activity clips are short and dense with difficult movements (jumps, stumbles, "
           "sitting down hard), so false alarms per hour here are a pessimistic upper bound for a "
           "normal shift, and with so few events the interval is wide. On the device each false alarm first opens the 10 s cancel window; it "
           "only reaches the supervisor if the worker neither presses \"I'm OK\" nor moves.", "",
           "## Threshold choice", "",
           f"With the pipeline's rule (maximum window-level F1 on the validation subjects), every "
           f"threshold from {plat['tied_from']:.2f} to {plat['tied_to']:.2f} ties at F1 = "
           f"{plat['max_f1']:.4f}. The pipeline takes the lowest ({hp['thr']:.2f}); the middle of the "
           f"tied range is {plat['midpoint']:.2f}, the choice furthest from both ends.", "",
           f"Falls detected at every threshold from {THRESHOLDS[0]:.2f} to {THRESHOLDS[-1]:.2f}: "
           f"validation {min(s['sensitivity'] for s in sweep['val']) * 100:.0f} %, "
           f"test {min(s['sensitivity'] for s in sweep['test']) * 100:.0f} % (worst case over the sweep). "
           "Raising the threshold mainly removes false alarms and adds a little latency. Picking "
           "the threshold from the test-subject curve would over-fit; only the validation "
           "subjects may choose it.", "", "![threshold sweep](deployment_eval.png)", "",
           "## False alarms by activity (test subjects, deployed threshold)", ""]
    fa = dep["test"]["false_alarms_by_task"]
    md += ["| KFall task | False alarms |", "|---|---|"] + [f"| {k} | {v} |" for k, v in fa.items()] \
        if fa else ["None."]
    md += ["", "Task numbers are KFall's file naming (S..T<task>R..); see the KFall paper for the activity names."]
    open(a.out + ".md", "w").write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
