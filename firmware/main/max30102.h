// MAX30102 driver: SpO2 mode (red + IR), 100 samples/s (400 sps, 4x averaging),
// 18-bit, 411 us pulses, 4096 nA range. Samples are read from the 32-deep FIFO.
#pragma once

#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

esp_err_t max30102_init(i2c_master_bus_handle_t bus);

// Read the samples waiting in the FIFO (at most max). Returns the number of
// samples or -1 on an I2C error. Must be called at least every 0.3 s.
int max30102_read_fifo(uint32_t *red, uint32_t *ir, int max);
