#include "sdkconfig.h"
#include "settings.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "nvs.h"

#define NS "wsm"

#ifndef CONFIG_SAFETY_WIFI_SSID
#define CONFIG_SAFETY_WIFI_SSID ""
#define CONFIG_SAFETY_WIFI_PASSWORD ""
#endif
#ifndef CONFIG_SAFETY_SUPERVISOR_URL
#define CONFIG_SAFETY_SUPERVISOR_URL ""
#endif

static const char *TAG = "settings";
static settings_t s;
static char s_id[16];

static void get_str(nvs_handle_t h, const char *key, char *dst, size_t size, const char *dflt)
{
    size_t len = size;
    if (h == 0 || nvs_get_str(h, key, dst, &len) != ESP_OK) {
        strlcpy(dst, dflt, size);
    }
}

char *settings_sanitize_name(char *dst, const char *src, int size)
{
    int j = 0;
    for (int i = 0; src[i] && j < size - 1; i++) {
        const char c = src[i];
        dst[j++] = (isalnum((unsigned char)c) || c == ' ' || c == '.' || c == '_' || c == '-') ? c : '-';
    }
    while (j > 0 && dst[j - 1] == ' ') {
        j--;
    }
    dst[j] = 0;
    return dst;
}

static void identity(float r[9])
{
    memset(r, 0, 9 * sizeof(float));
    r[0] = r[4] = r[8] = 1.0f;
}

void settings_init(void)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_id, sizeof(s_id), "ws-%02x%02x%02x", mac[3], mac[4], mac[5]);

    nvs_handle_t h = 0;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) {
        h = 0;   // nothing saved yet: menuconfig defaults
    }
    get_str(h, "ssid", s.ssid, sizeof(s.ssid), CONFIG_SAFETY_WIFI_SSID);
    get_str(h, "pass", s.pass, sizeof(s.pass), CONFIG_SAFETY_WIFI_PASSWORD);
    get_str(h, "server", s.server, sizeof(s.server), CONFIG_SAFETY_SUPERVISOR_URL);
    char raw[33];
    get_str(h, "worker", raw, sizeof(raw), CONFIG_SAFETY_WORKER_ID);
    settings_sanitize_name(s.worker, raw[0] ? raw : s_id, sizeof(s.worker));
    get_str(h, "token", s.token, sizeof(s.token), "");
    size_t len = sizeof(s.rot);
    s.calibrated = h != 0 && nvs_get_blob(h, "rot", s.rot, &len) == ESP_OK && len == sizeof(s.rot);
    if (!s.calibrated) {
        identity(s.rot);
    }
    if (h) {
        nvs_close(h);
    }
    const size_t n = strlen(s.server);
    if (n > 0 && s.server[n - 1] == '/') {
        s.server[n - 1] = 0;
    }
    ESP_LOGI(TAG, "device %s, worker \"%s\", Wi-Fi \"%s\", supervisor %s, orientation %s", s_id, s.worker,
             s.ssid, s.server[0] ? s.server : "(not set)", s.calibrated ? "calibrated" : "default mounting");
}

const settings_t *settings_get(void) { return &s; }
const char *settings_device_id(void) { return s_id; }
bool settings_have_network(void) { return s.ssid[0] != 0 && s.server[0] != 0; }

static esp_err_t open_rw(nvs_handle_t *h)
{
    const esp_err_t err = nvs_open(NS, NVS_READWRITE, h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot open settings storage: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t settings_save_network(const char *ssid, const char *pass, const char *server, const char *worker,
                                const char *token)
{
    nvs_handle_t h;
    esp_err_t err = open_rw(&h);
    if (err != ESP_OK) {
        return err;
    }
    char name[33];
    settings_sanitize_name(name, worker, sizeof(name));
    err = nvs_set_str(h, "ssid", ssid);
    err = err == ESP_OK ? nvs_set_str(h, "pass", pass) : err;
    err = err == ESP_OK ? nvs_set_str(h, "server", server) : err;
    err = err == ESP_OK ? nvs_set_str(h, "worker", name) : err;
    err = err == ESP_OK ? nvs_set_str(h, "token", token) : err;
    err = err == ESP_OK ? nvs_commit(h) : err;
    nvs_close(h);
    if (err == ESP_OK) {
        strlcpy(s.ssid, ssid, sizeof(s.ssid));
        strlcpy(s.pass, pass, sizeof(s.pass));
        strlcpy(s.server, server, sizeof(s.server));
        strlcpy(s.worker, name[0] ? name : s_id, sizeof(s.worker));
        strlcpy(s.token, token, sizeof(s.token));
    }
    return err;
}

esp_err_t settings_save_rotation(const float rot[9])
{
    nvs_handle_t h;
    esp_err_t err = open_rw(&h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(h, "rot", rot, 9 * sizeof(float));
    err = err == ESP_OK ? nvs_commit(h) : err;
    nvs_close(h);
    if (err == ESP_OK) {
        memcpy(s.rot, rot, sizeof(s.rot));
        s.calibrated = true;
    }
    return err;
}

esp_err_t settings_clear_rotation(void)
{
    nvs_handle_t h;
    esp_err_t err = open_rw(&h);
    if (err != ESP_OK) {
        return err;
    }
    nvs_erase_key(h, "rot");
    err = nvs_commit(h);
    nvs_close(h);
    identity(s.rot);
    s.calibrated = false;
    return err;
}

void settings_request_setup_and_restart(void)
{
    nvs_handle_t h;
    if (open_rw(&h) == ESP_OK) {
        nvs_set_u8(h, "setup", 1);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGW(TAG, "restarting into setup mode");
    esp_restart();
}

bool settings_take_setup_request(void)
{
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    if (nvs_get_u8(h, "setup", &v) == ESP_OK && v) {
        nvs_erase_key(h, "setup");
        nvs_commit(h);
    }
    nvs_close(h);
    return v != 0;
}
