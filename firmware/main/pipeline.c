#include "sdkconfig.h"
#include "pipeline.h"

#include <math.h>
#include <string.h>

#include "annunciator.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "fall_model.h"
#include "freertos/FreeRTOS.h"
#include "net.h"

#define PPG_N (SC_PPG_FS * CONFIG_SAFETY_PPG_WINDOW_S)
#define MOTION_N 50   // inactivity check over the last 1 s

_Static_assert(PPG_N <= SC_PPG_MAX_N, "PPG window too long for safety_core");

static const char *TAG = "pipeline";

static const fall_params_t *s_p;
static sc_lpf_t s_lpf;
static sc_window_t s_win;
static sc_detector_t s_det;
static sc_fsm_t s_fsm;
static int8_t s_q[SC_WIN * SC_CH];
static uint32_t s_imu_count;

static float s_red[PPG_N], s_ir[PPG_N];
static float s_red_lin[PPG_N], s_ir_lin[PPG_N];
static uint32_t s_ppg_count;

// shared between the IMU task, the PPG task, the button task and the net task
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static float s_hr = NAN, s_spo2 = NAN, s_last_prob;
static bool s_press, s_reset;
static uint32_t s_windows, s_infer_us_max;
static uint64_t s_infer_us_sum;
static pipeline_event_hook_t s_hook;

void pipeline_init(void)
{
    s_p = fall_model_params();
    sc_lpf_init(&s_lpf, s_p->lpf_sos, s_p->lpf_zi, s_p->lpf_sections);
    sc_window_init(&s_win, s_p->hop);
    sc_detector_init(&s_det, s_p->threshold, s_p->consec);
    const sc_fsm_cfg_t cfg = SC_FSM_CFG_DEFAULT;
    sc_fsm_init(&s_fsm, &cfg);
    s_imu_count = 0;
    s_ppg_count = 0;
    ESP_LOGI(TAG, "ready: %d Hz IMU, window %d, decision every %d samples; vitals over %d s",
             s_p->fs_hz, s_p->win, s_p->hop, CONFIG_SAFETY_PPG_WINDOW_S);
}

void pipeline_set_event_hook(pipeline_event_hook_t hook) { s_hook = hook; }

void pipeline_button_press(void)
{
    taskENTER_CRITICAL(&s_mux);
    s_press = true;
    taskEXIT_CRITICAL(&s_mux);
}

void pipeline_request_reset(void)
{
    taskENTER_CRITICAL(&s_mux);
    s_reset = true;
    taskEXIT_CRITICAL(&s_mux);
}

void pipeline_get_status(pipeline_status_t *out)
{
    taskENTER_CRITICAL(&s_mux);
    out->state = s_fsm.state;
    out->hr = s_hr;
    out->spo2 = s_spo2;
    out->last_prob = s_last_prob;
    out->windows = s_windows;
    out->infer_us_avg = s_windows ? (uint32_t)(s_infer_us_sum / s_windows) : 0;
    out->infer_us_max = s_infer_us_max;
    taskEXIT_CRITICAL(&s_mux);
}

static void on_event(double t, sc_event_t ev, float hr, float spo2, float prob, float motion)
{
    ESP_LOGW(TAG, "EVENT t=%.2f %s (p=%.3f motion=%.3f g hr=%.0f spo2=%.0f)", t, sc_event_name(ev),
             (double)prob, (double)motion, (double)hr, (double)spo2);
    switch (ev) {
    case SC_EV_FALL_SUSPECTED:
        annunciator_set(ANN_PENDING);   // buzzer + vibration, cancel window open
        break;
    case SC_EV_CANCELLED:
        annunciator_set(ANN_OFF);
        break;
    case SC_EV_ALERT_FALL_INACTIVE:
        annunciator_set(ANN_ALERT);
        net_send_event("ALERT_FALL", "post-fall inactivity", t, hr, spo2);
        break;
    case SC_EV_ALERT_FALL_TIMEOUT:
        annunciator_set(ANN_ALERT);
        net_send_event("ALERT_FALL", "no cancel in time", t, hr, spo2);
        break;
    case SC_EV_ALERT_VITALS:
        annunciator_set(ANN_ALERT);
        net_send_event("ALERT_VITALS", "heart rate / SpO2 out of range", t, hr, spo2);
        break;
    default:
        break;
    }
    if (s_hook) {
        s_hook(t, ev);
    }
}

void pipeline_push_imu(const float sample[SC_CH])
{
    float y[SC_CH];
    sc_lpf_process(&s_lpf, sample, y);
    s_imu_count++;
    if (!sc_window_push(&s_win, y)) {
        return;
    }
    sc_window_quantize(&s_win, s_p->mean, s_p->std, s_p->in_scale, s_p->in_zp, s_q);
    const int64_t t0 = esp_timer_get_time();
    const float prob = fall_model_run(s_q, NULL);
    const uint32_t dt = (uint32_t)(esp_timer_get_time() - t0);

    taskENTER_CRITICAL(&s_mux);
    const bool press = s_press, reset = s_reset;
    s_press = s_reset = false;
    const float hr = s_hr, spo2 = s_spo2;
    s_last_prob = prob;
    s_windows++;
    s_infer_us_sum += dt;
    if (dt > s_infer_us_max) {
        s_infer_us_max = dt;
    }
    taskEXIT_CRITICAL(&s_mux);

    const double t = (double)s_imu_count / s_p->fs_hz;
    if (reset) {
        const bool was_alert = s_fsm.state == SC_STATE_ALERT;
        taskENTER_CRITICAL(&s_mux);
        sc_fsm_reset(&s_fsm);
        taskEXIT_CRITICAL(&s_mux);
        s_det.run = 0;
        annunciator_set(ANN_OFF);
        ESP_LOGW(TAG, "RESET t=%.2f alert cleared by long press", t);
        if (was_alert) {
            net_send_event("ALERT_CLEARED", "long press on device", t, hr, spo2);
        }
    }
    const bool fall = sc_detector_update(&s_det, prob);
    const float motion = sc_window_motion_std(&s_win, MOTION_N);
    taskENTER_CRITICAL(&s_mux);
    const sc_event_t ev = sc_fsm_step(&s_fsm, t, fall, motion, press, hr, spo2);
    taskEXIT_CRITICAL(&s_mux);
    if (ev != SC_EV_NONE) {
        on_event(t, ev, hr, spo2, prob, motion);
    }
}

void pipeline_push_ppg(uint32_t red, uint32_t ir)
{
    const uint32_t i = s_ppg_count % PPG_N;
    s_red[i] = (float)red;
    s_ir[i] = (float)ir;
    s_ppg_count++;
    if (s_ppg_count < PPG_N || s_ppg_count % SC_PPG_FS != 0) {
        return;   // re-estimate once per second once the window is full
    }
    const uint32_t start = s_ppg_count % PPG_N;   // oldest sample
    double ir_sum = 0.0;
    for (uint32_t k = 0; k < PPG_N; k++) {
        s_red_lin[k] = s_red[(start + k) % PPG_N];
        s_ir_lin[k] = s_ir[(start + k) % PPG_N];
        ir_sum += s_ir_lin[k];
    }
    float hr = NAN, spo2 = NAN;
    if (ir_sum / PPG_N >= CONFIG_SAFETY_FINGER_IR_MIN) {
        hr = sc_estimate_hr(s_ir_lin, PPG_N);
        spo2 = sc_estimate_spo2(s_red_lin, s_ir_lin, PPG_N);
    }
    taskENTER_CRITICAL(&s_mux);
    s_hr = hr;
    s_spo2 = spo2;
    taskEXIT_CRITICAL(&s_mux);
}
