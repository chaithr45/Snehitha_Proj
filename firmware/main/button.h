// Worker button (the BOOT button of an ESP32 DevKit works too):
//   short press                    "I'm OK": cancels a pre-alarm
//   double press                   demo: play a synthetic fall (CONFIG_SAFETY_DEMO_FALL)
//   hold 3 s                       clear an alert (CONFIG_SAFETY_BUTTON_LONG_PRESS_MS)
//   hold 10 s, or 3 s right after power-on    setup mode (Wi-Fi builds)
#pragma once

void button_init(void);
