// Alert state machine: SafetyFSM (phase1_pipeline.py) + man-down + motion-aware vitals.
// Python reference: firmware/tools/device_model.py (SafetyFSM2), same order of operations.
#include <math.h>
#include <string.h>

#include "safety_core.h"

void sc_fsm_cfg_default(sc_fsm_cfg_t *c)
{
    c->cancel_s = 10.0;
    c->inactive_s = 5.0;
    c->motion_thr_g = 0.05;
    c->hr_lo = 45.0;
    c->hr_hi = 140.0;
    c->spo2_lo = 90.0;
    c->vitals_hold_s = 10.0;
    c->md_still_g = 0.02;
    c->md_lying_s = 30.0;
    c->md_upright_s = 180.0;
    c->md_cancel_s = 15.0;
    c->lying_deg = 60.0;
    c->vit_motion_g = 0.35;
    c->vit_motion_s = 10.0;
    c->step_s = 0.5;
}

void sc_fsm_cfg_phase1(sc_fsm_cfg_t *c)
{
    sc_fsm_cfg_default(c);
    c->md_lying_s = 0.0;
    c->md_upright_s = 0.0;
    c->vit_motion_g = 0.0;
}

void sc_fsm_init(sc_fsm_t *f, const sc_fsm_cfg_t *cfg)
{
    memset(f, 0, sizeof(*f));
    f->cfg = *cfg;
    f->state = SC_STATE_NORMAL;
    int n = cfg->step_s > 0 ? (int)lround(cfg->vit_motion_s / cfg->step_s) : 1;
    f->motion_len = n < 1 ? 1 : (n > SC_FSM_MOTION_HIST ? SC_FSM_MOTION_HIST : n);
}

void sc_fsm_reset(sc_fsm_t *f)
{
    sc_fsm_cfg_t cfg = f->cfg;
    sc_fsm_init(f, &cfg);
}

static double motion_mean(const sc_fsm_t *f)
{
    double s = 0.0;
    for (int i = 0; i < f->motion_n; i++) {
        s += f->motion_hist[i];
    }
    return f->motion_n ? s / f->motion_n : 0.0;
}

bool sc_fsm_vitals_reliable(const sc_fsm_t *f)
{
    return f->cfg.vit_motion_g <= 0.0 || motion_mean(f) < f->cfg.vit_motion_g;
}

sc_event_t sc_fsm_step(sc_fsm_t *f, double t, const sc_fsm_input_t *in)
{
    sc_event_t ev = SC_EV_NONE;
    const sc_fsm_cfg_t *c = &f->cfg;

    // motion history for vitals gating (last vit_motion_s seconds)
    f->motion_hist[f->motion_idx] = in->motion_std_g;
    f->motion_idx = (f->motion_idx + 1) % f->motion_len;
    if (f->motion_n < f->motion_len) {
        f->motion_n++;
    }

    // NaN compares false, so a missing reading never counts as "bad" (as in Python)
    const bool bad = sc_fsm_vitals_reliable(f) &&
                     (in->hr < c->hr_lo || in->hr > c->hr_hi || in->spo2 < c->spo2_lo);
    if (bad) {
        if (!f->vit_bad) {
            f->vit_bad = true;
            f->vit_bad_since = t;
        }
    } else {
        f->vit_bad = false;
    }
    if (f->state != SC_STATE_ALERT && f->vit_bad && t - f->vit_bad_since >= c->vitals_hold_s) {
        f->state = SC_STATE_ALERT;
        ev = SC_EV_ALERT_VITALS;
    }

    // man-down stillness timer (reset by any movement)
    if (in->motion_std_g < c->md_still_g) {
        if (!f->md_still) {
            f->md_still = true;
            f->md_still_since = t;
        }
    } else {
        f->md_still = false;
    }
    const bool lying = in->tilt_deg > c->lying_deg;

    switch (f->state) {
    case SC_STATE_NORMAL:
        if (in->fall) {
            f->state = SC_STATE_FALL_PENDING;
            f->t_fall = t;
            f->still = false;
            ev = SC_EV_FALL_SUSPECTED;
        } else {
            const double need = lying ? c->md_lying_s : c->md_upright_s;
            if (in->worn && need > 0.0 && f->md_still && t - f->md_still_since >= need) {
                f->state = SC_STATE_MAN_DOWN_PENDING;
                f->t_md = t;
                ev = SC_EV_MAN_DOWN_SUSPECTED;
            }
        }
        break;
    case SC_STATE_FALL_PENDING:
        if (in->button) {
            f->state = SC_STATE_NORMAL;
            f->md_still = false;
            return SC_EV_CANCELLED;
        }
        if (in->motion_std_g < c->motion_thr_g) {
            if (!f->still) {
                f->still = true;
                f->still_since = t;
            }
        } else {
            f->still = false;
        }
        {
            const bool inactive = f->still && t - f->still_since >= c->inactive_s;
            if (t - f->t_fall >= c->cancel_s || inactive) {
                f->state = SC_STATE_ALERT;
                ev = inactive ? SC_EV_ALERT_FALL_INACTIVE : SC_EV_ALERT_FALL_TIMEOUT;
            }
        }
        break;
    case SC_STATE_MAN_DOWN_PENDING:
        if (in->fall) {
            f->state = SC_STATE_FALL_PENDING;
            f->t_fall = t;
            f->still = false;
            ev = SC_EV_FALL_SUSPECTED;
        } else if (in->button) {
            f->state = SC_STATE_NORMAL;
            f->md_still = false;
            ev = SC_EV_CANCELLED;
        } else if (in->motion_std_g >= c->motion_thr_g) {
            f->state = SC_STATE_NORMAL;
            f->md_still = false;
            ev = SC_EV_MOVED;
        } else if (t - f->t_md >= c->md_cancel_s) {
            f->state = SC_STATE_ALERT;
            ev = SC_EV_ALERT_MAN_DOWN;
        }
        break;
    default:
        break;
    }
    return ev;
}

bool sc_event_is_alert(sc_event_t ev)
{
    return ev == SC_EV_ALERT_FALL_INACTIVE || ev == SC_EV_ALERT_FALL_TIMEOUT || ev == SC_EV_ALERT_VITALS ||
           ev == SC_EV_ALERT_MAN_DOWN;
}

const char *sc_event_name(sc_event_t ev)
{
    switch (ev) {
    case SC_EV_FALL_SUSPECTED: return "FALL_SUSPECTED";
    case SC_EV_CANCELLED: return "CANCELLED";
    case SC_EV_ALERT_FALL_INACTIVE: return "ALERT_FALL (post-fall inactivity)";
    case SC_EV_ALERT_FALL_TIMEOUT: return "ALERT_FALL (no cancel in time)";
    case SC_EV_ALERT_VITALS: return "ALERT_VITALS";
    case SC_EV_MAN_DOWN_SUSPECTED: return "MAN_DOWN_SUSPECTED";
    case SC_EV_MOVED: return "MOVED";
    case SC_EV_ALERT_MAN_DOWN: return "ALERT_MAN_DOWN";
    case SC_EV_SENSOR_FAULT: return "SENSOR_FAULT";
    case SC_EV_SENSOR_OK: return "SENSOR_OK";
    default: return "NONE";
    }
}

const char *sc_state_name(sc_state_t st)
{
    switch (st) {
    case SC_STATE_FALL_PENDING: return "FALL_PENDING";
    case SC_STATE_MAN_DOWN_PENDING: return "MAN_DOWN_PENDING";
    case SC_STATE_ALERT: return "ALERT";
    default: return "NORMAL";
    }
}
