// TFLite Micro wrapper around the int8 fall model in fall_model_data.h.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Everything the signal pipeline needs from fall_model_data.h (C-friendly view).
typedef struct {
    const float *mean;          // [FALL_CH]
    const float *std;           // [FALL_CH]
    const float (*lpf_sos)[6];  // [lpf_sections][6]
    const float (*lpf_zi)[2];   // [lpf_sections][2]
    int lpf_sections;
    int fs_hz, win, ch, hop, consec;
    float threshold;
    float in_scale;
    int in_zp;
    float out_scale;
    int out_zp;
} fall_params_t;

esp_err_t fall_model_init(void);
const fall_params_t *fall_model_params(void);
// Run one window (FALL_WIN x FALL_CH int8, oldest sample first). Returns the fall
// probability; the raw int8 output is stored in *raw_out if not NULL.
float fall_model_run(const int8_t *input, int8_t *raw_out);
size_t fall_model_arena_used(void);
// Compare TFLM outputs with TFLite reference-kernel outputs (selftest_data.h).
bool fall_model_selftest(void);

#ifdef __cplusplus
}
#endif
