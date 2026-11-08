#include "sdkconfig.h"
#include "replay.h"

#if CONFIG_SAFETY_REPLAY

#include <math.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "fall_model.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mpu6050.h"
#include "net.h"
#include "pipeline.h"
#include "replay_data.h"

#define MAX_EVENTS 32
#define N_OF(a) (sizeof(a) / sizeof((a)[0]))

static const char *TAG = "replay";
static struct {
    double t;
    sc_event_t ev;
} s_seen[MAX_EVENTS];
static int s_n_seen;
static int s_alerts_sent;

static void hook(double t, sc_event_t ev)
{
    if (s_n_seen < MAX_EVENTS) {
        s_seen[s_n_seen].t = t;
        s_seen[s_n_seen].ev = ev;
    }
    s_n_seen++;
    if (ev == SC_EV_ALERT_FALL_INACTIVE || ev == SC_EV_ALERT_FALL_TIMEOUT || ev == SC_EV_ALERT_VITALS) {
        s_alerts_sent++;
    }
}

static void task(void *arg)
{
    (void)arg;
    pipeline_set_event_hook(hook);
    size_t seg = 0, act = 0;
    int clears = 0;
    pipeline_status_t st;
    ESP_LOGI(TAG, "replaying %d s of sensor data (%d IMU + %d PPG samples)", REPLAY_IMU_N / 50, REPLAY_IMU_N,
             REPLAY_PPG_N);
    const int64_t t0 = esp_timer_get_time();
    for (uint32_t k = 0; k < REPLAY_IMU_N; k++) {
        if (seg < N_OF(REPLAY_SEGMENTS) && REPLAY_SEGMENTS[seg].imu_index == k) {
            ESP_LOGI(TAG, "SEGMENT t=%.2f %s", k / 50.0, REPLAY_SEGMENTS[seg].name);
            seg++;
        }
        while (act < N_OF(REPLAY_ACTIONS) && REPLAY_ACTIONS[act].imu_index == k) {
            if (REPLAY_ACTIONS[act].action == REPLAY_PRESS) {
                ESP_LOGI(TAG, "BUTTON t=%.2f short press (I'm OK)", k / 50.0);
                pipeline_button_press();
            } else {
                pipeline_get_status(&st);
                clears += st.state == SC_STATE_ALERT;   // the clear is reported to the supervisor
                ESP_LOGI(TAG, "BUTTON t=%.2f long press (clear alert)", k / 50.0);
                pipeline_request_reset();
            }
            act++;
        }
        pipeline_push_ppg(REPLAY_PPG[2 * k][0], REPLAY_PPG[2 * k][1]);
        pipeline_push_ppg(REPLAY_PPG[2 * k + 1][0], REPLAY_PPG[2 * k + 1][1]);
        float s[6];
        mpu6050_convert(REPLAY_IMU[k], s);   // same conversion as live MPU6050 data
        pipeline_push_imu(s);
        if (k % 25 == 0) {
            vTaskDelay(1);   // let the network / annunciator tasks run
        }
    }
    const double wall_s = (double)(esp_timer_get_time() - t0) / 1e6;

    // ---- compare with the Python model of the device ----
    const int n_exp = (int)N_OF(REPLAY_EXPECTED);
    bool events_ok = s_n_seen == n_exp;
    for (int i = 0; i < n_exp && i < s_n_seen && i < MAX_EVENTS; i++) {
        const bool ok = s_seen[i].ev == (sc_event_t)REPLAY_EXPECTED[i].event &&
                        fabs(s_seen[i].t - REPLAY_EXPECTED[i].t) < 0.011;
        events_ok &= ok;
        ESP_LOGI(TAG, "  %s firmware t=%.2f %-34s | python t=%.2f %s", ok ? "ok  " : "DIFF", s_seen[i].t,
                 sc_event_name(s_seen[i].ev), (double)REPLAY_EXPECTED[i].t,
                 sc_event_name((sc_event_t)REPLAY_EXPECTED[i].event));
    }
    ESP_LOGI(TAG, "events: firmware %d, python %d -> %s", s_n_seen, n_exp, events_ok ? "match" : "MISMATCH");

    bool net_ok = true;
#if !CONFIG_SAFETY_NET_NONE
    const int expected_msgs = s_alerts_sent + clears;
    net_flush(120000);
    net_ok = (int)net_alerts_delivered() == expected_msgs;
    ESP_LOGI(TAG, "supervisor messages delivered: %lu of %d (alerts + alert-cleared)",
             (unsigned long)net_alerts_delivered(), expected_msgs);
#endif
    const bool model_ok = fall_model_selftest();
    pipeline_get_status(&st);
    ESP_LOGI(TAG, "%lu model windows, inference avg %lu us / max %lu us, arena %u bytes, replay took %.1f s",
             (unsigned long)st.windows, (unsigned long)st.infer_us_avg, (unsigned long)st.infer_us_max,
             (unsigned)fall_model_arena_used(), wall_s);
    const bool pass = events_ok && net_ok && model_ok;
    ESP_LOGI(TAG, "REPLAY RESULT: %s", pass ? "PASS" : "FAIL");
    vTaskDelete(NULL);
}

void replay_start(void) { xTaskCreate(task, "replay", 8192, NULL, 5, NULL); }

#else

void replay_start(void) {}

#endif
