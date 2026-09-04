#!/bin/sh
# Fixed controls plus separately armed API creation.

RUNTIME=/tmp/kindle-tools-create-api-bridge
BACKEND=/mnt/us/Kindle_Tools/menu/run.sh

IFS= read -r request || exit 1
while IFS= read -r header; do
    header=$(printf '%s' "$header" | tr -d '\r')
    [ -z "$header" ] && break
done
set -- $request
path=${2%%\?*}
[ "$1" = GET ] || path=invalid
case "$path" in
    /status) action=status ;;
    /snapshot) action=snapshot ;;
    /on) action=on ;;
    /off) action=off ;;
    /restart-existing) action=restart-existing ;;
    /full-stop) action=full-stop ;;
    /force-day) action=force-day ;;
    /arm-create) action=arm-create ;;
    /create-api) action=create-api ;;
    *) printf 'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'; exit 0 ;;
esac

if mkdir "$RUNTIME/consumed-$action" 2>/dev/null; then
    printf 'HTTP/1.1 204 No Content\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'
    /bin/sh "$BACKEND" "$action"
else
    printf 'HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
fi
