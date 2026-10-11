#!/usr/bin/env bash
set -Eeuo pipefail

# ==============================================================================
# fnmusic-ext 升级脚本（不卸载、不丢配置）
#
# 【解决什么问题】
# 在应用中心手动装完新版 fpk 之后，容器不一定真的用上了新代码：
# compose 只在检测到配置变化时才重建容器，而 fpk 安装本身并不负责这一步。
# 本脚本补齐「装完 fpk 之后」的动作：
#   重建容器使挂载生效 → 等健康 → 应用热更新 → 写版本号 → 复验
#
# 【用法】
#   sudo bash tools/upgrade.sh [选项]
#
# 【选项】
#   --pkg <file>      本地热更新包（如 fnmusic-ext-2.7.0-hotfix.tar.gz）
#   --pkg-url <url>   远程热更新包（自动下载同名 .sha256 并校验）
#   --skip-hotfix     只做镜像层升级，不应用热更新
#   --dry-run         只演练：打印将要执行的操作，不落盘、不重启
#   -h, --help        显示本帮助
#
# 【本脚本刻意不做的事】
#   不自动安装 fpk。实测：appcenter-cli 对「已安装」的应用执行 install-fpk 是
#   空操作（输出 "[Info]Application [x] is installed." 且退出码 0，但什么都没
#   装）；install-local 会先 stop + uninstall，一旦失败会留下半卸载的坏状态。
#   因此 fpk 一律由用户在飞牛「应用中心」手动安装，本脚本只负责它之后的部分。
#
# 【退出码】
#   0 成功  1 运行期错误  2 参数错误  3 前置条件不满足（如新版 fpk 尚未安装）
# ==============================================================================

SCRIPT_PATH="${BASH_SOURCE[0]}"
BASE_DIR="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
CONTAINER_NAME="fnmusic-sources"
COMPOSE_FILE="${BASE_DIR}/docker-compose.yml"
APPLY_TOOL="${BASE_DIR}/tools/apply_hotfix.sh"
VERSION_FILE="${BASE_DIR}/VERSION"
# 新版（2.6.22+）compose 会把宿主这些目录挂到 /srv 同名路径，热更新才对容器侧生效
MOUNT_SERVICES=(musicdl-service musicbox-service lxmusic-service webui-service proxy)

PKG=""
PKG_URL=""
SKIP_HOTFIX=0
DRY_RUN=0

if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_INFO=$'\033[36m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_OK=""; C_INFO=""; C_WARN=""; C_ERR=""; C_OFF=""
fi

log_ok()   { printf '%s[ OK ]%s %s\n'   "${C_OK}"   "${C_OFF}" "$*"; }
log_info() { printf '%s[INFO]%s %s\n'   "${C_INFO}" "${C_OFF}" "$*"; }
log_warn() { printf '%s[WARN]%s %s\n'   "${C_WARN}" "${C_OFF}" "$*" >&2; }
log_err()  { printf '%s[ERR ]%s %s\n'   "${C_ERR}"  "${C_OFF}" "$*" >&2; }

usage() {
    sed -n '4,31p' "${SCRIPT_PATH}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------- 参数解析
while [ $# -gt 0 ]; do
    case "$1" in
        --pkg)        [ $# -ge 2 ] || { log_err "--pkg 需要一个路径参数"; exit 2; }; PKG="$2"; shift 2 ;;
        --pkg-url)    [ $# -ge 2 ] || { log_err "--pkg-url 需要一个 URL"; exit 2; }; PKG_URL="$2"; shift 2 ;;
        --skip-hotfix) SKIP_HOTFIX=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        -*)           log_err "未知参数: $1"; usage; exit 2 ;;
        *)            log_err "多余的位置参数: $1"; usage; exit 2 ;;
    esac
done

if [ -n "${PKG}" ] && [ -n "${PKG_URL}" ]; then
    log_err "--pkg 与 --pkg-url 只能选一个"
    exit 2
fi

run_docker() {
    # install.sh 同款封装：优先直连 daemon，否则退回 sudo（fnOS 上 docker 多需 root）
    if docker info >/dev/null 2>&1; then
        docker "$@"
    elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        sudo docker "$@"
    else
        return 1
    fi
}

container_state() {
    run_docker inspect -f '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo "missing"
}

container_health() {
    run_docker inspect -f '{{.State.Health.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo "unknown"
}

# ---------------------------------------------------------------- 0. 前置检查
log_info "部署目录: ${BASE_DIR}"

if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
    log_err "需要 root 权限（写 VERSION / 重启服务）。请用 sudo 重跑。"
    exit 3
fi
if ! command -v docker >/dev/null 2>&1; then
    log_err "未检测到 docker：请先安装并启动 Docker。"
    exit 3
fi
if ! run_docker info >/dev/null 2>&1; then
    log_err "docker 服务未运行（docker info 失败），请先启动 Docker。"
    exit 3
fi
if [ ! -f "${COMPOSE_FILE}" ]; then
    log_err "找不到 ${COMPOSE_FILE}：请确认在部署目录（…/fnmusic-ext/repo）下执行。"
    exit 3
fi

# ---------------------------------------------- 1. 新版 fpk 是否已安装（挂挂载判据）
# 判据用 compose 里有没有 2.6.22 新增的挂载，比看版本号准：
# 挂载不在 → 容器仍读镜像内的旧代码 → 后面做的一切都没有意义。
if ! grep -q ':/srv/proxy' "${COMPOSE_FILE}" 2>/dev/null; then
    log_err "当前部署的 compose 还没有 /srv 挂载，说明新版 fpk 尚未安装。"
    log_err "请先在飞牛「应用中心」手动安装 fnmusic-ext 2.6.22 或更高版本的 fpk，"
    log_err "然后再重跑本脚本。（fpk 不能由脚本代装，原因见脚本头部注释）"
    exit 3
fi
log_ok "compose 已包含 /srv 挂载（新版 fpk 已就位）"

CURRENT_VER="$(head -n 1 "${VERSION_FILE}" 2>/dev/null | tr -d '[:space:]' || true)"
CURRENT_VER="${CURRENT_VER:-未知}"
log_info "当前版本号: ${CURRENT_VER}"

# ---------------------------------------------------------------- 2. 重建容器
# --no-build 与 install.sh 保持一致：镜像必须由 fpk 安装阶段准备好，
# 禁止 compose 隐式联网构建（弱网下会表现为「卡住不动」）。
state="$(container_state)"
log_info "容器 ${CONTAINER_NAME} 当前状态: ${state}"
if [ "${DRY_RUN}" -eq 1 ]; then
    log_info "[dry-run] 将执行: docker compose -f ${COMPOSE_FILE} up -d --no-build"
else
    log_info "重建容器使挂载生效（docker compose up -d --no-build）..."
    if ! run_docker compose -f "${COMPOSE_FILE}" up -d --no-build; then
        log_err "容器重建失败。常见原因：镜像缺失（online fpk 需联网构建）或端口被占用。"
        log_err "诊断: docker logs ${CONTAINER_NAME}"
        exit 1
    fi
    log_ok "容器已重建"
fi

# ---------------------------------------------------------------- 3. 等待健康
if [ "${DRY_RUN}" -eq 0 ]; then
    log_info "等待容器健康..."
    ready=0
    for attempt in $(seq 1 45); do
        health="$(container_health)"
        if [ "${health}" = "healthy" ]; then
            ready=1
            break
        fi
        if [ $((attempt % 5)) -eq 0 ]; then
            log_warn "仍在等待康复（已等 $((attempt * 2))s，当前 health=${health}）..."
        fi
        sleep 2
    done
    if [ "${ready}" -ne 1 ]; then
        log_err "容器 90s 内未进入 healthy（health=$(container_health)）。"
        log_err "诊断信息如下:"
        run_docker logs --tail 40 "${CONTAINER_NAME}" >&2 2>&1 || true
        exit 1
    fi
    log_ok "容器健康"
fi

# ------------------------------------------------- 4. 复验挂载（硬判据：inode）
# 挂载列表里有目标路径只是「声明存在」，真正要证明的是「容器读到的就是宿主那个
# 文件」——同一文件的 inode 在宿主与容器内必须完全相同。
verify_mounts() {
    local dests probe host_ino cont_ino svc ok=1
    dests="$(run_docker inspect -f '{{range .Mounts}}{{.Destination}}{{println}}{{end}}' "${CONTAINER_NAME}")"
    for svc in "${MOUNT_SERVICES[@]}"; do
        if ! printf '%s\n' "${dests}" | grep -Fxq "/srv/${svc}"; then
            log_err "容器未挂载 /srv/${svc}"
            ok=0
            continue
        fi
        probe="app.py"
        if [ ! -f "${BASE_DIR}/${svc}/${probe}" ]; then
            log_warn "宿主缺少 ${BASE_DIR}/${svc}/${probe}，跳过 inode 比对"
            continue
        fi
        host_ino="$(stat -c %i "${BASE_DIR}/${svc}/${probe}" 2>/dev/null || echo "")"
        cont_ino="$(run_docker exec "${CONTAINER_NAME}" stat -c %i "/srv/${svc}/${probe}" 2>/dev/null || echo "")"
        if [ -z "${host_ino}" ] || [ -z "${cont_ino}" ] || [ "${host_ino}" != "${cont_ino}" ]; then
            log_err "/srv/${svc}/${probe} 与宿主不是同一文件（host inode=${host_ino} / container inode=${cont_ino}）"
            ok=0
        else
            log_ok "/srv/${svc} 已读到宿主文件（inode=${host_ino}）"
        fi
    done
    [ "${ok}" -eq 1 ]
}

if [ "${DRY_RUN}" -eq 0 ]; then
    if ! verify_mounts; then
        log_err "挂载复验未通过：容器仍在读镜像内的旧代码，热更新不会对容器侧生效。"
        exit 1
    fi
else
    log_info "[dry-run] 跳过挂载复验"
fi

# ---------------------------------------------------------------- 5. 热更新
if [ "${SKIP_HOTFIX}" -eq 1 ]; then
    log_warn "已指定 --skip-hotfix：跳过热更新，升级到此为止（仍缺运行时功能修复）。"
else
    if [ -n "${PKG_URL}" ]; then
        PKG="${BASE_DIR}/$(basename "${PKG_URL%%\?*}")"
        if [ "${DRY_RUN}" -eq 1 ]; then
            log_info "[dry-run] 将下载 ${PKG_URL} → ${PKG}"
        else
            log_info "下载热更新包: ${PKG_URL}"
            curl -fsSL --retry 2 --max-time 300 -o "${PKG}" "${PKG_URL}"
            sha_url="${PKG_URL}.sha256"
            if curl -fsSL --retry 2 --max-time 60 "${sha_url}" -o "${PKG}.sha256" 2>/dev/null; then
                expected="$(awk '{print $1}' "${PKG}.sha256")"
                actual="$(sha256sum "${PKG}" | awk '{print $1}')"
                if [ -n "${expected}" ] && [ "${expected}" != "${actual}" ]; then
                    log_err "热更新包 sha256 不匹配：期望 ${expected}，实测 ${actual}"
                    rm -f "${PKG}"
                    exit 1
                fi
                log_ok "sha256 校验通过: ${actual}"
            else
                log_warn "未取到 .sha256 校验文件（${sha_url}），跳过哈希校验"
            fi
        fi
    fi

    if [ -z "${PKG}" ]; then
        log_info "未指定热更新包（--pkg / --pkg-url），跳过这一步。"
        log_info "若需要运行时功能修复，请再跑一次并指定包，例如："
        log_info "  sudo bash tools/upgrade.sh --pkg /path/to/fnmusic-ext-2.7.0-hotfix.tar.gz"
    else
        # dry-run 下 --pkg-url 不会真的下载，PKG 只是一条待下载的路径，不能做存在性检查
        if [ "${DRY_RUN}" -eq 0 ] && [ ! -f "${PKG}" ]; then
            log_err "找不到热更新包: ${PKG}"
            exit 1
        fi
        if [ ! -f "${APPLY_TOOL}" ]; then
            log_err "找不到 ${APPLY_TOOL}（新版 fpk 应带此脚本），无法应用热更新。"
            exit 1
        fi
        hotfix_ver="$(python3 -c '
import sys, tarfile
try:
    with tarfile.open(sys.argv[1]) as tf:
        member = next((m for m in tf.getmembers() if m.name.endswith("MANIFEST.json")), None)
        if member is None:
            sys.exit(0)
        import json
        print(json.loads(tf.extractfile(member).read().decode("utf-8")).get("version", ""))
except Exception:
    sys.exit(0)
' "${PKG}" || true)"
        log_info "热更新包版本号: ${hotfix_ver:-未知}"
        if [ "${DRY_RUN}" -eq 1 ]; then
            log_info "[dry-run] 将执行: bash ${APPLY_TOOL} ${PKG}"
        else
            log_info "应用热更新（先演练，再实应用）..."
            bash "${APPLY_TOOL}" "${PKG}" --dry-run
            bash "${APPLY_TOOL}" "${PKG}"
            log_ok "热更新已应用"
            # 注意顺序：VERSION 必须在 apply 之后写。端点要求目标版本严格大于
            # max(appcenter, VERSION, .hotfix-version)，先写会把基线抬高导致 409。
            if [ -n "${hotfix_ver}" ] && printf '%s' "${hotfix_ver}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
                printf '%s\n' "${hotfix_ver}" > "${VERSION_FILE}"
                log_ok "已写入版本号 ${hotfix_ver} 到 ${VERSION_FILE}"
            fi
        fi
    fi
fi

# ---------------------------------------------------------------- 6. 最终状态
log_info "———— 最终状态 ————"
FINAL_VER="$(head -n 1 "${VERSION_FILE}" 2>/dev/null | tr -d '[:space:]' || true)"
log_info "VERSION:      ${FINAL_VER:-未知}（升级前 ${CURRENT_VER}）"
log_info "容器状态:     $(container_state) / $(container_health)"
if [ "${DRY_RUN}" -eq 0 ] && run_docker exec "${CONTAINER_NAME}" \
        python3 -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8004/healthz",timeout=3)' >/dev/null 2>&1; then
    log_ok "WebUI 健康检查通过"
else
    log_warn "WebUI 健康检查未通过或未纳入启用范围（可忽略：--no-webui 部署下正常）"
fi
log_ok "升级流程结束。刷新浏览器即可看到新版本。"
