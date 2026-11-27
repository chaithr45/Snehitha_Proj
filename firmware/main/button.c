#include "sdkconfig.h"
#include "button.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pipeline.h"
#include "settings.h"

#if CONFIG_SAFETY_BUTTON_GPIO >= 0

#define POLL_MS 20
#define DEBOUNCE_MS 40
#define DOUBLE_PRESS_US 600000          // second short press within 0.6 s = double press
#define SETUP_HOLD_MS 10000             // hold 10 s at any time = setup mode
#define SETUP_HOLD_EARLY_MS 3000        // or 3 s, when the press starts in the first 10 s after power-on
#define EARLY_US 10000000LL

static const char *TAG = "button";

static void task(void *arg)
{
    (void)arg;
    const gpio_num_t pin = (gpio_num_t)CONFIG_SAFETY_BUTTON_GPIO;
    uint32_t held_ms = 0;
    bool long_sent = false, early = false;
    int64_t last_short_us = -10 * DOUBLE_PRESS_US;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        const int64_t now = esp_timer_get_time();
        if (gpio_get_level(pin) == 0) {   // pressed (active low)
            if (held_ms == 0) {
                early = now < EARLY_US;
            }
            held_ms += POLL_MS;
            if (!long_sent && held_ms >= CONFIG_SAFETY_BUTTON_LONG_PRESS_MS) {
                long_sent = true;
                ESP_LOGI(TAG, "long press: clear alert");
                pipeline_request_reset();
            }
#if CONFIG_SAFETY_NET_WIFI
            if (held_ms >= SETUP_HOLD_MS || (early && held_ms >= SETUP_HOLD_EARLY_MS)) {
                ESP_LOGW(TAG, "button held: setup mode");
                settings_request_setup_and_restart();
            }
#endif
        } else {
            if (held_ms >= DEBOUNCE_MS && !long_sent) {
                if (now - last_short_us < DOUBLE_PRESS_US) {
#if CONFIG_SAFETY_DEMO_FALL
                    ESP_LOGI(TAG, "double press: demo fall");
                    pipeline_demo_fall();
#endif
                    last_short_us = -10 * DOUBLE_PRESS_US;
                } else {
                    ESP_LOGI(TAG, "press: I'm OK");
                    pipeline_button_press();
                    last_short_us = now;
                }
            }
            held_ms = 0;
            long_sent = false;
        }
    }
}

void button_init(void)
{
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << CONFIG_SAFETY_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&cfg);
    xTaskCreate(task, "button", 3072, NULL, 4, NULL);
}

#else

void button_init(void) {}

#endif
