# Phase 2 firmware: ESP32 / ESP32-S3

ESP-IDF application that runs the Phase 1 model on the wearable.

```
MPU6050 ─ 50 Hz ─▶ 5 Hz low-pass ─▶ 2.56 s window ─▶ int8 1D-CNN (TFLite Micro) ─┐
                                    every 0.5 s                                  │ 2 windows in a row
MAX30102 ─ 100 Hz ─▶ heart rate + SpO2 every 1 s ───────────────────────────────┤ ≥ threshold
                                                                                 ▼
                         alert state machine (same rules as SafetyFSM in Python)
             fall suspected ─▶ buzzer + vibration, 10 s to press "I'm OK"
             5 s motionless or no press ─▶ ALERT_FALL ─▶ supervisor (Wi-Fi, JSON)
             HR < 45 / > 140 or SpO2 < 90 for 10 s ─▶ ALERT_VITALS ─▶ supervisor
```

All model constants (normalisation, filter, quantisation, threshold) come straight from
`../fall_model_data.h`, so re-running `phase1_pipeline.py` and rebuilding is all it takes
to deploy a new model.

## Layout

| Path | What it is |
|---|---|
| `components/safety_core/` | Portable C port of the Phase 1 signal processing (filter, windows, quantisation, decision rule, HR/SpO2, alert state machine). Builds for the ESP32 **and** on a PC. |
| `main/` | ESP-IDF app: sensor drivers, TFLite Micro, buzzer/vibration, button, Wi-Fi alerts, replay mode |
| `main/Kconfig.projbuild` | Settings shown in `idf.py menuconfig` → *Worker safety monitor* |
| `test/host/` | Parity tests: C port vs `phase1_pipeline.py` |
| `tools/supervisor.py` | Supervisor station: receives alerts, live status page |
| `tools/run_qemu_replay.sh` | End-to-end test in the ESP32 emulator |
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
example `+X-Y-Z` for a board mounted with Y pointing up. Check it once: with the
device worn and the worker standing still, the log should show `ay` near -1 g.

## Build and flash

Install [ESP-IDF v5.5](https://docs.espressif.com/projects/esp-idf/en/v5.5.1/esp32/get-started/index.html), then from this folder:

```bash
idf.py set-target esp32
```
```bash
idf.py menuconfig
```
In *Worker safety monitor* set the Wi-Fi SSID and password, the supervisor URL (the PC
running `supervisor.py`, e.g. `http://192.168.1.100:8080`) and the worker ID.
```bash
idf.py build flash monitor
```

Use `idf.py set-target esp32s3` for an ESP32-S3 board. TensorFlow Lite Micro is fetched
automatically from the Espressif component registry on the first build.

## Supervisor station

```bash
python tools/supervisor.py --port 8080 --log alerts.jsonl
```

Alerts print in the console and are appended to `alerts.jsonl`; open
`http://<pc>:8080/` for a page with every worker's state, heart rate and SpO2.
Alert format (POST `/alert`):

```json
{"type":"alert","worker":"worker-01","event":"ALERT_FALL","detail":"post-fall inactivity",
 "t":27.56,"hr":88,"spo2":97,"uptime_s":31.2}
```

`event` is `ALERT_FALL`, `ALERT_VITALS` or `ALERT_CLEARED`. Devices also POST status
to `/telemetry` every 30 s.

## On the device

| Situation | Buzzer / vibration | Worker action |
|---|---|---|
| Fall suspected | beep every 0.5 s + vibration | short press = "I'm OK" (cancels) |
| Alert sent | fast beeping, vibration pulses | long press (3 s) clears it after help arrives |

Vitals are ignored while the MAX30102 is not touching skin (IR level below
*Minimum IR level*), so a loose sensor never raises a vitals alert.

## Tests

### 1. Host parity tests (C port vs Python), no hardware

From the repository root:

```bash
python firmware/test/host/gen_vectors.py --repo . --out build/host/vectors
```
```bash
cmake -S firmware/test/host -B build/host && cmake --build build/host && ctest --test-dir build/host --output-on-failure -V
```

Checks the low-pass filter against `scipy.signal.sosfilt`, the int8 model inputs, the
2-consecutive-windows rule, `estimate_hr`/`estimate_spo2` and every `SafetyFSM` event.

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
sensors it plays 89 s of synthetic sensor data (normal work, a fall where the worker lies
still, a fall the worker cancels, sustained tachycardia, plus button presses) through the
real pipeline in the emulated chip. The script passes only if:

- every event (type and time) matches the Python model of the device on the same data;
- TensorFlow Lite Micro on the chip reproduces the TFLite reference outputs for 28 model inputs;
- on `esp32`: every alert reaches `supervisor.py` over QEMU's emulated Ethernet (QEMU has
  no network model for the ESP32-S3, so there alerts are only logged).

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

`.github/workflows/firmware.yml` runs the host tests, builds for ESP32 and ESP32-S3
with ESP-IDF v5.5.6 and runs the QEMU test for both chips on every push.

## Not done yet

- **Real hardware.** Everything above runs without a board. Still to check on the
  device: sensor wiring, axis orientation, inference time, battery life.
- **LoRa.** Alerts go over Wi-Fi only. A LoRa module (e.g. SX1276/SX1262) would need a
  driver and a second transport in `main/net.c`.
- **SpO2 calibration.** The `110 - 25·R` curve must be calibrated against a reference
  oximeter before SpO2 values mean anything clinically. Some MAX30102 boards also have
  the red and IR LEDs swapped; check that IR reads higher than red on skin.
- **Store-and-forward.** If the network is down, alerts are retried for about 2.5 minutes,
  then logged as not delivered. They are not yet kept for later delivery.
