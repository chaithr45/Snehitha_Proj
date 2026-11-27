// Status LED (the on-board LED of an ESP32 DevKit, GPIO 2 by default).
//   setup mode            fast blink (5 per second)
//   joining Wi-Fi         double blink every second
//   online, supervisor OK short flash every 2 s
//   supervisor unreachable  slow blink (1 per second)
//   pre-alarm / alert     on (fast flicker during a pre-alarm)
#pragma once

typedef enum {
    LED_BOOT = 0,
    LED_SETUP,
    LED_CONNECTING,
    LED_ONLINE,
    LED_NO_SERVER,
} led_mode_t;

void status_led_init(void);
void status_led_set(led_mode_t mode);
