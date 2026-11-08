// Edge-AI worker safety monitor -- Phase 2 firmware (ESP32 / ESP32-S3).
//
//   MPU6050 (50 Hz) --> low-pass --> 2.56 s window --> int8 1D-CNN (TFLite Micro)
//                       --> 2 consecutive positive windows = fall suspected
//   MAX30102 (100 Hz) --> heart rate + SpO2 every second
//   alert state machine: buzzer/vibration, "I'm OK" button, post-fall inactivity,
//   vitals limits --> JSON alert to the supervisor over Wi-Fi.
#include "sdkconfig.h"
#include "annunciator.h"
#include "button.h"
#include "driver/i2c_master.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "fall_model.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "max30102.h"
#include "mpu6050.h"
#include "net.h"
#include "nvs_flash.h"
#include "pipeline.h"
#include "replay.h"

static const char *TAG = "app";

#if !CONFIG_SAFETY_REPLAY
#define IMU_FIFO_MAX 85   // MPU6050 FIFO: 1024 bytes / 12 bytes per sample
#define PPG_FIFO_MAX 32

static void imu_task(void *arg)
{
    (void)arg;
    static float buf[IMU_FIFO_MAX][6];
    uint32_t gap_ms = 0;
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        vTaskDelayUntil(&last, pdMS_TO_TICKS(100));   // ~5 samples per read; FIFO holds 1.7 s
        const int n = mpu6050_read_fifo(buf, IMU_FIFO_MAX);
        if (n <= 0) {
            if (n < 0) {
                ESP_LOGW(TAG, "MPU6050 read error");
            }
            gap_ms += 100;
            pipeline_report_imu_gap(gap_ms);   // >= 2 s without data: DEVICE_FAULT to the supervisor
            continue;
        }
        gap_ms = 0;
        for (int i = 0; i < n; i++) {
            pipeline_push_imu(buf[i]);
        }
    }
}

static void ppg_task(void *arg)
{
    (void)arg;
    static uint32_t red[PPG_FIFO_MAX], ir[PPG_FIFO_MAX];
    TickType_t last = xTaskGetTickCount();
    for (;;) {
        vTaskDelayUntil(&last, pdMS_TO_TICKS(100));   // ~10 samples per read; FIFO holds 0.32 s
        const int n = max30102_read_fifo(red, ir, PPG_FIFO_MAX);
        if (n < 0) {
            ESP_LOGW(TAG, "MAX30102 read error");
            continue;
        }
        for (int i = 0; i < n; i++) {
            pipeline_push_ppg(red[i], ir[i]);
        }
    }
}

static void start_sensors(void)
{
    const i2c_master_bus_config_t bus_cfg = {
        .i2c_port = -1,
        .sda_io_num = CONFIG_SAFETY_I2C_SDA,
        .scl_io_num = CONFIG_SAFETY_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,   // fit 4.7k external pull-ups for 400 kHz
    };
    i2c_master_bus_handle_t bus;
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &bus));

    if (mpu6050_init(bus, CONFIG_SAFETY_IMU_AXES) == ESP_OK) {
        xTaskCreate(imu_task, "imu", 6144, NULL, 10, NULL);
    } else {
        ESP_LOGE(TAG, "MPU6050 not available: FALL DETECTION DISABLED");
    }
    if (max30102_init(bus) == ESP_OK) {
        xTaskCreate(ppg_task, "ppg", 4096, NULL, 6, NULL);
    } else {
        ESP_LOGE(TAG, "MAX30102 not available: vitals monitoring disabled");
    }
}
#endif

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    ESP_LOGI(TAG, "worker safety monitor, device %s", CONFIG_SAFETY_WORKER_ID);
    ESP_ERROR_CHECK(fall_model_init());
    fall_model_selftest();
    pipeline_init();
    annunciator_init();
    net_init();
#if CONFIG_SAFETY_REPLAY
    ESP_LOGW(TAG, "REPLAY MODE: sensors are not read");
    replay_start();
#else
    button_init();
    start_sensors();
#endif
}
