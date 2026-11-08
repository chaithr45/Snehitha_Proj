# Edge-AI Worker Safety Monitor

A waist-worn wearable (ESP32 + MPU6050 IMU + MAX30102 pulse oximeter) that detects
falls **on the device** with a tiny int8 1D-CNN, watches heart rate and SpO2, and alerts
a supervisor over LoRa / Wi-Fi.

- **Phase 1 (model):** the training pipeline and the model files, in this folder.
- **Phase 2 (firmware):** the ESP32 / ESP32-S3 application in [`firmware/`](firmware/README.md).
  It is checked against the Python pipeline and in the ESP32 emulator (see the
  *firmware* workflow under Actions), but **has not yet run on real hardware**.

## Contents

| File | What it is |
|---|---|
| `phase1_pipeline.py` | End-to-end pipeline: load KFall → filter/resample to 50 Hz → window → train → pick threshold → int8 quantise → evaluate → export firmware header |
| `fall_model_data.h` | Copy into the ESP32 project: int8 model bytes, normalisation, low-pass filter coefficients, quantisation params, threshold, TFLM op list |
| `fall_model_int8.tflite` | int8 model for TensorFlow Lite Micro (18.6 kB) |
| `fall_cnn.keras` | float32 Keras model |
| `results.md`, `results.json` | Metrics on unseen test subjects + 5-fold subject-wise cross-validation |
| `evaluate_deployment.py`, `deployment_eval.*` | Device-level evaluation: false alarms per hour, time to alarm, threshold sweep |
| `fig_training_curves.png`, `fig_roc.png`, `fig_confusion_int8.png` | Figures for the paper |
| `firmware/` | Phase 2: ESP32 firmware, C port of the pipeline, tests, supervisor station ([README](firmware/README.md)) |
| `Snehitha_project.zip` | Original upload of Phase 1 (an earlier run, kept for reference) |

## Dataset

[KFall](https://doi.org/10.3389/fnagi.2021.692865): 32 subjects, 21 activity types +
15 fall types, low-back IMU at 100 Hz, with expert-labelled fall onset/impact frames.

KFall is **not** openly licensed: access is granted on request, and the dataset may not
be passed on to third parties. Request your own copy from the
[KFall project page](https://sites.google.com/view/kfalldataset), then unzip it into a
folder (e.g. `kfall/`) that contains `sensor_data_new/` and `label_data_new/`.

## Setup

```bash
pip install -r requirements.txt
```

Tested with Python 3.13 and TensorFlow 2.21.

## Usage

```bash
python phase1_pipeline.py --data kfall --cv 5
```

- Outputs go to `phase1_outputs/` (change with `--out`). The first run caches the
  parsed dataset as `kfall_cache.npz` there, so later runs start in seconds.
- `--synthetic` runs a ~15 s smoke test without any dataset.
- `--dataset sisfall --data path/to/SisFall_dataset` uses SisFall instead (not yet run).
- A full run with `--cv 5` takes about 20 minutes on a 4-core CPU.

## Results (KFall, 6 test subjects never seen in training)

int8 model (the one that runs on the ESP32), threshold 0.50, alarm after 2 consecutive
positive windows:

| Level | Accuracy | Sensitivity | Specificity | F1 |
|---|---|---|---|---|
| Per window | 99.93 % | 100.00 % | 99.92 % | 99.77 % |
| Per recording (deployment) | 99.90 % | 100.00 % | 99.81 % | 99.89 % |

All 441 test falls detected; 1 false alarm out of 516 normal-activity recordings.
5-fold subject-wise CV: per-recording sensitivity 100.00 ± 0.00 %, specificity
99.86 ± 0.28 %. Full tables in [`results.md`](results.md).

### As the device runs it ([`deployment_eval.md`](deployment_eval.md))

`evaluate_deployment.py` replays every validation and test recording through the
firmware's exact decision rule:

| | Test subjects | Validation subjects |
|---|---|---|
| Falls detected | 441 / 441 | 297 / 297 |
| Time from impact to alarm (median / 95th percentile) | 0.81 s / 1.08 s | 0.83 s / 1.08 s |
| False alarms during normal activity | 1 in 1.16 h (0.9 / h, 95 % CI 0.0–4.8) | 0 in 0.82 h |

KFall's activity clips are dense with hard movements, so the false-alarm rate is a
pessimistic bound for a normal shift. Every threshold from 0.05 to 0.95 detects all
falls; the threshold now sits in the middle of the range the validation subjects
support (it used to take the lowest, 0.05, which doubled the false alarms).

![threshold sweep](deployment_eval.png)

The heart-rate / SpO2 algorithms and the alert rules are checked only on synthetic
signals so far: **not clinically validated**.

## Phase 2 firmware in short

See [`firmware/README.md`](firmware/README.md). Beyond running the model, the device:

- raises a **man-down** alarm when the worker lies motionless for 30 s (or stands
  motionless for 3 min), which catches a collapse with no impact;
- pauses **vitals alerts while the worker moves vigorously**, when the PPG is unreliable;
- reports a **frozen or missing motion sensor** to the supervisor;
- keeps alerts in a **store-and-forward** queue until the supervisor confirms them;
- shows the supervisor's **acknowledgment** to the worker ("help is on the way");
- sends posture, impact force, heart rate and SpO2 with every alert.

Without a board, everything is checked automatically on every push: the C code against
the Python pipeline (identical decisions on all 957 KFall test recordings), real
ESP-IDF builds for ESP32 and ESP32-S3, and a 250 s scenario run in the ESP32 emulator.

## Still to do

**On real hardware** (the software is written and tested without a board)
- Wire the board, flash it, check sensor orientation (gravity mainly on −Y, as in KFall)
- Measure on the device: inference time, battery life
- Tune the man-down and vitals-motion settings on real workers
- LoRa alert link (Wi-Fi works now)

**Validation**
- Calibrate SpO2 against a reference oximeter
- Real-world tests with the device; report false alarms per hour of wear
- Validation subjects that are harder for the model, so the threshold choice is data-driven
  rather than "middle of a perfect plateau"
