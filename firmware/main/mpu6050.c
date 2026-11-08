#include "mpu6050.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define ADDR 0x68
#define REG_SMPLRT_DIV 0x19
#define REG_CONFIG 0x1A
#define REG_GYRO_CONFIG 0x1B
#define REG_ACCEL_CONFIG 0x1C
#define REG_FIFO_EN 0x23
#define REG_INT_STATUS 0x3A
#define REG_USER_CTRL 0x6A
#define REG_PWR_MGMT_1 0x6B
#define REG_FIFO_COUNTH 0x72
#define REG_FIFO_R_W 0x74
#define REG_WHO_AM_I 0x75
#define FIFO_SAMPLE_BYTES 12   // accel xyz + gyro xyz, big endian int16
#define FIFO_SIZE 1024

static const char *TAG = "mpu6050";
static i2c_master_dev_handle_t s_dev;
static int s_axis[3] = {0, 1, 2};
static float s_sign[3] = {1, 1, 1};
static uint8_t s_buf[FIFO_SIZE];

static esp_err_t wr(uint8_t reg, uint8_t val)
{
    const uint8_t b[2] = {reg, val};
    return i2c_master_transmit(s_dev, b, 2, 100);
}

static esp_err_t rd(uint8_t reg, uint8_t *buf, size_t n)
{
    return i2c_master_transmit_receive(s_dev, &reg, 1, buf, n, 100);
}

static esp_err_t parse_axes(const char *axes)
{
    if (!axes || strlen(axes) != 6) {
        return ESP_ERR_INVALID_ARG;
    }
    int used = 0;
    for (int i = 0; i < 3; i++) {
        const char s = axes[2 * i], a = axes[2 * i + 1];
        if ((s != '+' && s != '-') || a < 'X' || a > 'Z' || (used & (1 << (a - 'X')))) {
            return ESP_ERR_INVALID_ARG;
        }
        used |= 1 << (a - 'X');
        s_axis[i] = a - 'X';
        s_sign[i] = s == '-' ? -1.0f : 1.0f;
    }
    // A mirror image (odd permutation x odd sign flips) is not a physical rotation:
    // the gyroscope would then need the opposite sign. Refuse it.
    int inversions = (s_axis[0] > s_axis[1]) + (s_axis[0] > s_axis[2]) + (s_axis[1] > s_axis[2]);
    float det = (inversions % 2 ? -1.0f : 1.0f) * s_sign[0] * s_sign[1] * s_sign[2];
    return det > 0 ? ESP_OK : ESP_ERR_INVALID_ARG;
}

esp_err_t mpu6050_init(i2c_master_bus_handle_t bus, const char *axes)
{
    if (parse_axes(axes) != ESP_OK) {
        ESP_LOGE(TAG, "invalid SAFETY_IMU_AXES \"%s\" (need a rotation like +X+Y+Z, +X-Y-Z)", axes);
        return ESP_ERR_INVALID_ARG;
    }
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ADDR,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &s_dev);
    if (err != ESP_OK) {
        return err;
    }
    if ((err = wr(REG_PWR_MGMT_1, 0x80)) != ESP_OK) {   // device reset
        ESP_LOGE(TAG, "no answer at 0x%02x: check wiring / pull-ups", ADDR);
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(100));
    uint8_t who = 0;
    rd(REG_WHO_AM_I, &who, 1);
    if (who != 0x68) {
        ESP_LOGW(TAG, "WHO_AM_I = 0x%02x (expected 0x68; some clones report 0x70/0x72)", who);
    }
    const struct { uint8_t reg, val; } init[] = {
        {REG_PWR_MGMT_1, 0x01},   // wake up, clock = PLL with X gyro
        {REG_CONFIG, 0x04},       // DLPF 21 Hz (acc) / 20 Hz (gyro): anti-aliasing for 50 Hz, gyro rate 1 kHz
        {REG_SMPLRT_DIV, 19},     // 1 kHz / (1 + 19) = 50 Hz
        {REG_GYRO_CONFIG, 0x18},  // +-2000 dps
        {REG_ACCEL_CONFIG, 0x18}, // +-16 g
        {REG_USER_CTRL, 0x04},    // FIFO reset
        {REG_USER_CTRL, 0x40},    // FIFO enable
        {REG_FIFO_EN, 0x78},      // gyro x/y/z + accel into the FIFO
    };
    for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); i++) {
        if ((err = wr(init[i].reg, init[i].val)) != ESP_OK) {
            return err;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    ESP_LOGI(TAG, "50 Hz, +-16 g, +-2000 dps, FIFO on, axes %s", axes);
    return ESP_OK;
}

void mpu6050_convert(const int16_t *raw, float out[6])
{
    float phys[6];
    for (int c = 0; c < 3; c++) {
        phys[c] = (float)((double)raw[c] / MPU6050_ACC_LSB_PER_G);
        phys[3 + c] = (float)((double)raw[3 + c] / MPU6050_GYR_LSB_PER_DPS);
    }
    for (int i = 0; i < 3; i++) {
        out[i] = s_sign[i] * phys[s_axis[i]];
        out[3 + i] = s_sign[i] * phys[3 + s_axis[i]];
    }
}

int mpu6050_read_fifo(float out[][6], int max)
{
    uint8_t cnt[2], status = 0;
    if (rd(REG_INT_STATUS, &status, 1) != ESP_OK || rd(REG_FIFO_COUNTH, cnt, 2) != ESP_OK) {
        return -1;
    }
    const int count = (cnt[0] << 8) | cnt[1];
    if ((status & 0x10) || count >= FIFO_SIZE) {   // FIFO_OFLOW_INT: samples were lost
        ESP_LOGW(TAG, "FIFO overflow, resetting (read the FIFO more often)");
        wr(REG_USER_CTRL, 0x44);
        return 0;
    }
    int n = count / FIFO_SAMPLE_BYTES;
    if (n > max) {
        n = max;
    }
    if (n == 0) {
        return 0;
    }
    if (rd(REG_FIFO_R_W, s_buf, (size_t)n * FIFO_SAMPLE_BYTES) != ESP_OK) {
        return -1;
    }
    for (int i = 0; i < n; i++) {
        const uint8_t *b = s_buf + i * FIFO_SAMPLE_BYTES;
        int16_t raw[6];
        for (int c = 0; c < 6; c++) {
            raw[c] = (int16_t)((b[2 * c] << 8) | b[2 * c + 1]);
        }
        mpu6050_convert(raw, out[i]);
    }
    return n;
}
