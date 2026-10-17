#!/bin/sh
# fnmusic-sources 容器入口：supervisor 起来后按 /repo/.env 只拉起所选音源（+WebUI）。
# 全部程序 autostart=false（supervisord.conf），实现按需加载：
# 未启用的音源进程不驻留内存；容器重启后本脚本按 .env 自愈恢复同样的进程集。
set -eu

SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

log() { echo "[entrypoint] $*"; }

export_source_env

# lxserver（#55 公共歌单发现层）数据目录与初始配置落盘。
# 注意：lxserver v2.1.3 只接线了 PORT 环境变量，BIND_IP 实际不生效（代码里被注释），
# 绑定地址 127.0.0.1 只能靠 config.js 保证 —— 缺配置时 zip 自带的 config.js 是
# 0.0.0.0:9527，会被复制到数据目录，故必须在启动前把安全配置写进 DATA_PATH。
init_lxserver_config() {
    _lx_data="${DATA_PATH:-/data/lxserver}"
    _cfg_file="${CONFIG_PATH:-${_lx_data}/config.js}"
    mkdir -p "${_lx_data}"
    if [ ! -f "${_cfg_file}" ]; then
        cat <<EOF > "${_cfg_file}"
module.exports = {
  serverName: "fnmusic-lxserver",
  bindIP: "127.0.0.1",
  port: 8005,
  "player.enableAuth": false,
  "subsonic.enable": false,
  "webdav.enable": false
};
EOF
        log "已初始化 lxserver 配置: ${_cfg_file}"
    fi
}

init_lxserver_config

supervisord -c "$SUP_CONF" &
SUP_PID=$!

# 等 supervisor 控制套接字就绪（最多 10s）。
# 用 pid 而非 status：status 的退出码反映“是否全部程序 RUNNING”，全 STOPPED
# （autostart=false 的初始态）也返回非 0，用它会白等满 10s 才超时跳出。
_i=0
until supervisorctl -c "$SUP_CONF" pid >/dev/null 2>&1; do
    _i=$((_i + 1))
    if [ "$_i" -ge 100 ]; then
        log "supervisor 套接字未就绪，继续等待（进程状态将由 healthcheck 反映）"
        break
    fi
    sleep 0.1
done

start_prog() {
    if supervisorctl -c "$SUP_CONF" start "$1" >/dev/null 2>&1; then
        log "started $1"
    else
        log "WARN: start $1 失败（supervisorctl status 查看原因）"
    fi
}

# 三选一音源：只启动 .env 选中的那个（多开视为异常，全部忽略只取第一个命中）。
# 洛雪音源附带 lxserver 发现层（#55 公共歌单）：先起 lxserver 再起 lxmusic，
# 避免 lxmusic 开机预热发现接口时 lxserver 尚未就绪。
if [ "$(env_flag FNMUSIC_MUSICDL_ENABLED)" = "true" ]; then
    start_prog musicdl
elif [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" = "true" ]; then
    start_prog musicbox
elif [ "$(env_flag FNMUSIC_LX_ENABLED)" = "true" ]; then
    start_prog lxserver
    start_prog lxmusic
else
    log "未配置任何音源（FNMUSIC_MUSICDL_ENABLED/FNMUSIC_NETEASE_ENABLED/FNMUSIC_LX_ENABLED 均未启用）"
fi

# WebUI 默认随容器启动（--no-webui 安装会写 FNMUSIC_WEBUI_ENABLED=false）
if [ "$(env_flag FNMUSIC_WEBUI_ENABLED)" = "true" ]; then
    start_prog webui
fi

# 信号转发：docker stop → supervisorctl shutdown → 各程序优雅退出
trap 'supervisorctl -c "$SUP_CONF" shutdown >/dev/null 2>&1' TERM INT
log "fnmusic-sources ready (pid=$SUP_PID)"
wait "$SUP_PID"
