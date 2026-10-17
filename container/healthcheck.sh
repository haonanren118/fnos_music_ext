#!/bin/sh
# 容器健康检查：按 .env 应启的程序在 supervisor 里均为 RUNNING 才算健康。
set -eu
SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

running() {
    supervisorctl -c "$SUP_CONF" status "$1" 2>/dev/null | grep -q "RUNNING"
}

fail=0
checked=0

check() {
    prog="$1"; key="$2"; default="${3:-false}"
    if [ "$(env_flag "$key" "$default")" = "true" ]; then
        checked=$((checked + 1))
        if ! running "$prog"; then
            echo "[healthcheck] $prog 应运行但状态异常" >&2
            fail=1
        fi
    fi
}

check musicdl FNMUSIC_MUSICDL_ENABLED
check musicbox FNMUSIC_NETEASE_ENABLED
check lxmusic FNMUSIC_LX_ENABLED
check webui FNMUSIC_WEBUI_ENABLED

if [ "$checked" -eq 0 ]; then
    echo "[healthcheck] 未配置任何音源" >&2
    exit 1
fi
exit "$fail"
