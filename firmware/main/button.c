#include "sdkconfig.h"
#include "button.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pipeline.h"

#if CONFIG_SAFETY_BUTTON_GPIO >= 0

#define POLL_MS 20
#define DEBOUNCE_MS 40

static const char *TAG = "button";

static void task(void *arg)
{
    (void)arg;
    const gpio_num_t pin = (gpio_num_t)CONFIG_SAFETY_BUTTON_GPIO;
    uint32_t held_ms = 0;
    bool long_sent = false;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        if (gpio_get_level(pin) == 0) {   // pressed (active low)
            held_ms += POLL_MS;
            if (!long_sent && held_ms >= CONFIG_SAFETY_BUTTON_LONG_PRESS_MS) {
                long_sent = true;
                ESP_LOGI(TAG, "long press: clear alert");
                pipeline_request_reset();
            }
        } else {
            if (held_ms >= DEBOUNCE_MS && !long_sent) {
                ESP_LOGI(TAG, "press: I'm OK");
                pipeline_button_press();
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
    xTaskCreate(task, "button", 2560, NULL, 4, NULL);
}

#else

void button_init(void) {}

#endif
