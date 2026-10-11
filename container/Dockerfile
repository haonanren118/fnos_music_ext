# fnmusic-sources 统一镜像：musicdl + musicbox + lxmusic + webui 四进程（supervisor 管理，按需启动）
# 基础镜像引用可由 build args 覆盖（默认官方源；国内网络自动回退见根目录 ensure_base_image.sh）
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

# 复位为 root：base 可能是上一版自己的镜像（ensure_base_image.sh 的增量构建设计），
# 而那种镜像末尾带 `USER appuser`，会继承进本阶段，导致下面所有 RUN 以 appuser 跑
# （写 /tmp、/etc 全部 Permission denied）。末尾 L126 的 `USER appuser` 会再切回去。
USER root

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

# base 可能是上一版自己的镜像，其中 appuser 已存在；useradd 幂等化：
# 已存在则跳过创建，只补目录与属主（两者本身幂等）。
RUN id -u appuser >/dev/null 2>&1 \
    || useradd -m -u 1000 appuser; \
    mkdir -p /data/cache /data/config /data/lxmusic \
    && chown -R appuser:appuser /data

# 代码目录不 COPY 进镜像，而是建指向 /repo 的符号链接。
#
# 背景：compose 已把宿主 repo 目录整体挂到 /repo（`.:/repo`），是 bind mount，
# 容器内 /repo 始终等于宿主 repo 的实时内容。但 supervisord.conf 里四个程序的
# directory 是 /srv/*，而 /srv 属镜像层 —— 所以改宿主代码不生效，必须重建镜像。
# 改成软链后：容器内 /srv/<svc> -> /repo/<svc>，直接读宿主实时文件。
#
# 生效方式（无需 rebuild）：
#   - *.py 改动    → docker exec fnmusic-sources supervisorctl restart <prog>  （1~2s）
#   - static/ 改动 → 浏览器刷新即可（app.py 用 Path(__file__).resolve() 解析软链，
#                    且 NoCacheStatic 每次请求读盘，连进程都不用重启）
#
# build 期 /repo 尚为空目录（挂载发生在 run 期），此时 ln -sfn 会创建「悬空软链」，
# 容器启动挂载后自动接上 —— 这是期望行为。
# 注意：本层所有 RUN 命令都不得跟随软链读取内容（build 期会失败）。
#
# ⚠ 必须先 rm -rf 再 ln：base 可能是上一版自己的镜像，其中 /srv/<svc> 是真实目录
#（含旧 COPY 进去的代码）。此时 `ln -sfn TARGET DIR` 不会替换 DIR，而是在 DIR 内部
#   创建一个同名软链（rc=0，静默「成功」，实际毫无效果）。必须显式删除旧目录。
RUN set -eu; \
    mkdir -p /repo; \
    for d in musicdl-service musicbox-service lxmusic-service webui-service proxy; do \
        rm -rf "/srv/$d"; \
        ln -sfn "/repo/$d" "/srv/$d"; \
    done; \
    chmod 0755 /repo /data

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
