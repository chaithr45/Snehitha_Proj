#!/usr/bin/env bash
# End-to-end firmware test in QEMU (ESP32), no hardware needed:
#   replay build -> QEMU with emulated Ethernet -> supervisor.py on the host.
# Passes when the firmware's events match the Python device model, every alert
# reached the supervisor, and TFLite Micro matches the reference model outputs.
#
# Needs an ESP-IDF environment (. $IDF_PATH/export.sh) with qemu-xtensa installed:
#   python $IDF_PATH/tools/idf_tools.py install qemu-xtensa && . $IDF_PATH/export.sh
# Usage (from the firmware/ directory): tools/run_qemu_replay.sh
set -euo pipefail

BUILD=build_qemu
PORT=8080
TIMEOUT_S=${TIMEOUT_S:-600}

idf.py -B "$BUILD" -D SDKCONFIG="$BUILD/sdkconfig" \
       -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.qemu" set-target esp32 build

cd "$BUILD"
esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o flash_qemu.bin @flash_args
rm -f qemu.log supervisor.jsonl

python3 ../tools/supervisor.py --port "$PORT" --log supervisor.jsonl > supervisor.log 2>&1 &
SUP=$!
qemu-system-xtensa -machine esp32 -display none \
    -drive file=flash_qemu.bin,if=mtd,format=raw \
    -nic user,model=open_eth \
    -global driver=timer.esp32.timg,property=wdt_disable,value=true \
    -serial file:qemu.log -monitor none &
QEMU=$!
trap 'kill $QEMU $SUP 2>/dev/null || true' EXIT

for _ in $(seq "$TIMEOUT_S"); do
    grep -q "REPLAY RESULT" qemu.log 2>/dev/null && break
    kill -0 $QEMU 2>/dev/null || break
    sleep 1
done
sleep 2
echo "================ firmware log ================"
cat qemu.log
echo "================ supervisor ================"
cat supervisor.log

status=0
grep -q "REPLAY RESULT: PASS" qemu.log || { echo "FAIL: replay result not PASS"; status=1; }
grep -q "MODEL SELFTEST PASS" qemu.log || { echo "FAIL: model self-test"; status=1; }
python3 - <<'EOF' || status=1
import json, sys
events = [json.loads(l).get("event") for l in open("supervisor.jsonl") if '"kind": "alert"' in l]
print("supervisor received alerts:", events)
need = ["ALERT_FALL", "ALERT_CLEARED", "ALERT_VITALS", "ALERT_CLEARED"]
sys.exit(0 if events == need else 1)
EOF
[ $status -eq 0 ] && echo "QEMU END-TO-END TEST PASSED" || echo "QEMU END-TO-END TEST FAILED"
exit $status
