# Phase 2 firmware: ESP32 / ESP32-S3

ESP-IDF application that runs the Phase 1 model on the wearable and decides, on the
device, when a worker needs help.

```
MPU6050 ─ 50 Hz ─▶ 5 Hz low-pass ─▶ 2.56 s window ─▶ int8 1D-CNN (TFLite Micro) ─┐ 2 windows in a row
           │                         every 0.5 s                                 │ ≥ threshold
           └─▶ motion level, posture (tilt), frozen-sensor check ────────────────┤
MAX30102 ─ 100 Hz ─▶ heart rate + SpO2 every 1 s, skin contact ──────────────────┤
                                                                                 ▼
                                      alert state machine
  fall suspected ─▶ beep + vibrate, 10 s to press "I'm OK" ─▶ 5 s motionless / no press ─▶ ALERT_FALL
  motionless 30 s lying / 3 min upright ─▶ 15 s pre-alarm (move or press) ─▶ ALERT_MAN_DOWN
  HR < 45 / > 140 or SpO2 < 90 for 10 s (paused while moving vigorously) ─▶ ALERT_VITALS
  motion sensor frozen / missing ─▶ DEVICE_FAULT
                                                                                 ▼
          store-and-forward queue ─▶ Wi-Fi, JSON ─▶ supervisor station ─▶ acknowledgment
                                                    "help is on the way" buzzer ◀─┘
```

All model constants (normalisation, filter, quantisation, threshold) come straight from
`../fall_model_data.h`, so re-running `phase1_pipeline.py` and rebuilding is all it takes
to deploy a new model.

## What it does

| Feature | Why |
|---|---|
| **Fall detection** with the int8 model, 2 consecutive windows, then 10 s to cancel | The Phase 1 rule, ported 1:1 and checked against Python |
| **Man-down**: motionless 30 s while lying (body tilted > 60°) or 3 min upright | Catches a collapse *without* an impact (fainting, gas, heat stroke) that the fall model cannot see. Posture makes it fast when it matters and quiet for a worker standing still |
| **Motion-aware vitals**: HR/SpO2 ignored while the mean motion over 10 s is above 0.35 g | Running corrupts the PPG: in the replay test, jogging reads a false 165 bpm, which without this would raise a vitals alert |
| **Skin-contact check** | A loose PPG sensor never raises a vitals alert and does not arm man-down |
| **Sensor-fault alert** when the motion sensor stops or freezes | Fall detection silently failing is the worst case; the supervisor is told |
| **Store-and-forward**: alerts wait in a queue and are retried (back-off up to 30 s) until the supervisor confirms them | A Wi-Fi dropout delays an alert, never loses it |
| **Acknowledgment**: the supervisor's click reaches the device, which changes its beep to "help is on the way" | The worker knows the alert got through |
| **Rich alerts**: posture, tilt, impact (g), HR, SpO2 | The supervisor knows what happened before arriving |

## Layout

| Path | What it is |
|---|---|
| `components/safety_core/` | Portable C: filter, windows, quantisation, decision rule, HR/SpO2, posture, alert state machine. Builds for the ESP32 **and** on a PC |
| `main/` | ESP-IDF app: sensor drivers, TFLite Micro, buzzer/vibration, button, Wi-Fi link, replay mode |
| `main/Kconfig.projbuild` | Settings in `idf.py menuconfig` → *Worker safety monitor* |
| `test/host/` | Parity tests: C port vs `phase1_pipeline.py` and `tools/device_model.py` |
| `tools/device_model.py` | Python reference model of the whole device (used by the tests) |
| `tools/supervisor.py` | Supervisor station: alerts, acknowledgments, live status page (`test_supervisor.py`: its tests) |
| `tools/run_qemu_replay.sh` | End-to-end test in the ESP32 / ESP32-S3 emulator |
| `tools/gen_replay.py`, `tools/gen_ppg_coeffs.py` | Regenerate `main/replay_data.h`, `main/selftest_data.h`, `sc_ppg_coeffs.h` |

## Hardware and wiring

| Signal | ESP32 GPIO | ESP32-S3 GPIO |
|---|---|---|
| I2C SDA (MPU6050 + MAX30102) | 21 | 8 |
| I2C SCL | 22 | 9 |
| Button to GND ("I'm OK" / long press = clear alert) | 0 | 0 |
| Active buzzer (high = on) | 25 | 4 |
| Vibration motor via NPN/MOSFET (high = on) | 26 | 5 |

All pins can be changed in `menuconfig`. Use 4.7 kΩ pull-ups on SDA/SCL; the internal
ones are too weak for 400 kHz. Power both sensor boards from 3.3 V.

**Mounting matters.** The model was trained on KFall, where the sensor sits on the
**lower back with its Y axis pointing down** (standing still reads `ay ≈ -1 g`).
Mount the MPU6050 the same way, or set *MPU6050 axis mapping* in `menuconfig`, for
example `+X-Y-Z` for a board mounted with Y pointing up. Posture for man-down uses the
same axes. Check it once: standing still, the telemetry should say `"posture":"upright"`
with `tilt_deg` near 0; lying on the back, `"lying"` near 90.

## Build and flash

Install [ESP-IDF v5.5](https://docs.espressif.com/projects/esp-idf/en/v5.5.1/esp32/get-started/index.html), then from this folder:

```bash
idf.py set-target esp32
```
```bash
idf.py menuconfig
```
In *Worker safety monitor* set the Wi-Fi SSID and password, the supervisor URL (the PC
running `supervisor.py`, e.g. `http://192.168.1.100:8080`) and the worker ID. *Alert
rules* holds the man-down times and the vitals motion limit.
```bash
idf.py build flash monitor
```

Use `idf.py set-target esp32s3` for an ESP32-S3 board. TensorFlow Lite Micro is fetched
automatically from the Espressif component registry on the first build.

## Supervisor station

```bash
python tools/supervisor.py --port 8080 --log alerts.jsonl
```

Open `http://<pc>:8080/`: every worker's state, heart rate, SpO2, posture, online /
offline, and every alert with an **Acknowledge** button. Alerts also print in the console
and go to `alerts.jsonl`. Alert format (POST `/alert`):

```json
{"type":"alert","id":"3fa2c1-1","worker":"worker-01","event":"ALERT_FALL",
 "detail":"post-fall inactivity","needs_ack":true,"t":39.56,"hr":88,"spo2":97,
 "posture":"lying","tilt_deg":88,"motion_g":0.004,"impact_g":5.6,"uptime_s":41.2}
```

`event` is `ALERT_FALL`, `ALERT_MAN_DOWN`, `ALERT_VITALS`, `DEVICE_FAULT` (need an
acknowledgment), or `ALERT_CLEARED`, `DEVICE_OK` (notices). Every reply lists the IDs
the supervisor has acknowledged. Devices POST status to `/telemetry` every 30 s, and
every 3 s while waiting for an acknowledgment.

## On the device

| Situation | Buzzer / vibration | Worker action |
|---|---|---|
| Fall suspected, or man-down pre-alarm | beep every 0.5 s + vibration | short press = "I'm OK"; for man-down, moving also cancels |
| Alert sent | fast beeping, vibration pulses | long press (3 s) clears it after help arrives |
| Supervisor acknowledged | two short beeps every 3 s | help is on the way |

## Tests

### 1. Host parity tests (C port vs Python), no hardware

From the repository root:

```bash
python firmware/test/host/gen_vectors.py --repo . --out build/host/vectors
```
```bash
cmake -S firmware/test/host -B build/host && cmake --build build/host && ctest --test-dir build/host --output-on-failure -V
```
```bash
python firmware/tools/test_supervisor.py
```

Checks the low-pass filter against `scipy.signal.sosfilt`, the int8 model inputs, the
2-consecutive-windows rule, `estimate_hr`/`estimate_spo2`, every event of the Phase 1
`SafetyFSM` (features off) and of the extended state machine (400 long scenarios with
man-down, posture, motion and sensor contact), plus the supervisor station.

With your own copy of KFall you can run the same checks on the real test subjects,
and also check the decisions the model makes on the C code's windows:

```bash
python firmware/test/host/gen_vectors.py --repo . --out build/kfall_vec --kfall path/to/kfall
```
```bash
build/host/test_host build/kfall_vec --dump-windows build/kfall_vec/c_windows.bin
```
```bash
python firmware/test/host/eval_c_windows.py --repo . --vectors build/kfall_vec
```

KFall-derived files must not be committed or shared (KFall licence).

### 2. ESP32 emulator (QEMU), no hardware

`tools/run_qemu_replay.sh` builds the firmware in **replay mode**: instead of reading the
sensors it plays 250 s of synthetic sensor data through the real pipeline in the
emulated chip:

| Situation | Expected |
|---|---|
| normal work | nothing |
| jogging, PPG motion artefacts read ~165 bpm | nothing (vitals paused; without that rule: ALERT_VITALS) |
| fall, worker lies still | fall suspected → ALERT_FALL (post-fall inactivity) |
| fall, worker presses "I'm OK" | fall suspected → cancelled |
| sustained tachycardia (150 bpm) | ALERT_VITALS |
| slow collapse without impact, lying still | man-down pre-alarm → ALERT_MAN_DOWN |
| lying still, then moves | man-down pre-alarm → cancelled by moving |
| standing still upright for 40 s | nothing |
| motion sensor frozen for 3 s | DEVICE_FAULT → DEVICE_OK |

It passes only if every event (type and time) matches the Python model of the device on
the same data and TensorFlow Lite Micro reproduces the TFLite reference outputs for 28
model inputs. On `esp32` the supervisor is also started 20 s *after* the firmware, so the
first alerts only get through by store-and-forward; every message must arrive, in
order, and every alert must be acknowledged back to the device. (QEMU has no network
model for the ESP32-S3; there alerts are only logged.)

```bash
python $IDF_PATH/tools/idf_tools.py install qemu-xtensa && . $IDF_PATH/export.sh
```
```bash
tools/run_qemu_replay.sh esp32
```
```bash
tools/run_qemu_replay.sh esp32s3
```

### 3. Continuous integration

`.github/workflows/firmware.yml` runs the host and supervisor tests, builds for ESP32
and ESP32-S3 with ESP-IDF v5.5.6 and runs the QEMU test for both chips on every push.

## Not done yet

- **Real hardware.** Everything above runs without a board. Still to check on the
  device: sensor wiring, axis orientation, inference time, battery life.
- **Thresholds on real workers.** The man-down times (30 s lying, 3 min upright), the
  stillness level (0.02 g) and the vitals motion limit (0.35 g) are reasonable defaults,
  not values tuned on people at work. All are in `menuconfig`.
- **LoRa.** Alerts go over Wi-Fi only. A LoRa module (e.g. SX1276/SX1262) would need a
  driver and a second transport in `main/net.c`.
- **SpO2 calibration.** The `110 - 25·R` curve must be calibrated against a reference
  oximeter before SpO2 values mean anything clinically. Some MAX30102 boards also have
  the red and IR LEDs swapped; check that IR reads higher than red on skin.
- **Queue across reboots.** Queued alerts live in RAM; a reboot before delivery loses them.
