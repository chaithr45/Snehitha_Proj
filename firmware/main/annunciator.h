// Buzzer + vibration motor patterns for the worker.
#pragma once

typedef enum {
    ANN_OFF = 0,
    ANN_PENDING,   // fall suspected: beep + vibrate, worker can press "I'm OK"
    ANN_ALERT,     // alert sent: fast beeping until cleared with a long press
} ann_mode_t;

void annunciator_init(void);
void annunciator_set(ann_mode_t mode);
