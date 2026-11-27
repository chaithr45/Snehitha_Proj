#include "sdkconfig.h"
#include "provision.h"

#if CONFIG_SAFETY_NET_WIFI

#include <ctype.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "mpu6050.h"
#include "settings.h"
#include "status_led.h"

#define MAX_APS 20
#define FORM_MAX 1024
#define IMU_MAX 85

static const char *TAG = "setup";
static bool s_imu_ok, s_ppg_ok;
static char s_ap_ssid[33];
static wifi_ap_record_t s_aps[MAX_APS];
static uint16_t s_ap_n;
static float s_up[3];
static bool s_have_up;
static char s_msg[240];

// ---------------------------------------------------------------- captive DNS
// Answers every name with 192.168.4.1, so a phone that joins the setup network shows the
// setup page as its "sign in to network" page.
static void dns_task(void *arg)
{
    (void)arg;
    const int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(53);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (sock < 0 || bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "DNS server could not start; open http://192.168.4.1 by hand");
        vTaskDelete(NULL);
        return;
    }
    static uint8_t buf[512];
    for (;;) {
        struct sockaddr_in from;
        socklen_t flen = sizeof(from);
        const int n = recvfrom(sock, buf, sizeof(buf) - 16, 0, (struct sockaddr *)&from, &flen);
        if (n < 12 || (buf[2] & 0x80) || ((buf[4] << 8) | buf[5]) != 1) {
            continue;   // not a query with exactly one question
        }
        int p = 12;
        while (p < n && buf[p] != 0) {
            p += buf[p] + 1;   // skip the labels of the name
        }
        if (p + 5 > n) {
            continue;
        }
        const uint16_t qtype = (uint16_t)((buf[p + 1] << 8) | buf[p + 2]);
        p += 5;                                // end of name, QTYPE, QCLASS
        buf[2] = (uint8_t)(0x84 | (buf[2] & 0x01));   // response, authoritative, keep RD
        buf[3] = 0x00;
        buf[6] = 0;
        buf[7] = qtype == 1 ? 1 : 0;           // one A record, nothing for other types
        memset(buf + 8, 0, 4);                 // no authority / additional records
        if (qtype == 1) {
            static const uint8_t ans[16] = {0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, 30, 0, 4, 192, 168, 4, 1};
            memcpy(buf + p, ans, sizeof(ans));
            p += sizeof(ans);
        }
        sendto(sock, buf, p, 0, (struct sockaddr *)&from, flen);
    }
}

// ---------------------------------------------------------------- helpers
static void html_esc(char *dst, size_t size, const char *src)
{
    size_t j = 0;
    for (; *src && j + 7 < size; src++) {
        const char *rep = NULL;
        switch (*src) {
        case '&': rep = "&amp;"; break;
        case '<': rep = "&lt;"; break;
        case '>': rep = "&gt;"; break;
        case '"': rep = "&quot;"; break;
        case '\'': rep = "&#39;"; break;
        default: break;
        }
        if (rep) {
            const size_t k = strlen(rep);
            memcpy(dst + j, rep, k);
            j += k;
        } else {
            dst[j++] = *src;
        }
    }
    dst[j] = 0;
}

static void url_decode(char *s)
{
    char *o = s;
    for (; *s; s++) {
        if (*s == '+') {
            *o++ = ' ';
        } else if (*s == '%' && isxdigit((unsigned char)s[1]) && isxdigit((unsigned char)s[2])) {
            const char hex[3] = {s[1], s[2], 0};
            *o++ = (char)strtol(hex, NULL, 16);
            s += 2;
        } else {
            *o++ = *s;
        }
    }
    *o = 0;
}

static void field(const char *body, const char *key, char *out, size_t size)
{
    if (httpd_query_key_value(body, key, out, size) != ESP_OK) {
        out[0] = 0;
    }
    url_decode(out);
}

static int read_form(httpd_req_t *r, char *buf)
{
    const int total = r->content_len < FORM_MAX - 1 ? (int)r->content_len : FORM_MAX - 1;
    int got = 0;
    while (got < total) {
        const int n = httpd_req_recv(r, buf + got, total - got);
        if (n <= 0) {
            return -1;
        }
        got += n;
    }
    buf[got] = 0;
    return got;
}

static void put(httpd_req_t *r, const char *s) { httpd_resp_sendstr_chunk(r, s); }

static void putf(httpd_req_t *r, const char *fmt, ...)
{
    char b[600];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    httpd_resp_sendstr_chunk(r, b);
}

static esp_err_t redirect_home(httpd_req_t *r)
{
    httpd_resp_set_status(r, "303 See Other");
    httpd_resp_set_hdr(r, "Location", "http://192.168.4.1/");
    return httpd_resp_send(r, NULL, 0);
}

static void do_restart(void *arg)
{
    (void)arg;
    esp_restart();
}

static void restart_soon(void)
{
    static esp_timer_handle_t t;
    const esp_timer_create_args_t args = {.callback = do_restart, .name = "restart"};
    if (!t && esp_timer_create(&args, &t) != ESP_OK) {
        esp_restart();
    }
    esp_timer_start_once(t, 1500 * 1000);
}

static void scan(void)
{
    s_ap_n = 0;
    if (esp_wifi_scan_start(NULL, true) == ESP_OK) {
        uint16_t n = MAX_APS;
        if (esp_wifi_scan_get_ap_records(&n, s_aps) == ESP_OK) {
            s_ap_n = n;
        }
    }
    ESP_LOGI(TAG, "%u Wi-Fi networks found", s_ap_n);
}

static const char *STYLE =
    "<style>body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#191919}"
    "main{max-width:520px;margin:auto;padding:14px}h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:0 0 6px}"
    ".card{background:#fff;border:1px solid #d9dce1;border-radius:10px;padding:14px 16px;margin:12px 0}"
    "label{display:block;font-size:14px;margin:12px 0 4px;color:#444}"
    "input{width:100%;box-sizing:border-box;font:inherit;padding:10px;border:1px solid #b9bec7;border-radius:8px}"
    "button{font:inherit;padding:10px 14px;border-radius:8px;border:1px solid #257de0;background:#257de0;color:#fff;"
    "margin:10px 6px 0 0}button.alt{background:#fff;color:#1f5fb0}.msg{background:#e8f1fc;border:1px solid #257de0;"
    "border-radius:8px;padding:10px 12px}.ok{color:#067647}.bad{color:#b42318}small{color:#5f6368}</style>";

// ---------------------------------------------------------------- pages
static esp_err_t page_root(httpd_req_t *r)
{
    char q[32];
    if (httpd_req_get_url_query_str(r, q, sizeof(q)) == ESP_OK && strstr(q, "rescan")) {
        scan();
    }
    const settings_t *st = settings_get();
    char e1[200], e2[400], e3[200], e4[400];
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    put(r, "<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>Worker Safety setup</title>");
    put(r, STYLE);
    putf(r, "</head><body><main><h1>Worker Safety Monitor: setup</h1><small>Device %s &middot; firmware %s</small>",
         settings_device_id(), esp_app_get_description()->version);
    if (s_msg[0]) {
        html_esc(e4, sizeof(e4), s_msg);
        putf(r, "<p class=msg>%s</p>", e4);
        s_msg[0] = 0;
    }
    putf(r, "<div class=card><h2>Sensors</h2>Motion sensor (MPU6050): %s<br>Pulse sensor (MAX30102): %s</div>",
         s_imu_ok ? "<b class=ok>found</b>" : "<b class=bad>not found: fall detection will not work, check the wiring</b>",
         s_ppg_ok ? "<b class=ok>found</b>" : "<b class=bad>not found: no heart rate or SpO2</b>");

    html_esc(e1, sizeof(e1), st->ssid);
    put(r, "<form class=card method=post action=/save><h2>Network</h2>"
           "<label for=ssid>Wi-Fi network (2.4 GHz)</label>");
    putf(r, "<input id=ssid name=ssid list=nets value=\"%s\" required maxlength=32 autocapitalize=off "
            "autocomplete=off><datalist id=nets>", e1);
    for (int i = 0; i < s_ap_n; i++) {
        bool dup = s_aps[i].ssid[0] == 0;
        for (int j = 0; j < i && !dup; j++) {
            dup = strcmp((const char *)s_aps[i].ssid, (const char *)s_aps[j].ssid) == 0;
        }
        if (!dup) {
            html_esc(e2, sizeof(e2), (const char *)s_aps[i].ssid);
            putf(r, "<option value=\"%s\">", e2);
        }
    }
    put(r, "</datalist><small>Tap the box to pick a network found nearby. <a href=\"/?rescan=1\">Search again</a>"
           "</small><label for=pass>Wi-Fi password</label>");
    putf(r, "<input id=pass name=pass type=password maxlength=64 placeholder=\"%s\">",
         st->pass[0] ? "(unchanged)" : "");
    html_esc(e3, sizeof(e3), st->server[0] ? st->server : "http://");
    putf(r, "<label for=server>Supervisor server address</label><input id=server name=server value=\"%s\" "
            "required maxlength=120 autocapitalize=off autocomplete=off inputmode=url>"
            "<small>Shown on the web app's Setup page, for example http://192.168.1.20:8080</small>", e3);
    html_esc(e1, sizeof(e1), st->worker);
    putf(r, "<label for=worker>Worker name</label><input id=worker name=worker value=\"%s\" maxlength=32>", e1);
    html_esc(e2, sizeof(e2), st->token);
    putf(r, "<label for=token>Device token (only if the server was started with --token)</label>"
            "<input id=token name=token value=\"%s\" maxlength=64 autocapitalize=off autocomplete=off>"
            "<button>Save and restart</button></form>", e2);

    putf(r, "<form class=card method=post action=/calibrate><h2>Sensor orientation</h2><p>Now: <b>%s</b></p>"
            "<p>Calibrate once the device is fixed where it will be worn (lower back or belt). Each step takes "
            "2 seconds of standing still.</p><ol><li>Stand upright and still, then tap <b>1. Standing</b>.</li>"
            "<li>Bend forward at the hips by about 45&deg;, hold still, then tap <b>2. Bent forward</b>.</li></ol>"
            "<button name=step value=up>1. Standing</button><button name=step value=forward>2. Bent forward"
            "</button><button class=alt name=step value=reset>Use the default mounting</button></form>",
         st->calibrated ? "calibrated" : (s_have_up ? "step 1 done, now step 2" :
                                          "default mounting (sensor Y axis pointing down, Z forward)"));
    if (settings_have_network()) {
        put(r, "<form class=card method=post action=/exit><button class=alt>Leave setup without changes"
               "</button></form>");
    }
    put(r, "</main></body></html>");
    return httpd_resp_sendstr_chunk(r, NULL);
}

static esp_err_t page_save(httpd_req_t *r)
{
    static char body[FORM_MAX];
    if (read_form(r, body) < 0) {
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "bad form");
    }
    char ssid[33], pass[65], server[128], worker[33], token[65];
    field(body, "ssid", ssid, sizeof(ssid));
    field(body, "pass", pass, sizeof(pass));
    field(body, "server", server, sizeof(server));
    field(body, "worker", worker, sizeof(worker));
    field(body, "token", token, sizeof(token));
    const settings_t *st = settings_get();
    if (!pass[0] && strcmp(ssid, st->ssid) == 0) {
        strlcpy(pass, st->pass, sizeof(pass));   // left empty = keep the saved password
    }
    char url[sizeof(server) + 8] = "";
    if (strncmp(server, "http://", 7) != 0 && strncmp(server, "https://", 8) != 0) {
        strlcpy(url, "http://", sizeof(url));
    }
    strlcat(url, server, sizeof(url));
    size_t n = strlen(url);
    while (n > 0 && (url[n - 1] == '/' || url[n - 1] == ' ')) {
        url[--n] = 0;
    }
    if (!ssid[0] || n <= 7 || n >= sizeof(settings_get()->server) || strncmp(url, "https://", 8) == 0) {
        snprintf(s_msg, sizeof(s_msg), "%s", !ssid[0] ? "Enter the Wi-Fi network name." :
                 n <= 7 ? "Enter the server address shown on the web app's Setup page." :
                 n >= sizeof(settings_get()->server) ? "The server address is too long." :
                 "Use the http:// address of the web app (https is not supported).");
        return redirect_home(r);
    }
    if (settings_save_network(ssid, pass, url, worker, token) != ESP_OK) {
        snprintf(s_msg, sizeof(s_msg), "Could not save the settings. Try again.");
        return redirect_home(r);
    }
    char e1[200], e2[200];
    html_esc(e1, sizeof(e1), ssid);
    html_esc(e2, sizeof(e2), url);
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    put(r, "<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>Saved</title>");
    put(r, STYLE);
    putf(r, "</head><body><main><h1>Saved</h1><div class=card><p>The device restarts now, joins <b>%s</b> and "
            "reports to <b>%s</b>.</p><p>Reconnect this phone to your normal Wi-Fi and open the web app: the worker "
            "appears within a few seconds. The LED flashes briefly every 2 seconds once it is connected.</p>",
         e1, e2);
    put(r, "<p>If the device cannot join that network within 90 seconds, it comes back to setup mode (fast "
           "blinking LED) and this page tells you so. To return here later, hold the button for 10 seconds."
           "</p></div></main></body></html>");
    httpd_resp_sendstr_chunk(r, NULL);
    ESP_LOGW(TAG, "settings saved, restarting");
    restart_soon();
    return ESP_OK;
}

// Mean acceleration over 2 s (sensor frame after the menuconfig axis mapping); false if
// the device moved too much.
static bool still_gravity(float out[3])
{
    static float buf[IMU_MAX][6];
    if (!s_imu_ok) {
        return false;
    }
    mpu6050_read_fifo(buf, IMU_MAX);   // drop old samples
    double sum[3] = {0}, mag = 0, mag2 = 0;
    int n = 0;
    for (int k = 0; k < 20; k++) {
        vTaskDelay(pdMS_TO_TICKS(100));
        const int m = mpu6050_read_fifo(buf, IMU_MAX);
        for (int i = 0; i < m; i++) {
            const double g = sqrt(buf[i][0] * buf[i][0] + buf[i][1] * buf[i][1] + buf[i][2] * buf[i][2]);
            for (int c = 0; c < 3; c++) {
                sum[c] += buf[i][c];
            }
            mag += g;
            mag2 += g * g;
            n++;
        }
    }
    if (n < 50) {
        return false;
    }
    const double mean = mag / n, sd = sqrt(fmax(0.0, mag2 / n - mean * mean));
    if (sd > 0.05 || mean < 0.7 || mean > 1.3) {
        return false;
    }
    const double norm = sqrt(sum[0] * sum[0] + sum[1] * sum[1] + sum[2] * sum[2]);
    for (int c = 0; c < 3; c++) {
        out[c] = (float)(sum[c] / norm);
    }
    return true;
}

static esp_err_t page_calibrate(httpd_req_t *r)
{
    char body[64], step[16];
    if (read_form(r, body) < 0) {
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "bad form");
    }
    field(body, "step", step, sizeof(step));
    float g[3];
    if (strcmp(step, "reset") == 0) {
        settings_clear_rotation();
        s_have_up = false;
        snprintf(s_msg, sizeof(s_msg), "Back to the default mounting: sensor Y axis pointing down, Z forward.");
    } else if (!s_imu_ok) {
        snprintf(s_msg, sizeof(s_msg), "The motion sensor was not found, so it cannot be calibrated.");
    } else if (!still_gravity(g)) {
        snprintf(s_msg, sizeof(s_msg), "The device was moving. Hold still for the 2 seconds and tap again.");
    } else if (strcmp(step, "up") == 0) {
        memcpy(s_up, g, sizeof(s_up));
        s_have_up = true;
        const float tilt = acosf(fmaxf(-1.0f, fminf(1.0f, -g[1]))) * 180.0f / (float)M_PI;
        snprintf(s_msg, sizeof(s_msg), "Standing recorded (%.0f deg away from the default mounting). Now bend "
                 "forward and tap 2.", (double)tilt);
    } else if (strcmp(step, "forward") == 0 && !s_have_up) {
        snprintf(s_msg, sizeof(s_msg), "Do step 1 (standing) first.");
    } else if (strcmp(step, "forward") == 0) {
        // up = reading while standing; when bending forward the reading tilts away from
        // the body's forward axis: forward = -(b - (b.up) up), normalised
        const float *a = s_up;
        const float dot = g[0] * a[0] + g[1] * a[1] + g[2] * a[2];
        float f[3] = {-(g[0] - dot * a[0]), -(g[1] - dot * a[1]), -(g[2] - dot * a[2])};
        const float fn = sqrtf(f[0] * f[0] + f[1] * f[1] + f[2] * f[2]);
        if (fn < 0.34f) {   // less than 20 degrees
            snprintf(s_msg, sizeof(s_msg), "Bend further forward (at least 20 deg) and tap 2 again.");
        } else {
            // KFall frame: +Y down, +Z forward, X = Y x Z
            float rot[9];
            const float y[3] = {-a[0], -a[1], -a[2]}, z[3] = {f[0] / fn, f[1] / fn, f[2] / fn};
            rot[0] = y[1] * z[2] - y[2] * z[1];
            rot[1] = y[2] * z[0] - y[0] * z[2];
            rot[2] = y[0] * z[1] - y[1] * z[0];
            memcpy(rot + 3, y, sizeof(y));
            memcpy(rot + 6, z, sizeof(z));
            if (settings_save_rotation(rot) == ESP_OK) {
                snprintf(s_msg, sizeof(s_msg), "Calibrated (bend %.0f deg). Now enter the network settings and save.",
                         (double)(asinf(fminf(1.0f, fn)) * 180.0f / (float)M_PI));
                ESP_LOGI(TAG, "orientation calibrated: X=(%.2f %.2f %.2f) Y=(%.2f %.2f %.2f) Z=(%.2f %.2f %.2f)",
                         (double)rot[0], (double)rot[1], (double)rot[2], (double)rot[3], (double)rot[4],
                         (double)rot[5], (double)rot[6], (double)rot[7], (double)rot[8]);
            } else {
                snprintf(s_msg, sizeof(s_msg), "Could not save the calibration. Try again.");
            }
        }
    }
    return redirect_home(r);
}

static esp_err_t page_exit(httpd_req_t *r)
{
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    httpd_resp_sendstr(r, "<!doctype html><meta name=viewport content=\"width=device-width,initial-scale=1\">"
                          "<p style=\"font-family:system-ui;padding:16px\">Leaving setup: the device restarts with "
                          "its saved settings.</p>");
    restart_soon();
    return ESP_OK;
}

static esp_err_t catch_all(httpd_req_t *r) { return redirect_home(r); }

void provision_run(bool imu_ok, bool ppg_ok)
{
    s_imu_ok = imu_ok;
    s_ppg_ok = ppg_ok;
    status_led_set(LED_SETUP);
    if (settings_take_join_failed()) {
        snprintf(s_msg, sizeof(s_msg), "The device could not join the Wi-Fi network \"%.32s\" with the saved "
                 "settings. Check the name and password (2.4 GHz networks only).", settings_get()->ssid);
    }

    esp_netif_t *ap_netif = esp_netif_create_default_wifi_ap();
    esp_netif_create_default_wifi_sta();
    // Hand out this device as the phones' DNS server (so the captive DNS above answers) and
    // announce the setup page as the captive-portal address (DHCP option 114). Best effort:
    // without it the page is still at http://192.168.4.1.
    esp_netif_dns_info_t dns = {0};
    dns.ip.type = ESP_IPADDR_TYPE_V4;
    dns.ip.u_addr.ip4.addr = ESP_IP4TOADDR(192, 168, 4, 1);
    uint32_t offer_dns = 0x02;   // OFFER_DNS
    static char portal_uri[] = "http://192.168.4.1/";
    esp_netif_dhcps_stop(ap_netif);
    if (esp_netif_set_dns_info(ap_netif, ESP_NETIF_DNS_MAIN, &dns) != ESP_OK ||
        esp_netif_dhcps_option(ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER, &offer_dns,
                               sizeof(offer_dns)) != ESP_OK) {
        ESP_LOGW(TAG, "could not offer DNS by DHCP: phones may not open the setup page by themselves");
    }
    esp_netif_dhcps_option(ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_CAPTIVEPORTAL_URI, portal_uri,
                           strlen(portal_uri));
    esp_netif_dhcps_start(ap_netif);
    const wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(s_ap_ssid, sizeof(s_ap_ssid), "WorkerSafety-%02X%02X", mac[4], mac[5]);
    wifi_config_t ap;
    memset(&ap, 0, sizeof(ap));
    strlcpy((char *)ap.ap.ssid, s_ap_ssid, sizeof(ap.ap.ssid));
    ap.ap.ssid_len = (uint8_t)strlen(s_ap_ssid);
    ap.ap.channel = 1;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ap.ap.max_connection = 4;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
    scan();
    xTaskCreate(dns_task, "dns", 3072, NULL, 5, NULL);

    httpd_handle_t h = NULL;
    httpd_config_t hc = HTTPD_DEFAULT_CONFIG();
    hc.uri_match_fn = httpd_uri_match_wildcard;
    hc.stack_size = 8192;
    hc.max_uri_handlers = 8;
    hc.lru_purge_enable = true;
    ESP_ERROR_CHECK(httpd_start(&h, &hc));
    const httpd_uri_t uris[] = {
        {.uri = "/", .method = HTTP_GET, .handler = page_root},
        {.uri = "/save", .method = HTTP_POST, .handler = page_save},
        {.uri = "/calibrate", .method = HTTP_POST, .handler = page_calibrate},
        {.uri = "/exit", .method = HTTP_POST, .handler = page_exit},
        {.uri = "/*", .method = HTTP_GET, .handler = catch_all},   // phones' captive-portal checks
    };
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        httpd_register_uri_handler(h, &uris[i]);
    }
    ESP_LOGW(TAG, "SETUP MODE: join the Wi-Fi network \"%s\" with a phone; the setup page opens by itself "
                  "(or go to http://192.168.4.1)", s_ap_ssid);
    for (int s = 0;; s++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (s >= 15 * 60 && settings_have_network()) {
            ESP_LOGW(TAG, "nobody used the setup page for 15 minutes: back to normal operation");
            esp_restart();
        }
    }
}

#else

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

void provision_run(bool imu_ok, bool ppg_ok)
{
    (void)imu_ok;
    (void)ppg_ok;
    ESP_LOGE("setup", "setup mode needs the Wi-Fi alert link");
    for (;;) {
        vTaskDelay(portMAX_DELAY);
    }
}

#endif
