#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext Docker 基础镜像源探测 / 保障（v1.2.3+）
# 背景：fnOS 等 NAS 系统通常在 Docker daemon 全局配置镜像加速器（如 docker.fnnas.com），
#       加速器异常（401/超时）时 BuildKit 解析 python:3.13-slim 元数据会失败且不会
#       回退官方源，导致 docker compose up --build 直接失败。
# 本脚本不修改任何系统配置，仅为本应用解析一个「真实可拉取」的基础镜像引用：
#   1. 环境变量 BASE_IMAGE 手动指定 → 仅验证该引用（跳过自动探测，支持本地短路）
#   2. 本地探测短路优先：候选引用拉取前先本地探测，已存在则秒级跳过拉取，零网络请求
#   3. 最多尝试 FNMUSIC_MIRROR_TRIES（默认 2）个镜像源（含 .env 缓存源）：
#      缓存源优先复用，随后国内镜像源补足至上限；拉取时打印候选进度并保留错误信息
#   4. 官方 python:3.13-slim 作为最后兜底（不计入镜像源上限）
# 探测结果经 proxy/env_merge.py 安全增量写入 .env 的 FNMUSIC_BASE_IMAGE（备份+600 权限），
# docker-compose.yml 的 build.args 自动读取该值；install.sh / extend.sh / 手动重建全部生效。
# 用法: bash ensure_base_image.sh   （由 install.sh / extend.sh 在 docker 模式构建前调用）
# 可调环境变量: BASE_IMAGE / FNMUSIC_DOCKER_MIRRORS / FNMUSIC_MIRROR_TRIES（镜像源尝试上限，默认 2） / PULL_TIMEOUT（单次拉取超时秒数，默认 240）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PATH="${BASE_DIR}/.env"
BASE_TAG="python:3.13-slim"
DEFAULT_MIRRORS="docker.1ms.run docker.m.daocloud.io docker.1panel.live hub.rat.dev"
PULL_TIMEOUT="${PULL_TIMEOUT:-240}"
MIRROR_TRIES="${FNMUSIC_MIRROR_TRIES:-2}"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

# 与 install.sh / extend.sh 同款 docker 访问回退（docker 或 sudo docker）
if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
else
    log_err "Docker daemon 不可用（docker / sudo docker 均无法连接），无法探测基础镜像源。"
    exit 1
fi

# 读取 .env 中已缓存的 FNMUSIC_BASE_IMAGE（只取该键，不打印 .env 其他内容）
cached_base_image() {
    [ -f "${ENV_PATH}" ] || return 0
    grep -E "^\s*(export\s+)?FNMUSIC_BASE_IMAGE=" "${ENV_PATH}" 2>/dev/null \
        | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true
}

# 检查镜像是否已存在于本地（超时 10 秒，避免 daemon 挂起）
image_exists_local() {
    local ref="$1"
    # shellcheck disable=SC2086
    timeout 10 ${DOCKER_CMD} image inspect "${ref}" >/dev/null 2>&1
}

# 用真实 docker pull 验证（与 BuildKit 构建走同一条 daemon 链路，顺带预取镜像层）。
# 心跳（issue #24）：--quiet 拉取大镜像长时间无输出，在 fpk 安装器里表现为
# "卡在 55%"——后台拉取 + 每 30s 打一行已耗时，让用户知道仍在推进。
pull_ok() {
    local ref="$1" rc=0 pull_pid waited
    # shellcheck disable=SC2086
    timeout "${PULL_TIMEOUT}" ${DOCKER_CMD} pull --quiet "${ref}" &
    pull_pid=$!
    waited=0
    while kill -0 "${pull_pid}" 2>/dev/null; do
        sleep 5
        waited=$((waited + 5))
        if (( waited % 30 == 0 )) && kill -0 "${pull_pid}" 2>/dev/null; then
            log_info "仍在拉取 ${ref}（已等 ${waited}s / 上限 ${PULL_TIMEOUT}s）..."
        fi
    done
    wait "${pull_pid}" || rc=$?
    return "${rc}"
}

# 安全增量写入 FNMUSIC_BASE_IMAGE（沿用 install.sh 的 env_merge 备份惯例）
persist_base_image() {
    local ref="$1" desired
    desired="$(mktemp)"
    echo "FNMUSIC_BASE_IMAGE='$(printf "%s" "${ref}" | sed "s/'/'\\\\''/g")'" > "${desired}"
    if [ -f "${ENV_PATH}" ]; then
        cp -p "${ENV_PATH}" "${ENV_PATH}.bak.$(date +%Y%m%d%H%M%S)"
        python3 "${BASE_DIR}/proxy/env_merge.py" \
            --existing "${ENV_PATH}" --desired "${desired}" \
            --output "${ENV_PATH}" --explicit FNMUSIC_BASE_IMAGE --quiet
    else
        python3 "${BASE_DIR}/proxy/env_merge.py" \
            --existing /dev/null --desired "${desired}" \
            --output "${ENV_PATH}" --explicit FNMUSIC_BASE_IMAGE --quiet
    fi
    rm -f "${desired}"
    chmod 600 "${ENV_PATH}"
}

# ==============================================================================
# 离线模式（v2.6.4-offline）：优先从内置镜像包加载，彻底跳过联网拉取基础镜像
# （原「部署时间太长」根因：国内镜像源逐个探测 + 每源 240s 超时拉 python:3.13-slim）
# 镜像包由打包阶段 docker save 进 app/repo/images/fnmusic-sources.tar，安装即本地 load。
# 排障日志统一写 /tmp/fnmusic_offline.log（安装失败后可 SSH 读取）。
# ==============================================================================
OFFLINE_LOG="/tmp/fnmusic_offline.log"
olog() { echo "[$(date '+%F %T')] $*" >> "${OFFLINE_LOG}" 2>/dev/null || true; }

# 快路径：镜像已在本地（历史构建或上次加载）→ 秒级返回，零网络零 IO
if ${DOCKER_CMD} image inspect fnmusic-sources:latest >/dev/null 2>&1; then
    olog "image fnmusic-sources:latest already present, skip load"
    log_info "镜像 fnmusic-sources:latest 本地已存在，直接复用。"
    persist_base_image "fnmusic-sources:latest"
    exit 0
fi

OFFLINE_TAR="${BASE_DIR}/images/fnmusic-sources.tar"
if [ -f "${OFFLINE_TAR}" ]; then
    olog "offline load start: ${OFFLINE_TAR} ($(stat -c%s "${OFFLINE_TAR}" 2>/dev/null || echo '?') bytes)"
    log_info "离线模式：从内置镜像包加载 fnmusic-sources:latest（无需联网）..."
    # 心跳（issue #24 同源）：docker load 1.2GB 大镜像长时间无输出，fpk 安装器里
    # 表现为"卡在 55%"；后台 load + 每 30s 打一行已耗时，让用户知道仍在推进。
    ${DOCKER_CMD} load -i "${OFFLINE_TAR}" >>"${OFFLINE_LOG}" 2>&1 &
    load_pid=$!
    lwaited=0
    while kill -0 "${load_pid}" 2>/dev/null; do
        sleep 5
        lwaited=$((lwaited + 5))
        if (( lwaited % 30 == 0 )) && kill -0 "${load_pid}" 2>/dev/null; then
            log_info "仍在加载内置镜像包（已等 ${lwaited}s）..."
        fi
    done
    if wait "${load_pid}" 2>/dev/null; then
        olog "offline load OK"
        log_info "镜像已加载：fnmusic-sources:latest（部署不再联网拉取）"
        persist_base_image "fnmusic-sources:latest"
        exit 0
    fi
    olog "offline load FAILED (see log above)"
    log_warn "内置镜像包加载失败，回退到在线探测拉取..."
fi
# ---- 以下为原在线探测逻辑（仅当内置镜像包缺失时兜底）----

MANUAL="${BASE_IMAGE:-}"
CACHED="$(cached_base_image)"
MIRRORS="${FNMUSIC_DOCKER_MIRRORS:-${DEFAULT_MIRRORS}}"

# 组装候选列表（保序去重；手动指定时仅验证该引用）
CANDIDATES=()
SEEN=" "
add_candidate() {
    local c="$1"
    [ -z "${c}" ] && return 0
    case "${SEEN}" in *" ${c} "*) return 0 ;; esac
    SEEN="${SEEN}${c} "
    CANDIDATES+=("${c}")
}
if [ -n "${MANUAL}" ]; then
    log_info "已通过 BASE_IMAGE 手动指定基础镜像，跳过自动探测。"
    add_candidate "${MANUAL}"
else
    # 缓存的镜像源引用优先复用（计入镜像源上限）；缓存的官方短引用（docker.io 域）不提前——
    # daemon 加速器链路不稳时它最慢最不可靠，统一沉底做最后兜底
    case "${CACHED}" in
        ""|"${BASE_TAG}"|"docker.io/"*|"registry.hub.docker.com/"*) ;;
        *)
            if [ "${#CANDIDATES[@]}" -lt "${MIRROR_TRIES}" ]; then
                add_candidate "${CACHED}"
            fi
            ;;
    esac
    local_mirrors="${MIRRORS}"
    # shellcheck disable=SC2086
    for m in ${local_mirrors}; do
        if [ "${#CANDIDATES[@]}" -ge "${MIRROR_TRIES}" ]; then
            break
        fi
        add_candidate "${m}/library/${BASE_TAG}"
    done
    add_candidate "${BASE_TAG}"
fi

# ---- 测速择优（issue #24：安装卡 55%）---------------------------------------
# 固定顺序会把当前最慢的镜像排前面，弱网下逐个拉满 240s 超时表现为"安装卡住"。
# 拉取前对未命中本地的镜像源做 /v2/ 探活测速（单源 ≤4s，任意状态码即可，
# 只比响应速度），按耗时升序重排候选；本地已存在的引用（前缀 0）保持最优先，
# 官方短引用不测速沉底兜底。
speed_rank_candidates() {
    local ranked=() plain=()
    local ref host t
    for ref in "${CANDIDATES[@]}"; do
        if image_exists_local "${ref}"; then
            ranked+=("0 ${ref}")
            continue
        fi
        # 无斜杠 = 无 registry 前缀（官方短引用，如 python:3.13-slim）：不测速沉底。
        # 不能用 host 是否含冒号判断——官方短引用自身就带 tag 冒号。
        case "${ref}" in
            */*) ;;
            *) plain+=("${ref}"); continue ;;
        esac
        host="${ref%%/*}"
        t="$(curl -s -o /dev/null -m 4 -w '%{time_total}' "https://${host}/v2/" 2>/dev/null || echo 9999)"
        case "${t}" in ''|*[!0-9.]*) t="9999" ;; esac
        ranked+=("${t} ${ref}")
    done
    # 数值排序（-s 稳定）：本地命中(0) > 测速快的镜像源 > 测速失败(9999)
    mapfile -t CANDIDATES < <(
        printf '%s\n' "${ranked[@]}" | sort -k1,1g -s | while read -r _t _ref; do
            [ -n "${_ref}" ] && printf '%s\n' "${_ref}"
        done
        printf '%s\n' "${plain[@]}"
    )
}
if [ -z "${MANUAL}" ] && command -v curl >/dev/null 2>&1 && [ "${#CANDIDATES[@]}" -gt 1 ]; then
    speed_rank_candidates
    log_info "镜像候选已按测速重排: ${CANDIDATES[*]}"
fi

CHOSEN=""
total="${#CANDIDATES[@]}"
idx=0
for ref in "${CANDIDATES[@]}"; do
    idx=$((idx + 1))
    log_info "[${idx}/${total}] ${ref}（超时 ${PULL_TIMEOUT}s，失败将自动切换下一源）"
    if image_exists_local "${ref}"; then
        log_info "基础镜像 ${ref} 本地已存在，跳过拉取。"
        CHOSEN="${ref}"
        break
    fi
    rc=0
    pull_ok "${ref}" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        CHOSEN="${ref}"
        break
    fi
    log_warn "拉取 ${ref} 失败（exit=${rc}），尝试下一个候选..."
done

if [ -z "${CHOSEN}" ]; then
    log_err "所有基础镜像候选均拉取失败: ${CANDIDATES[*]}"
    log_err "国内网络直连 docker.io 不稳定是安装卡住的最常见原因（issue #24），可尝试："
    log_err "  1) 为 Docker 配置可用代理后重试（daemon.json 的 proxies 段，改完重启 Docker）；"
    log_err "  2) 设置 BASE_IMAGE 环境变量手动指定可用镜像引用（如 docker.m.daocloud.io/library/python:3.13-slim）；"
    log_err "  3) 通过 FNMUSIC_DOCKER_MIRRORS 自定义国内镜像候选列表；"
    log_err "  4) 检查 fnOS Docker 镜像加速器（如 docker.fnnas.com）是否可用，或改用 ./install.sh --mode host。"
    exit 1
fi

if [ "${CHOSEN}" = "${CACHED}" ]; then
    log_info "基础镜像源可用: ${CHOSEN}（沿用 .env 缓存，无需更新）"
else
    persist_base_image "${CHOSEN}"
    log_info "基础镜像源已确定并写入 .env: FNMUSIC_BASE_IMAGE=${CHOSEN}"
fi
