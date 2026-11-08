// Host parity tests: safety_core (C port) vs phase1_pipeline.py (Python reference).
//
//   test_host <vector_dir> [--dump-windows <file>]
//
// Vectors come from gen_vectors.py. Model constants come from the real
// fall_model_data.h, exactly as on the ESP32. Exit code 0 = all checks passed.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "fall_model_data.h"
#include "safety_core.h"

static_assert(FALL_WIN == SC_WIN, "window length mismatch");
static_assert(FALL_CH == SC_CH, "channel count mismatch");

namespace {

int g_failures = 0;

void check(bool ok, const char *what)
{
    std::printf("  [%s] %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) {
        g_failures++;
    }
}

struct Reader {
    std::vector<uint8_t> d;
    size_t p = 0;
    bool open(const std::string &path)
    {
        FILE *f = std::fopen(path.c_str(), "rb");
        if (!f) {
            return false;
        }
        std::fseek(f, 0, SEEK_END);
        d.resize((size_t)std::ftell(f));
        std::fseek(f, 0, SEEK_SET);
        size_t got = std::fread(d.data(), 1, d.size(), f);
        std::fclose(f);
        return got == d.size();
    }
    template <typename T> T get()
    {
        T v;
        if (p + sizeof(T) > d.size()) {
            std::fprintf(stderr, "vector file truncated\n");
            std::exit(2);
        }
        std::memcpy(&v, d.data() + p, sizeof(T));
        p += sizeof(T);
        return v;
    }
    template <typename T> const T *array(size_t n)
    {
        if (p + n * sizeof(T) > d.size()) {
            std::fprintf(stderr, "vector file truncated\n");
            std::exit(2);
        }
        const T *a = reinterpret_cast<const T *>(d.data() + p);
        p += n * sizeof(T);
        return a;
    }
    void magic(const char *m)
    {
        if (std::memcmp(array<char>(4), m, 4) != 0) {
            std::fprintf(stderr, "bad magic, expected %s\n", m);
            std::exit(2);
        }
    }
};

void test_imu(const std::string &dir, const char *dump_path)
{
    std::printf("IMU path (filter -> window -> int8 -> decision)\n");
    Reader r;
    if (!r.open(dir + "/imu.bin")) {
        check(false, "open imu.bin");
        return;
    }
    r.magic("SCV1");
    const uint32_t n_rec = r.get<uint32_t>();
    const uint32_t win = r.get<uint32_t>(), ch = r.get<uint32_t>();
    const uint32_t hop = r.get<uint32_t>(), consec = r.get<uint32_t>();
    const float thr = r.get<float>(), in_scale = r.get<float>();
    const int32_t in_zp = r.get<int32_t>();
    check(win == FALL_WIN && ch == FALL_CH && hop == FALL_HOP && consec == FALL_CONSEC &&
              thr == FALL_THRESHOLD && in_scale == FALL_IN_SCALE && in_zp == FALL_IN_ZP,
          "vectors were generated for this fall_model_data.h");

    FILE *dump = dump_path ? std::fopen(dump_path, "wb") : nullptr;
    double max_err_acc = 0, max_err_gyr = 0;
    uint64_t q_total = 0, q_mismatch = 0;
    int q_maxdiff = 0, win_count_bad = 0, alarm_bad = 0;
    int falls = 0, falls_alarm = 0, adl = 0, adl_alarm = 0;

    for (uint32_t k = 0; k < n_rec; k++) {
        const uint32_t n = r.get<uint32_t>(), nwin = r.get<uint32_t>();
        const uint8_t is_fall = r.get<uint8_t>(), py_alarm = r.get<uint8_t>();
        (void)r.get<uint16_t>();
        const float *raw = r.array<float>((size_t)n * SC_CH);
        const float *filt = r.array<float>((size_t)n * SC_CH);
        const int8_t *q = r.array<int8_t>((size_t)nwin * SC_WIN * SC_CH);
        const float *prob = r.array<float>(nwin);

        sc_lpf_t lpf;
        sc_lpf_init(&lpf, LPF_SOS, LPF_ZI, sizeof(LPF_SOS) / sizeof(LPF_SOS[0]));
        sc_window_t w;
        sc_window_init(&w, FALL_HOP);
        sc_detector_t det;
        sc_detector_init(&det, FALL_THRESHOLD, FALL_CONSEC);
        int8_t qc[SC_WIN * SC_CH];
        uint32_t wi = 0;
        bool alarm = false;

        for (uint32_t i = 0; i < n; i++) {
            float y[SC_CH];
            sc_lpf_process(&lpf, raw + (size_t)i * SC_CH, y);
            for (int c = 0; c < SC_CH; c++) {
                double e = std::fabs((double)y[c] - filt[(size_t)i * SC_CH + c]);
                double &m = c < 3 ? max_err_acc : max_err_gyr;
                m = e > m ? e : m;
            }
            if (sc_window_push(&w, y)) {
                sc_window_quantize(&w, FALL_MEAN, FALL_STD, FALL_IN_SCALE, FALL_IN_ZP, qc);
                if (wi < nwin) {
                    const int8_t *qp = q + (size_t)wi * SC_WIN * SC_CH;
                    for (int j = 0; j < SC_WIN * SC_CH; j++) {
                        int d = std::abs((int)qc[j] - (int)qp[j]);
                        q_mismatch += d != 0;
                        q_maxdiff = d > q_maxdiff ? d : q_maxdiff;
                    }
                    q_total += SC_WIN * SC_CH;
                    alarm |= sc_detector_update(&det, prob[wi]);
                }
                if (dump) {
                    std::fwrite(qc, 1, sizeof(qc), dump);
                }
                wi++;
            }
        }
        win_count_bad += wi != nwin;
        alarm_bad += (int)alarm != (int)py_alarm;
        if (is_fall) {
            falls++;
            falls_alarm += alarm;
        } else {
            adl++;
            adl_alarm += alarm;
        }
    }
    if (dump) {
        std::fclose(dump);
    }
    std::printf("  recordings %u | windows %llu | filter max |err| acc %.2e g, gyro %.2e dps\n", n_rec,
                (unsigned long long)(q_total / (SC_WIN * SC_CH)), max_err_acc, max_err_gyr);
    std::printf("  int8 inputs differing from Python: %llu of %llu (%.4f%%), max diff %d\n",
                (unsigned long long)q_mismatch, (unsigned long long)q_total,
                q_total ? 100.0 * (double)q_mismatch / (double)q_total : 0.0, q_maxdiff);
    std::printf("  decisions: falls alarmed %d/%d, ADL alarmed %d/%d\n", falls_alarm, falls, adl_alarm, adl);
    check(n_rec > 0, "vectors contain recordings");
    check(max_err_acc < 1e-4 && max_err_gyr < 1e-2, "low-pass filter matches scipy sosfilt");
    check(win_count_bad == 0, "windows evaluated at the same samples (every FALL_HOP)");
    check(q_maxdiff <= 1 && (double)q_mismatch <= 1e-3 * (double)q_total,
          "int8 model input matches Python (<=0.1% off by one LSB)");
    check(alarm_bad == 0, "fall decision (consecutive windows) matches Python on every recording");
}

void test_ppg(const std::string &dir)
{
    std::printf("Vitals (estimate_hr / estimate_spo2)\n");
    Reader r;
    if (!r.open(dir + "/ppg.bin")) {
        check(false, "open ppg.bin");
        return;
    }
    r.magic("SCP1");
    const uint32_t n_cases = r.get<uint32_t>();
    double max_hr = 0, max_sp = 0, max_true_hr = 0;
    int nan_bad = 0;
    for (uint32_t k = 0; k < n_cases; k++) {
        const uint32_t n = r.get<uint32_t>();
        const float true_hr = r.get<float>(), true_sp = r.get<float>();
        const float py_hr = r.get<float>(), py_sp = r.get<float>();
        const float *red = r.array<float>(n);
        const float *ir = r.array<float>(n);
        const float hr = sc_estimate_hr(ir, (int)n);
        const float sp = sc_estimate_spo2(red, ir, (int)n);
        (void)true_sp;
        if (std::isnan(hr) != std::isnan(py_hr) || std::isnan(sp) != std::isnan(py_sp)) {
            nan_bad++;
            continue;
        }
        if (!std::isnan(hr)) {
            max_hr = std::fmax(max_hr, std::fabs(hr - py_hr));
            max_true_hr = std::fmax(max_true_hr, std::fabs(hr - true_hr));
        }
        if (!std::isnan(sp)) {
            max_sp = std::fmax(max_sp, std::fabs(sp - py_sp));
        }
    }
    std::printf("  cases %u | max |HR - python| %.4f bpm | max |SpO2 - python| %.5f %% | "
                "max |HR - true| %.2f bpm\n", n_cases, max_hr, max_sp, max_true_hr);
    check(n_cases > 0, "vectors contain PPG recordings");
    check(nan_bad == 0, "same cases give no estimate (NaN) as in Python");
    check(max_hr < 0.01 && max_sp < 0.001, "HR and SpO2 match Python");
}

void test_fsm(const std::string &dir, const char *file, const char *title)
{
    std::printf("%s\n", title);
    Reader r;
    if (!r.open(dir + "/" + file)) {
        check(false, "open FSM vectors");
        return;
    }
    r.magic("SCF2");
    const uint32_t mode = r.get<uint32_t>();
    const uint32_t n_scen = r.get<uint32_t>();
    sc_fsm_cfg_t cfg;
    if (mode == 0) {
        sc_fsm_cfg_phase1(&cfg);
    } else {
        sc_fsm_cfg_default(&cfg);
    }
    uint64_t steps = 0, events = 0, bad = 0;
    uint32_t per_event[16] = {0};
    for (uint32_t k = 0; k < n_scen; k++) {
        const uint32_t n = r.get<uint32_t>();
        sc_fsm_t f;
        sc_fsm_init(&f, &cfg);
        for (uint32_t i = 0; i < n; i++) {
            sc_fsm_input_t in;
            const double t = r.get<double>();
            in.fall = r.get<uint8_t>();
            in.motion_std_g = r.get<float>();
            in.button = r.get<uint8_t>();
            in.hr = r.get<float>();
            in.spo2 = r.get<float>();
            in.tilt_deg = r.get<float>();
            in.worn = r.get<uint8_t>();
            const uint8_t exp = r.get<uint8_t>();
            const sc_event_t ev = sc_fsm_step(&f, t, &in);
            steps++;
            if (exp) {
                events++;
                per_event[exp & 15]++;
            }
            if ((uint8_t)ev != exp) {
                if (bad < 5) {
                    std::printf("  scenario %u step %u t=%.1f: C %s, Python code %u\n", k, i, t,
                                sc_event_name(ev), exp);
                }
                bad++;
            }
        }
    }
    std::printf("  scenarios %u | steps %llu | events %llu | mismatches %llu\n", n_scen,
                (unsigned long long)steps, (unsigned long long)events, (unsigned long long)bad);
    std::printf("  events:");
    for (int e = 1; e < 16; e++) {
        if (per_event[e]) {
            std::printf(" %s %u;", sc_event_name((sc_event_t)e), per_event[e]);
        }
    }
    std::printf("\n");
    check(n_scen > 0 && events > 0, "vectors contain scenarios with events");
    check(bad == 0, "every event matches Python at the same step");
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <vector_dir> [--dump-windows file]\n", argv[0]);
        return 2;
    }
    const char *dump = (argc >= 4 && std::strcmp(argv[2], "--dump-windows") == 0) ? argv[3] : nullptr;
    test_imu(argv[1], dump);
    test_ppg(argv[1]);
    test_fsm(argv[1], "fsm.bin", "Alert state machine, Phase 1 features (vs phase1_pipeline.SafetyFSM)");
    test_fsm(argv[1], "fsm2.bin", "Alert state machine, man-down + motion-aware vitals (vs device_model.SafetyFSM2)");
    std::printf("%s (%d failed check%s)\n", g_failures ? "FAILED" : "ALL PASSED", g_failures,
                g_failures == 1 ? "" : "s");
    return g_failures ? 1 : 0;
}
