#!/usr/bin/env bash
set -Eeuo pipefail

# ==============================================================================
# fnmusic-ext 一键扩展脚本 (Unix Socket 接管架构)
# 功能：接管 /var/run/trim_music.socket，实现零侵入扩展（严禁修改 nginx 配置）
# 具备幂等性与自动回滚能力
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared outer lock is never acquired by systemd service helpers.
source "${BASE_DIR}/proxy/install_common.sh"
installation_lock "$@"
FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
TARGET_SOCK="/var/run/trim_music.socket"
UPSTREAM_SOCK="/var/run/trim_music_upstream.socket"
MUSICDL_URL="http://127.0.0.1:8768"
MUSICBOX_URL="http://127.0.0.1:8770"
LX_URL="http://127.0.0.1:8772"
FORCE_RELOAD=0
ADOPT=0

for arg in "$@"; do
    case "${arg}" in
        --qr)
            bash "${BASE_DIR}/netease_login.sh"
            exit 0
            ;;
        --force)
            FORCE_RELOAD=1
            ;;
        --adopt)
            # Explicit deployment migration: skip the cross-checkout registry
            # check so this checkout can become the deployment (install_common.sh).
            ADOPT=1
            ;;
        -h|--help)
            echo "用法: $0 [--force] [--adopt] [--qr]"
            echo "  --force  强制重写 unit 并重启代理（安装改配置后使用）"
            echo "  --adopt  把部署迁移到当前目录（部署登记或代理 unit 属于其他目录时使用）"
            echo "  --qr     启动终端网易云扫码登录流程"
            exit 0
            ;;
        *)
            if [ "${arg}" != "" ]; then
                echo "未知参数: ${arg}"
                exit 1
            fi
            ;;
    esac
done

log_info() {
    echo -e "\033[32m[INFO]\033[0m $*"
}

log_warn() {
    echo -e "\033[33m[WARN]\033[0m $*"
}

log_err() {
    echo -e "\033[31m[ERROR]\033[0m $*" >&2
}

is_enabled() {
    case "$(printf '%s' "${1:-true}" | tr '[:upper:]' '[:lower:]')" in
        false|0|no|off) return 1 ;;
        *) return 0 ;;
    esac
}

# ------------------------------------------------------------------------------
# 获取 fnOS 网关 http/https 端口 (读取失败时默认 5666/5667)
# 输出: "<http_port> <https_port>"
# ------------------------------------------------------------------------------
get_fnos_gateway_ports() {
    cat /usr/trim/etc/network_gateway_setting.conf 2>/dev/null | python3 -c '
import sys, json, re
text = sys.stdin.read()
http_port, https_port = "5666", "5667"
def scan(obj):
    global http_port, https_port
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, (int, str)) and str(v).isdigit():
                if "https" in kl and "port" in kl:
                    https_port = str(v)
                elif "http" in kl and "port" in kl:
                    http_port = str(v)
            else:
                scan(v)
    elif isinstance(obj, list):
        for it in obj:
            scan(it)
if text.strip():
    try:
        scan(json.loads(text))
    except Exception:
        pass
    if http_port == "5666":
        m = re.search(r"\"?http_port\"?\s*[:=]\s*(\d+)", text)
        if m:
            http_port = m.group(1)
    if https_port == "5667":
        m = re.search(r"\"?https_port\"?\s*[:=]\s*(\d+)", text)
        if m:
            https_port = m.group(1)
print(http_port, https_port)
' 2>/dev/null || echo "5666 5667"
}

check_proxy_unit_owner "$@" || exit 1
# Refuse to extend from a second checkout while the machine-wide deployment
# registry names another live directory (skip with the explicit --adopt flag).
check_deployment_owner "$@" || exit 1

if [ ! -f "${BASE_DIR}/.env" ]; then
    if [ -t 0 ]; then
        log_warn "检测到尚未完成初次安装配置（未找到 .env 配置文件）。"
        read -r -p "检测到尚未完成初次安装配置，是否现在启动安装向导 (./install.sh)？[Y/n] " prompt_ans || true
        case "${prompt_ans:-y}" in
            y|Y|yes|YES|"")
                log_info "正在启动安装向导 (./install.sh)..."
                exec /bin/bash "${BASE_DIR}/install.sh"
                ;;
            *)
                log_err "请先执行 ./install.sh 完成音源与配置安装。"
                exit 1
                ;;
        esac
    else
        log_err "请先执行 ./install.sh 完成音源与配置安装。"
        exit 1
    fi
fi

set -a
# shellcheck disable=SC1091
source "${BASE_DIR}/.env"
set +a
# .env 里的 FNMUSIC_VERSION 可能是上次安装写进去的旧值，以 VERSION 文件为准。
# 不用 read：文件末尾没有换行时 read 返回 1，set -e 会在打出任何日志前静默退出。
FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"
MUSICDL_URL="${FNMUSIC_MUSICDL_URL:-${MUSICDL_URL}}"
MUSICBOX_URL="${FNMUSIC_MUSICBOX_URL:-${MUSICBOX_URL}}"
LX_URL="${FNMUSIC_LX_URL:-${LX_URL}}"
ENABLE_MUSICDL=0
ENABLE_MUSICBOX=0
ENABLE_LX=0
ENABLE_WEBUI=0
is_enabled "${FNMUSIC_MUSICDL_ENABLED:-true}" && ENABLE_MUSICDL=1
is_enabled "${FNMUSIC_NETEASE_ENABLED:-true}" && ENABLE_MUSICBOX=1
is_enabled "${FNMUSIC_LX_ENABLED:-false}" && ENABLE_LX=1
is_enabled "${FNMUSIC_WEBUI_ENABLED:-false}" && ENABLE_WEBUI=1
if [ "${ENABLE_MUSICDL}" -eq 0 ] && [ "${ENABLE_MUSICBOX}" -eq 0 ] && [ "${ENABLE_LX}" -eq 0 ]; then
    log_err "至少需要启用一个音源（FNMUSIC_MUSICDL_ENABLED / FNMUSIC_NETEASE_ENABLED / FNMUSIC_LX_ENABLED）。"
    exit 1
fi

run_docker() {
    if docker info >/dev/null 2>&1; then
        docker "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        sudo docker "$@"
    else
        return 1
    fi
}


# ------------------------------------------------------------------------------
# 回滚函数 (restore 逻辑)
# ------------------------------------------------------------------------------
rollback() {
    trap - ERR INT TERM
    log_err "部署失败/中断：校验可恢复性并尝试验证回滚（不打印响应正文或环境变量）。"
    if ! plan_json="$(takeover restore-plan 2>/dev/null)"; then
        log_err "无法在停止前确认可恢复身份，保留代理运行状态并中止。请检查归属冲突后重试。"
        exit 1
    fi
    log_info "回滚预检通过 (${plan_json})。"
    takeover remember || log_warn "无法刷新身份记录；将使用既有归属记录恢复。"
    sudo systemctl stop fnmusic-ext.service || log_warn "停止服务失败。"
    if takeover restore; then
        log_info "官方 socket 回滚已验证。"
    else
        log_err "回滚未能验证：保留未知 socket。请检查身份/冲突后重启飞牛音乐。"
    fi
    exit 1
}

# ------------------------------------------------------------------------------
# 验收测试函数
# ------------------------------------------------------------------------------
warn_unregistered_mdl_platforms() {
    # 所选 musicdl 平台不在服务注册表时告警（musicdl 库版本差异可能不含个别平台），不阻断
    [ "${ENABLE_MUSICDL}" -eq 1 ] || return 0
    local raw="${FNMUSIC_ONLINE_SOURCES:-}"
    [ -n "${raw}" ] || return 0
    local body unknown
    body="$(curl -s --max-time 5 "${MUSICDL_URL}/sources" 2>/dev/null || true)"
    [ -n "${body}" ] || return 0
    unknown="$(python3 - "${raw}" "${body}" <<'PY' 2>/dev/null || true
import json, sys
raw, body = sys.argv[1], sys.argv[2]
try:
    registered = {str(x).strip().lower().removesuffix("musicclient")
                  for x in json.loads(body).get("registered", [])}
except Exception:
    sys.exit(0)
unknown = [s for s in (x.strip() for x in raw.split(",")) if s
           and s.lower().removesuffix("musicclient") not in registered]
if unknown:
    print(",".join(unknown))
PY
)" || true
    [ -n "${unknown}" ] && log_warn "musicdl 平台 [${unknown}] 不在服务注册表中（musicdl 库版本差异），相关平台搜索将返回空。"
    return 0
}

verify_acceptance() {
    log_info "==> 执行链路与功能验收..."
    warn_unregistered_mdl_platforms

    local GW_HTTP_PORT GW_HTTPS_PORT
    read -r GW_HTTP_PORT GW_HTTPS_PORT <<< "$(get_fnos_gateway_ports)"
    log_info "fnOS 网关端口: http=${GW_HTTP_PORT} https=${GW_HTTPS_PORT}"

    # 6a. 401 快速路径响应与时延测试 (< 3s)
    log_info "验收 6a: 验证未登录 401/99999 快速路径透传 (耗时必须 < 3s)..."
    local url_https="https://127.0.0.1:${GW_HTTPS_PORT}/music/api/v1/search/track?keyword=test"
    local url_http="http://127.0.0.1:${GW_HTTP_PORT}/music/api/v1/search/track?keyword=test"
    local url_443="https://127.0.0.1/music/api/v1/search/track?keyword=test"
    local resp_file http_code="" time_total="" resp_content="" probe_stats
    resp_file="$(mktemp)"

    # 探测成功以拿到 HTTP 状态码为准；不同 fnOS 版本的未登录响应正文可能为空或格式不同。
    probe_401() {
        : > "${resp_file}"
        probe_stats="$(curl "$@" -w "%{http_code} %{time_total}" -o "${resp_file}" 2>/dev/null || true)"
        http_code="${probe_stats%% *}"
        time_total="${probe_stats##* }"
        [ -n "${http_code}" ] && [ "${http_code}" != "000" ]
    }

    # 优先通过 Unix socket 探测，依次回退：网关 https -> 网关 http -> 443
    if ! probe_401 -s --max-time 8 --unix-socket "${TARGET_SOCK}" "http://localhost/music/api/v1/search/track?keyword=test"; then
        log_warn "socket 探测异常，尝试 fallback 访问网关 https 端口 (${GW_HTTPS_PORT})..."
        if ! probe_401 -sk --max-time 8 "${url_https}"; then
            log_warn "网关 https 端口连接异常，尝试 fallback 访问网关 http 端口 (${GW_HTTP_PORT})..."
            if ! probe_401 -s --max-time 8 "${url_http}"; then
                log_warn "网关 http 端口连接异常，尝试 fallback 访问 443 端口 (302 跳转)..."
                probe_401 -skL --max-time 8 "${url_443}" || true
            fi
        fi
    fi
    resp_content="$(cat "${resp_file}" 2>/dev/null || true)"
    rm -f "${resp_file}"

    if echo "${resp_content}" | grep -q 'INVALID TOKEN\|"code":99999\|code:99999'; then
        :
    elif [ "${http_code}" = "401" ]; then
        log_info "验收 6a：未登录响应为 401（正文非 INVALID TOKEN 格式，按状态码判定）。"
    else
        log_err "验收 6a 失败：未收到预期的 INVALID TOKEN/401 响应 (HTTP=${http_code:-000})。响应正文已隐藏"
        return 1
    fi

    local is_fast
    is_fast="$(awk -v t="${time_total}" 'BEGIN{print (t < 3.0) ? "1" : "0"}')"
    if [ "${is_fast}" != "1" ]; then
        log_err "验收 6a 失败：401 请求耗时过长 (${time_total}s >= 3s)，快速路径可能被阻塞！"
        return 1
    fi
    log_info "验收 6a 通过：未登录拒绝正确透传，耗时 ${time_total}s (< 3s)。"

    # 6b. 在线音频取流与全链路测试 (Range: bytes=0-1048575 -> 200/206)
    log_info "验收 6b: 验证在线播放全链路取流 (Range 200/206 及数据流传输)..."

    local probe_keywords=("晴天" "海阔天空" "稻香")
    local search_any_result=0

    # 从指定音源搜索候选歌曲，逐行输出 id（可能为空）
    search_probe_ids() {
        local source="$1" keyword="$2" encoded
        encoded="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "${keyword}" 2>/dev/null || true)"
        [ -z "${encoded}" ] && return 0
        if [ "${source}" = "musicdl" ]; then
            # 按用户选择的 musicdl 平台白名单探测（FNMUSIC_ONLINE_SOURCES，空=服务默认白名单）
            local mdl_sources_q=""
            [ -n "${FNMUSIC_ONLINE_SOURCES:-}" ] && mdl_sources_q="&sources=${FNMUSIC_ONLINE_SOURCES}"
            curl -s --max-time 20 "${MUSICDL_URL}/search?keyword=${encoded}&limit=3${mdl_sources_q}" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    for it in (d.get('items') or [])[:3]:
        i=it.get('id')
        if i: print(i)
except Exception:
    pass" 2>/dev/null || true
        elif [ "${source}" = "lxmusic" ]; then
            # 只轮询用户选择的 lx 平台（LX_SOURCES；未配置时默认 kg/wy/mg/kw 并补充 tx 探测），
            # 避免单一子源故障导致候选题库全灭
            local lx_sub
            local lx_probe_list="${LX_SOURCES:-kg,wy,mg,kw,tx}"
            for lx_sub in ${lx_probe_list//,/ }; do
                [ -z "${lx_sub}" ] && continue
                curl -s --max-time 20 "${LX_URL}/api/v1/search?keyword=${encoded}&limit=5&sources=${lx_sub}" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    rows=d.get('items') if isinstance(d, dict) else None
    if not isinstance(rows, list):
        rows=d.get('data') if isinstance(d, dict) else None
    for it in (rows or [])[:2]:
        i=str(it.get('id') or '')
        if i:
            print(i if i.startswith('lx:') else 'lx:'+i)
except Exception:
    pass" 2>/dev/null || true
            done
        else
            curl -s --max-time 20 "${MUSICBOX_URL}/api/v1/search?keyword=${encoded}&limit=3&type=song" 2>/dev/null | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    rows=d.get('data') if isinstance(d, dict) else None
    if not isinstance(rows, list):
        rows=d.get('songs') if isinstance(d, dict) else None
    for it in (rows or [])[:3]:
        sid=str(it.get('song_id') or it.get('id') or '')
        if sid: print('netease:'+sid)
except Exception:
    pass" 2>/dev/null || true
        fi
    }

    # 对指定 guid 尝试全链路取流，成功返回 0
    try_probe_stream() {
        local probe_id="$1"
        local stream_guid="online:${probe_id}"
        local stream_sock="http://localhost/music/api/v1/track/stream?guid=${stream_guid}"
        local stream_https="https://127.0.0.1:${GW_HTTPS_PORT}/music/api/v1/track/stream?guid=${stream_guid}"
        local stream_http="http://127.0.0.1:${GW_HTTP_PORT}/music/api/v1/track/stream?guid=${stream_guid}"
        local stream_443="https://127.0.0.1/music/api/v1/track/stream?guid=${stream_guid}"
        local out_file http_code recv_size=0
        out_file="$(mktemp)"
        log_info "试播 guid=${stream_guid}"
        http_code="$(curl -s -o "${out_file}" -w "%{http_code}" --unix-socket "${TARGET_SOCK}" -H "Range: bytes=0-1048575" --max-time 90 "${stream_sock}" 2>/dev/null || echo "000")"
        if [ "${http_code}" = "000" ]; then
            log_warn "socket 取流异常，尝试 fallback 访问网关 https 端口 (${GW_HTTPS_PORT}) 取流..."
            http_code="$(curl -sk -o "${out_file}" -w "%{http_code}" -H "Range: bytes=0-1048575" --max-time 90 "${stream_https}" 2>/dev/null || echo "000")"
        fi
        if [ "${http_code}" = "000" ]; then
            log_warn "网关 https 端口取流异常，尝试 fallback 访问网关 http 端口 (${GW_HTTP_PORT}) 取流..."
            http_code="$(curl -s -o "${out_file}" -w "%{http_code}" -H "Range: bytes=0-1048575" --max-time 90 "${stream_http}" 2>/dev/null || echo "000")"
        fi
        if [ "${http_code}" = "000" ]; then
            log_warn "网关 http 端口取流异常，尝试 fallback 访问 443 端口取流..."
            http_code="$(curl -skL -o "${out_file}" -w "%{http_code}" -H "Range: bytes=0-1048575" --max-time 90 "${stream_443}" 2>/dev/null || echo "000")"
        fi
        if [ -f "${out_file}" ]; then
            recv_size="$(wc -c < "${out_file}" | tr -d ' ')"
            rm -f "${out_file}"
        fi
        if { [ "${http_code}" = "206" ] || [ "${http_code}" = "200" ]; } && [ "${recv_size}" -gt 10000 ]; then
            if [ "${recv_size}" -lt 500000 ]; then
                log_warn "在线音频流接收大小为 ${recv_size} 字节 (偏小但已收到有效数据)。"
            else
                log_info "在线音频流接收大小为 ${recv_size} 字节 (≈1MB)。"
            fi
            return 0
        fi
        log_warn "取流未成功: guid=${stream_guid} HTTP=${http_code} recv=${recv_size}B"
        return 1
    }

    local sources=()
    [ "${ENABLE_MUSICDL}" -eq 1 ] && sources+=("musicdl")
    [ "${ENABLE_MUSICBOX}" -eq 1 ] && sources+=("musicbox")
    [ "${ENABLE_LX}" -eq 1 ] && sources+=("lxmusic")

    local src kw id first_failed_lx_id=""
    for src in "${sources[@]}"; do
        for kw in "${probe_keywords[@]}"; do
            while IFS= read -r id; do
                [ -z "${id}" ] && continue
                search_any_result=1
                if [ "${src}" = "lxmusic" ] && [ -z "${first_failed_lx_id}" ]; then
                    first_failed_lx_id="${id}"
                fi
                if try_probe_stream "${id}"; then
                    log_info "验收 6b 通过：音源 ${src} 在线播放流取流成功。"
                    return 0
                fi
            done < <(search_probe_ids "${src}" "${kw}")
        done
    done

    # 外部音源网络波动不阻断部署：仅输出警告，绝不触发 return 1 / rollback
    if [ "${search_any_result}" -eq 0 ]; then
        log_warn "所有已启用音源 (${sources[*]}) 搜索结果均为空：可能是外部网络异常或第三方平台限流。"
    else
        log_warn "所有候选歌曲均未能成功取流 (外部音源网络波动，不阻断部署)。"
        if [ "${ENABLE_LX}" -eq 1 ] && [ -n "${first_failed_lx_id}" ]; then
            local lx_diag
            lx_diag="$(curl -s --max-time 5 "${LX_URL}/api/v1/track/url?id=${first_failed_lx_id}&quality=standard" 2>/dev/null || echo "")"
            if [ -n "${lx_diag}" ]; then
                log_warn "洛雪音源直链诊断返回数据（正文已隐藏）。"
            fi
        fi
    fi
    log_warn "跳过在线播放自动验收，建议稍后在飞牛音乐 Web 端手动搜索试播验证。"
    return 0
}

# ------------------------------------------------------------------------------
# 1. 预检
# ------------------------------------------------------------------------------
log_info "==> 步骤 1/5: 环境预检... (fnmusic-ext v${FNMUSIC_VERSION})"

# 1.1 检查 Python 3 与 venv 模块
if ! command -v python3 >/dev/null 2>&1; then
    log_err "【缺少基础依赖】系统未检测到 python3。"
    log_err "请先执行以下命令安装基础组件：sudo apt-get update && sudo apt-get install -y python3 python3-venv"
    exit 1
fi

if ! python3 -c "import venv" >/dev/null 2>&1; then
    log_err "【缺少基础依赖】系统 Python 缺少 venv 模块。"
    log_err "请先执行以下命令安装基础组件：sudo apt-get update && sudo apt-get install -y python3-venv"
    exit 1
fi

# 1.2 sudo 权限检查
if ! sudo -n true 2>/dev/null; then
    if [ -t 0 ]; then
        log_warn "需要管理员权限执行扩展配置，正在请求 sudo 授权..."
        sudo -v || {
            log_err "管理员权限获取失败，请确认当前用户具备 sudo 权限。"
            exit 1
        }
    else
        log_err "当前用户无法进行无密码 sudo 授权，请确认当前用户具备 sudo 权限。"
        exit 1
    fi
fi

# 1.3 检查飞牛音乐 socket 文件
if [ ! -S "${TARGET_SOCK}" ] && [ ! -S "${UPSTREAM_SOCK}" ]; then
    log_err "【前置条件未满足】未检测到飞牛音乐运行套接字。"
    log_err "请先在 fnOS 管理界面 -> 应用中心，安装并启动【飞牛音乐】应用后，再运行本脚本。"
    exit 1
fi

# 1.4 检查 / 自动拉起单容器 fnmusic-sources（v2.0.0 仅 Docker 部署，entrypoint 按需加载）
CONTAINER_NAME="fnmusic-sources"
if ! command -v docker >/dev/null 2>&1 || ! run_docker info >/dev/null 2>&1; then
    log_err "【缺少组件】v2.0.0 仅支持 Docker 部署，但当前 Docker 不可用。"
    log_err "请先在 fnOS 应用中心安装 Docker 后重试。"
    exit 1
fi

# v1.x 直接 git pull 后运行本脚本的兜底：迁移旧数据目录 musicbox-data -> sources-data
if [ -d "${BASE_DIR}/musicbox-data" ] && [ ! -d "${BASE_DIR}/sources-data" ]; then
    log_info "迁移数据目录: musicbox-data -> sources-data（网易登录态/缓存原样保留）"
    mv "${BASE_DIR}/musicbox-data" "${BASE_DIR}/sources-data"
fi
mkdir -p "${BASE_DIR}/sources-data/cache/netease-musicbox" \
    "${BASE_DIR}/sources-data/config/netease-musicbox" \
    "${BASE_DIR}/sources-data/netease-musicbox" \
    "${BASE_DIR}/sources-data/lxmusic"
# 数据卷权限收紧：属主对齐容器 appuser（uid 1000），group/other 全收（凭据防本机其他用户读取）
chown -R 1000:1000 "${BASE_DIR}/sources-data" 2>/dev/null || true
find "${BASE_DIR}/sources-data" -type d -exec chmod 0700 {} + 2>/dev/null || true
find "${BASE_DIR}/sources-data" -type f -exec chmod 0600 {} + 2>/dev/null || true

# 旧 v1.x 部署形态清理：宿主机三 unit + 三容器（释放端口 8768/8770/8772 给单容器）
for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic; do
    stop_owned_source_unit "${unit}" || exit 1
    remove_owned_container "${unit}" || exit 1
done

source_healthy() {
    curl -sf --max-time 5 "${1}/healthz" >/dev/null 2>&1
}

# 任一应启程序未就绪即视为需要拉起/重启容器（.env 改开关后 extend 会重启容器重选进程集）
need_start=0
if [ "${ENABLE_MUSICDL}" -eq 1 ] && ! source_healthy "${MUSICDL_URL}"; then need_start=1; fi
if [ "${ENABLE_MUSICBOX}" -eq 1 ] && ! source_healthy "${MUSICBOX_URL}"; then need_start=1; fi
if [ "${ENABLE_LX}" -eq 1 ] && ! source_healthy "${LX_URL}"; then need_start=1; fi
if [ "${ENABLE_WEBUI}" -eq 1 ] && ! source_healthy "http://127.0.0.1:8774"; then need_start=1; fi

# 构建并确保容器与当前代码同步。compose up -d --build 幂等：镜像与配置均未变时
# 不动运行中的容器（仅秒级缓存校验）；镜像有变（git pull 升级后）则自动换新容器。
ensure_image_current() {
    # 基础镜像源保障（国内镜像优先/官方兜底，见 ensure_base_image.sh），整次运行只执行一次
    if [ "${BASE_IMAGE_ENSURED:-0}" -ne 1 ]; then
        if bash "${BASE_DIR}/ensure_base_image.sh"; then
            BASE_IMAGE_ENSURED=1
        else
            log_err "基础镜像源探测失败，无法构建容器。"
            exit 1
        fi
    fi
    # issue #24：构建日志逐层可见（非 tty 下默认进度条会被压成静默）
    export BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}"
    # 离线模式：镜像已在（历史构建或 fpk 内置包加载）时绝不重建——
    # --build 会无条件重跑 Dockerfile 的 apt/pip 步骤，构建容器无外网时直接失败，
    # 表现为 fpk 安装/启动"卡在 55%"后报错。
    if run_docker image inspect fnmusic-sources:latest >/dev/null 2>&1; then
        if ! run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --no-build; then
            log_err "启动 ${CONTAINER_NAME} 失败（离线模式：镜像已存在，不重建）。"
            exit 1
        fi
    elif ! run_docker compose -f "${BASE_DIR}/docker-compose.yml" up -d --build; then
        log_err "构建/启动 ${CONTAINER_NAME} 失败。"
        exit 1
    fi
}

# .env 比容器新时需重启让 entrypoint 重读开关：env_newer_than_container 已移入
# proxy/install_common.sh（install.sh 的等待点同样依赖，见 install_sources_container）。

if [ "${need_start}" -eq 0 ]; then
    # 全部就绪：确认端口确由本目录的 fnmusic-sources 提供（不借用其他 checkout 的容器）
    if run_docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        if [ "${ADOPT:-0}" -eq 1 ]; then
            reclaim_container "${CONTAINER_NAME}" --adopt || exit 1
        else
            reclaim_container "${CONTAINER_NAME}" || exit 1
        fi
        log_info "音源容器 ${CONTAINER_NAME} 已就绪（按需加载：仅所选音源进程驻留内存）。"
        # 升级同步：服务健康也要重建镜像，否则 git pull 后新代码永远不生效
        img_before="$(run_docker inspect -f '{{.Image}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
        ensure_image_current
        img_after="$(run_docker inspect -f '{{.Image}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
        if [ "${img_before}" = "${img_after}" ] && env_newer_than_container; then
            log_info "检测到 .env 更新，重启容器使音源开关生效..."
            run_docker restart "${CONTAINER_NAME}" || exit 1
        fi
    else
        log_warn "音源 healthz 已就绪，但未发现 ${CONTAINER_NAME} 容器（疑似 v1.x 宿主机服务残留）。"
        log_warn "建议重新运行 ./install.sh 完成 v2.0.0 单容器迁移。"
    fi
else
    log_info "音源服务未全部就绪，拉起单容器 ${CONTAINER_NAME}..."
    if [ "${ADOPT:-0}" -eq 1 ]; then
        reclaim_container "${CONTAINER_NAME}" --adopt || exit 1
    else
        reclaim_container "${CONTAINER_NAME}" || exit 1
    fi
    ensure_image_current
    # compose 对配置未变的运行中容器不会重启：手动 restart 让 entrypoint 按最新 .env 重选进程集
    run_docker restart "${CONTAINER_NAME}" || exit 1
fi

wait_source() {
    local name="$1" url="$2" tries="${3:-60}"
    if wait_http "${url}/healthz" "${tries}" 2; then
        log_info "${name} 已就绪 ${url}/healthz"
        return 0
    fi
    log_err "等待 ${name} healthz 超时 (${url}/healthz)"
    diagnose_sources_container "${CONTAINER_NAME}"
    return 1
}

if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    wait_source "musicbox" "${MUSICBOX_URL}" || exit 1
fi
if [ "${ENABLE_MUSICDL}" -eq 1 ]; then
    wait_source "musicdl" "${MUSICDL_URL}" 90 || exit 1
fi
if [ "${ENABLE_LX}" -eq 1 ]; then
    wait_source "lxmusic" "${LX_URL}" || exit 1
fi
if [ "${ENABLE_WEBUI}" -eq 1 ]; then
    wait_source "WebUI" "http://127.0.0.1:8774" || exit 1
fi

# 洛雪用户源状态：healthz 的 user_source.initialized（未配置/初始化失败仅告警，可稍后在 WebUI 配置）
if [ "${ENABLE_LX}" -eq 1 ]; then
    lx_state="$(curl -sf --max-time 5 "${LX_URL}/healthz" 2>/dev/null | python3 -c '
import json, sys
try:
    us = (json.load(sys.stdin) or {}).get("user_source") or {}
except Exception:
    print("unknown|"); raise SystemExit
if us.get("initialized"):
    s = us.get("source") or {}
    print("ok|%s|%s" % (s.get("name") or "", s.get("version") or ""))
elif us.get("configured"):
    print("broken|%s" % (us.get("last_error") or "初始化失败"))
else:
    print("unconfigured|")
' 2>/dev/null || true)"
    case "${lx_state%%|*}" in
        ok)
            log_info "洛雪用户自定义源已加载: $(printf '%s' "${lx_state#*|}" | tr '|' ' ')"
            ;;
        broken)
            log_warn "洛雪用户源初始化失败: ${lx_state#*|}"
            log_warn "请在 WebUI 或 ./install.sh --sources lxmusic --lx-source-url <URL> 重新配置。"
            ;;
        *)
            log_warn "尚未配置洛雪用户自定义源（播放解析不可用，搜索/榜单不受影响）。"
            log_warn "可在飞牛管理员打开的管理页配置，或重跑 install.sh 时提供 --lx-source-url。"
            ;;
    esac
fi

# 1.5 检查 Python 虚拟环境与依赖（缺失时经多源回退安装，见 ensure_proxy_deps.sh）
if [ ! -f "${BASE_DIR}/.venv-proxy/bin/python" ]; then
    log_info "创建 .venv-proxy 虚拟环境..."
    PIP_INDEX="${PIP_INDEX:-}" bash "${BASE_DIR}/ensure_proxy_deps.sh"
fi

# 1.6 编译与语法检查
takeover preflight --base "${BASE_DIR}"
bash -n "${BASE_DIR}/proxy/run_proxy.sh"

# ------------------------------------------------------------------------------
# 2. 幂等性检查
# ------------------------------------------------------------------------------
log_info "==> 步骤 2/5: 幂等性检查..."


if [ "${FORCE_RELOAD}" -eq 1 ]; then
    log_info "已指定 --force：跳过幂等提前退出，将重写 unit 并重启代理以加载最新 .env。"
elif takeover ready --timeout 5; then
    log_info "检测到代理服务已在运行且上游健康 (处于扩展接管态)。"
    log_info "直接运行验收测试确认状态..."
    if verify_acceptance; then
        # This checkout is the live deployment; refresh the machine-wide
        # registry so a second checkout cannot silently take over later.
        takeover deployment-remember --base "${BASE_DIR}" || true
        log_info "============================================================"
        log_info "fnmusic-ext 当前已处于扩展态且运行正常，无需重复操作！"
        log_info "============================================================"
        exit 0
    else
        log_warn "现有扩展态验收未通过，将重启服务重新接管..."
    fi
fi

# ------------------------------------------------------------------------------
# 3. 安装并启动 systemd unit（按当前目录生成，禁止写死个人路径）
# ------------------------------------------------------------------------------
log_info "==> 步骤 3/5: 安装 systemd 服务并启动接管..."
UNIT_TMP="$(mktemp)"
takeover render-unit --base "${BASE_DIR}" > "${UNIT_TMP}"
# Arm rollback before any service mutation, including failed systemctl commands.
trap rollback ERR INT TERM
takeover remember
sudo cp "${UNIT_TMP}" /etc/systemd/system/fnmusic-ext.service
rm -f "${UNIT_TMP}"
sudo systemctl daemon-reload

if systemctl is-active --quiet fnmusic-ext.service 2>/dev/null; then
    log_info "重启 fnmusic-ext 服务..."
    sudo systemctl restart fnmusic-ext.service
else
    log_info "启用并启动 fnmusic-ext 服务..."
    sudo systemctl enable --now fnmusic-ext.service
fi

# ------------------------------------------------------------------------------
# 4. 等待接管完成与健康检查
# ------------------------------------------------------------------------------
log_info "==> 步骤 4/5: 等待代理服务接管完成并就绪..."
# True monotonic deadline; each health request allows 4s (> 2.5s readiness budget).
if ! takeover ready --timeout 30; then
    log_err "等待代理身份与健康就绪超时（30s）。"
    rollback
fi

log_info "Unix socket 接管成功且健康探测通过。"

# ------------------------------------------------------------------------------
# 5. 验收测试与自动回滚
# ------------------------------------------------------------------------------
log_info "==> 步骤 5/5: 验收链路连通性..."
if ! verify_acceptance; then
    rollback
fi

log_info "============================================================"
trap - ERR INT TERM
# Deployment registry: mark this checkout as the machine-wide deployment so
# a second checkout cannot silently steal the unit/containers/lock later.
takeover deployment-remember --base "${BASE_DIR}" || true
log_info "fnmusic-ext v${FNMUSIC_VERSION} 扩展已成功部署并生效！"
log_info "架构：Unix Socket 接管 (零侵入，不修改 nginx 配置)"
log_info "在线音源搜索合并、在线播放与元数据代理已就绪。"
log_info "------------------------------------------------------------"
log_info "【后续验证与使用指引】"
log_info "1. 验证搜索与播放："
log_info "   打开飞牛音乐 Web 端或手机 App，搜索歌曲（如“晴天”或“周杰伦”），"
log_info "   点击在线源歌曲试听，确认可以流畅播放并显示歌词与封面。"
if [ "${ENABLE_MUSICBOX}" -eq 1 ]; then
    log_info "2. 网易云扫码登录（可选）："
    log_info "   若遇到部分网易云 VIP/无损歌曲需登录："
    log_info "   • 命令行扫码登录（推荐）: ./extend.sh --qr 或 ./netease_login.sh"
    log_info "     （自动展示二维码、轮询登录状态、过期自动刷新，支持随时 Ctrl+C 跳过）"
    log_info "   • 管理页内扫码：飞牛管理员打开「fnMusic 扩展管理」后在「音乐源」扫码"
    log_info "   • 本机查询登录状态: curl -s http://127.0.0.1:8770/api/v1/auth/status"
fi
log_info "3. 健康检查与运维："
log_info "   • 探测状态: curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz"
log_info "   • 查看日志: sudo journalctl -u fnmusic-ext -f"
if [ "${ENABLE_WEBUI}" -eq 1 ]; then
    log_info "   • 管理 WebUI: 飞牛桌面「fnMusic 扩展管理」（仅管理员；音源三选一/音质/推荐/LLM 运行期可调）"
fi
log_info "   • 一键还原: ./restore.sh (一键无损切回官方原生直连)"
log_info "============================================================"
