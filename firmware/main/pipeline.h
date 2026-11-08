// Device pipeline: IMU (50 Hz) -> filter -> window -> int8 model -> decision -> alert
// state machine, and PPG (100 Hz) -> HR / SpO2. Same order of operations as the
// Python device model in firmware/tools/gen_replay.py.
#pragma once

#include <stdint.h>

#include "safety_core.h"

#ifdef __cplusplus
extern "C" {
#endif

void pipeline_init(void);

// One IMU sample in physical units: ax ay az [g], gx gy gz [deg/s], already in the
// KFall axis frame. Call at exactly FALL_FS_HZ.
void pipeline_push_imu(const float sample[SC_CH]);

// One MAX30102 sample (raw 18-bit counts). Call at exactly SC_PPG_FS.
void pipeline_push_ppg(uint32_t red, uint32_t ir);

// Button: short press = "I'm OK" (cancels a suspected fall), long press = clear an alert.
// Both are applied at the next 0.5 s decision step. Safe to call from any task.
void pipeline_button_press(void);
void pipeline_request_reset(void);

typedef struct {
    sc_state_t state;
    float hr, spo2;            // NAN when not available
    float last_prob;
    uint32_t windows;          // model evaluations so far
    uint32_t infer_us_avg, infer_us_max;
} pipeline_status_t;

void pipeline_get_status(pipeline_status_t *out);

// Optional observer for every state-machine event (used by the replay self-check).
typedef void (*pipeline_event_hook_t)(double t, sc_event_t ev);
void pipeline_set_event_hook(pipeline_event_hook_t hook);

#ifdef __cplusplus
}
#endif
