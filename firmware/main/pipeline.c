#include "sdkconfig.h"
#include "pipeline.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "annunciator.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "fall_model.h"
#include "freertos/FreeRTOS.h"
#include "net.h"

#define PPG_N (SC_PPG_FS * CONFIG_SAFETY_PPG_WINDOW_S)
#define MOTION_N 50      // motion / posture over the last 1 s
#define STUCK_N 100      // identical raw IMU samples in a row (2 s) = sensor frozen
#define IMU_GAP_MS 2000  // no IMU data for this long = sensor fault

_Static_assert(PPG_N <= SC_PPG_MAX_N, "PPG window too long for safety_core");

static const char *TAG = "pipeline";

static const fall_params_t *s_p;
static sc_lpf_t s_lpf;
static sc_window_t s_win;
static sc_detector_t s_det;
static sc_fsm_t s_fsm;
static int8_t s_q[SC_WIN * SC_CH];
static uint32_t s_imu_count;

// raw |a| of the last window (impact strength) and frozen-sensor monitor
static float s_raw_mag[SC_WIN];
static float s_last_raw[SC_CH];
static uint32_t s_same;
static float s_impact_g;

static float s_red[PPG_N], s_ir[PPG_N];
static float s_red_lin[PPG_N], s_ir_lin[PPG_N];
static uint32_t s_ppg_count;

// shared between the IMU task, the PPG task, the button task and the net task
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static float s_hr = NAN, s_spo2 = NAN, s_last_prob, s_tilt, s_motion;
static bool s_worn = true, s_press, s_reset, s_fault, s_acked;
static uint32_t s_windows, s_infer_us_max, s_alert_id;
static uint64_t s_infer_us_sum;
static pipeline_event_hook_t s_hook;

void pipeline_init(void)
{
    s_p = fall_model_params();
    sc_lpf_init(&s_lpf, s_p->lpf_sos, s_p->lpf_zi, s_p->lpf_sections);
    sc_window_init(&s_win, s_p->hop);
    sc_detector_init(&s_det, s_p->threshold, s_p->consec);
    sc_fsm_cfg_t cfg;
    sc_fsm_cfg_default(&cfg);
    cfg.md_lying_s = CONFIG_SAFETY_MAN_DOWN_LYING_S;
    cfg.md_upright_s = CONFIG_SAFETY_MAN_DOWN_UPRIGHT_S;
    cfg.vit_motion_g = CONFIG_SAFETY_VITALS_MOTION_MG / 1000.0;
    sc_fsm_init(&s_fsm, &cfg);
    ESP_LOGI(TAG, "ready: %d Hz IMU, window %d, decision every %d samples, threshold %.2f; vitals over %d s",
             s_p->fs_hz, s_p->win, s_p->hop, (double)s_p->threshold, CONFIG_SAFETY_PPG_WINDOW_S);
    ESP_LOGI(TAG, "man-down after %d s lying / %d s upright motionless; vitals paused above %d mg motion",
             CONFIG_SAFETY_MAN_DOWN_LYING_S, CONFIG_SAFETY_MAN_DOWN_UPRIGHT_S, CONFIG_SAFETY_VITALS_MOTION_MG);
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

void pipeline_on_ack(uint32_t alert_id)
{
    bool now_acked = false;
    taskENTER_CRITICAL(&s_mux);
    if (alert_id != 0 && alert_id == s_alert_id && s_fsm.state == SC_STATE_ALERT && !s_acked) {
        s_acked = now_acked = true;
    }
    taskEXIT_CRITICAL(&s_mux);
    if (now_acked) {
        annunciator_set(ANN_ACKED);   // "help is on the way"
        ESP_LOGW(TAG, "ACK: supervisor acknowledged alert %lu", (unsigned long)alert_id);
    }
}

void pipeline_get_status(pipeline_status_t *out)
{
    taskENTER_CRITICAL(&s_mux);
    out->state = s_fsm.state;
    out->hr = s_hr;
    out->spo2 = s_spo2;
    out->worn = s_worn;
    out->vitals_reliable = sc_fsm_vitals_reliable(&s_fsm);
    out->sensor_fault = s_fault;
    out->acked = s_acked;
    out->tilt_deg = s_tilt;
    out->motion_g = s_motion;
    out->last_prob = s_last_prob;
    out->windows = s_windows;
    out->infer_us_avg = s_windows ? (uint32_t)(s_infer_us_sum / s_windows) : 0;
    out->infer_us_max = s_infer_us_max;
    out->active_alert_id = s_fsm.state == SC_STATE_ALERT ? s_alert_id : 0;
    taskEXIT_CRITICAL(&s_mux);
}

static void notify(double t, sc_event_t ev)
{
    if (s_hook) {
        s_hook(t, ev);
    }
}

static void set_fault(double t, bool fault, const char *why)
{
    if (fault == s_fault) {
        return;
    }
    taskENTER_CRITICAL(&s_mux);
    s_fault = fault;
    taskEXIT_CRITICAL(&s_mux);
    ESP_LOGW(TAG, "EVENT t=%.2f %s (%s)", t, fault ? "SENSOR_FAULT" : "SENSOR_OK", why);
    char extra[64];
    snprintf(extra, sizeof(extra), "\"fall_detection\":\"%s\"", fault ? "unavailable" : "restored");
    net_send_alert(fault ? "DEVICE_FAULT" : "DEVICE_OK", why, t, s_hr, s_spo2, extra);
    notify(t, fault ? SC_EV_SENSOR_FAULT : SC_EV_SENSOR_OK);
}

void pipeline_report_imu_gap(uint32_t ms)
{
    if (ms >= IMU_GAP_MS) {
        set_fault((double)s_imu_count / s_p->fs_hz, true, "motion sensor not responding");
    }
}

static float max_raw_g(void)
{
    float m = 0.0f;
    for (int i = 0; i < SC_WIN; i++) {
        m = s_raw_mag[i] > m ? s_raw_mag[i] : m;
    }
    return m;
}

static void on_event(double t, sc_event_t ev, const sc_fsm_input_t *in, float prob)
{
    ESP_LOGW(TAG, "EVENT t=%.2f %s (p=%.3f motion=%.3f g tilt=%.0f deg hr=%.0f spo2=%.0f)", t, sc_event_name(ev),
             (double)prob, (double)in->motion_std_g, (double)in->tilt_deg, (double)in->hr, (double)in->spo2);
    char extra[160];
    const char *posture = in->tilt_deg > s_fsm.cfg.lying_deg ? "lying" : "upright";
    snprintf(extra, sizeof(extra), "\"posture\":\"%s\",\"tilt_deg\":%.0f,\"motion_g\":%.3f", posture,
             (double)in->tilt_deg, (double)in->motion_std_g);
    switch (ev) {
    case SC_EV_FALL_SUSPECTED:
        s_impact_g = max_raw_g();
        annunciator_set(ANN_PENDING);
        break;
    case SC_EV_MAN_DOWN_SUSPECTED:
        annunciator_set(ANN_PENDING);
        break;
    case SC_EV_CANCELLED:
    case SC_EV_MOVED:
        annunciator_set(ANN_OFF);
        break;
    case SC_EV_ALERT_FALL_INACTIVE:
    case SC_EV_ALERT_FALL_TIMEOUT: {
        char fx[224];
        snprintf(fx, sizeof(fx), "%s,\"impact_g\":%.1f", extra, (double)s_impact_g);
        annunciator_set(ANN_ALERT);
        s_alert_id = net_send_alert("ALERT_FALL",
                                    ev == SC_EV_ALERT_FALL_INACTIVE ? "post-fall inactivity" : "no cancel in time",
                                    t, in->hr, in->spo2, fx);
        break;
    }
    case SC_EV_ALERT_VITALS:
        annunciator_set(ANN_ALERT);
        s_alert_id = net_send_alert("ALERT_VITALS", "heart rate / SpO2 out of range", t, in->hr, in->spo2, extra);
        break;
    case SC_EV_ALERT_MAN_DOWN:
        annunciator_set(ANN_ALERT);
        s_alert_id = net_send_alert("ALERT_MAN_DOWN", "no movement, no response to pre-alarm", t, in->hr,
                                    in->spo2, extra);
        break;
    default:
        break;
    }
    if (sc_event_is_alert(ev)) {
        taskENTER_CRITICAL(&s_mux);
        s_acked = false;
        taskEXIT_CRITICAL(&s_mux);
    }
    notify(t, ev);
}

void pipeline_push_imu(const float sample[SC_CH])
{
    float y[SC_CH];
    sc_lpf_process(&s_lpf, sample, y);
    s_raw_mag[s_imu_count % SC_WIN] = sqrtf(sample[0] * sample[0] + sample[1] * sample[1] + sample[2] * sample[2]);
    s_imu_count++;
    const double t = (double)s_imu_count / s_p->fs_hz;

    // frozen-sensor monitor (real sensor noise never repeats a sample exactly)
    s_same = memcmp(sample, s_last_raw, sizeof(s_last_raw)) == 0 ? s_same + 1 : 1;
    memcpy(s_last_raw, sample, sizeof(s_last_raw));
    if (!s_fault && s_same >= STUCK_N) {
        set_fault(t, true, "motion sensor data frozen");
    } else if (s_fault && s_same == 1) {
        set_fault(t, false, "motion sensor data back");
    }

    if (!sc_window_push(&s_win, y)) {
        return;
    }
    sc_window_quantize(&s_win, s_p->mean, s_p->std, s_p->in_scale, s_p->in_zp, s_q);
    const int64_t t0 = esp_timer_get_time();
    const float prob = fall_model_run(s_q, NULL);
    const uint32_t dt = (uint32_t)(esp_timer_get_time() - t0);

    sc_fsm_input_t in;
    taskENTER_CRITICAL(&s_mux);
    in.button = s_press;
    const bool reset = s_reset;
    s_press = s_reset = false;
    in.hr = s_hr;
    in.spo2 = s_spo2;
    in.worn = s_worn;
    s_last_prob = prob;
    s_windows++;
    s_infer_us_sum += dt;
    if (dt > s_infer_us_max) {
        s_infer_us_max = dt;
    }
    taskEXIT_CRITICAL(&s_mux);

    if (reset) {
        const bool was_alert = s_fsm.state == SC_STATE_ALERT;
        taskENTER_CRITICAL(&s_mux);
        sc_fsm_reset(&s_fsm);
        s_acked = false;
        taskEXIT_CRITICAL(&s_mux);
        s_det.run = 0;
        annunciator_set(ANN_OFF);
        ESP_LOGW(TAG, "RESET t=%.2f alert cleared by long press", t);
        if (was_alert) {
            net_send_alert("ALERT_CLEARED", "long press on device", t, in.hr, in.spo2, NULL);
        }
    }
    in.fall = sc_detector_update(&s_det, prob);
    in.motion_std_g = sc_window_motion_std(&s_win, MOTION_N);
    in.tilt_deg = sc_window_tilt_deg(&s_win, MOTION_N);
    taskENTER_CRITICAL(&s_mux);
    const sc_event_t ev = sc_fsm_step(&s_fsm, t, &in);
    s_tilt = in.tilt_deg;
    s_motion = in.motion_std_g;
    taskEXIT_CRITICAL(&s_mux);
    if (ev != SC_EV_NONE) {
        on_event(t, ev, &in, prob);
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
    const bool worn = ir_sum / PPG_N >= CONFIG_SAFETY_FINGER_IR_MIN;
    float hr = NAN, spo2 = NAN;
    if (worn) {
        hr = sc_estimate_hr(s_ir_lin, PPG_N);
        spo2 = sc_estimate_spo2(s_red_lin, s_ir_lin, PPG_N);
    }
    taskENTER_CRITICAL(&s_mux);
    s_hr = hr;
    s_spo2 = spo2;
    s_worn = worn;
    taskEXIT_CRITICAL(&s_mux);
}
