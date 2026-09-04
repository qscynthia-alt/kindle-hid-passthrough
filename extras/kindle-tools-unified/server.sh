#!/bin/sh
exec timeout 180 nc -lk -s 127.0.0.1 -p 8322 -e /tmp/kindle-tools-create-api-bridge/request.sh
