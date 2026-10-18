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
    # unzip/curl：#55 的 lxserver 段要解包预编译包，并在 vendor 缺失时回退镜像链下载
apt-get install -y --no-install-recommends nodejs ffmpeg supervisor ca-certificates unzip curl; \
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
    && mkdir -p /data/cache /data/config /data/lxmusic /data/lxserver \
    && chown -R appuser:appuser /data

# --chown+chmod：镜像权限不依赖构建上下文的 umask/属主
COPY --chown=appuser:appuser musicdl-service/app.py musicdl-service/hardening.py /srv/musicdl-service/
COPY --chown=appuser:appuser musicbox-service/app.py musicbox-service/runner.py musicbox-service/netease_ext.py /srv/musicbox-service/
# lxserver_client.py 是 #55 公共歌单发现层的客户端，app.py 顶层 import，必须随镜像分发
COPY --chown=appuser:appuser lxmusic-service/app.py lxmusic-service/source_runtime.py lxmusic-service/verify_source.py lxmusic-service/lxserver_client.py /srv/lxmusic-service/
COPY --chown=appuser:appuser lxmusic-service/js/bridge.js /srv/lxmusic-service/js/bridge.js
COPY --chown=appuser:appuser webui-service/app.py /srv/webui-service/
# upgrade.py 必须随镜像分发：app.py 第 36 行 `import upgrade`，
# 且 /api/upgrade/state、一键升级全靠它（start_upgrade / read_state / disk_free_mb）。
# 漏掉它服务会在 import 阶段直接崩 —— 镜像里只放 app.py 是不够的。
COPY --chown=appuser:appuser webui-service/upgrade.py /srv/webui-service/
COPY --chown=appuser:appuser webui-service/static/ /srv/webui-service/static/
# webui 复用 .env 安全合并（sys.path 上加 /srv 后 import proxy.env_merge），必须随镜像分发
COPY --chown=appuser:appuser proxy/__init__.py proxy/env_merge.py /srv/proxy/
RUN find /srv -name "*.py" -exec chmod 0644 {} + \
    && chmod 0755 /srv/lxmusic-service/js/bridge.js \
    && mkdir -p /repo \
    && chmod 0755 /repo /data

# lxserver（#55 公共歌单发现层）：预编译包随仓库分发（container/lxserver-vendor/），
# 缺失才回退镜像链下载 —— 与 proxy/wheels 同策略，保证 ARM/x86 构建可复现、可离线。
# 架构说明：我方 lxserver 仅提供歌单/榜单发现（/api/music/songList|leaderboard），
# 不参与播放、不使用自定义源沙箱，故不需要上游的 lyric / compat 两个补丁。
# zip 内 node_modules 只带 x64 预编译 music-tag-native，ARM 构建需装入 vendored
# arm64 原生模块（server.js 顶层 require music-tag-native，缺了起不来）。
ARG LXSERVER_MIRROR="https://ghfast.top"
COPY container/lxserver.version /srv/lxserver.version
COPY container/lxserver-vendor/ /srv/lxserver-vendor/
# resolv.conf 的改写不跨 RUN 层持久（Docker 每个 RUN 独立挂载），也不会写进最终
# 镜像，无需事后复位；fix_dns 与 apt/pip 两步同款，仅为本步可能的回退下载服务。
RUN set -eu; \
    fix_dns() { \
        { \
            printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n'; \
            grep -vE '^[[:space:]]*nameserver[[:space:]]+(127\.|::1|localhost)' /etc/resolv.conf 2>/dev/null || true; \
        } > /tmp/resolv.fnmusic 2>/dev/null || true; \
        cat /tmp/resolv.fnmusic > /etc/resolv.conf 2>/dev/null || true; \
    }; \
    fix_dns; \
    . /srv/lxserver.version; \
    VENDOR_DIR=/srv/lxserver-vendor; \
    TARGET_ZIP="${VENDOR_DIR}/${LXSERVER_ZIP_NAME}"; \
    if [ ! -s "${TARGET_ZIP}" ]; then \
        echo "[INFO] vendor 预置包缺失，通过镜像链下载 ${LXSERVER_ZIP_NAME}..."; \
        DOWNLOAD_OK=0; \
        if [ -z "${APT_MIRROR:-}" ]; then \
            _chain="\"\" ${LXSERVER_MIRROR} https://gh-proxy.com"; \
        else \
            _chain="${LXSERVER_MIRROR} https://gh-proxy.com https://github.moeyy.xyz \"\""; \
        fi; \
        for mirror in ${_chain}; do \
            if [ "${mirror}" = "\"\"" ] || [ -z "${mirror}" ]; then \
                URL="https://github.com/XCQ0607/lxserver/releases/download/${LXSERVER_TAG}/${LXSERVER_ZIP_NAME}"; \
            else \
                URL="${mirror}/https://github.com/XCQ0607/lxserver/releases/download/${LXSERVER_TAG}/${LXSERVER_ZIP_NAME}"; \
            fi; \
            echo "[INFO] 尝试从 ${URL} 下载..."; \
            if curl -fSL --connect-timeout 10 --max-time 300 "${URL}" -o "${TARGET_ZIP}"; then \
                if [ -s "${TARGET_ZIP}" ]; then DOWNLOAD_OK=1; break; fi; \
            fi; \
        done; \
        if [ "${DOWNLOAD_OK}" -ne 1 ]; then \
            echo "ERROR: lxserver zip 下载失败，请预先放置于 container/lxserver-vendor/" >&2; \
            exit 1; \
        fi; \
    fi; \
    ACTUAL_SHA="$(sha256sum "${TARGET_ZIP}" | awk '{print $1}')"; \
    if [ "${ACTUAL_SHA}" != "${LXSERVER_SHA256}" ]; then \
        echo "ERROR: lxserver zip 校验和不符 (${ACTUAL_SHA} != ${LXSERVER_SHA256})" >&2; \
        exit 1; \
    fi; \
    mkdir -p /srv/lxserver; \
    rm -rf /srv/lxserver-tmp; \
    unzip -q -o "${TARGET_ZIP}" -d /srv/lxserver-tmp; \
    if [ -d "/srv/lxserver-tmp/lx-music-sync-server" ]; then \
        cp -a /srv/lxserver-tmp/lx-music-sync-server/. /srv/lxserver/; \
    else \
        cp -a /srv/lxserver-tmp/. /srv/lxserver/; \
    fi; \
    if [ ! -f /srv/lxserver/server/server/server.js ]; then \
        echo "ERROR: lxserver 解包后缺少 server/server/server.js（zip 结构与预期不符）" >&2; \
        exit 1; \
    fi; \
    if [ "$(dpkg --print-architecture)" = "arm64" ]; then \
        if [ ! -s "${VENDOR_DIR}/music-tag-native-linux-arm64-gnu/music-tag-native.linux-arm64-gnu.node" ]; then \
            echo "ERROR: ARM 构建缺少 vendored music-tag-native-linux-arm64-gnu 原生模块" >&2; \
            exit 1; \
        fi; \
        rm -rf /srv/lxserver/node_modules/music-tag-native-linux-arm64-gnu; \
        cp -a "${VENDOR_DIR}/music-tag-native-linux-arm64-gnu" /srv/lxserver/node_modules/; \
    fi; \
    printf '%s\n' \
        'module.exports = {' \
        '  serverName: "fnmusic-lxserver",' \
        '  bindIP: "127.0.0.1",' \
        '  port: 8005,' \
        '  "player.enableAuth": false,' \
        '  "subsonic.enable": false,' \
        '  "webdav.enable": false' \
        '};' > /srv/lxserver/config.js; \
    rm -rf /srv/lxserver-tmp "${VENDOR_DIR}" /srv/lxserver.version; \
    chown -R appuser:appuser /srv/lxserver

# 注意：此处刻意不使用 `find /srv -name "*.py" -exec chmod` —— 软链后 find 会
# 跟随进入 /repo，把宿主 repo 里的文件权限一并改掉（UMask=0077 下会让 appuser 读不了）。
# 镜像权限由构建上下文保证，运行期代码权限由宿主 repo 自身权限（root:root 664/775）保证。

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
