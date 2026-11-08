#!/usr/bin/env bash
# End-to-end firmware test in QEMU, no hardware needed.
#
#   esp32   : replay build -> QEMU with emulated Ethernet -> supervisor.py on the host.
#             The supervisor starts SUP_DELAY_S after the firmware, so the first alerts
#             can only get through by store-and-forward; it acknowledges alerts itself.
#   esp32s3 : replay build -> QEMU (no network model for this chip: alerts are only logged)
#
# Passes when the firmware's events match the Python device model, TFLite Micro
# reproduces the TFLite reference outputs, and (esp32) every message reached the
# supervisor in order and every alert was acknowledged back to the device.
#
# Needs an ESP-IDF environment (. $IDF_PATH/export.sh) with qemu-xtensa installed:
#   python $IDF_PATH/tools/idf_tools.py install qemu-xtensa && . $IDF_PATH/export.sh
# Usage (from the firmware/ directory): tools/run_qemu_replay.sh [esp32|esp32s3]
set -euo pipefail

TARGET=${1:-esp32}
BUILD=build_qemu_$TARGET
PORT=8080
TIMEOUT_S=${TIMEOUT_S:-600}
SUP_DELAY_S=${SUP_DELAY_S:-20}

DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.qemu"
NET=1
if [ "$TARGET" != "esp32" ]; then
    DEFAULTS="$DEFAULTS;sdkconfig.defaults.qemu_nonet"   # OpenCores Ethernet exists only for esp32
    NET=0
fi

idf.py -B "$BUILD" -D SDKCONFIG="$BUILD/sdkconfig" -D SDKCONFIG_DEFAULTS="$DEFAULTS" set-target "$TARGET" build

cd "$BUILD"
esptool.py --chip "$TARGET" merge_bin --fill-flash-size 4MB -o flash_qemu.bin @flash_args
rm -f qemu.log supervisor.jsonl supervisor.log

# Same machine options and default eFuses as `idf.py qemu` (tools/idf_py_actions/qemu_ext.py)
CHIP_ARGS=$(python3 - "$TARGET" <<'EOF'
import os, sys
sys.path.insert(0, os.path.join(os.environ["IDF_PATH"], "tools"))
from idf_py_actions.qemu_ext import QEMU_TARGETS
t = QEMU_TARGETS[sys.argv[1]]
open("qemu_efuse.bin", "wb").write(t.default_efuse)
print(t.qemu_args)
EOF
)

NIC=(-nic none)
if [ $NET -eq 1 ]; then
    # supervisor comes up late on purpose: alerts must wait in the device's queue
    (sleep "$SUP_DELAY_S"; exec python3 ../tools/supervisor.py --port "$PORT" --log supervisor.jsonl \
        --auto-ack 1 > supervisor.log 2>&1) &
    SUP=$!
    NIC=(-nic user,model=open_eth)
fi
# shellcheck disable=SC2086
qemu-system-xtensa $CHIP_ARGS -display none \
    -drive file=flash_qemu.bin,if=mtd,format=raw \
    -drive file=qemu_efuse.bin,if=none,format=raw,id=efuse \
    -global driver=nvram."$TARGET".efuse,property=drive,value=efuse \
    -global driver=timer."$TARGET".timg,property=wdt_disable,value=true \
    "${NIC[@]}" -serial file:qemu.log -monitor none &
QEMU=$!
trap 'kill $QEMU ${SUP:-} 2>/dev/null || true' EXIT

for _ in $(seq "$TIMEOUT_S"); do
    grep -q "REPLAY RESULT" qemu.log 2>/dev/null && break
    kill -0 $QEMU 2>/dev/null || break
    sleep 1
done
sleep 2
echo "================ firmware log ($TARGET) ================"
cat qemu.log

status=0
grep -q "REPLAY RESULT: PASS" qemu.log || { echo "FAIL: replay result not PASS"; status=1; }
grep -q "MODEL SELFTEST PASS" qemu.log || { echo "FAIL: model self-test"; status=1; }
if [ $NET -eq 1 ]; then
    echo "================ supervisor ================"
    cat supervisor.log
    python3 - <<'EOF' || status=1
import json, sys
recs = [json.loads(l) for l in open("supervisor.jsonl")]
alerts = [r for r in recs if r["kind"] == "alert"]
acks = {r["id"] for r in recs if r["kind"] == "ack"}
events = [a["event"] for a in alerts]
print("supervisor received, in order:", events)
need = ["ALERT_FALL", "ALERT_CLEARED", "ALERT_VITALS", "ALERT_CLEARED", "ALERT_MAN_DOWN", "ALERT_CLEARED",
        "DEVICE_FAULT", "DEVICE_OK"]
needs_ack = [a["id"] for a in alerts if a.get("needs_ack")]
print("acknowledged:", sorted(acks), "of", needs_ack)
fall = next((a for a in alerts if a["event"] == "ALERT_FALL"), {})
md = next((a for a in alerts if a["event"] == "ALERT_MAN_DOWN"), {})
print(f"ALERT_FALL posture={fall.get('posture')} impact_g={fall.get('impact_g')}; "
      f"ALERT_MAN_DOWN posture={md.get('posture')}")
ok = (events == need and len({a["id"] for a in alerts}) == len(alerts) and set(needs_ack) == acks
      and md.get("posture") == "lying" and fall.get("impact_g", 0) > 1.5)
sys.exit(0 if ok else 1)
EOF
fi
[ $status -eq 0 ] && echo "QEMU END-TO-END TEST PASSED ($TARGET)" || echo "QEMU END-TO-END TEST FAILED ($TARGET)"
exit $status
