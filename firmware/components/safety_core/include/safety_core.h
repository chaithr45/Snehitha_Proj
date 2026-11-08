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
// Alert state machine (port of SafetyFSM).
// ---------------------------------------------------------------------------
typedef enum { SC_STATE_NORMAL = 0, SC_STATE_FALL_PENDING, SC_STATE_ALERT } sc_state_t;

typedef enum {
    SC_EV_NONE = 0,
    SC_EV_FALL_SUSPECTED,    // buzzer + vibration, cancel window open
    SC_EV_CANCELLED,         // worker pressed the button: false alarm suppressed
    SC_EV_ALERT_FALL_INACTIVE,  // fall confirmed by post-fall inactivity
    SC_EV_ALERT_FALL_TIMEOUT,   // fall confirmed: no cancel in time
    SC_EV_ALERT_VITALS,      // HR / SpO2 out of range for vitals_hold_s
} sc_event_t;

typedef struct {
    double cancel_s, inactive_s, motion_thr_g;
    double hr_lo, hr_hi, spo2_lo, vitals_hold_s;
} sc_fsm_cfg_t;

// Defaults identical to SafetyFSM.__init__
#define SC_FSM_CFG_DEFAULT { 10.0, 5.0, 0.05, 45.0, 140.0, 90.0, 10.0 }

typedef struct {
    sc_fsm_cfg_t cfg;
    sc_state_t state;
    double t_fall;
    bool still, vit_bad;
    double still_since, vit_bad_since;
} sc_fsm_t;

void sc_fsm_init(sc_fsm_t *f, const sc_fsm_cfg_t *cfg);
// One step (call every 0.5 s). hr/spo2 may be NAN (no valid reading = not "bad").
sc_event_t sc_fsm_step(sc_fsm_t *f, double t, bool fall, float motion_std_g, bool button,
                       float hr, float spo2);
// Return to NORMAL after an alert has been handled (not in the Python model;
// triggered on the device by a long press of the button).
void sc_fsm_reset(sc_fsm_t *f);

const char *sc_event_name(sc_event_t ev);
const char *sc_state_name(sc_state_t st);

#ifdef __cplusplus
}
#endif
