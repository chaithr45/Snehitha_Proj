#!/usr/bin/env python3
"""
Simulated worker devices. They send exactly the messages the firmware sends
(firmware/main/net.c), keep unsent alerts in a queue like the real device, and react to
acknowledgments, so the web app can be shown and tested without hardware.

    python webapp/simulate.py --server http://localhost:8080 --workers 3
    python webapp/server.py --demo 3              (the same, started by the server)

Each simulated worker does normal work and, every one to three minutes, one of: a fall
(pre-alarm, then ALERT_FALL), a fall cancelled with the button (no alert, as on the
device), a racing heart at rest (ALERT_VITALS), a collapse without impact
(ALERT_MAN_DOWN), or a motion-sensor fault (DEVICE_FAULT, then DEVICE_OK).
"""
import argparse
import json
import random
import threading
import time
import urllib.request

SCENARIOS = (("fall", 4), ("cancelled_fall", 2), ("vitals", 2), ("man_down", 2), ("sensor_fault", 1))


class SimDevice:
    def __init__(self, worker, server, token=None, period=2.0, seed=None, speed=1.0):
        self.worker, self.server, self.token = worker, server.rstrip("/"), token
        self.period, self.speed = period, speed
        self.rng = random.Random(seed)
        self.boot = f"{self.rng.randrange(1 << 24):06x}"
        self.n = 0
        self.t0 = time.time()
        self.queue, self.waiting, self.acked = [], {}, set()
        self.state, self.posture, self.tilt = "NORMAL", "upright", 6.0
        self.hr, self.spo2, self.motion = 70 + self.rng.uniform(-8, 8), 97.0 + self.rng.uniform(0, 1.5), 0.06
        self.hr_target, self.moving, self.fault, self.worn = self.hr, False, False, True
        self.active_alert = None
        self.demo_requested = False
        self.rssi = -50 - self.rng.randrange(25)
        self.stop = threading.Event()

    # ------------------------------------------------------------------ link
    def post(self, path, body):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Device-Token"] = self.token
        req = urllib.request.Request(self.server + path, data=json.dumps(body).encode(), method="POST",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                rep = json.loads(r.read() or b"{}")
        except (OSError, ValueError):
            return False
        if rep.get("cmd") == "demo_fall":
            self.demo_requested = True
        for aid in rep.get("acked", []):
            if aid in self.waiting and aid not in self.acked:
                self.acked.add(aid)
        return True

    def flush(self):
        while self.queue and self.post("/alert", self.queue[0]):
            self.queue.pop(0)

    def uptime(self):
        return round((time.time() - self.t0) * self.speed, 1)

    def send_alert(self, event, detail, needs_ack, **extra):
        self.n += 1
        aid = f"{self.boot}-{self.n}"
        self.queue.append(dict(type="alert", id=aid, worker=self.worker, event=event, detail=detail,
                               needs_ack=needs_ack, t=self.uptime(), hr=round(self.hr) if self.worn else None,
                               spo2=round(self.spo2) if self.worn else None, uptime_s=self.uptime(), **extra))
        if needs_ack:
            self.waiting[aid] = event
            self.active_alert = aid
        self.flush()
        return aid

    def telemetry(self):
        self.post("/telemetry", dict(
            type="telemetry", worker=self.worker, state=self.state,
            hr=round(self.hr) if self.worn else None, spo2=round(self.spo2) if self.worn else None,
            vitals_reliable=not self.moving, worn=self.worn, posture=self.posture, tilt_deg=round(self.tilt),
            motion_g=round(self.motion, 3), sensor_fault=self.fault, active_alert=self.active_alert,
            acked=self.active_alert in self.acked, fall_prob=round(self.rng.uniform(0, 0.03), 3),
            windows=int(self.uptime() * 2), infer_us_avg=0, queued=len(self.queue), uptime_s=self.uptime(),
            rssi=self.rssi + self.rng.randrange(-3, 4), fw="simulator", ip="127.0.0.1", device=self.worker,
            calibrated=True, demo=True, demo_playing=False))

    # ------------------------------------------------------------------ behaviour
    def sleep(self, seconds):
        """Wait `seconds` of device time, sending telemetry every period and drifting the vitals."""
        end = time.time() + seconds / self.speed
        while time.time() < end and not self.stop.is_set():
            self.hr += (self.hr_target - self.hr) * 0.25 + self.rng.uniform(-1.2, 1.2)
            self.spo2 = min(99.5, max(85.0, self.spo2 + self.rng.uniform(-0.3, 0.3)))
            if self.state == "NORMAL" and not self.fault:
                self.moving = self.rng.random() < 0.3
                self.motion = self.rng.uniform(0.15, 0.35) if self.moving else self.rng.uniform(0.02, 0.08)
                self.tilt = self.rng.uniform(3, 15)
            self.flush()
            self.telemetry()
            self.stop.wait(min(self.period / self.speed, max(0.0, end - time.time())))

    def lie_still(self):
        self.posture, self.tilt, self.motion, self.moving = "lying", self.rng.uniform(75, 90), 0.01, False

    def get_up(self):
        self.state, self.posture, self.tilt, self.active_alert = "NORMAL", "upright", 8.0, None

    def wait_ack(self, aid, limit=180):
        end = time.time() + limit / self.speed
        while aid not in self.acked and time.time() < end and not self.stop.is_set():
            self.sleep(min(3.0, self.period))

    def scenario(self, kind):
        if kind == "fall":
            self.state = "FALL_PENDING"
            self.lie_still()
            self.sleep(10)                                     # pre-alarm: nobody presses "I'm OK"
            self.state = "ALERT"
            aid = self.send_alert("ALERT_FALL", "post-fall inactivity", True, posture="lying",
                                  tilt_deg=round(self.tilt), motion_g=0.01, impact_g=round(self.rng.uniform(3.2, 6.5), 1))
            self.wait_ack(aid)
            self.sleep(15)
            self.send_alert("ALERT_CLEARED", "long press on device", False)
            self.get_up()
        elif kind == "cancelled_fall":
            self.state = "FALL_PENDING"
            self.sleep(3)
            self.get_up()                                      # cancelled on the device: no alert
        elif kind == "vitals":
            self.hr_target = 150 + self.rng.uniform(0, 12)
            self.sleep(12)
            self.state = "ALERT"
            aid = self.send_alert("ALERT_VITALS", "heart rate / SpO2 out of range", True, posture="upright",
                                  tilt_deg=round(self.tilt), motion_g=round(self.motion, 3))
            self.wait_ack(aid)
            self.hr_target = 72 + self.rng.uniform(-6, 6)
            self.sleep(20)
            self.send_alert("ALERT_CLEARED", "long press on device", False)
            self.get_up()
        elif kind == "man_down":
            self.lie_still()
            self.sleep(10)
            self.state = "MAN_DOWN_PENDING"
            self.sleep(10)
            self.state = "ALERT"
            aid = self.send_alert("ALERT_MAN_DOWN", "no movement, no response to pre-alarm", True,
                                  posture="lying", tilt_deg=round(self.tilt), motion_g=0.01)
            self.wait_ack(aid)
            self.sleep(15)
            self.send_alert("ALERT_CLEARED", "long press on device", False)
            self.get_up()
        elif kind == "sensor_fault":
            self.fault = True
            self.send_alert("DEVICE_FAULT", "motion sensor not responding", True, fall_detection="unavailable")
            self.sleep(25)
            self.fault = False
            self.send_alert("DEVICE_OK", "motion sensor recovered", False, fall_detection="restored")
            self.active_alert = None

    def idle(self, seconds):
        """Normal work for `seconds`; a demo fall requested from the web app interrupts it."""
        end = time.time() + seconds / self.speed
        while time.time() < end and not self.stop.is_set():
            if self.demo_requested:
                self.demo_requested = False
                self.scenario("fall")
            self.sleep(min(self.period, max(0.0, (end - time.time()) * self.speed)))

    def run(self, first_event_s=30.0, gap_s=(60, 180)):
        self.idle(first_event_s)
        kinds, weights = zip(*SCENARIOS)
        while not self.stop.is_set():
            self.scenario(self.rng.choices(kinds, weights)[0])
            self.idle(self.rng.uniform(*gap_s))


def run_fleet(server, n, token=None, period=2.0, speed=1.0, seed=1):
    devices = [SimDevice(f"worker-{i + 1:02d}", server, token, period, seed + i, speed) for i in range(n)]
    threads = [threading.Thread(target=d.run, kwargs=dict(first_event_s=20 + 25 * i), daemon=True)
               for i, d in enumerate(devices)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--server", default="http://localhost:8080")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--token")
    ap.add_argument("--period", type=float, default=2.0, help="telemetry period in seconds")
    ap.add_argument("--speed", type=float, default=1.0, help="run the scenarios this many times faster")
    a = ap.parse_args()
    print(f"{a.workers} simulated workers sending to {a.server} (Ctrl+C to stop)")
    try:
        run_fleet(a.server, a.workers, a.token, a.period, a.speed)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
