# fnmusic-sources 统一镜像：musicdl + musicbox + lxmusic + webui 四进程（supervisor 管理，按需启动）
# 基础镜像引用可由 build args 覆盖（默认官方源；国内网络自动回退见根目录 ensure_base_image.sh）
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/
ARG APT_MIRROR="https://mirrors.tencent.com"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    XDG_DATA_HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    XDG_CONFIG_HOME=/data/config \
    LX_DATA_DIR=/data/lxmusic \
    WEBUI_REPO_DIR=/repo \
    UPGRADE_HOST_DIR=/vol1/@appcenter/fnmusic-ext/repo/sources-data/upgrade

WORKDIR /srv

# nodejs（洛雪自定义源 Node 沙箱，trixie 自带 20.x）/ ffmpeg（musicdl 转封装）/ supervisor
# apt 源默认国内镜像（与 pip 同策略，APT_MIRROR 可覆盖），仅在本构建层内临时替换。
# 构建容器无法复用"指向本机"的宿主 DNS（Docker 会剔除 127.x 并回退 8.8.8.8，国内不可达），
# 因此不探测、始终把国内可达的公共 DNS 写到最前（写入失败不中断）；apt-get update
# 对 DNS 失败只报 W: 警告且退出码为 0，回退判定看「索引是否真正落地」而非退出码；
# 镜像源与官方源都取不到索引才判失败。安装完成后恢复官方源，宿主机与最终镜像不受影响。
# 失败文案用 "ERROR:" 不用 "[ERROR]"：后者会被应用中心弹窗 grep 成整段 RUN 标题。
RUN set -eu; \
    fix_dns() { \
        { \
            printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n'; \
            grep -vE '^[[:space:]]*nameserver[[:space:]]+(127\.|::1|localhost)' /etc/resolv.conf 2>/dev/null || true; \
        } > /tmp/resolv.fnmusic 2>/dev/null || true; \
        cat /tmp/resolv.fnmusic > /etc/resolv.conf 2>/dev/null || true; \
    }; \
    swap_apt_host() { \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "${f}" ] && sed -i -E "s#${1}#${2}#g" "${f}" || true; \
        done; \
    }; \
    apt_update() { apt-get update -o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 || true; }; \
    lists_fetched() { [ -n "$(find /var/lib/apt/lists -name '*Packages*' -print -quit 2>/dev/null || true)" ]; }; \
    fix_dns; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host 'https?://deb\.debian\.org' "${APT_MIRROR}"; fi; \
    rm -rf /var/lib/apt/lists/*; \
    apt_update; \
    if ! lists_fetched; then \
        echo "[WARN] apt 镜像源未取到任何索引，回退官方源 deb.debian.org 重试..." >&2; \
        fix_dns; \
        rm -rf /var/lib/apt/lists/*; \
        if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
        apt_update; \
    fi; \
    if ! lists_fetched; then \
        echo "ERROR: apt 镜像源与官方源均未取到索引：构建容器无法访问 Debian 源。" >&2; \
        echo "ERROR: 若宿主机 /etc/resolv.conf 的 DNS 指向本机（127.x），Docker 容器无法复用，" >&2; \
        echo "ERROR: 可在 Docker daemon.json 配置 \"dns\": [\"223.5.5.5\"] 并重启 Docker 后重试。" >&2; \
        exit 1; \
    fi; \
    apt-get install -y --no-install-recommends nodejs ffmpeg supervisor ca-certificates; \
    if [ -n "${APT_MIRROR}" ]; then swap_apt_host "${APT_MIRROR}" 'http://deb.debian.org'; fi; \
    rm -rf /var/lib/apt/lists/*

# 四个服务的 Python 依赖并集（各自 requirements 独立演进，镜像统一安装）
# 构建容器 DNS 失败自愈同 apt 步骤（resolv.conf 改写不跨 RUN 层持久，需每步自带）；
# 镜像源安装失败自动回退官方 PyPI 重试
COPY musicdl-service/requirements.txt /srv/req.musicdl
COPY musicbox-service/requirements.txt /srv/req.musicbox
COPY lxmusic-service/requirements.txt /srv/req.lxmusic
COPY webui-service/requirements.txt /srv/req.webui
RUN set -eu; \
    fix_dns() { \
        { \
            printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n'; \
            grep -vE '^[[:space:]]*nameserver[[:space:]]+(127\.|::1|localhost)' /etc/resolv.conf 2>/dev/null || true; \
        } > /tmp/resolv.fnmusic 2>/dev/null || true; \
        cat /tmp/resolv.fnmusic > /etc/resolv.conf 2>/dev/null || true; \
    }; \
    fix_dns; \
    pip install --no-cache-dir -r /srv/req.musicdl -r /srv/req.musicbox -r /srv/req.lxmusic -r /srv/req.webui \
    || { echo "[WARN] pip 镜像源（${PIP_INDEX_URL}）安装失败，回退官方 PyPI 重试..." >&2; \
         pip install --no-cache-dir -i https://pypi.org/simple \
            -r /srv/req.musicdl -r /srv/req.musicbox -r /srv/req.lxmusic -r /srv/req.webui; } \
    && rm -f /srv/req.*

RUN useradd -m -u 1000 appuser \
    && mkdir -p /data/cache /data/config /data/lxmusic \
    && chown -R appuser:appuser /data

# --chown+chmod：镜像权限不依赖构建上下文的 umask/属主
COPY --chown=appuser:appuser musicdl-service/app.py musicdl-service/hardening.py /srv/musicdl-service/
COPY --chown=appuser:appuser musicbox-service/app.py musicbox-service/runner.py musicbox-service/netease_ext.py /srv/musicbox-service/
COPY --chown=appuser:appuser lxmusic-service/app.py lxmusic-service/source_runtime.py lxmusic-service/verify_source.py /srv/lxmusic-service/
COPY --chown=appuser:appuser lxmusic-service/js/bridge.js /srv/lxmusic-service/js/bridge.js
COPY --chown=appuser:appuser webui-service/app.py /srv/webui-service/
COPY --chown=appuser:appuser webui-service/static/ /srv/webui-service/static/
# webui 复用 .env 安全合并（sys.path 上加 /srv 后 import proxy.env_merge），必须随镜像分发
COPY --chown=appuser:appuser proxy/__init__.py proxy/env_merge.py /srv/proxy/
RUN find /srv -name "*.py" -exec chmod 0644 {} + \
    && chmod 0755 /srv/lxmusic-service/js/bridge.js \
    && mkdir -p /repo \
    && chmod 0755 /repo /data

COPY container/supervisord.conf /etc/supervisor/supervisord.conf
COPY container/env_flag.sh container/entrypoint.sh container/healthcheck.sh /usr/local/bin/
# COPY 保留上下文文件 mode：umask 077 检出下 supervisord.conf 会以 600 进镜像，
# appuser 读不了配置导致容器起不来；权限全部显式规范化，不依赖构建上下文
RUN chmod 0755 /usr/local/bin/env_flag.sh /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh \
    && chmod 0644 /etc/supervisor/supervisord.conf

USER appuser
EXPOSE 8001 8002 8003 8004

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
