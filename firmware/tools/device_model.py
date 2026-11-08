"""Python reference model of the firmware (firmware/main/pipeline.c + safety_core).

SafetyFSM2 extends the Phase 1 SafetyFSM (phase1_pipeline.py) with
  * man-down: worker motionless for md_lying_s while lying (tilt > lying_deg) or
    md_upright_s while upright -> 15 s pre-alarm, cancelled by moving or the button
  * motion-aware vitals: HR / SpO2 are not trusted while the average motion over
    vit_motion_s is above vit_motion_g (PPG motion artefacts)
With those features switched off (SafetyFSM2.phase1()) it behaves exactly like SafetyFSM;
firmware/test/host checks both.

DeviceModel runs the whole device (filter, windows, int8 model, decision rule, vitals,
sensor-fault monitor, state machine) on a sensor stream, in the firmware's order of
operations. firmware/tools/gen_replay.py uses it to produce the expected events.
"""
import math

import numpy as np

# sc_event_t codes (firmware/components/safety_core/include/safety_core.h)
EV_NONE, EV_FALL_SUSPECTED, EV_CANCELLED, EV_ALERT_FALL_INACTIVE, EV_ALERT_FALL_TIMEOUT, \
    EV_ALERT_VITALS, EV_MAN_DOWN_SUSPECTED, EV_MOVED, EV_ALERT_MAN_DOWN, EV_SENSOR_FAULT, \
    EV_SENSOR_OK = range(11)
EV_NAMES = ["NONE", "FALL_SUSPECTED", "CANCELLED", "ALERT_FALL (post-fall inactivity)",
            "ALERT_FALL (no cancel in time)", "ALERT_VITALS", "MAN_DOWN_SUSPECTED", "MOVED",
            "ALERT_MAN_DOWN", "SENSOR_FAULT", "SENSOR_OK"]

PPG_FS, PPG_WINDOW_S, FINGER_IR_MIN = 100, 10, 30000.0   # Kconfig defaults
MOTION_N = 50            # motion / posture over the last 1 s of IMU samples
STUCK_N = 100            # identical raw IMU samples (2 s) = sensor frozen


class SafetyFSM2:
    NORMAL, FALL_PENDING, MAN_DOWN_PENDING, ALERT = range(4)

    def __init__(self, cancel_s=10.0, inactive_s=5.0, motion_thr_g=0.05, hr_lo=45, hr_hi=140,
                 spo2_lo=90, vitals_hold_s=10.0, md_still_g=0.02, md_lying_s=30.0,
                 md_upright_s=180.0, md_cancel_s=15.0, lying_deg=60.0, vit_motion_g=0.35,
                 vit_motion_s=10.0, step_s=0.5):
        self.cancel_s, self.inactive_s, self.motion_thr = cancel_s, inactive_s, motion_thr_g
        self.hr_lo, self.hr_hi, self.spo2_lo, self.hold = hr_lo, hr_hi, spo2_lo, vitals_hold_s
        self.md_still_g, self.md_lying_s, self.md_upright_s = md_still_g, md_lying_s, md_upright_s
        self.md_cancel_s, self.lying_deg = md_cancel_s, lying_deg
        self.vit_motion_g = vit_motion_g
        self.vit_n = max(1, int(round(vit_motion_s / step_s)))
        self.reset()

    @classmethod
    def phase1(cls):
        """Feature set of the Phase 1 SafetyFSM (man-down and vitals gating off)."""
        return cls(md_lying_s=0.0, md_upright_s=0.0, vit_motion_g=0.0)

    def reset(self):
        self.state, self.t_fall, self.t_md = self.NORMAL, None, None
        self.still_since = self.vit_bad_since = self.md_still_since = None
        self.motion_hist = []

    def step(self, t, fall, motion, button, hr, spo2, tilt_deg=0.0, worn=True):
        ev = EV_NONE
        self.motion_hist = (self.motion_hist + [float(motion)])[-self.vit_n:]
        reliable = self.vit_motion_g <= 0 or float(np.mean(self.motion_hist)) < self.vit_motion_g
        bad = reliable and (hr < self.hr_lo or hr > self.hr_hi or spo2 < self.spo2_lo)
        self.vit_bad_since = (t if self.vit_bad_since is None else self.vit_bad_since) if bad else None
        if self.state != self.ALERT and self.vit_bad_since is not None and t - self.vit_bad_since >= self.hold:
            self.state, ev = self.ALERT, EV_ALERT_VITALS

        self.md_still_since = (t if self.md_still_since is None else self.md_still_since) \
            if motion < self.md_still_g else None
        lying = tilt_deg > self.lying_deg

        if self.state == self.NORMAL and fall:
            self.state, self.t_fall, self.still_since = self.FALL_PENDING, t, None
            ev = EV_FALL_SUSPECTED
        elif self.state == self.NORMAL:
            need = self.md_lying_s if lying else self.md_upright_s
            if worn and need > 0 and self.md_still_since is not None and t - self.md_still_since >= need:
                self.state, self.t_md, ev = self.MAN_DOWN_PENDING, t, EV_MAN_DOWN_SUSPECTED
        elif self.state == self.FALL_PENDING:
            if button:
                self.state, self.md_still_since = self.NORMAL, None
                return EV_CANCELLED
            self.still_since = (t if self.still_since is None else self.still_since) \
                if motion < self.motion_thr else None
            inactive = self.still_since is not None and t - self.still_since >= self.inactive_s
            if t - self.t_fall >= self.cancel_s or inactive:
                self.state = self.ALERT
                ev = EV_ALERT_FALL_INACTIVE if inactive else EV_ALERT_FALL_TIMEOUT
        elif self.state == self.MAN_DOWN_PENDING:
            if fall:
                self.state, self.t_fall, self.still_since = self.FALL_PENDING, t, None
                ev = EV_FALL_SUSPECTED
            elif button:
                self.state, self.md_still_since, ev = self.NORMAL, None, EV_CANCELLED
            elif motion >= self.motion_thr:
                self.state, self.md_still_since, ev = self.NORMAL, None, EV_MOVED
            elif t - self.t_md >= self.md_cancel_s:
                self.state, ev = self.ALERT, EV_ALERT_MAN_DOWN
        return ev


def tilt_deg(acc):
    """Angle between the mean acceleration and the body's up axis (+Y points down in KFall)."""
    m = acc.astype(np.float64).mean(0)
    n = float(np.linalg.norm(m))
    if n < 1e-6:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, -m[1] / n))))


class DeviceModel:
    """Python model of firmware/main/pipeline.c (same order of operations)."""

    def __init__(self, P, hp, q8, fsm=None):
        self.P, self.hp, self.q8 = P, hp, q8
        self.ppg_red, self.ppg_ir = [], []
        self.hr = self.spo2 = math.nan
        self.worn = True
        self.run = 0
        self.fsm = fsm or SafetyFSM2()
        self.press = self.reset = False
        self.events = []      # (t, code)
        self.probs = []
        self.last_raw, self.same = None, 0
        self.fault = False

    def push_ppg(self, red, ir):
        self.ppg_red.append(float(red))
        self.ppg_ir.append(float(ir))
        n, N = len(self.ppg_ir), PPG_FS * PPG_WINDOW_S
        if n >= N and n % PPG_FS == 0:
            r, i = np.array(self.ppg_red[-N:]), np.array(self.ppg_ir[-N:])
            self.worn = bool(i.mean() >= FINGER_IR_MIN)
            if not self.worn:
                self.hr = self.spo2 = math.nan
            else:
                self.hr = self.P.estimate_hr(i, PPG_FS)
                self.spo2 = self.P.estimate_spo2(r, i, PPG_FS)

    def check_raw(self, t, raw_counts):
        """Sensor-fault monitor: STUCK_N identical raw samples in a row = frozen IMU."""
        key = tuple(int(v) for v in raw_counts)
        self.same = self.same + 1 if key == self.last_raw else 1
        self.last_raw = key
        if not self.fault and self.same >= STUCK_N:
            self.fault = True
            self.events.append((t, EV_SENSOR_FAULT))
        elif self.fault and self.same == 1:
            self.fault = False
            self.events.append((t, EV_SENSOR_OK))

    def step_window(self, filt_win, t):
        hp, q8 = self.hp, self.q8
        q = np.clip(np.round(((filt_win - hp["mean"]) / hp["std"]) / np.float32(q8.in_scale)
                             + np.float32(q8.in_zp)), -128, 127).astype(np.int8)
        q8.it.set_tensor(q8.inp["index"], q[None])
        q8.it.invoke()
        o = q8.it.get_tensor(q8.out["index"]).astype(np.float32)
        prob = (o.ravel()[0] - q8.out_zp) * q8.out_scale
        self.probs.append(prob)
        if self.reset:
            self.reset = False
            self.fsm.reset()
            self.run = 0
        if prob >= np.float32(hp["thr"]):
            self.run += 1
            fall = self.run == hp["consec"]
        else:
            self.run = 0
            fall = False
        button, self.press = self.press, False
        acc = filt_win[-MOTION_N:, :3].astype(np.float64)
        motion = float(np.linalg.norm(acc, axis=1).std())
        ev = self.fsm.step(t, fall, motion, button, self.hr, self.spo2, tilt_deg(acc), self.worn)
        if ev != EV_NONE:
            self.events.append((t, ev))


def simulate(P, hp, q8, counts, phys, red, ir, actions, dev=None, press_code=1, long_press_code=2):
    """Run the device model on a sensor stream. counts: raw MPU6050 int16 (N x 6),
    phys: the same in g / dps (float32), red/ir: 2 PPG samples per IMU sample.
    `dev` lets the caller pass a DeviceModel with a different state machine."""
    dev = dev or DeviceModel(P, hp, q8)
    filt = P.filter50(phys.astype(np.float64))   # causal: identical to sample-by-sample
    act = dict(actions)
    for k in range(len(phys)):
        dev.push_ppg(red[2 * k], ir[2 * k])
        dev.push_ppg(red[2 * k + 1], ir[2 * k + 1])
        a = act.get(k)
        if a == press_code:
            dev.press = True
        elif a == long_press_code:
            dev.reset = True
        count = k + 1
        dev.check_raw(count / P.FS, counts[k])
        if count >= P.WIN and (count - P.WIN) % hp["hop"] == 0:
            dev.step_window(filt[count - P.WIN:count], count / P.FS)
    return dev
