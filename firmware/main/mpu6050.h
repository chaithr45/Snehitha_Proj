// MPU6050 driver: 50 Hz, +-16 g, +-2000 dps, samples read from the on-chip FIFO
// (no drift, no lost samples as long as it is read at least every ~1.5 s).
#pragma once

#include "driver/i2c_master.h"
#include "esp_err.h"

#define MPU6050_ACC_LSB_PER_G 2048.0    // +-16 g
#define MPU6050_GYR_LSB_PER_DPS 16.4    // +-2000 deg/s

// axes: CONFIG_SAFETY_IMU_AXES string, e.g. "+X+Y+Z" or "+X-Y-Z"
esp_err_t mpu6050_init(i2c_master_bus_handle_t bus, const char *axes);

// Convert raw counts (ax ay az gx gy gz) to g / deg/s in the KFall frame.
void mpu6050_convert(const int16_t raw[6], float out[6]);

// Read all complete samples waiting in the FIFO (at most max). Returns the number
// of samples, or -1 on an I2C error. A FIFO overflow resets the FIFO and returns 0.
int mpu6050_read_fifo(float out[][6], int max);
