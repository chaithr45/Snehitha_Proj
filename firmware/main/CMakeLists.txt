idf_component_register(
    SRCS "app_main.c" "pipeline.c" "fall_model.cpp" "net.c" "annunciator.c" "button.c"
         "mpu6050.c" "max30102.c" "replay.c"
    INCLUDE_DIRS "."
    # Repository root: fall_model_data.h written by phase1_pipeline.py (single source of truth)
    PRIV_INCLUDE_DIRS "../..")
