// Alert link to the supervisor: JSON over HTTP (Wi-Fi on the device, emulated
// Ethernet in QEMU). Alerts are queued and sent by a background task with retries.
#pragma once

#include <stdbool.h>
#include <stdint.h>

void net_init(void);

// Queue an alert for POST <SUPERVISOR_URL>/alert. Non-blocking.
void net_send_event(const char *event, const char *detail, double t, float hr, float spo2);

// Number of alerts the supervisor acknowledged (HTTP 2xx) so far.
uint32_t net_alerts_delivered(void);

// Wait until every queued alert has been handled (sent or given up).
bool net_flush(uint32_t timeout_ms);
