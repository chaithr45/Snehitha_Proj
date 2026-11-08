# Phase 1 Results -- kfall (subject-independent test set)

Model: 1D-CNN, 7169 parameters, int8 TFLite = 18.6 kB, host int8 latency 0.03 ms/window

| Level | Model | Accuracy | Sensitivity | Specificity | Precision | F1 |
|---|---|---|---|---|---|---|
| Window | float32 | 99.86 | 100.00 | 99.84 | 99.10 | 99.55 |
| Window | int8 | 99.86 | 100.00 | 99.84 | 99.10 | 99.55 |
| Trial (deployment) | float32 | 99.79 | 100.00 | 99.61 | 99.55 | 99.77 |
| Trial (deployment) | int8 | 99.79 | 100.00 | 99.61 | 99.55 | 99.77 |

## 5-fold subject-wise CV (float32, mean ± std)

- window_accuracy: 99.94 ± 0.08 %
- window_sensitivity: 100.00 ± 0.00 %
- window_specificity: 99.93 ± 0.10 %
- window_f1: 99.81 ± 0.27 %
- trial_accuracy: 99.87 ± 0.18 %
- trial_sensitivity: 100.00 ± 0.00 %
- trial_specificity: 99.76 ± 0.34 %
- trial_f1: 99.86 ± 0.19 %

## Vitals algorithm self-check (synthetic PPG, NOT clinical validation)

| True HR | Est HR | True SpO2 | Est SpO2 |
|---|---|---|---|
| 55 | 55.0 | 98 | 98.0 |
| 72 | 72.3 | 97 | 97.0 |
| 95 | 95.2 | 95 | 95.0 |
| 120 | 120.0 | 93 | 93.0 |
| 150 | 150.0 | 90 | 90.0 |
| 170 | 171.4 | 88 | 88.0 |

## Alert state machine scenarios

- **Real fall, worker motionless**: t=5.0s FALL_SUSPECTED -> buzzer + vibration, cancel window open; t=10.5s ALERT_FALL (post-fall inactivity) -> LoRa/Wi-Fi to supervisor
- **False alarm, worker presses cancel**: t=5.0s FALL_SUSPECTED -> buzzer + vibration, cancel window open; t=8.0s CANCELLED by worker (false alarm suppressed)
- **No fall, sustained tachycardia**: t=10.0s ALERT_VITALS hr=150 spo2=96 -> LoRa/Wi-Fi to supervisor
- **Normal work**: t=-s no events (correct)