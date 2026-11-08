# Phase 1 Results -- kfall (subject-independent test set)

Model: 1D-CNN, 7169 parameters, int8 TFLite = 18.6 kB, host int8 latency 0.02 ms/window

| Level | Model | Accuracy | Sensitivity | Specificity | Precision | F1 |
|---|---|---|---|---|---|---|
| Window | float32 | 99.73 | 100.00 | 99.68 | 98.22 | 99.10 |
| Window | int8 | 99.69 | 100.00 | 99.64 | 98.00 | 98.99 |
| Trial (deployment) | float32 | 99.48 | 100.00 | 99.03 | 98.88 | 99.44 |
| Trial (deployment) | int8 | 99.48 | 100.00 | 99.03 | 98.88 | 99.44 |

## 5-fold subject-wise CV (float32, mean ± std)

- window_accuracy: 99.22 ± 0.85 %
- window_sensitivity: 99.88 ± 0.16 %
- window_specificity: 99.11 ± 1.00 %
- window_f1: 97.49 ± 2.69 %
- trial_accuracy: 98.47 ± 1.62 %
- trial_sensitivity: 99.96 ± 0.09 %
- trial_specificity: 97.19 ± 3.01 %
- trial_f1: 98.39 ± 1.69 %

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