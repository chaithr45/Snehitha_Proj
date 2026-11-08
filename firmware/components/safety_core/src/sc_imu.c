// IMU side of safety_core: low-pass filter, sliding window, quantisation,
// motion level and the consecutive-window fall decision.
#include <math.h>
#include <string.h>

#include "safety_core.h"

void sc_lpf_init(sc_lpf_t *f, const float sos[][6], const float zi[][2], int n_sections)
{
    memset(f, 0, sizeof(*f));
    if (n_sections > SC_LPF_MAX_SECTIONS) {
        n_sections = SC_LPF_MAX_SECTIONS;
    }
    f->n_sections = n_sections;
    memcpy(f->sos, sos, sizeof(float) * 6 * (size_t)n_sections);
    memcpy(f->zi_unit, zi, sizeof(float) * 2 * (size_t)n_sections);
}

void sc_lpf_process(sc_lpf_t *f, const float in[SC_CH], float out[SC_CH])
{
    if (!f->primed) {
        // zi = sosfilt_zi(SOS) * x[0]  (steady state for a constant input x[0])
        for (int s = 0; s < f->n_sections; s++) {
            for (int c = 0; c < SC_CH; c++) {
                f->z[s][c][0] = f->zi_unit[s][0] * in[c];
                f->z[s][c][1] = f->zi_unit[s][1] * in[c];
            }
        }
        f->primed = true;
    }
    for (int c = 0; c < SC_CH; c++) {
        float x = in[c];
        for (int s = 0; s < f->n_sections; s++) {
            const float *k = f->sos[s];
            float *z = f->z[s][c];
            // Same operation order as scipy's _sosfilt (k[3] == a0 == 1)
            float y = k[0] * x + z[0];
            z[0] = k[1] * x - k[4] * y + z[1];
            z[1] = k[2] * x - k[5] * y;
            x = y;
        }
        out[c] = x;
    }
}

void sc_window_init(sc_window_t *w, int hop)
{
    memset(w, 0, sizeof(*w));
    w->hop = hop > 0 ? hop : 1;
}

bool sc_window_push(sc_window_t *w, const float sample[SC_CH])
{
    memcpy(w->buf[w->count % SC_WIN], sample, sizeof(float) * SC_CH);
    w->count++;
    return w->count >= SC_WIN && (w->count - SC_WIN) % (uint32_t)w->hop == 0;
}

void sc_window_quantize(const sc_window_t *w, const float mean[SC_CH], const float std[SC_CH],
                        float in_scale, int in_zp, int8_t out[SC_WIN * SC_CH])
{
    uint32_t start = w->count % SC_WIN;  // oldest sample once the buffer is full
    for (int t = 0; t < SC_WIN; t++) {
        const float *x = w->buf[(start + (uint32_t)t) % SC_WIN];
        for (int c = 0; c < SC_CH; c++) {
            // float32 arithmetic, round-half-to-even: identical to Int8Model.__call__
            float xn = (x[c] - mean[c]) / std[c];
            float q = rintf(xn / in_scale + (float)in_zp);
            if (q < -128.0f) {
                q = -128.0f;
            } else if (q > 127.0f) {
                q = 127.0f;
            }
            out[t * SC_CH + c] = (int8_t)q;
        }
    }
}

float sc_window_motion_std(const sc_window_t *w, int n)
{
    if (n > SC_WIN) {
        n = SC_WIN;
    }
    if ((uint32_t)n > w->count) {
        n = (int)w->count;
    }
    if (n < 2) {
        return 0.0f;
    }
    double sum = 0.0, sum2 = 0.0;
    for (int i = 0; i < n; i++) {
        const float *x = w->buf[(w->count - 1u - (uint32_t)i) % SC_WIN];
        double m = sqrt((double)x[0] * x[0] + (double)x[1] * x[1] + (double)x[2] * x[2]);
        sum += m;
        sum2 += m * m;
    }
    double mu = sum / n;
    double var = sum2 / n - mu * mu;
    return var > 0.0 ? (float)sqrt(var) : 0.0f;
}

void sc_detector_init(sc_detector_t *d, float threshold, int consec)
{
    d->threshold = threshold;
    d->consec = consec > 0 ? consec : 1;
    d->run = 0;
}

bool sc_detector_update(sc_detector_t *d, float prob)
{
    if (prob >= d->threshold) {
        d->run++;
        return d->run == d->consec;
    }
    d->run = 0;
    return false;
}
