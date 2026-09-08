#!/bin/sh
# Kindle Tools unified KUAL backend. No autostart, pairing, reboot, or retry loop.
umask 077

ACTION=${1:-}
BASE=/mnt/us/kindle_hid_passthrough
BIN="$BASE/kindle-hid-passthrough"
TOOLS=/mnt/us/Kindle_Tools
LOGROOT="$TOOLS/logs"
ARM="$TOOLS/api-create.arm"
PAIR_ARM="$TOOLS/pair-api-create.arm"
EXPECTED_VERSION=3.15.2-a4175a9
mkdir -p "$LOGROOT" || exit 1
OUT="$LOGROOT/menu-$ACTION-$(date +%Y%m%d-%H%M%S)-$$"
mkdir "$OUT" || exit 1
exec >"$OUT/action.log" 2>&1

alert() {
    title=$(printf '%s' "Kindle Tools" | sed 's/"/\\"/g')
    body=$(printf '%s' "$1" | sed 's/"/\\"/g')
    json='{ "clientParams":{"alertId":"appAlert1","show":true,"customStrings":[{"matchStr":"alertTitle","replaceStr":"'"$title"'"},{"matchStr":"alertText","replaceStr":"'"$body"'"}]}}'
    lipc-set-prop com.lab126.pillow pillowAlert "$json" >/dev/null 2>&1 || true
    printf '%s\n' "$1" >"$TOOLS/last-result.txt" 2>/dev/null || true
    echo "RESULT: $1"
}

status() {
    curl --noproxy '*' -fsS --connect-timeout 1 --max-time 2 http://127.0.0.1:8321/status
}

valid_status() {
    grep -Eq '"version"[[:space:]]*:[[:space:]]*"3\.15\.2-a4175a9"' "$1" &&
    grep -Eq '"daemon_running"[[:space:]]*:[[:space:]]*(true|false)' "$1"
}

get_status() {
    status >"$1" 2>>"$OUT/api-errors.log" && valid_status "$1"
}

wait_state() {
    wanted=$1
    file=$2
    i=0
    while [ "$i" -lt 10 ]; do
        sleep 1
        if get_status "$file" && grep -Eq "\"daemon_running\"[[:space:]]*:[[:space:]]*$wanted" "$file"; then
            return 0
        fi
        i=$((i + 1))
    done
    return 1
}

exact_pids() {
    for proc in /proc/[0-9]*; do
        [ -r "$proc/cmdline" ] || continue
        cmd=$(tr '\000' ' ' <"$proc/cmdline" 2>/dev/null)
        case "$cmd" in
            *kindle_hid_passthrough/dist/main.bin*--daemon*|*/kindle_hid_passthrough/kindle-hid-passthrough*--daemon*)
                echo "${proc##*/}" ;;
        esac
    done
}

stock_bt_ready() {
    line=$(initctl status btmanagerd 2>/dev/null) || return 1
    case "$line" in *'start/running, process '*) ;; *) return 1;; esac
    pid=${line##*process }
    case "$pid" in *[!0-9]*|'') return 1;; esac
    [ -d "/proc/$pid/fd" ] || return 1
    for fd in "/proc/$pid/fd/"*; do
        [ "$(readlink "$fd" 2>/dev/null)" = /dev/stpbt ] && return 0
    done
    return 1
}

validate_creation_config() {
    active=$(grep -v '^[[:space:]]*#' "$BASE/devices.conf" 2>/dev/null | grep -v '^[[:space:]]*$')
    [ "$(printf '%s\n' "$active" | wc -l | tr -d ' ')" = 3 ] || return 1
    printf '%s\n' "$active" | grep -Eq '^[0-9A-Fa-f:]{17}[[:space:]]+ble[[:space:]]+KeyKey Mini BLE1$' || return 1
    printf '%s\n' "$active" | grep -Eq '^[0-9A-Fa-f:]{17}[[:space:]]+classic[[:space:]]+Joy-Con \(R\)$' || return 1
    printf '%s\n' "$active" | grep -Eq '^[0-9A-Fa-f:]{17}[[:space:]]+classic[[:space:]]+Joy-Con \(L\)$' || return 1
    grep -Eq '^connect_timeout[[:space:]]*=[[:space:]]*120[[:space:]]*$' "$BASE/config.ini"
}

connection_label() {
    json=$1
    connections=$(sed -n 's/.*"connections"[[:space:]]*:[[:space:]]*\[\(.*\)\][[:space:]]*,[[:space:]]*"ok".*/\1/p' "$json")
    [ -n "$connections" ] || { echo 'no device connected'; return; }
    has_keykey=no
    has_joycon_r=no
    has_joycon_l=no
    keykey_addr=$(awk '$2 == "ble" && index($0, "KeyKey Mini BLE1") { print $1; exit }' "$BASE/devices.conf")
    joycon_r_addr=$(awk '$2 == "classic" && index($0, "Joy-Con (R)") { print $1; exit }' "$BASE/devices.conf")
    joycon_l_addr=$(awk '$2 == "classic" && index($0, "Joy-Con (L)") { print $1; exit }' "$BASE/devices.conf")
    [ -n "$keykey_addr" ] && printf '%s' "$connections" | grep -qi "$keykey_addr" && has_keykey=yes
    [ -n "$joycon_r_addr" ] && printf '%s' "$connections" | grep -qi "$joycon_r_addr" && has_joycon_r=yes
    [ -n "$joycon_l_addr" ] && printf '%s' "$connections" | grep -qi "$joycon_l_addr" && has_joycon_l=yes
    if [ "$has_keykey" = yes ] && { [ "$has_joycon_r" = yes ] || [ "$has_joycon_l" = yes ]; }; then
        echo 'KeyKey + Joy-Con connected'
    elif [ "$has_keykey" = yes ]; then
        echo 'KeyKey connected'
    elif [ "$has_joycon_r" = yes ] && [ "$has_joycon_l" = yes ]; then
        echo 'Joy-Con (R) + Joy-Con (L) connected'
    elif [ "$has_joycon_r" = yes ]; then
        echo 'Joy-Con (R) connected'
    elif [ "$has_joycon_l" = yes ]; then
        echo 'Joy-Con (L) connected'
    else
        echo 'unknown device connected'
    fi
}

api_action() {
    endpoint=$1
    wanted=$2
    get_status "$OUT/status-before.json" || { alert "API unavailable or wrong version; no action"; return 1; }
    if grep -Eq "\"daemon_running\"[[:space:]]*:[[:space:]]*$wanted" "$OUT/status-before.json"; then
        alert "Already daemon_running=$wanted"
        return 0
    fi
    curl --noproxy '*' -fsS --connect-timeout 1 --max-time 5 \
        "http://127.0.0.1:8321/$endpoint" >"$OUT/$endpoint-response.json" 2>>"$OUT/api-errors.log" || {
        alert "$endpoint result uncertain; not retried"; return 1; }
    wait_state "$wanted" "$OUT/status-final.json" || {
        alert "$endpoint sent; target state not verified"; return 1; }
    alert "Verified daemon_running=$wanted"
}

case "$ACTION" in
    on) api_action start true ;;
    off) api_action stop false ;;
    connect-joycon-r|connect-joycon-l)
        get_status "$OUT/status-before.json" || { alert "API unavailable; nothing was changed"; exit 1; }
        suffix=R; [ "$ACTION" = connect-joycon-l ] && suffix=L
        addr=$(awk -v n="Joy-Con ($suffix)" '$2 == "classic" && index($0, n) { print $1; exit }' "$BASE/devices.conf")
        [ -n "$addr" ] || { alert "Saved Joy-Con ($suffix) not found"; exit 1; }
        curl --noproxy '*' -fsS --connect-timeout 1 --max-time 5 \
            "http://127.0.0.1:8321/retry-classic?addr=$addr" >"$OUT/response.json" 2>>"$OUT/api-errors.log" || {
            alert "Connect request uncertain; not retried"; exit 1; }
        grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$OUT/response.json" || { alert "Connect request refused"; exit 1; }
        alert "Connecting Joy-Con ($suffix)…"
        i=0
        while [ "$i" -lt 13 ]; do
            sleep 2
            if get_status "$OUT/status-check-$i.json" && grep -qi "$addr" "$OUT/status-check-$i.json"; then
                alert "Joy-Con ($suffix) connected"
                exit 0
            fi
            i=$((i + 1))
        done
        alert "Joy-Con ($suffix) connection failed"
        exit 1
        ;;
    mtk-preflight)
        REPORT="$OUT/mtk-preflight.txt"
        {
            echo "timestamp=$(date '+%Y-%m-%dT%H:%M:%S%z')"
            echo "uptime=$(cat /proc/uptime 2>/dev/null)"
            echo "kernel=$(uname -a 2>/dev/null)"
            echo "--- memory ---"
            grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):' /proc/meminfo 2>/dev/null
            echo "--- relevant processes ---"
            ps 2>/dev/null | grep -E '[b]tmanagerd|[a]csbtfd|[w]mt_service|[m]tk_wmtd|[w]ifid|[w]ifim|kindle[-_]hid[-_]passthrough|main.bin.*--daemon' || true
            echo "--- upstart status ---"
            for job in wmt btmanagerd acsbtfd hid-passthrough; do
                printf '%s: ' "$job"
                /sbin/initctl status "$job" 2>&1 || true
            done
            echo "--- stpbt metadata ---"
            ls -l /dev/stpbt 2>&1 || true
            echo "--- stpbt holders (readlink only; device is not opened) ---"
            found=0
            for proc in /proc/[0-9]*; do
                [ -d "$proc/fd" ] || continue
                for fd in "$proc"/fd/*; do
                    target=$(readlink "$fd" 2>/dev/null) || continue
                    [ "$target" = /dev/stpbt ] || continue
                    pid=${proc##*/}
                    comm=$(cat "$proc/comm" 2>/dev/null)
                    cmd=$(tr '\000' ' ' <"$proc/cmdline" 2>/dev/null)
                    echo "pid=$pid comm=$comm cmd=$cmd fd=${fd##*/}"
                    found=1
                done
            done
            [ "$found" -eq 1 ] || echo "none"
            echo "--- previous-crash facilities (metadata only) ---"
            for path in /proc/last_kmsg /sys/fs/pstore /var/log/messages /var/log/hid_passthrough.log /var/local; do
                if [ -e "$path" ]; then
                    [ -r "$path" ] && readable=yes || readable=no
                    [ -w "$path" ] && writable=yes || writable=no
                    echo "$path exists=yes readable=$readable writable=$writable"
                    ls -ld "$path" 2>/dev/null || true
                else
                    echo "$path exists=no"
                fi
            done
            echo "--- mounts ---"
            grep -E ' /(mnt/us|var/local|var)( |/)' /proc/mounts 2>/dev/null || true
            echo "--- exact HID daemon pids ---"
            exact_pids || true
        } >"$REPORT"
        sync
        alert "MTK preflight saved; no Bluetooth action was run"
        ;;
    status)
        if ! get_status "$OUT/status.json"; then
            alert "HID API unavailable or unexpected version"
            exit 1
        fi
        if grep -Eq '"daemon_running"[[:space:]]*:[[:space:]]*true' "$OUT/status.json"; then state=RUNNING; else state=PARKED; fi
        conn=$(connection_label "$OUT/status.json")
        alert "$EXPECTED_VERSION; $state; $conn"
        ;;
    snapshot)
        /bin/sh "$TOOLS/scripts/hid-incident-snapshot.sh"
        rc=$?
        [ "$rc" -eq 0 ] && alert "Incident snapshot saved" || alert "Snapshot failed; see log"
        exit "$rc"
        ;;
    restart-existing)
        get_status "$OUT/status-before.json" || { alert "API unavailable; restart refused"; exit 1; }
        if grep -Eq '"daemon_running"[[:space:]]*:[[:space:]]*true' "$OUT/status-before.json"; then
            curl --noproxy '*' -fsS --connect-timeout 1 --max-time 5 http://127.0.0.1:8321/stop \
                >"$OUT/stop-response.json" 2>>"$OUT/api-errors.log" || { alert "Stop uncertain; start refused"; exit 1; }
            wait_state false "$OUT/status-parked.json" || { alert "Park not verified; start refused"; exit 1; }
            sleep 1
        fi
        curl --noproxy '*' -fsS --connect-timeout 1 --max-time 5 http://127.0.0.1:8321/start \
            >"$OUT/start-response.json" 2>>"$OUT/api-errors.log" || { alert "Start uncertain; not retried"; exit 1; }
        wait_state true "$OUT/status-final.json" && alert "Existing API daemon restarted once" || { alert "Restart not verified; not retried"; exit 1; }
        ;;
    pair-joycon-once)
        # Public snapshot placeholder. Set this to the target Joy-Con address
        # before using the one-shot pairing action.
        JOY_ADDR='11:22:33:44:55:66'
        ATTEMPT="$TOOLS/joycon-pair.attempted"
        [ ! -e "$ATTEMPT" ] || { alert "Joy-Con pairing was already attempted; no retry"; exit 1; }
        get_status "$OUT/status-before.json" || { alert "API unavailable or wrong version; no pairing"; exit 1; }
        grep -Eq '"pairing"[[:space:]]*:[[:space:]]*false' "$OUT/status-before.json" || { alert "API pairing state is busy or unknown; no pairing"; exit 1; }
        grep -Eq '"scanning"[[:space:]]*:[[:space:]]*false' "$OUT/status-before.json" || { alert "API scan state is busy or unknown; no pairing"; exit 1; }
        active=$(grep -v '^[[:space:]]*#' "$BASE/devices.conf" 2>/dev/null | grep -v '^[[:space:]]*$')
        printf '%s\n' "$active" | grep -Eq '^[0-9A-Fa-f:]{17}[[:space:]]+ble[[:space:]]+KeyKey Mini BLE1$' || { alert "Device configuration changed; no pairing"; exit 1; }
        grep -qi "$JOY_ADDR" "$BASE/devices.conf" "$BASE/cache/pairing_keys.json" 2>/dev/null && { alert "Joy-Con already exists; no pairing"; exit 1; }
        backup="$TOOLS/backups/joycon-prepair-$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$backup/cache" || { alert "Cannot create pairing backup; no pairing"; exit 1; }
        cp "$BASE/devices.conf" "$backup/devices.conf" || { alert "Cannot back up devices.conf; no pairing"; exit 1; }
        cp "$BASE/config.ini" "$backup/config.ini" || { alert "Cannot back up config.ini; no pairing"; exit 1; }
        cp "$BASE/cache/"*.json "$backup/cache/" 2>>"$OUT/backup-errors.log" || { alert "Cannot back up pairing cache; no pairing"; exit 1; }
        printf '%s address=%s backup=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$JOY_ADDR" "$backup" >"$ATTEMPT" || { alert "Cannot create one-shot marker; no pairing"; exit 1; }
        curl --noproxy '*' -fsS --connect-timeout 1 --max-time 5 \
            "http://127.0.0.1:8321/pair?addr=$JOY_ADDR%2FP&protocol=classic&name=Joy-Con%20%28R%29" \
            >"$OUT/pair-response.json" 2>>"$OUT/api-errors.log" || { alert "Pair request uncertain; not retried"; exit 1; }
        grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$OUT/pair-response.json" || { alert "Pair request rejected; not retried"; exit 1; }
        i=0
        while [ "$i" -lt 45 ]; do
            sleep 2
            curl --noproxy '*' -fsS --connect-timeout 1 --max-time 3 \
                http://127.0.0.1:8321/pair-status >"$OUT/pair-status.json" 2>>"$OUT/api-errors.log" || true
            if grep -Eq '"pairing"[[:space:]]*:[[:space:]]*true' "$OUT/pair-status.json" 2>/dev/null; then
                i=$((i + 1)); continue
            fi
            if grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$OUT/pair-status.json" 2>/dev/null &&
               grep -qi "$JOY_ADDR" "$OUT/pair-status.json" 2>/dev/null; then
                get_status "$OUT/status-final.json" || true
                alert "Joy-Con paired once; reconnect USB for review"
                exit 0
            fi
            if grep -Eq '"ok"[[:space:]]*:[[:space:]]*false' "$OUT/pair-status.json" 2>/dev/null; then
                alert "Joy-Con pairing failed; not retried"
                exit 1
            fi
            i=$((i + 1))
        done
        alert "Joy-Con pairing result not verified in 90 seconds; not retried"
        exit 1
        ;;
    arm-pair-api)
        [ ! -e "$TOOLS/pair-api-create.attempted" ] || { alert "Pairing API creation was already attempted"; exit 1; }
        now=$(date +%s) || exit 1
        printf '%s\n' "$now" >"$PAIR_ARM" || exit 1
        alert "Pairing API creation armed for 60 seconds"
        ;;
    create-pair-api)
        ATTEMPT="$TOOLS/pair-api-create.attempted"
        [ ! -e "$ATTEMPT" ] || { alert "Pairing API creation was already attempted; no retry"; exit 1; }
        [ -r "$PAIR_ARM" ] || { alert "Pairing API creation not armed"; exit 1; }
        now=$(date +%s) || exit 1
        armed=$(cat "$PAIR_ARM" 2>/dev/null)
        case "$armed" in *[!0-9]*|'') alert "Invalid pairing API arm marker"; exit 1;; esac
        age=$((now - armed))
        [ "$age" -ge 0 ] && [ "$age" -le 60 ] || { rm -f "$PAIR_ARM"; alert "Pairing API arm expired; no creation"; exit 1; }
        status >"$OUT/status-before.json" 2>"$OUT/api-errors.log"
        rc=$?
        [ "$rc" -eq 7 ] || { rm -f "$PAIR_ARM"; alert "API state not cleanly unavailable; no creation"; exit 1; }
        set -- $(exact_pids)
        [ "$#" -eq 0 ] || { rm -f "$PAIR_ARM"; alert "HID process exists; no creation"; exit 1; }
        active=$(grep -v '^[[:space:]]*#' "$BASE/devices.conf" 2>/dev/null | grep -v '^[[:space:]]*$')
        printf '%s\n' "$active" | grep -Eq '^[0-9A-Fa-f:]{17}[[:space:]]+ble[[:space:]]+KeyKey Mini BLE1$' || { rm -f "$PAIR_ARM"; alert "KeyKey baseline changed; no creation"; exit 1; }
        [ -x "$BIN" ] || { rm -f "$PAIR_ARM"; alert "HID binary unavailable"; exit 1; }
        [ "$(cat "$BASE/dist/kindle_hid_passthrough/BUILD_SHA" 2>/dev/null)" = a4175a9 ] || { rm -f "$PAIR_ARM"; alert "Build mismatch"; exit 1; }
        launcher_hash=$(sha256sum "$BIN" 2>/dev/null); launcher_hash=${launcher_hash%% *}
        main_hash=$(sha256sum "$BASE/dist/main.bin" 2>/dev/null); main_hash=${main_hash%% *}
        [ "$launcher_hash" = 8bea5e753d1263a695d238d5c39c388bc9bd342e51422dc5ddbe67acd8ab5229 ] || { rm -f "$PAIR_ARM"; alert "Launcher hash mismatch"; exit 1; }
        [ "$main_hash" = d912bce9a322ce68ec848d1f88b3c0b9de862cc85b9f6a8ace3c88cb39a065f8 ] || { rm -f "$PAIR_ARM"; alert "Runtime hash mismatch"; exit 1; }
        backup="$TOOLS/backups/pair-api-prep-$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$backup/cache" || { rm -f "$PAIR_ARM"; alert "Cannot create pairing API backup"; exit 1; }
        cp "$BASE/devices.conf" "$backup/devices.conf" || { rm -f "$PAIR_ARM"; alert "Cannot back up devices.conf"; exit 1; }
        cp "$BASE/config.ini" "$backup/config.ini" || { rm -f "$PAIR_ARM"; alert "Cannot back up config.ini"; exit 1; }
        cp "$BASE/cache/"*.json "$backup/cache/" 2>>"$OUT/backup-errors.log" || { rm -f "$PAIR_ARM"; alert "Cannot back up pairing cache"; exit 1; }
        empty="$BASE/devices.conf.pairing-api.$$"
        printf '%s\n' '# Temporarily empty for controlled Joy-Con pairing' '# Previous KeyKey configuration is preserved in Kindle_Tools/backups' >"$empty" || { rm -f "$PAIR_ARM"; alert "Cannot prepare empty device configuration"; exit 1; }
        mv "$empty" "$BASE/devices.conf" || { rm -f "$PAIR_ARM"; alert "Cannot activate empty device configuration"; exit 1; }
        printf '%s address=%s backup=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" '11:22:33:44:55:66' "$backup" >"$ATTEMPT" || { cp "$backup/devices.conf" "$BASE/devices.conf" 2>/dev/null; rm -f "$PAIR_ARM"; alert "Cannot create one-shot marker; configuration restored"; exit 1; }
        rm -f "$PAIR_ARM"
        ( setsid "$BIN" --daemon </dev/null >>"$OUT/daemon-stdio.log" 2>&1 & )
        i=0
        while [ "$i" -lt 20 ]; do
            sleep 1
            if get_status "$OUT/status-final.json" &&
               grep -Eq '"daemon_running"[[:space:]]*:[[:space:]]*true' "$OUT/status-final.json" &&
               grep -Eq '"device_count"[[:space:]]*:[[:space:]]*0' "$OUT/status-final.json" &&
               grep -Eq '"connections"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]' "$OUT/status-final.json"; then
                alert "Pairing API created with zero devices; reconnect USB for review"
                exit 0
            fi
            i=$((i + 1))
        done
        alert "Pairing API state not verified; do not retry"
        exit 1
        ;;
    arm-create)
        now=$(date +%s) || exit 1
        printf '%s\n' "$now" >"$ARM" || exit 1
        alert "API creation armed for 60 seconds"
        ;;
    create-api)
        [ -r "$ARM" ] || { alert "Not armed; no API created"; exit 1; }
        now=$(date +%s) || exit 1
        armed=$(cat "$ARM" 2>/dev/null)
        case "$armed" in *[!0-9]*|'') alert "Invalid arm marker"; exit 1;; esac
        age=$((now - armed))
        [ "$age" -ge 0 ] && [ "$age" -le 60 ] || { rm -f "$ARM"; alert "Arm expired; no API created"; exit 1; }
        status >"$OUT/status-before.json" 2>"$OUT/api-errors.log"
        rc=$?
        [ "$rc" -eq 7 ] || { rm -f "$ARM"; alert "API state not cleanly unavailable; no creation"; exit 1; }
        set -- $(exact_pids)
        [ "$#" -eq 0 ] || { rm -f "$ARM"; alert "HID process exists; no creation"; exit 1; }
        stock_bt_ready || { rm -f "$ARM"; alert "Stock Bluetooth is not warm; no API created"; exit 1; }
        [ -x "$BIN" ] || { rm -f "$ARM"; alert "HID binary unavailable"; exit 1; }
        validate_creation_config || { rm -f "$ARM"; alert "Saved-device configuration mismatch"; exit 1; }
        [ "$(cat "$BASE/dist/kindle_hid_passthrough/BUILD_SHA" 2>/dev/null)" = a4175a9 ] || { rm -f "$ARM"; alert "Build mismatch"; exit 1; }
        launcher_hash=$(sha256sum "$BIN" 2>/dev/null); launcher_hash=${launcher_hash%% *}
        main_hash=$(sha256sum "$BASE/dist/main.bin" 2>/dev/null); main_hash=${main_hash%% *}
        [ "$launcher_hash" = 8bea5e753d1263a695d238d5c39c388bc9bd342e51422dc5ddbe67acd8ab5229 ] || { rm -f "$ARM"; alert "Launcher hash mismatch"; exit 1; }
        [ "$main_hash" = d912bce9a322ce68ec848d1f88b3c0b9de862cc85b9f6a8ace3c88cb39a065f8 ] || { rm -f "$ARM"; alert "Runtime hash mismatch"; exit 1; }
        rm -f "$ARM"
        ( setsid "$BIN" --daemon </dev/null >>"$OUT/daemon-stdio.log" 2>&1 & )
        created=no
        i=0
        while [ "$i" -lt 20 ]; do
            sleep 1
            if get_status "$OUT/status-final.json" &&
               grep -Eq '"daemon_running"[[:space:]]*:[[:space:]]*true' "$OUT/status-final.json" &&
               grep -Eq '"device_count"[[:space:]]*:[[:space:]]*2' "$OUT/status-final.json"; then
                created=yes
                break
            fi
            i=$((i + 1))
        done
        if [ "$created" = yes ]; then
            conn=$(connection_label "$OUT/status-final.json")
            alert "API created once; $conn"
        else
            alert "API not verified within 20 seconds; do not retry"
            exit 1
        fi
        ;;
    full-stop)
        get_status "$OUT/status-before.json" || { alert "API unavailable; no signal sent"; exit 1; }
        if grep -Eq '"daemon_running"[[:space:]]*:[[:space:]]*true' "$OUT/status-before.json"; then
            curl --noproxy '*' -fsS --connect-timeout 1 --max-time 5 http://127.0.0.1:8321/stop \
                >"$OUT/stop-response.json" 2>>"$OUT/api-errors.log" || { alert "Stop uncertain; no signal sent"; exit 1; }
            wait_state false "$OUT/status-parked.json" || { alert "Park not verified; no signal sent"; exit 1; }
        fi
        set -- $(exact_pids)
        [ "$#" -eq 1 ] || { alert "Expected one API PID, found $#; no signal sent"; exit 1; }
        pid=$1
        kill -TERM "$pid" || { alert "SIGTERM failed; not retried"; exit 1; }
        i=0
        while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 15 ]; do
            sleep 1
            i=$((i + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then alert "API still present; no SIGKILL"; exit 1; fi
        alert "API fully stopped; future use requires armed creation"
        ;;
    force-day)
        current=$(lipc-get-prop com.lab126.winmgr epdcMode 2>>"$OUT/lipc-errors.log") || { alert "Cannot read system display mode"; exit 1; }
        echo "before=$current"
        [ "$current" = Y8 ] && { alert "System is already in Y8 day mode"; exit 0; }
        [ "$current" = Y8INV ] || { alert "Unexpected display mode; no change"; exit 1; }
        lipc-set-prop com.lab126.winmgr epdcMode Y8 2>>"$OUT/lipc-errors.log" || { alert "Day-mode request failed"; exit 1; }
        after=$(lipc-get-prop com.lab126.winmgr epdcMode 2>>"$OUT/lipc-errors.log")
        echo "after=$after"
        [ "$after" = Y8 ] && alert "System display mode set to Y8" || { alert "Day mode not verified"; exit 1; }
        ;;
    *) alert "Unknown action; nothing changed"; exit 2 ;;
esac
