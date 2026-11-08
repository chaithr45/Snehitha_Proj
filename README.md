# Edge-AI Worker Safety Monitor — Phase 1

A waist-worn wearable (ESP32 + MPU6050 IMU + MAX30102 pulse oximeter) that detects
falls **on the device** with a tiny int8 1D-CNN, watches heart rate and SpO2, and alerts
a supervisor over LoRa / Wi-Fi.

This repository holds **Phase 1 (software and model)**: the training pipeline and the
model files the firmware will use. **Phase 2 (ESP32 firmware) has not started yet.**

## Contents

| File | What it is |
|---|---|
| `phase1_pipeline.py` | End-to-end pipeline: load KFall → filter/resample to 50 Hz → window → train → pick threshold → int8 quantise → evaluate → export firmware header |
| `fall_model_data.h` | Copy into the ESP32 project: int8 model bytes, normalisation, low-pass filter coefficients, quantisation params, threshold, TFLM op list |
| `fall_model_int8.tflite` | int8 model for TensorFlow Lite Micro (18.6 kB) |
| `fall_cnn.keras` | float32 Keras model |
| `results.md`, `results.json` | Metrics on unseen test subjects + 5-fold subject-wise cross-validation |
| `fig_training_curves.png`, `fig_roc.png`, `fig_confusion_int8.png` | Figures for the paper |
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

int8 model (the one that runs on the ESP32), threshold 0.05, alarm after 2 consecutive
positive windows:

| Level | Accuracy | Sensitivity | Specificity | F1 |
|---|---|---|---|---|
| Per window | 99.86 % | 100.00 % | 99.84 % | 99.55 % |
| Per recording (deployment) | 99.79 % | 100.00 % | 99.61 % | 99.77 % |

All 441 test falls detected; 2 false alarms out of 516 normal-activity recordings.
5-fold subject-wise CV: per-recording sensitivity 100.00 ± 0.00 %, specificity
99.76 ± 0.34 %. Full tables in [`results.md`](results.md).

The heart-rate / SpO2 algorithms and the alert state machine are checked only on
synthetic signals so far — **not clinically validated**.

## Still to do

**Phase 2 — firmware**
- MPU6050 at 50 Hz (±16 g, ±2000 °/s), causal low-pass filter from the header,
  128×6 ring buffer, int8 inference every 0.5 s, alarm on 2 consecutive positives
- Mount the sensor with the same axis orientation as KFall (gravity mainly on −Y),
  or remap axes in firmware — training only covers ±10° of tilt
- MAX30102 driver and C port of `estimate_hr` / `estimate_spo2`
- C port of `SafetyFSM` with cancel button, buzzer and vibration motor
- LoRa / Wi-Fi alert link and a supervisor-side receiver
- Python-vs-ESP32 parity test; on-device latency, RAM and battery life

**Validation**
- Calibrate SpO2 against a reference oximeter
- Real-world tests with the device; report false alarms per hour of wear
- Make threshold selection more robust: the validation subjects are too easy (every
  threshold from 0.05 to 0.95 scores perfectly), so the pipeline falls back to the
  lowest one
