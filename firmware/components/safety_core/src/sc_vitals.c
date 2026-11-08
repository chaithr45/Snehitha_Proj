// MAX30102 vitals: port of estimate_hr() / estimate_spo2() from phase1_pipeline.py,
// including the parts of scipy.signal.find_peaks they rely on (local maxima,
// `distance` and `prominence` selection). Double precision on purpose: the
// band-pass poles sit close to the unit circle.
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "safety_core.h"
#include "sc_ppg_coeffs.h"

_Static_assert(SC_PPG_COEFFS_FS == SC_PPG_FS, "PPG filter designed for another sample rate");

#define SKIP_N (2 * SC_PPG_FS)            // int(fs * 2): drop filter settling
#define PEAK_DISTANCE ((int)(SC_PPG_FS * 0.33))  // int(fs * 0.33)

static double s_y[SC_PPG_MAX_N];
static int s_peaks[SC_PPG_MAX_N / 2 + 1];
static int s_order[SC_PPG_MAX_N / 2 + 1];
static unsigned char s_keep[SC_PPG_MAX_N / 2 + 1];
static const double *s_sort_vals;

// sosfilt(sos, x - x[0]) with zero initial state
static void bandpass(const float *x, int n, double *y)
{
    double z[SC_PPG_SECTIONS][2];
    memset(z, 0, sizeof(z));
    const double x0 = x[0];
    for (int i = 0; i < n; i++) {
        double cur = (double)x[i] - x0;
        for (int s = 0; s < SC_PPG_SECTIONS; s++) {
            const double *k = SC_PPG_SOS[s];
            double nw = k[0] * cur + z[s][0];
            z[s][0] = k[1] * cur - k[4] * nw + z[s][1];
            z[s][1] = k[2] * cur - k[5] * nw;
            cur = nw;
        }
        y[i] = cur;
    }
}

static void mean_std(const double *x, int n, double *mean, double *std)
{
    double s = 0.0;
    for (int i = 0; i < n; i++) {
        s += x[i];
    }
    double mu = s / n;
    double v = 0.0;
    for (int i = 0; i < n; i++) {
        double d = x[i] - mu;
        v += d * d;
    }
    *mean = mu;
    *std = sqrt(v / n);  // numpy default ddof = 0
}

// scipy.signal._peak_finding_utils._local_maxima_1d (plateaus -> midpoint)
static int local_maxima(const double *x, int n, int *peaks)
{
    int m = 0;
    int i = 1;
    const int i_max = n - 1;
    while (i < i_max) {
        if (x[i - 1] < x[i]) {
            int i_ahead = i + 1;
            while (i_ahead < i_max && x[i_ahead] == x[i]) {
                i_ahead++;
            }
            if (x[i_ahead] < x[i]) {
                peaks[m++] = (i + (i_ahead - 1)) / 2;
                i = i_ahead;
            }
        }
        i++;
    }
    return m;
}

static int cmp_by_value(const void *a, const void *b)
{
    double va = s_sort_vals[*(const int *)a], vb = s_sort_vals[*(const int *)b];
    return (va > vb) - (va < vb);
}

// scipy.signal._peak_finding_utils._select_by_peak_distance
static int select_by_distance(const double *x, int *peaks, int m, int distance)
{
    static double vals[SC_PPG_MAX_N / 2 + 1];
    for (int i = 0; i < m; i++) {
        vals[i] = x[peaks[i]];
        s_order[i] = i;
        s_keep[i] = 1;
    }
    s_sort_vals = vals;
    qsort(s_order, (size_t)m, sizeof(int), cmp_by_value);  // ascending priority
    for (int i = m - 1; i >= 0; i--) {                     // highest first
        int j = s_order[i];
        if (!s_keep[j]) {
            continue;
        }
        for (int k = j - 1; k >= 0 && peaks[j] - peaks[k] < distance; k--) {
            s_keep[k] = 0;
        }
        for (int k = j + 1; k < m && peaks[k] - peaks[j] < distance; k++) {
            s_keep[k] = 0;
        }
    }
    int out = 0;
    for (int i = 0; i < m; i++) {
        if (s_keep[i]) {
            peaks[out++] = peaks[i];
        }
    }
    return out;
}

// scipy.signal._peak_finding_utils._peak_prominences (wlen = None)
static double prominence(const double *x, int n, int peak)
{
    double left_min = x[peak];
    for (int i = peak; i >= 0 && x[i] <= x[peak]; i--) {
        if (x[i] < left_min) {
            left_min = x[i];
        }
    }
    double right_min = x[peak];
    for (int i = peak; i < n && x[i] <= x[peak]; i++) {
        if (x[i] < right_min) {
            right_min = x[i];
        }
    }
    return x[peak] - (left_min > right_min ? left_min : right_min);
}

static int cmp_int(const void *a, const void *b)
{
    int va = *(const int *)a, vb = *(const int *)b;
    return (va > vb) - (va < vb);
}

float sc_estimate_hr(const float *ir, int n)
{
    if (n > SC_PPG_MAX_N || n <= SKIP_N + 2) {
        return NAN;
    }
    bandpass(ir, n, s_y);
    const double *s = s_y + SKIP_N;
    const int m = n - SKIP_N;

    double mu, sd;
    mean_std(s, m, &mu, &sd);
    const double pmin = sd * 0.5;

    int np_ = local_maxima(s, m, s_peaks);
    np_ = select_by_distance(s, s_peaks, np_, PEAK_DISTANCE);
    int kept = 0;
    for (int i = 0; i < np_; i++) {
        if (prominence(s, m, s_peaks[i]) >= pmin) {
            s_peaks[kept++] = s_peaks[i];
        }
    }
    if (kept < 3) {
        return NAN;
    }
    // median(diff(peaks))
    for (int i = 0; i < kept - 1; i++) {
        s_order[i] = s_peaks[i + 1] - s_peaks[i];
    }
    const int nd = kept - 1;
    qsort(s_order, (size_t)nd, sizeof(int), cmp_int);
    double med = (nd % 2) ? s_order[nd / 2] : 0.5 * (s_order[nd / 2 - 1] + s_order[nd / 2]);
    return (float)(60.0 / (med / SC_PPG_FS));
}

float sc_estimate_spo2(const float *red, const float *ir, int n)
{
    if (n > SC_PPG_MAX_N || n <= SKIP_N + 2) {
        return NAN;
    }
    double mu, ac_r, ac_i;
    bandpass(red, n, s_y);
    mean_std(s_y + SKIP_N, n - SKIP_N, &mu, &ac_r);
    bandpass(ir, n, s_y);
    mean_std(s_y + SKIP_N, n - SKIP_N, &mu, &ac_i);

    double sum_r = 0.0, sum_i = 0.0;
    for (int i = 0; i < n; i++) {
        sum_r += red[i];
        sum_i += ir[i];
    }
    const double dc_r = sum_r / n, dc_i = sum_i / n;
    if (ac_i <= 0.0 || dc_r <= 0.0 || dc_i <= 0.0) {
        return NAN;
    }
    // Ratio of ratios, 110 - 25 R. Must be calibrated against a reference oximeter.
    double r = (ac_r / dc_r) / (ac_i / dc_i);
    double spo2 = 110.0 - 25.0 * r;
    if (spo2 < 0.0) {
        spo2 = 0.0;
    } else if (spo2 > 100.0) {
        spo2 = 100.0;
    }
    return (float)spo2;
}
