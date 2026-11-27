#include "sdkconfig.h"
#include "status_led.h"

#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "pipeline.h"

#if CONFIG_SAFETY_LED_GPIO >= 0

#define TICK_MS 50

static volatile led_mode_t s_mode = LED_BOOT;

static void task(void *arg)
{
    (void)arg;
    uint32_t tick = 0;
    pipeline_status_t st;
    for (;;) {
        const uint32_t ms = tick * TICK_MS;
        tick++;
        int on;
        const bool running = s_mode != LED_SETUP && s_mode != LED_BOOT;   // pipeline started
        if (running) {
            pipeline_get_status(&st);
        } else {
            st.state = SC_STATE_NORMAL;
        }
        if (running && st.state == SC_STATE_ALERT) {
            on = 1;
        } else if (running && st.state != SC_STATE_NORMAL) {
            on = (ms % 200) < 100;
        } else {
            switch (s_mode) {
            case LED_SETUP: on = (ms % 200) < 100; break;
            case LED_CONNECTING: on = (ms % 1000) < 100 || ((ms % 1000) >= 200 && (ms % 1000) < 300); break;
            case LED_ONLINE: on = (ms % 2000) < 60; break;
            case LED_NO_SERVER: on = (ms % 1000) < 500; break;
            default: on = 1; break;
            }
        }
        gpio_set_level((gpio_num_t)CONFIG_SAFETY_LED_GPIO, on);
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));
    }
}

void status_led_init(void)
{
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << CONFIG_SAFETY_LED_GPIO,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&cfg);
    xTaskCreate(task, "led", 2048, NULL, 2, NULL);
}

void status_led_set(led_mode_t mode) { s_mode = mode; }

#else

void status_led_init(void) {}
void status_led_set(led_mode_t mode) { (void)mode; }

#endif
