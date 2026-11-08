#include "sdkconfig.h"
#include "annunciator.h"

#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TICK_MS 50

static volatile ann_mode_t s_mode = ANN_OFF;

static void set_pin(int gpio, int level)
{
    if (gpio >= 0) {
        gpio_set_level((gpio_num_t)gpio, level);
    }
}

static void task(void *arg)
{
    (void)arg;
    uint32_t tick = 0;
    for (;;) {
        const uint32_t ms = (tick++ * TICK_MS) % 1000;
        int buzz = 0, vib = 0;
        switch (s_mode) {
        case ANN_PENDING:   // 200 ms beep every 500 ms, continuous vibration
            buzz = (ms % 500) < 200;
            vib = 1;
            break;
        case ANN_ALERT:     // 100 ms on / 100 ms off, vibration pulses
            buzz = (ms % 200) < 100;
            vib = ms < 500;
            break;
        default:
            break;
        }
        set_pin(CONFIG_SAFETY_BUZZER_GPIO, buzz);
        set_pin(CONFIG_SAFETY_VIBRATION_GPIO, vib);
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));
    }
}

static void init_output(int gpio)
{
    if (gpio < 0) {
        return;   // not fitted
    }
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << gpio,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&cfg);
    gpio_set_level((gpio_num_t)gpio, 0);
}

void annunciator_init(void)
{
    init_output(CONFIG_SAFETY_BUZZER_GPIO);
    init_output(CONFIG_SAFETY_VIBRATION_GPIO);
    xTaskCreate(task, "annunciator", 2048, NULL, 3, NULL);
}

void annunciator_set(ann_mode_t mode) { s_mode = mode; }
