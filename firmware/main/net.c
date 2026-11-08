#include "sdkconfig.h"
#include "net.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "pipeline.h"

#if CONFIG_SAFETY_NET_WIFI
#include "esp_wifi.h"
#endif
#if CONFIG_SAFETY_NET_OPENETH
#include "esp_eth.h"
#endif
#if !CONFIG_SAFETY_NET_NONE
#include "cJSON.h"
#include "esp_http_client.h"
#endif

#define BODY_MAX 384
#define QUEUE_LEN 16          // messages waiting for the supervisor
#define ACK_TRACK 32          // delivered alerts waiting for an acknowledgment
#define ACK_POLL_MS 3000      // status poll while an acknowledgment is outstanding
#define BACKOFF_MAX_MS 30000
#define BIT_UP BIT0           // network has an IP address
#define BIT_IDLE BIT1         // queue empty

typedef struct {
    uint32_t n;
    bool needs_ack;
    char body[BODY_MAX];
} msg_t;

static const char *TAG = "net";
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
#if !CONFIG_SAFETY_NET_NONE
static msg_t s_q[QUEUE_LEN];
static int s_head, s_count;
#endif
static uint32_t s_boot, s_next_n = 1;
static volatile uint32_t s_queued, s_delivered, s_need_ack, s_acked;

static void fmt_num(char *buf, size_t n, float v, int decimals)
{
    if (isnan(v)) {
        snprintf(buf, n, "null");   // JSON has no NaN
    } else {
        snprintf(buf, n, "%.*f", decimals, (double)v);
    }
}

static double uptime_s(void) { return (double)esp_timer_get_time() / 1e6; }

uint32_t net_messages_queued(void) { return s_queued; }
uint32_t net_messages_delivered(void) { return s_delivered; }
uint32_t net_alerts_needing_ack(void) { return s_need_ack; }
uint32_t net_alerts_acked(void) { return s_acked; }

#if CONFIG_SAFETY_NET_NONE

void net_init(void)
{
    s_boot = esp_random() & 0xFFFFFF;
    ESP_LOGW(TAG, "alert link: none (alerts are only logged)");
}

bool net_flush(uint32_t timeout_ms)
{
    (void)timeout_ms;
    return true;
}

#else

static EventGroupHandle_t s_ev;
static TaskHandle_t s_task;
static uint32_t s_wait_ack[ACK_TRACK];
static int s_wait_n;

bool net_flush(uint32_t timeout_ms)
{
    return xEventGroupWaitBits(s_ev, BIT_IDLE, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & BIT_IDLE;
}

#endif

uint32_t net_send_alert(const char *event, const char *detail, double t, float hr, float spo2,
                        const char *extra_json)
{
    msg_t m;
    const bool needs_ack = (strncmp(event, "ALERT_", 6) == 0 && strcmp(event, "ALERT_CLEARED") != 0) ||
                           strcmp(event, "DEVICE_FAULT") == 0;
    taskENTER_CRITICAL(&s_mux);
    m.n = s_next_n++;
    taskEXIT_CRITICAL(&s_mux);
    m.needs_ack = needs_ack;
    char h[16], s[16];
    fmt_num(h, sizeof(h), hr, 0);
    fmt_num(s, sizeof(s), spo2, 0);
    snprintf(m.body, sizeof(m.body),
             "{\"type\":\"alert\",\"id\":\"%06lx-%lu\",\"worker\":\"%s\",\"event\":\"%s\",\"detail\":\"%s\","
             "\"needs_ack\":%s,\"t\":%.2f,\"hr\":%s,\"spo2\":%s,\"uptime_s\":%.1f%s%s}",
             (unsigned long)s_boot, (unsigned long)m.n, CONFIG_SAFETY_WORKER_ID, event, detail,
             needs_ack ? "true" : "false", t, h, s, uptime_s(), extra_json ? "," : "", extra_json ? extra_json : "");
    s_queued++;
#if CONFIG_SAFETY_NET_NONE
    ESP_LOGW(TAG, "no alert link configured: %s", m.body);
#else
    bool dropped = false;
    taskENTER_CRITICAL(&s_mux);
    if (s_count == QUEUE_LEN) {   // keep the newest; should never happen with 16 slots
        s_head = (s_head + 1) % QUEUE_LEN;
        s_count--;
        dropped = true;
    }
    s_q[(s_head + s_count) % QUEUE_LEN] = m;
    s_count++;
    if (needs_ack) {
        s_need_ack++;
    }
    taskEXIT_CRITICAL(&s_mux);
    if (dropped) {
        ESP_LOGE(TAG, "queue full: oldest message dropped");
    }
    xEventGroupClearBits(s_ev, BIT_IDLE);
    xTaskNotifyGive(s_task);
#endif
    return m.n;
}

#if !CONFIG_SAFETY_NET_NONE

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
        ESP_LOGW(TAG, "Wi-Fi disconnected, reconnecting (alerts wait in the queue)");
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

// ---- acknowledgments: the supervisor lists acknowledged IDs in every reply ----
static void handle_reply(const char *body)
{
    cJSON *root = cJSON_Parse(body);
    if (!root) {
        return;
    }
    const cJSON *acked = cJSON_GetObjectItem(root, "acked");
    const cJSON *item;
    cJSON_ArrayForEach(item, acked)
    {
        unsigned long boot = 0, n = 0;
        if (!cJSON_IsString(item) || sscanf(item->valuestring, "%lx-%lu", &boot, &n) != 2 || boot != s_boot) {
            continue;
        }
        for (int i = 0; i < s_wait_n; i++) {
            if (s_wait_ack[i] == n) {
                s_wait_ack[i] = s_wait_ack[--s_wait_n];
                s_acked++;
                ESP_LOGI(TAG, "ACKNOWLEDGED by supervisor: %s", item->valuestring);
                pipeline_on_ack((uint32_t)n);
                break;
            }
        }
    }
    cJSON_Delete(root);
}

typedef struct {
    char buf[512];
    int len;
} reply_t;

static esp_err_t on_http(esp_http_client_event_t *e)
{
    reply_t *r = (reply_t *)e->user_data;
    if (e->event_id == HTTP_EVENT_ON_DATA && r && e->data_len > 0) {
        int n = e->data_len;
        if (n > (int)sizeof(r->buf) - 1 - r->len) {
            n = (int)sizeof(r->buf) - 1 - r->len;
        }
        if (n > 0) {
            memcpy(r->buf + r->len, e->data, n);
            r->len += n;
            r->buf[r->len] = 0;
        }
    }
    return ESP_OK;
}

static bool post(const char *path, const char *body)
{
    static reply_t reply;
    reply.len = 0;
    reply.buf[0] = 0;
    char url[192];
    snprintf(url, sizeof(url), "%s%s", CONFIG_SAFETY_SUPERVISOR_URL, path);
    const esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
        .event_handler = on_http,
        .user_data = &reply,
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
    handle_reply(reply.buf);
    return true;
}

static void send_telemetry(void)
{
    pipeline_status_t st;
    pipeline_get_status(&st);
    char h[16], s[16], body[BODY_MAX];
    fmt_num(h, sizeof(h), st.hr, 0);
    fmt_num(s, sizeof(s), st.spo2, 0);
    char alert[24] = "null";
    if (st.active_alert_id) {
        snprintf(alert, sizeof(alert), "\"%06lx-%lu\"", (unsigned long)s_boot, (unsigned long)st.active_alert_id);
    }
    snprintf(body, sizeof(body),
             "{\"type\":\"telemetry\",\"worker\":\"%s\",\"state\":\"%s\",\"hr\":%s,\"spo2\":%s,"
             "\"vitals_reliable\":%s,\"worn\":%s,\"posture\":\"%s\",\"tilt_deg\":%.0f,\"motion_g\":%.3f,"
             "\"sensor_fault\":%s,\"active_alert\":%s,\"acked\":%s,\"fall_prob\":%.3f,\"windows\":%lu,"
             "\"infer_us_avg\":%lu,\"queued\":%d,\"uptime_s\":%.1f}",
             CONFIG_SAFETY_WORKER_ID, sc_state_name(st.state), h, s, st.vitals_reliable ? "true" : "false",
             st.worn ? "true" : "false", st.tilt_deg > 60.0f ? "lying" : "upright", (double)st.tilt_deg,
             (double)st.motion_g, st.sensor_fault ? "true" : "false", alert, st.acked ? "true" : "false",
             (double)st.last_prob, (unsigned long)st.windows, (unsigned long)st.infer_us_avg, s_count, uptime_s());
    post("/telemetry", body);
}

static void task(void *arg)
{
    (void)arg;
    static msg_t m;
    uint32_t backoff_ms = 1000;
    int64_t next_status = 0;
    for (;;) {
        taskENTER_CRITICAL(&s_mux);
        const bool have = s_count > 0;
        if (have) {
            m = s_q[s_head];
        }
        taskEXIT_CRITICAL(&s_mux);

        if (have) {
            if ((xEventGroupGetBits(s_ev) & BIT_UP) && post("/alert", m.body)) {
                taskENTER_CRITICAL(&s_mux);
                s_head = (s_head + 1) % QUEUE_LEN;
                s_count--;
                taskEXIT_CRITICAL(&s_mux);
                s_delivered++;
                if (m.needs_ack && s_wait_n < ACK_TRACK) {
                    s_wait_ack[s_wait_n++] = m.n;
                }
                ESP_LOGI(TAG, "DELIVERED %s", m.body);
                backoff_ms = 1000;
                next_status = 0;   // poll for the acknowledgment soon
                continue;
            }
            // store-and-forward: keep the message, retry with back-off
            if (xEventGroupGetBits(s_ev) & BIT_UP) {
                ESP_LOGW(TAG, "supervisor unreachable, %d message(s) queued, retry in %lu ms", s_count,
                         (unsigned long)backoff_ms);
                vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            } else {
                xEventGroupWaitBits(s_ev, BIT_UP, pdFALSE, pdTRUE, pdMS_TO_TICKS(backoff_ms));
            }
            backoff_ms = backoff_ms * 2 > BACKOFF_MAX_MS ? BACKOFF_MAX_MS : backoff_ms * 2;
        } else {
            xEventGroupSetBits(s_ev, BIT_IDLE);
            taskENTER_CRITICAL(&s_mux);
            const bool raced = s_count > 0;   // a message arrived meanwhile
            taskEXIT_CRITICAL(&s_mux);
            if (raced) {
                xEventGroupClearBits(s_ev, BIT_IDLE);
            }
        }

        // status: fast while an acknowledgment is outstanding, otherwise the telemetry period
        const int64_t period_us = s_wait_n > 0 ? (int64_t)ACK_POLL_MS * 1000
                                               : (int64_t)CONFIG_SAFETY_TELEMETRY_PERIOD_S * 1000000;
        const int64_t now = esp_timer_get_time();
        if (period_us > 0 && now >= next_status && (xEventGroupGetBits(s_ev) & BIT_UP)) {
            next_status = now + period_us;
            send_telemetry();
        }
        if (!have) {
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(500));
        }
    }
}

void net_init(void)
{
    s_boot = esp_random() & 0xFFFFFF;
    s_ev = xEventGroupCreate();
    xEventGroupSetBits(s_ev, BIT_IDLE);
    xTaskCreate(task, "net", 6144, NULL, 5, &s_task);
    start_link();
}

#endif
