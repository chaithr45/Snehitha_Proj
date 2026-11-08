# Live demo for the professor

The real firmware runs in the **ESP32 emulator** on your laptop and plays a 4-minute
story in real time: normal work, jogging, two falls, a racing heart, a slow collapse
and a sensor failure. A **supervisor page** in your browser shows each alert as it
arrives; the professor clicks **Acknowledge** and the worker's device answers. No board
is needed.

## Before the meeting (once, about 15 minutes)

1. Install **Docker Desktop** (Windows, macOS or Linux) and start it.
2. Get the code (or download the ZIP from GitHub and unzip it):
   ```bash
   git clone -b claude/great-euler-gnc1oj https://github.com/chaithr45/Snehitha_Proj.git
   ```
   ```bash
   cd Snehitha_Proj
   ```
3. Do a full rehearsal. This also builds the firmware and installs the emulator, so it
   takes 5-10 minutes the first time:
   - macOS / Linux:
     ```bash
     docker run --name safety-demo -it -p 8080:8080 -v "$PWD":/project -w /project/firmware espressif/idf:v5.5.6 bash tools/demo.sh
     ```
   - Windows PowerShell:
     ```powershell
     docker run --name safety-demo -it -p 8080:8080 -v "${PWD}:/project" -w /project/firmware espressif/idf:v5.5.6 bash tools/demo.sh
     ```
4. When the banner `Supervisor page: http://localhost:8080/` appears, open that address
   in your browser and watch the whole story once. Press Ctrl+C at the end.

## In the meeting

1. Start the demo again. This takes seconds now, because everything is already built:
   ```bash
   docker start -ai safety-demo
   ```
2. Put the terminal (the worker's device) and the browser (the supervisor) side by side.
3. Narrate as it plays. The times are approximate; each alert waits until you click
   **Acknowledge**.

| Time | Device log (terminal) | Supervisor page | What to say |
|---|---|---|---|
| 0:00 | `SEGMENT normal work` | worker appears, *online*; heart rate after ~10 s | "The device sends its status every few seconds." |
| 0:13 | `SEGMENT jogging...` | note *vitals paused (moving)* | "Running makes the pulse sensor read a fake 165 bpm. The device knows it is moving and ignores it, so there is no false alarm." |
| 0:40 | `EVENT FALL_SUSPECTED`, `BUZZER: beeping + vibrating` | nothing yet | "The neural network detected a fall within a second. The worker has 10 s to press *I'm OK*." |
| 0:46 | `EVENT ALERT_FALL (post-fall inactivity)`, `BUZZER: ALARM` | red **ALERT_FALL**, posture *lying*, impact *5.6 g* | "The worker is not moving, so the alert goes out with the posture and impact force." **Click Acknowledge.** |
| | `ACK: supervisor acknowledged`, `BUZZER: two short beeps...` | *Acknowledged* time | "The device tells the worker help is on the way." |
| ~1:05 | `EVENT FALL_SUSPECTED`, `BUTTON short press`, `EVENT CANCELLED` | nothing | "A false alarm is cancelled on the device. The supervisor is never bothered." |
| ~1:40 | `EVENT ALERT_VITALS` | **ALERT_VITALS**, HR 150 | "150 bpm for 10 s while not running." **Click Acknowledge.** |
| ~2:50 | `EVENT MAN_DOWN_SUSPECTED`, then `ALERT_MAN_DOWN` | **ALERT_MAN_DOWN**, *lying* | "A slow collapse with no impact, like fainting. The fall model cannot see it, but the man-down rule can: lying still for 30 s." **Click Acknowledge.** |
| ~3:30 | `MAN_DOWN_SUSPECTED`, then `EVENT MOVED` | nothing | "The worker moved during the pre-alarm, so it cancels itself." |
| ~4:10 | (standing still) | nothing | "Standing still is normal, so no alarm." |
| ~4:50 | `EVENT SENSOR_FAULT`, then `SENSOR_OK` | **DEVICE_FAULT**, then *DEVICE_OK* | "If the motion sensor fails, the supervisor is told that fall detection is down." |
| end | `MODEL SELFTEST PASS: 28/28`, `REPLAY RESULT: PASS`, `DEMO FINISHED` | | "The emulated chip ran the same int8 model as Python, bit for bit, and every event matched the Python reference." |

Press Ctrl+C to stop. To run it again: `docker start -ai safety-demo`.

## Things worth showing next to the demo

- [`README.md`](README.md): results (441/441 falls, 1 false alarm in 516 activity recordings).
- [`deployment_eval.md`](deployment_eval.md) and its figure: time from impact to alarm
  (median 0.81 s) and the threshold sweep.
- GitHub → **Actions** → *firmware* → latest run: every push builds the firmware for
  ESP32 and ESP32-S3 and runs this scenario in the emulator automatically. The job
  *QEMU end-to-end test esp32* starts the supervisor 20 s late, so its log shows the
  alerts waiting in the device's queue and then arriving in order.

## If something goes wrong in the meeting

- **Port 8080 busy on your laptop:** in the `docker run` command use `-p 8090:8080`
  instead of `-p 8080:8080` and open `http://localhost:8090/` (needs a new container:
  `docker rm safety-demo` first).
- **Docker not working:** open the latest *QEMU end-to-end test esp32* job log on
  GitHub. It is the same story, already run, with every event and alert.
- **Alert not acknowledged:** the device waits up to 3 minutes, then moves on.

## With real hardware (the most convincing demo)

Parts (about USD 15-20): ESP32 DevKit, MPU6050 (GY-521) board, MAX30102 board, active
buzzer, small vibration motor with an NPN transistor, a push button, breadboard and
wires. Wiring, axis orientation and flashing are in
[`firmware/README.md`](firmware/README.md). Run `supervisor.py` on your laptop on the
same Wi-Fi.

Then:
- **Fall:** wear it at the lower back, with the Y axis pointing down. Do a controlled
  fall onto a mattress, then lie still. Only do this safely and with someone present.
  The model was trained on human falls, so dropping the board alone is not a valid test.
- **Cancel:** do the same fall, then press the button within 10 s.
- **Man-down:** lie still on your back for 30 s.
- **Vitals:** put a finger on the MAX30102 and watch heart rate and SpO2 on the page.
