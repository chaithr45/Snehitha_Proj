// Alert link to the supervisor: JSON over HTTP (Wi-Fi on the device, emulated
// Ethernet in QEMU).
//
// * store-and-forward: messages wait in a queue and are retried (back-off up to 30 s)
//   until the supervisor confirms them, so a Wi-Fi dropout delays an alert but never
//   loses it;
// * every message has an ID "<boot>-<n>"; the supervisor returns the IDs it has
//   acknowledged in its replies, which turns the buzzer to "help is on the way".
#pragma once

#include <stdbool.h>
#include <stdint.h>

void net_init(void);

// Queue a message for POST <SUPERVISOR_URL>/alert. Non-blocking. `extra_json` (may be
// NULL) is a fragment of extra fields, e.g. "\"posture\":\"lying\"". Returns the
// message number n (the "<n>" in its ID), which pipeline_on_ack() receives back.
uint32_t net_send_alert(const char *event, const char *detail, double t, float hr, float spo2,
                        const char *extra_json);

uint32_t net_messages_queued(void);      // total queued since boot
uint32_t net_messages_delivered(void);   // confirmed by the supervisor (HTTP 2xx)
uint32_t net_alerts_needing_ack(void);   // alerts that ask for an acknowledgment
uint32_t net_alerts_acked(void);         // acknowledgments received

// Wait until the queue is empty (everything delivered).
bool net_flush(uint32_t timeout_ms);
