// Alert state machine: port of SafetyFSM from phase1_pipeline.py.
#include <math.h>
#include <string.h>

#include "safety_core.h"

void sc_fsm_init(sc_fsm_t *f, const sc_fsm_cfg_t *cfg)
{
    memset(f, 0, sizeof(*f));
    f->cfg = *cfg;
    f->state = SC_STATE_NORMAL;
}

void sc_fsm_reset(sc_fsm_t *f)
{
    sc_fsm_cfg_t cfg = f->cfg;
    sc_fsm_init(f, &cfg);
}

sc_event_t sc_fsm_step(sc_fsm_t *f, double t, bool fall, float motion_std_g, bool button,
                       float hr, float spo2)
{
    sc_event_t ev = SC_EV_NONE;
    const sc_fsm_cfg_t *c = &f->cfg;

    // NaN compares false, so a missing reading never counts as "bad" (as in Python)
    bool bad = (hr < c->hr_lo || hr > c->hr_hi || spo2 < c->spo2_lo);
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

    if (f->state == SC_STATE_NORMAL && fall) {
        f->state = SC_STATE_FALL_PENDING;
        f->t_fall = t;
        f->still = false;
        ev = SC_EV_FALL_SUSPECTED;
    } else if (f->state == SC_STATE_FALL_PENDING) {
        if (button) {
            f->state = SC_STATE_NORMAL;
            return SC_EV_CANCELLED;
        }
        if (motion_std_g < c->motion_thr_g) {
            if (!f->still) {
                f->still = true;
                f->still_since = t;
            }
        } else {
            f->still = false;
        }
        bool inactive = f->still && t - f->still_since >= c->inactive_s;
        if (t - f->t_fall >= c->cancel_s || inactive) {
            f->state = SC_STATE_ALERT;
            ev = inactive ? SC_EV_ALERT_FALL_INACTIVE : SC_EV_ALERT_FALL_TIMEOUT;
        }
    }
    return ev;
}

const char *sc_event_name(sc_event_t ev)
{
    switch (ev) {
    case SC_EV_FALL_SUSPECTED: return "FALL_SUSPECTED";
    case SC_EV_CANCELLED: return "CANCELLED";
    case SC_EV_ALERT_FALL_INACTIVE: return "ALERT_FALL (post-fall inactivity)";
    case SC_EV_ALERT_FALL_TIMEOUT: return "ALERT_FALL (no cancel in time)";
    case SC_EV_ALERT_VITALS: return "ALERT_VITALS";
    default: return "NONE";
    }
}

const char *sc_state_name(sc_state_t st)
{
    switch (st) {
    case SC_STATE_FALL_PENDING: return "FALL_PENDING";
    case SC_STATE_ALERT: return "ALERT";
    default: return "NORMAL";
    }
}
