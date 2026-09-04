#!/bin/sh
# Start the temporary create-api bridge, then the existing Kindle Tools WAF.

BASE=/mnt/us/Kindle_Tools
PAYLOAD="$BASE/v2-safe-restore/create-api-step"
RUNTIME=/tmp/kindle-tools-create-api-bridge
PIDFILE="$RUNTIME/server.pid"
LOG="$BASE/logs/create-api-bridge-launch.log"
APP_ID=com.local.kindtools
DB=/var/local/appreg.db
DEST=/var/local/mesquite/com.local.kindtools
EXPECTED_OLD=a0cf0cb75bbab7ab426d4cd9f8148ccacf1a2b24aee18c4a525da2bf45e99c0f
EXPECTED_NEW=f92658c27d7a553b07c9e88eac5afdd4b9c75dfcf25b443d468f615ae1291b36

mkdir -p "$BASE/logs" "$RUNTIME" || exit 1
exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%dT%H:%M:%S%z') ==="
[ -r "$DEST/index.html" ] && [ -r "$PAYLOAD/index.html" ] || exit 7
current_ui=$(sha256sum "$DEST/index.html" 2>/dev/null); current_ui=${current_ui%% *}
if [ "$current_ui" = "$EXPECTED_OLD" ]; then
    cp "$DEST/index.html" "$BASE/backups/pre-unified-api-index.html" || exit 17
    cp "$PAYLOAD/index.html" "$DEST/index.html" || { cp "$BASE/backups/pre-unified-api-index.html" "$DEST/index.html" 2>/dev/null; exit 18; }
    installed_ui=$(sha256sum "$DEST/index.html" 2>/dev/null); installed_ui=${installed_ui%% *}
    [ "$installed_ui" = "$EXPECTED_NEW" ] || { cp "$BASE/backups/pre-unified-api-index.html" "$DEST/index.html" 2>/dev/null; exit 19; }
elif [ "$current_ui" != "$EXPECTED_NEW" ]; then
    echo "ABORT: installed UI hash is unknown"
    exit 20
fi
test_count=$(sqlite3 -readonly "$DB" "SELECT count(*) FROM handlerIds WHERE handlerId='com.local.kindtools.test';" 2>/dev/null) || exit 9
[ "$test_count" = 0 ] || { echo "ABORT: old AppMgr test handler still exists"; exit 8; }

if [ -r "$PIDFILE" ]; then
    oldpid=$(cat "$PIDFILE" 2>/dev/null)
    case "$oldpid" in
        *[!0-9]*|'') ;;
        *)
            if [ -r "/proc/$oldpid/cmdline" ]; then
                cmd=$(tr '\000' ' ' <"/proc/$oldpid/cmdline" 2>/dev/null)
                case "$cmd" in *'/tmp/kindle-tools-create-api-bridge/request.sh'*) kill -TERM "$oldpid" 2>/dev/null || true; sleep 1 ;; esac
            fi
            ;;
    esac
fi
rmdir "$RUNTIME/consumed-status" 2>/dev/null || true
rmdir "$RUNTIME/consumed-snapshot" 2>/dev/null || true
rmdir "$RUNTIME/consumed-mtk-preflight" 2>/dev/null || true
rmdir "$RUNTIME/consumed-on" 2>/dev/null || true
rmdir "$RUNTIME/consumed-off" 2>/dev/null || true
rmdir "$RUNTIME/consumed-restart-existing" 2>/dev/null || true
rmdir "$RUNTIME/consumed-pair-joycon-once" 2>/dev/null || true
rmdir "$RUNTIME/consumed-arm-pair-api" 2>/dev/null || true
rmdir "$RUNTIME/consumed-create-pair-api" 2>/dev/null || true
rmdir "$RUNTIME/consumed-full-stop" 2>/dev/null || true
rmdir "$RUNTIME/consumed-force-day" 2>/dev/null || true
rmdir "$RUNTIME/consumed-arm-create" 2>/dev/null || true
rmdir "$RUNTIME/consumed-create-api" 2>/dev/null || true
cp "$PAYLOAD/request.sh" "$RUNTIME/request.sh" || exit 10
cp "$PAYLOAD/server.sh" "$RUNTIME/server.sh" || exit 11
chmod 700 "$RUNTIME/request.sh" "$RUNTIME/server.sh" || exit 12
/bin/sh "$RUNTIME/server.sh" &
server_pid=$!
printf '%s\n' "$server_pid" >"$PIDFILE"
sleep 1
kill -0 "$server_pid" 2>/dev/null || { echo "ABORT: create-api bridge failed to start"; exit 13; }
echo "bridge_pid=$server_pid"

stale=""
for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    cmd=$(tr '\000' ' ' <"$proc/cmdline" 2>/dev/null)
    case "$cmd" in */usr/bin/mesquite*'-l com.local.kindtools'*) stale="$stale ${proc##*/}" ;; esac
done
for pid in $stale; do
    cmd=$(tr '\000' ' ' <"/proc/$pid/cmdline" 2>/dev/null)
    case "$cmd" in
        */usr/bin/mesquite*'-l com.local.kindtools'*) kill -TERM "$pid" 2>/dev/null || exit 14 ;;
        *) echo "ABORT: WAF pid changed before signal"; exit 15 ;;
    esac
done
if [ -n "$stale" ]; then
    waited=0
    while [ "$waited" -lt 5 ]; do
        remaining=""
        for pid in $stale; do kill -0 "$pid" 2>/dev/null && remaining="$remaining $pid"; done
        [ -z "$remaining" ] && break
        sleep 1
        waited=$((waited + 1))
    done
    [ -z "$remaining" ] || { echo "ABORT: WAF did not exit; no second launch"; exit 16; }
fi
lipc-set-prop com.lab126.appmgrd start "app://$APP_ID"
echo "launch_rc=$?"
