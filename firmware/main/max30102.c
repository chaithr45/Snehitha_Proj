#include "max30102.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define ADDR 0x57
#define REG_INT_ENABLE_1 0x02
#define REG_INT_ENABLE_2 0x03
#define REG_FIFO_WR_PTR 0x04
#define REG_OVF_COUNTER 0x05
#define REG_FIFO_RD_PTR 0x06
#define REG_FIFO_DATA 0x07
#define REG_FIFO_CONFIG 0x08
#define REG_MODE_CONFIG 0x09
#define REG_SPO2_CONFIG 0x0A
#define REG_LED1_PA 0x0C   // red
#define REG_LED2_PA 0x0D   // IR
#define REG_PART_ID 0xFF
#define FIFO_DEPTH 32

static const char *TAG = "max30102";
static i2c_master_dev_handle_t s_dev;
static uint8_t s_buf[FIFO_DEPTH * 6];

static esp_err_t wr(uint8_t reg, uint8_t val)
{
    const uint8_t b[2] = {reg, val};
    return i2c_master_transmit(s_dev, b, 2, 100);
}

static esp_err_t rd(uint8_t reg, uint8_t *buf, size_t n)
{
    return i2c_master_transmit_receive(s_dev, &reg, 1, buf, n, 100);
}

esp_err_t max30102_init(i2c_master_bus_handle_t bus)
{
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ADDR,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &s_dev);
    if (err != ESP_OK) {
        return err;
    }
    if ((err = wr(REG_MODE_CONFIG, 0x40)) != ESP_OK) {   // reset
        ESP_LOGE(TAG, "no answer at 0x%02x: check wiring / pull-ups", ADDR);
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
    uint8_t id = 0;
    rd(REG_PART_ID, &id, 1);
    if (id != 0x15) {
        ESP_LOGW(TAG, "PART_ID = 0x%02x (expected 0x15)", id);
    }
    const struct { uint8_t reg, val; } init[] = {
        {REG_INT_ENABLE_1, 0x00},
        {REG_INT_ENABLE_2, 0x00},
        {REG_FIFO_WR_PTR, 0x00},
        {REG_OVF_COUNTER, 0x00},
        {REG_FIFO_RD_PTR, 0x00},
        {REG_FIFO_CONFIG, 0x5F},   // 4-sample averaging, roll over when full
        {REG_SPO2_CONFIG, 0x2F},   // 4096 nA range, 400 sps (-> 100 Hz after averaging), 411 us / 18 bit
        {REG_LED1_PA, 0x24},       // ~7 mA red
        {REG_LED2_PA, 0x24},       // ~7 mA IR
        {REG_MODE_CONFIG, 0x03},   // SpO2 mode: red + IR
    };
    for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); i++) {
        if ((err = wr(init[i].reg, init[i].val)) != ESP_OK) {
            return err;
        }
    }
    ESP_LOGI(TAG, "SpO2 mode, 100 Hz, 18 bit");
    return ESP_OK;
}

int max30102_read_fifo(uint32_t *red, uint32_t *ir, int max)
{
    uint8_t wp = 0, rp = 0, ovf = 0;
    if (rd(REG_FIFO_WR_PTR, &wp, 1) != ESP_OK || rd(REG_OVF_COUNTER, &ovf, 1) != ESP_OK ||
        rd(REG_FIFO_RD_PTR, &rp, 1) != ESP_OK) {
        return -1;
    }
    int n = (wp - rp) & (FIFO_DEPTH - 1);
    if (n == 0 && ovf) {
        n = FIFO_DEPTH;   // FIFO completely full
        ESP_LOGW(TAG, "FIFO overflow: %u samples lost", ovf);
    }
    if (n > max) {
        n = max;
    }
    if (n == 0) {
        return 0;
    }
    if (rd(REG_FIFO_DATA, s_buf, (size_t)n * 6) != ESP_OK) {
        return -1;
    }
    for (int i = 0; i < n; i++) {
        const uint8_t *b = s_buf + 6 * i;   // LED1 (red) then LED2 (IR), 3 bytes each
        red[i] = (((uint32_t)b[0] << 16) | ((uint32_t)b[1] << 8) | b[2]) & 0x3FFFF;
        ir[i] = (((uint32_t)b[3] << 16) | ((uint32_t)b[4] << 8) | b[5]) & 0x3FFFF;
    }
    return n;
}
