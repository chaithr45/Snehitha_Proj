#include "sdkconfig.h"
#include "net.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "pipeline.h"

#if CONFIG_SAFETY_NET_WIFI
#include "esp_wifi.h"
#endif
#if CONFIG_SAFETY_NET_OPENETH
#include "esp_eth.h"
#endif
#if !CONFIG_SAFETY_NET_NONE
#include "esp_http_client.h"
#endif

#define BODY_MAX 320
#define QUEUE_LEN 8
#define BIT_UP BIT0     // network has an IP address
#define BIT_IDLE BIT1   // no alert queued or in flight

typedef struct {
    char body[BODY_MAX];
} msg_t;

static const char *TAG = "net";
static QueueHandle_t s_q;
static EventGroupHandle_t s_ev;
static volatile uint32_t s_delivered;

static void fmt_num(char *buf, size_t n, float v, int decimals)
{
    if (isnan(v)) {
        snprintf(buf, n, "null");   // JSON has no NaN
    } else {
        snprintf(buf, n, "%.*f", decimals, (double)v);
    }
}

static double uptime_s(void) { return (double)esp_timer_get_time() / 1e6; }

void net_send_event(const char *event, const char *detail, double t, float hr, float spo2)
{
    msg_t m;
    char h[16], s[16];
    fmt_num(h, sizeof(h), hr, 0);
    fmt_num(s, sizeof(s), spo2, 0);
    snprintf(m.body, sizeof(m.body),
             "{\"type\":\"alert\",\"worker\":\"%s\",\"event\":\"%s\",\"detail\":\"%s\",\"t\":%.2f,"
             "\"hr\":%s,\"spo2\":%s,\"uptime_s\":%.1f}",
             CONFIG_SAFETY_WORKER_ID, event, detail, t, h, s, uptime_s());
#if CONFIG_SAFETY_NET_NONE
    ESP_LOGW(TAG, "no alert link configured: %s", m.body);
#else
    xEventGroupClearBits(s_ev, BIT_IDLE);
    if (xQueueSend(s_q, &m, 0) != pdTRUE) {
        ESP_LOGE(TAG, "alert queue full, dropped: %s", m.body);
        if (uxQueueMessagesWaiting(s_q) == 0) {
            xEventGroupSetBits(s_ev, BIT_IDLE);
        }
    }
#endif
}

uint32_t net_alerts_delivered(void) { return s_delivered; }

#if CONFIG_SAFETY_NET_NONE

void net_init(void) { ESP_LOGW(TAG, "alert link: none (alerts are only logged)"); }
bool net_flush(uint32_t timeout_ms)
{
    (void)timeout_ms;
    return true;
}

#else

bool net_flush(uint32_t timeout_ms)
{
    return xEventGroupWaitBits(s_ev, BIT_IDLE, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & BIT_IDLE;
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;
    ESP_LOGI(TAG, "network up, IP " IPSTR ", supervisor %s", IP2STR(&e->ip_info.ip), CONFIG_SAFETY_SUPERVISOR_URL);
    xEventGroupSetBits(s_ev, BIT_UP);
}

#if CONFIG_SAFETY_NET_WIFI
static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, BIT_UP);
        ESP_LOGW(TAG, "Wi-Fi disconnected, reconnecting");
        esp_wifi_connect();
    }
}

static void start_link(void)
{
    esp_netif_create_default_wifi_sta();
    const wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_got_ip, NULL));
    wifi_config_t wc;
    memset(&wc, 0, sizeof(wc));
    strlcpy((char *)wc.sta.ssid, CONFIG_SAFETY_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_SAFETY_WIFI_PASSWORD, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "alert link: Wi-Fi \"%s\"", CONFIG_SAFETY_WIFI_SSID);
}
#elif CONFIG_SAFETY_NET_OPENETH
static void start_link(void)
{
    // OpenCores Ethernet MAC emulated by QEMU (-nic user,model=open_eth)
    const esp_netif_config_t ncfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&ncfg);
    const eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.autonego_timeout_ms = 100;
    phy_cfg.reset_gpio_num = -1;
    esp_eth_mac_t *mac = esp_eth_mac_new_openeth(&mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_dp83848(&phy_cfg);
    const esp_eth_config_t cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = NULL;
    ESP_ERROR_CHECK(esp_eth_driver_install(&cfg, &eth));
    ESP_ERROR_CHECK(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, NULL));
    ESP_ERROR_CHECK(esp_eth_start(eth));
    ESP_LOGI(TAG, "alert link: emulated Ethernet (QEMU)");
}
#endif

static bool post(const char *path, const char *body)
{
    char url[192];
    snprintf(url, sizeof(url), "%s%s", CONFIG_SAFETY_SUPERVISOR_URL, path);
    const esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) {
        return false;
    }
    esp_http_client_set_header(c, "Content-Type", "application/json");
    esp_http_client_set_post_field(c, body, (int)strlen(body));
    const esp_err_t err = esp_http_client_perform(c);
    const int status = err == ESP_OK ? esp_http_client_get_status_code(c) : -1;
    esp_http_client_cleanup(c);
    if (err != ESP_OK || status < 200 || status >= 300) {
        ESP_LOGW(TAG, "POST %s failed (%s, HTTP %d)", url, esp_err_to_name(err), status);
        return false;
    }
    return true;
}

static void send_alert(const msg_t *m)
{
    // Alerts are safety-critical: wait for the link and retry with back-off
    for (int attempt = 0, delay_ms = 1000; attempt < 6; attempt++, delay_ms *= 2) {
        if ((xEventGroupWaitBits(s_ev, BIT_UP, pdFALSE, pdTRUE, pdMS_TO_TICKS(15000)) & BIT_UP) &&
            post("/alert", m->body)) {
            s_delivered++;
            ESP_LOGI(TAG, "ALERT DELIVERED %s", m->body);
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(delay_ms));
    }
    ESP_LOGE(TAG, "ALERT NOT DELIVERED after retries: %s", m->body);
}

static void send_telemetry(void)
{
    if (!(xEventGroupGetBits(s_ev) & BIT_UP)) {
        return;
    }
    pipeline_status_t st;
    pipeline_get_status(&st);
    char h[16], s[16], body[BODY_MAX];
    fmt_num(h, sizeof(h), st.hr, 0);
    fmt_num(s, sizeof(s), st.spo2, 0);
    snprintf(body, sizeof(body),
             "{\"type\":\"telemetry\",\"worker\":\"%s\",\"state\":\"%s\",\"hr\":%s,\"spo2\":%s,"
             "\"fall_prob\":%.3f,\"windows\":%lu,\"infer_us_avg\":%lu,\"uptime_s\":%.1f}",
             CONFIG_SAFETY_WORKER_ID, sc_state_name(st.state), h, s, (double)st.last_prob,
             (unsigned long)st.windows, (unsigned long)st.infer_us_avg, uptime_s());
    post("/telemetry", body);
}

static void task(void *arg)
{
    (void)arg;
    static msg_t m;
    const int period_s = CONFIG_SAFETY_TELEMETRY_PERIOD_S;
    int64_t next_tel = esp_timer_get_time() + (int64_t)period_s * 1000000;
    for (;;) {
        if (xQueueReceive(s_q, &m, pdMS_TO_TICKS(1000)) == pdTRUE) {
            send_alert(&m);
            if (uxQueueMessagesWaiting(s_q) == 0) {
                xEventGroupSetBits(s_ev, BIT_IDLE);
            }
        }
        if (period_s > 0 && esp_timer_get_time() >= next_tel) {
            next_tel += (int64_t)period_s * 1000000;
            send_telemetry();
        }
    }
}

void net_init(void)
{
    s_q = xQueueCreate(QUEUE_LEN, sizeof(msg_t));
    s_ev = xEventGroupCreate();
    xEventGroupSetBits(s_ev, BIT_IDLE);
    start_link();
    xTaskCreate(task, "net", 6144, NULL, 5, NULL);
}

#endif
