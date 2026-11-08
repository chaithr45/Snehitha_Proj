// safety_core: hardware-independent C port of the Phase 1 signal processing.
//
// Everything here is a 1:1 port of phase1_pipeline.py and compiles both for the
// ESP32 (ESP-IDF component) and on a PC (host parity tests in firmware/test/host).
// Model-specific constants (mean/std, filter coefficients, quantisation, threshold)
// are NOT hard-coded here: they are passed in from fall_model_data.h at start-up.
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SC_WIN 128            // samples per window (2.56 s at 50 Hz) == FALL_WIN
#define SC_CH 6               // ax ay az [g], gx gy gz [deg/s]   == FALL_CH
#define SC_LPF_MAX_SECTIONS 4

// ---------------------------------------------------------------------------
// IMU low-pass filter (causal Butterworth, SOS / direct form II transposed,
// same operation order as scipy.signal.sosfilt). The state is primed with the
// first sample (zi * x0), exactly like filter50() in the pipeline.
// ---------------------------------------------------------------------------
typedef struct {
    int n_sections;
    float sos[SC_LPF_MAX_SECTIONS][6];
    float zi_unit[SC_LPF_MAX_SECTIONS][2];
    float z[SC_LPF_MAX_SECTIONS][SC_CH][2];
    bool primed;
} sc_lpf_t;

void sc_lpf_init(sc_lpf_t *f, const float sos[][6], const float zi[][2], int n_sections);
void sc_lpf_process(sc_lpf_t *f, const float in[SC_CH], float out[SC_CH]);

// ---------------------------------------------------------------------------
// Sliding window of filtered samples. sc_window_push() returns true every `hop`
// samples once the first full window is available, i.e. the device evaluates
// windows starting at samples 0, hop, 2*hop, ... exactly like trial_level().
// ---------------------------------------------------------------------------
typedef struct {
    float buf[SC_WIN][SC_CH];
    uint32_t count;   // total samples pushed since reset
    int hop;
} sc_window_t;

void sc_window_init(sc_window_t *w, int hop);
bool sc_window_push(sc_window_t *w, const float sample[SC_CH]);
// Copy the latest SC_WIN samples (oldest first) into `out` as model input:
// q = clamp(round_half_even(((x - mean) / std) / in_scale + in_zp), -128, 127)
void sc_window_quantize(const sc_window_t *w, const float mean[SC_CH], const float std[SC_CH],
                        float in_scale, int in_zp, int8_t out[SC_WIN * SC_CH]);
// Population std of the acceleration magnitude |a| over the last n samples (g).
// Used by the alert state machine to detect post-fall inactivity.
float sc_window_motion_std(const sc_window_t *w, int n);
// Body tilt in degrees from upright, from the mean acceleration of the last n samples
// (KFall frame: +Y points down when standing, so upright = 0 deg, lying = ~90 deg).
float sc_window_tilt_deg(const sc_window_t *w, int n);

// Convert the model's int8 output to a probability.
static inline float sc_dequantize(int8_t q, float scale, int zp) { return ((float)q - (float)zp) * scale; }

// ---------------------------------------------------------------------------
// Fall decision: >= `consec` consecutive windows with prob >= threshold.
// sc_detector_update() returns true once, on the window where the run reaches
// `consec` (deployment rule of trial_level()).
// ---------------------------------------------------------------------------
typedef struct {
    float threshold;
    int consec;
    int run;
} sc_detector_t;

void sc_detector_init(sc_detector_t *d, float threshold, int consec);
bool sc_detector_update(sc_detector_t *d, float prob);

// ---------------------------------------------------------------------------
// MAX30102 vitals (port of estimate_hr / estimate_spo2, fs fixed at SC_PPG_FS).
// Inputs are raw ADC counts, oldest first. Return NAN when no estimate.
// ---------------------------------------------------------------------------
#define SC_PPG_FS 100         // MAX30102 output rate the vitals filters are designed for
#define SC_PPG_MAX_N 2000     // longest buffer accepted (20 s at 100 Hz)

float sc_estimate_hr(const float *ir, int n);
float sc_estimate_spo2(const float *red, const float *ir, int n);

// ---------------------------------------------------------------------------
// Alert state machine: SafetyFSM from phase1_pipeline.py, extended with
//  * man-down: motionless for md_lying_s while lying (tilt > lying_deg) or
//    md_upright_s while upright -> pre-alarm, cancelled by moving or the button
//  * motion-aware vitals: HR/SpO2 ignored while the mean motion over
//    vit_motion_s is above vit_motion_g (PPG motion artefacts)
// sc_fsm_cfg_phase1() switches both off and then behaves exactly like SafetyFSM.
// Python reference: firmware/tools/device_model.py (SafetyFSM2).
// ---------------------------------------------------------------------------
typedef enum {
    SC_STATE_NORMAL = 0,
    SC_STATE_FALL_PENDING,
    SC_STATE_MAN_DOWN_PENDING,
    SC_STATE_ALERT,
} sc_state_t;

typedef enum {
    SC_EV_NONE = 0,
    SC_EV_FALL_SUSPECTED,       // buzzer + vibration, cancel window open
    SC_EV_CANCELLED,            // worker pressed the button: false alarm suppressed
    SC_EV_ALERT_FALL_INACTIVE,  // fall confirmed by post-fall inactivity
    SC_EV_ALERT_FALL_TIMEOUT,   // fall confirmed: no cancel in time
    SC_EV_ALERT_VITALS,         // HR / SpO2 out of range for vitals_hold_s
    SC_EV_MAN_DOWN_SUSPECTED,   // motionless too long: pre-alarm, cancel window open
    SC_EV_MOVED,                // worker moved during the man-down pre-alarm
    SC_EV_ALERT_MAN_DOWN,       // no movement or button during the pre-alarm
    SC_EV_SENSOR_FAULT,         // IMU data frozen / missing (raised by the device pipeline)
    SC_EV_SENSOR_OK,            // IMU data back to normal
} sc_event_t;

typedef struct {
    double cancel_s, inactive_s, motion_thr_g;          // fall confirmation (Phase 1)
    double hr_lo, hr_hi, spo2_lo, vitals_hold_s;        // vitals limits (Phase 1)
    double md_still_g, md_lying_s, md_upright_s;        // man-down (0 s = off)
    double md_cancel_s, lying_deg;
    double vit_motion_g, vit_motion_s;                  // vitals gating (0 g = off)
    double step_s;                                      // time between steps
} sc_fsm_cfg_t;

void sc_fsm_cfg_default(sc_fsm_cfg_t *c);   // device defaults, all features on
void sc_fsm_cfg_phase1(sc_fsm_cfg_t *c);    // exactly the Phase 1 SafetyFSM

typedef struct {
    bool fall;           // sc_detector_update() fired on this window
    float motion_std_g;  // sc_window_motion_std() over the last 1 s
    bool button;         // short press since the previous step
    float hr, spo2;      // NAN = no valid reading (never "bad")
    float tilt_deg;      // sc_window_tilt_deg() over the last 1 s
    bool worn;           // PPG sees skin (or no PPG sensor fitted)
} sc_fsm_input_t;

#define SC_FSM_MOTION_HIST 64

typedef struct {
    sc_fsm_cfg_t cfg;
    sc_state_t state;
    double t_fall, t_md;
    bool still, vit_bad, md_still;
    double still_since, vit_bad_since, md_still_since;
    float motion_hist[SC_FSM_MOTION_HIST];
    int motion_n, motion_len, motion_idx;
} sc_fsm_t;

void sc_fsm_init(sc_fsm_t *f, const sc_fsm_cfg_t *cfg);
// One step (call every cfg.step_s = 0.5 s). Returns the event of this step, if any.
sc_event_t sc_fsm_step(sc_fsm_t *f, double t, const sc_fsm_input_t *in);
// Back to NORMAL after an alert has been handled (long press on the device).
void sc_fsm_reset(sc_fsm_t *f);
// True if vitals are currently trusted (mean motion below vit_motion_g).
bool sc_fsm_vitals_reliable(const sc_fsm_t *f);

const char *sc_event_name(sc_event_t ev);
const char *sc_state_name(sc_state_t st);
bool sc_event_is_alert(sc_event_t ev);   // events that go to the supervisor

#ifdef __cplusplus
}
#endif
