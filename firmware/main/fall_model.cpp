// TFLite Micro wrapper. This is the only file that includes fall_model_data.h
// (the header defines the model array, so it must live in one translation unit).
#include "sdkconfig.h"
#include "fall_model.h"

#include <cmath>
#include <cstdlib>
#include <cstring>

#include "esp_log.h"
#include "esp_timer.h"
#include "fall_model_data.h"
// micro_interpreter.h also provides the model schema (tflite::GetModel,
// TFLITE_SCHEMA_VERSION); its own path moved between esp-tflite-micro 1.4.1 and 1.4.2.
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"

#if CONFIG_SAFETY_MODEL_SELFTEST
#include "selftest_data.h"
static_assert(SELFTEST_LEN == FALL_WIN * FALL_CH, "self-test data was generated for another model shape");
#endif

namespace {

const char *TAG = "fall_model";
constexpr size_t kArenaSize = CONFIG_SAFETY_TENSOR_ARENA_KB * 1024;
alignas(16) uint8_t g_arena[kArenaSize];

tflite::MicroInterpreter *g_interp = nullptr;
TfLiteTensor *g_in = nullptr;
TfLiteTensor *g_out = nullptr;

const fall_params_t g_params = {
    FALL_MEAN, FALL_STD, LPF_SOS, LPF_ZI, (int)(sizeof(LPF_SOS) / sizeof(LPF_SOS[0])),
    FALL_FS_HZ, FALL_WIN, FALL_CH, FALL_HOP, FALL_CONSEC,
    FALL_THRESHOLD, FALL_IN_SCALE, FALL_IN_ZP, FALL_OUT_SCALE, FALL_OUT_ZP,
};

bool close_to(float a, float b) { return std::fabs(a - b) <= 1e-6f * std::fabs(b); }

}  // namespace

extern "C" const fall_params_t *fall_model_params(void) { return &g_params; }

extern "C" esp_err_t fall_model_init(void)
{
    const tflite::Model *model = tflite::GetModel(g_fall_model);
    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "model schema %lu != supported %d", (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
        return ESP_FAIL;
    }
    // Exactly the ops listed in fall_model_data.h ("TFLM ops to register")
    static tflite::MicroMutableOpResolver<7> resolver;
    resolver.AddConv2D();
    resolver.AddExpandDims();
    resolver.AddFullyConnected();
    resolver.AddLogistic();
    resolver.AddMaxPool2D();
    resolver.AddMean();
    resolver.AddReshape();

    static tflite::MicroInterpreter interp(model, resolver, g_arena, kArenaSize);
    if (interp.AllocateTensors() != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors failed: increase SAFETY_TENSOR_ARENA_KB");
        return ESP_ERR_NO_MEM;
    }
    g_interp = &interp;
    g_in = interp.input(0);
    g_out = interp.output(0);

    // The header and the model must agree, otherwise the firmware feeds wrong inputs
    const bool shape_ok = g_in->type == kTfLiteInt8 && g_out->type == kTfLiteInt8 &&
                          g_in->dims->size == 3 && g_in->dims->data[1] == FALL_WIN &&
                          g_in->dims->data[2] == FALL_CH;
    const bool quant_ok = close_to(g_in->params.scale, FALL_IN_SCALE) && g_in->params.zero_point == FALL_IN_ZP &&
                          close_to(g_out->params.scale, FALL_OUT_SCALE) && g_out->params.zero_point == FALL_OUT_ZP;
    if (!shape_ok || !quant_ok) {
        ESP_LOGE(TAG, "model tensors do not match fall_model_data.h (shape_ok=%d quant_ok=%d)", shape_ok, quant_ok);
        return ESP_ERR_INVALID_STATE;
    }
    ESP_LOGI(TAG, "model %u bytes, arena used %u of %u bytes, threshold %.3f, %d consecutive windows",
             g_fall_model_len, (unsigned)interp.arena_used_bytes(), (unsigned)kArenaSize,
             (double)FALL_THRESHOLD, FALL_CONSEC);
    return ESP_OK;
}

extern "C" size_t fall_model_arena_used(void) { return g_interp ? g_interp->arena_used_bytes() : 0; }

extern "C" float fall_model_run(const int8_t *input, int8_t *raw_out)
{
    std::memcpy(g_in->data.int8, input, FALL_WIN * FALL_CH);
    if (g_interp->Invoke() != kTfLiteOk) {
        ESP_LOGE(TAG, "Invoke failed");
        return 0.0f;
    }
    const int8_t q = g_out->data.int8[0];
    if (raw_out) {
        *raw_out = q;
    }
    return ((float)q - (float)FALL_OUT_ZP) * FALL_OUT_SCALE;
}

extern "C" bool fall_model_selftest(void)
{
#if CONFIG_SAFETY_MODEL_SELFTEST
    int exact = 0, max_diff = 0;
    int64_t t_total = 0;
    for (int i = 0; i < SELFTEST_N; i++) {
        int8_t q = 0;
        const int64_t t0 = esp_timer_get_time();
        fall_model_run(&SELFTEST_IN[i * SELFTEST_LEN], &q);
        t_total += esp_timer_get_time() - t0;
        const int d = std::abs((int)q - (int)SELFTEST_OUT[i]);
        exact += d == 0;
        max_diff = d > max_diff ? d : max_diff;
        if (d > 1) {
            ESP_LOGE(TAG, "self-test window %d: TFLM %d, reference %d", i, q, SELFTEST_OUT[i]);
        }
    }
    const bool ok = max_diff <= 1;
    ESP_LOGI(TAG, "MODEL SELFTEST %s: %d/%d outputs identical to TFLite reference kernels, max diff %d LSB, "
                  "%.2f ms per inference",
             ok ? "PASS" : "FAIL", exact, SELFTEST_N, max_diff, (double)t_total / SELFTEST_N / 1000.0);
    return ok;
#else
    return true;
#endif
}
