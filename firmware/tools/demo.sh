#!/usr/bin/env bash
# Live demo without hardware: the real firmware runs in the ESP32 emulator (QEMU) and
# plays a 4-minute scenario in real time; the supervisor page shows the alerts as they
# arrive, and you acknowledge them by clicking.
#
#   With Docker (Windows / macOS / Linux), from the repository root:
#     docker run --rm -it -p 8080:8080 -v "$PWD":/project -w /project/firmware \
#         espressif/idf:v5.5.6 bash tools/demo.sh
#   With ESP-IDF v5.5 installed (. $IDF_PATH/export.sh), from firmware/:
#     tools/demo.sh
#
# Then open http://localhost:8080/ . Ctrl+C stops the demo.
#   --ci              run unattended (alerts acknowledged automatically), exit 0 on success
#   DEMO_AUTO_ACK=s   acknowledge alerts automatically after s seconds
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD=build_demo
PORT=8080   # inside the container; the firmware posts to 10.0.2.2:8080 (map another host port with -p)
CI_MODE=0
if [ "${1:-}" = "--ci" ]; then
    CI_MODE=1
    DEMO_AUTO_ACK=${DEMO_AUTO_ACK:-2}
fi

if ! command -v qemu-system-xtensa > /dev/null; then
    echo "== installing the ESP32 emulator (one time) =="
    if command -v apt-get > /dev/null && [ "$(id -u)" = 0 ]; then
        apt-get update -qq
        apt-get install -y -qq libgcrypt20 libglib2.0-0 libpixman-1-0 libsdl2-2.0-0 libslirp0 > /dev/null
    fi
    python3 "$IDF_PATH/tools/idf_tools.py" install qemu-xtensa
    # shellcheck disable=SC1091
    . "$IDF_PATH/export.sh" > /dev/null
fi
git config --global --add safe.directory '*' 2> /dev/null || true

echo "== building the firmware (a few minutes the first time) =="
idf.py -B "$BUILD" -D SDKCONFIG="$BUILD/sdkconfig" \
       -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.qemu;sdkconfig.defaults.demo" \
       set-target esp32 build > "$BUILD.log" 2>&1 || { tail -40 "$BUILD.log"; exit 1; }

cd "$BUILD"
esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o flash_demo.bin @flash_args > /dev/null
CHIP_ARGS=$(python3 - <<'EOF'
import os, sys
sys.path.insert(0, os.path.join(os.environ["IDF_PATH"], "tools"))
from idf_py_actions.qemu_ext import QEMU_TARGETS
t = QEMU_TARGETS["esp32"]
open("qemu_efuse.bin", "wb").write(t.default_efuse)
print(t.qemu_args)
EOF
)

ACK=()
if [ -n "${DEMO_AUTO_ACK:-}" ]; then
    ACK=(--auto-ack "$DEMO_AUTO_ACK")
fi
rm -f demo.log demo_alerts.jsonl
python3 ../tools/supervisor.py --port "$PORT" --log demo_alerts.jsonl --offline-after 15 "${ACK[@]}" \
    > supervisor.log 2>&1 &
SUP=$!
# shellcheck disable=SC2086
qemu-system-xtensa $CHIP_ARGS -display none \
    -drive file=flash_demo.bin,if=mtd,format=raw \
    -drive file=qemu_efuse.bin,if=none,format=raw,id=efuse \
    -global driver=nvram.esp32.efuse,property=drive,value=efuse \
    -global driver=timer.esp32.timg,property=wdt_disable,value=true \
    -nic user,model=open_eth -serial file:demo.log -monitor none &
QEMU=$!
touch demo.log
tail -n +1 -F demo.log 2> /dev/null \
    | grep --line-buffered -E "SEGMENT|EVENT|BUZZER|BUTTON|DEMO|ACK|RESET|REPLAY RESULT|SELFTEST|network up" \
    | sed -u -E 's/^(\x1b\[[0-9;]*m)?[IWE] \([0-9]+\) //' &
TAIL=$!
trap 'kill $QEMU $SUP $TAIL 2> /dev/null || true' EXIT

echo
echo "=================================================================="
echo "  Supervisor page:  http://localhost:$PORT/"
echo "  When an alert appears there, click Acknowledge."
echo "  The worker's device log follows below. Ctrl+C to stop."
echo "=================================================================="
echo

if [ $CI_MODE -eq 1 ]; then
    for _ in $(seq 900); do
        grep -q "DEMO FINISHED" demo.log 2> /dev/null && break
        kill -0 $QEMU 2> /dev/null || break
        sleep 1
    done
    sleep 1
    grep -q "REPLAY RESULT: PASS" demo.log && echo "DEMO RUN PASSED" || { echo "DEMO RUN FAILED"; exit 1; }
else
    wait $QEMU
fi
