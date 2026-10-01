#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 宿主机代理依赖保障（v2.1.0+）
# 背景：install.sh / extend.sh 为 .venv-proxy 安装 fastapi/uvicorn 等代理依赖时，
#       此前只使用单一 pip 源；部分用户网络到该源不可达（限流 403 / DNS /
#       路由 / 代理拦截），pip 报 "Could not find a version that satisfies
#       the requirement ... (from versions: none)" 且无回退，安装直接中断。
# 本脚本不修改任何系统配置，只负责：
#   1. 确保 .venv-proxy 存在（缺失时用 python3 -m venv 创建）
#   2. 按候选链安装：PIP_INDEX（默认阿里云，用户自定义永远第一位）
#      → 清华镜像 → 官方 PyPI（重复候选自动去重；每源带 --retries/--timeout 抗抖动）
#   3. 「升级 pip」与「安装 requirements」在同一候选源上连续完成，换源即整组重试
#   4. 全部候选源失败时报出排查指引并以非零退出
# 用法: bash ensure_proxy_deps.sh   （由 install.sh / extend.sh 调用，也可手动重跑）
# 可调环境变量: PIP_INDEX / FNMUSIC_VENV_DIR（默认 <脚本所在目录>/.venv-proxy，测试用）
# ==============================================================================

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${FNMUSIC_VENV_DIR:-${BASE_DIR}/.venv-proxy}"
REQ_FILE="${BASE_DIR}/proxy/requirements.txt"
PIP_INDEX="${PIP_INDEX:-https://mirrors.tencent.com/pypi/simple/}"
FALLBACK_INDEXES="https://mirrors.aliyun.com/pypi/simple/ https://pypi.tuna.tsinghua.edu.cn/simple https://pypi.org/simple"

log_info() { echo -e "\033[32m[INFO]\033[0m $*"; }
log_warn() { echo -e "\033[33m[WARN]\033[0m $*"; }
log_err() { echo -e "\033[31m[ERROR]\033[0m $*" >&2; }

if [ ! -x "${VENV_DIR}/bin/python" ]; then
    log_info "创建虚拟环境 ${VENV_DIR} ..."
    python3 -m venv "${VENV_DIR}"
fi
PIP_BIN="${VENV_DIR}/bin/pip"
if [ ! -x "${PIP_BIN}" ]; then
    log_err "虚拟环境异常：${PIP_BIN} 不存在。"
    log_err "可删除 ${VENV_DIR} 后重跑本脚本（或 install.sh）重建。"
    exit 1
fi

# 候选源去重：用户自定义 PIP_INDEX 永远第一位
# 离线模式：优先用内置 wheels 安装代理依赖（无需联网）
WHEELS_DIR="${BASE_DIR}/proxy/wheels"
if [ -d "${WHEELS_DIR}" ] && ls "${WHEELS_DIR}"/*.whl >/dev/null 2>&1; then
    log_info "离线模式：从内置 wheels 安装代理依赖（无需联网）..."
    if "${PIP_BIN}" install -q -U pip --no-index --find-links "${WHEELS_DIR}" \
       && "${PIP_BIN}" install -q -r "${REQ_FILE}" --no-index --find-links "${WHEELS_DIR}"; then
        log_info "代理依赖安装完成（离线 wheels）。"
        exit 0
    fi
    log_warn "内置 wheels 安装失败，回退到在线 pip 源..."
fi

candidates="${PIP_INDEX}"
for idx in ${FALLBACK_INDEXES}; do
    [ "${idx}" = "${PIP_INDEX}" ] || candidates="${candidates} ${idx}"
done

ok_index=""
for idx in ${candidates}; do
    log_info "尝试 pip 源: ${idx}"
    if "${PIP_BIN}" install -q -U pip --retries 2 --timeout 30 -i "${idx}" \
       && "${PIP_BIN}" install -q -r "${REQ_FILE}" --retries 2 --timeout 30 -i "${idx}"; then
        ok_index="${idx}"
        break
    fi
    log_warn "pip 源不可用: ${idx}，切换下一候选源重试..."
done

if [ -z "${ok_index}" ]; then
    log_err "所有 pip 源均安装失败（尝试了: ${candidates}）。"
    log_err "排查建议：1) 检查宿主机网络 / DNS / 代理（含 http_proxy、https_proxy 环境变量）；"
    log_err "           2) 换源重试: PIP_INDEX=<其他源URL> ./install.sh（或 extend.sh）。"
    exit 1
fi
log_info "代理依赖安装完成（pip 源: ${ok_index}）。"
