// Device settings kept in NVS (flash), so a prebuilt firmware can be configured without
// rebuilding: Wi-Fi network, supervisor address, worker name, device token and the
// sensor-orientation calibration. Set on the device's own setup page (provision.c);
// menuconfig values are only the defaults for an empty device.
#pragma once

#include <stdbool.h>

#include "esp_err.h"

typedef struct {
    char ssid[33];
    char pass[65];
    char server[128];     // e.g. "http://192.168.1.20:8080", no trailing slash
    char worker[33];      // worker / device name shown on the dashboard
    char token[65];       // optional shared secret (X-Device-Token)
    bool calibrated;
    bool net_verified;    // joined this Wi-Fi at least once since the settings were saved
    float rot[9];         // sensor frame -> KFall frame (row-major), identity if not calibrated
} settings_t;

void settings_init(void);                 // after nvs_flash_init()
const settings_t *settings_get(void);
const char *settings_device_id(void);     // "ws-xxxxxx" from the MAC address
bool settings_have_network(void);         // Wi-Fi name set (Wi-Fi builds)

esp_err_t settings_save_network(const char *ssid, const char *pass, const char *server, const char *worker,
                                const char *token);
esp_err_t settings_save_rotation(const float rot[9]);
esp_err_t settings_clear_rotation(void);

// The Wi-Fi network was joined: remember it (once), so later outages never send the
// device back to setup mode.
void settings_mark_network_ok(void);
// Freshly saved settings never worked: note it for the setup page and restart into setup.
void settings_join_failed_restart(void);
bool settings_take_join_failed(void);     // true once after such a restart

// Setup mode on the next start (button held, or chosen on the setup page), then restart.
void settings_request_setup_and_restart(void);
bool settings_take_setup_request(void);   // true once after such a request

// Keep only characters that are safe in JSON, URLs and the dashboard: letters, digits,
// space, '.', '_' and '-'. Returns dst.
char *settings_sanitize_name(char *dst, const char *src, int size);
