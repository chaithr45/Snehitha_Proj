// Setup mode (Wi-Fi builds): the device opens its own Wi-Fi network "WorkerSafety-XXXX"
// with a setup page at http://192.168.4.1 (phones open it by themselves, like a hotel
// Wi-Fi login). There the user picks the Wi-Fi network, enters the supervisor's address
// and the worker's name, and can calibrate the sensor orientation. Saving restarts the
// device into normal operation.
//
// Entered on the first start (nothing configured yet), or by holding the button for 3 s
// within 10 s of power-on, or for 10 s at any time.
#pragma once

#include <stdbool.h>

// Never returns. imu_ok / ppg_ok: the sensors answered at start-up (shown on the page;
// the motion sensor is also needed for the orientation calibration).
void provision_run(bool imu_ok, bool ppg_ok);
