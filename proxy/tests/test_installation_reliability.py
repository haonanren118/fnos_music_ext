"""Installation tests: real temporary UDS/processes, never host services or .env."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

BASE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('takeover', BASE / 'proxy/takeover.py')
takeover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(takeover)

SERVER = r'''
import json, os, socketserver, sys, time
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(4096)
        if not data: return
        if b'/_ext/livez ' in data:
            body = {'service': 'fnmusic-ext', 'pid': os.getpid() + (1 if sys.argv[2]=='wrongpid' else 0)} if sys.argv[2] != 'unknown' else {'code': 99999, 'message': 'INVALID TOKEN'}
        else:
            if sys.argv[2] == 'slow': time.sleep(2.6)
            body = {'ok': sys.argv[2]!='unhealthy', 'upstream': 'ok'}
        payload = json.dumps(body).encode()
        try: self.request.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
        except OSError: pass
class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
Server(sys.argv[1], Handler).serve_forever()
'''


@pytest.fixture(autouse=True)
def test_interpreter_packages(monkeypatch):
    # Keep test dependencies available while preflight isolates HOME.
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(p for p in sys.path if p and Path(p).is_dir()))


@pytest.fixture
def servers(tmp_path):
    children = []
    def start(path, kind='proxy', official=False):
        python = sys.executable
        if official:
            python = str(tmp_path / 'trim-music')
            if not Path(python).exists():
                shutil.copy2(sys.executable, python)
        p = subprocess.Popen([python, '-u', '-c', SERVER, str(path), kind],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(p)
        deadline = time.monotonic()+5
        while not path.exists() and p.poll() is None and time.monotonic()<deadline:
            time.sleep(0.01)
        assert p.poll() is None and path.exists()
        return p
    yield start
    for p in children:
        if p.poll() is None:
            p.terminate()
        p.wait(timeout=5)


@pytest.fixture
def state(tmp_path):
    return takeover.State(tmp_path/'target.sock', tmp_path/'upstream.sock', tmp_path/'state')


def test_business_response_is_not_identity(state, servers):
    servers(state.target, 'unknown')
    assert takeover.snapshot(state.target)['kind'] == 'unknown'
    servers(state.upstream, official=True)
    before = takeover.inode(state.target)
    with pytest.raises(takeover.Unsafe):
        state.restore()
    assert takeover.inode(state.target) == before


def test_livez_requires_peer_pid(state, servers):
    servers(state.target, 'wrongpid')
    assert takeover.snapshot(state.target)['kind'] == 'unknown'


def test_takeover_stale_proxy_restore_and_repeat(state, servers, tmp_path):
    official = servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    staged = tmp_path/'staged.sock'
    proxy = servers(staged)
    identity = takeover.snapshot(staged)
    state.publish(staged, identity)
    takeover.wait_ready(state, 2)
    assert takeover.snapshot(state.upstream) == original
    with pytest.raises(takeover.Unsafe):
        state.restore()  # never unlink a live listener, even our own
    proxy.terminate(); proxy.wait(timeout=3)
    state.restore()
    state.restore()
    assert takeover.snapshot(state.target) == original
    assert official.poll() is None


def test_publish_reclaims_live_upstream_orphan(state, servers, tmp_path):
    """An orphaned pre-restart official listener must not block installation."""
    official = servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    orphan = servers(state.upstream, official=True)
    staged = tmp_path/'staged.sock'
    servers(staged)
    state.publish(staged, takeover.snapshot(staged))
    assert takeover.snapshot(state.target)['kind'] == 'proxy'
    assert takeover.snapshot(state.upstream) == original
    # Only the orphan's pathname is dropped; both official processes stay alive.
    assert official.poll() is None and orphan.poll() is None


def test_replaced_stale_inode_is_reclaimed_and_restored(state, servers):
    servers(state.upstream, official=True)
    proxy = servers(state.target)
    state.remember()
    proxy.terminate(); proxy.wait(timeout=3)
    old = state.target.with_suffix('.old')
    state.target.rename(old)  # hold the recorded inode allocated elsewhere
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.target)); s.close()
    # The replacement is provably vacant: recovery must not deadlock on it.
    state.restore()
    assert takeover.snapshot(state.target)['kind'] == 'official'
    assert not state.upstream.exists()
    assert old.exists()


def test_legacy_absent_target_official_upstream_recovers(state, servers):
    servers(state.upstream, official=True)
    identity = takeover.snapshot(state.upstream)
    state.restore()
    assert takeover.snapshot(state.target) == identity


def test_legacy_dead_unrecorded_target_is_repaired(state, servers):
    servers(state.upstream, official=True)
    identity = takeover.snapshot(state.upstream)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.target)); s.close()
    assert takeover.snapshot(state.target)['kind'] == 'stale'
    assert state.restore_plan() == 'vacant-target-repair'
    state.restore()
    assert takeover.snapshot(state.target) == identity
    assert not state.upstream.exists()


def test_restore_plan_allows_official_target_with_live_orphan(state, servers, tmp_path):
    servers(state.target, official=True)
    servers(state.upstream, official=True)
    assert state.restore_plan() == 'official-reclaim'
    # Any live upstream occupant is reclaimed, not just a verified official orphan.
    alt = takeover.State(tmp_path/'t2.sock', tmp_path/'u2.sock', tmp_path/'s2')
    servers(alt.target, official=True)
    servers(alt.upstream, 'unknown')
    assert alt.restore_plan() == 'official-reclaim'


def test_restore_reclaims_live_upstream_orphan(state, servers, capsys):
    official = servers(state.target, official=True)
    before = takeover.snapshot(state.target)
    orphan = servers(state.upstream, official=True)
    state.restore()
    assert takeover.snapshot(state.target) == before
    assert not state.upstream.exists()
    # Only the pathname is dropped; the orphan keeps its inode and the
    # official listener keeps serving clients on target.
    assert official.poll() is None and orphan.poll() is None
    assert state.load() == {'target': str(state.target), 'upstream': str(state.upstream)}
    assert 'official socket already in place' in capsys.readouterr().out


def test_restore_plan_still_refuses_unverified_target(state, servers):
    servers(state.target, 'unknown')
    servers(state.upstream, 'unknown')
    with pytest.raises(takeover.Unsafe, match='upstream identity unverifiable'):
        state.restore_plan()


def test_publish_reclaims_dead_target_leftover(state, servers, tmp_path):
    """An aborted takeover's leftover must never block the next installation."""
    servers(state.upstream, official=True)
    official = takeover.snapshot(state.upstream)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.target)); s.close()
    staged = tmp_path/'staged.sock'
    servers(staged)
    proxy = takeover.snapshot(staged)
    state.publish(staged, proxy)
    assert takeover.snapshot(state.target) == proxy
    assert takeover.snapshot(state.upstream) == official


def test_publish_reclaims_dead_upstream_leftover(state, servers, tmp_path):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.upstream)); s.close()
    staged = tmp_path/'staged.sock'
    servers(staged)
    state.publish(staged, takeover.snapshot(staged))
    assert takeover.snapshot(state.upstream) == original


def test_restore_clears_dead_upstream_leftover(state, servers):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    s = socket.socket(socket.AF_UNIX); s.bind(str(state.upstream)); s.close()
    state.restore()
    assert takeover.snapshot(state.target) == original
    assert not state.upstream.exists()


def test_restore_reports_absent_official_and_leaves_no_record(state):
    for path in (state.target, state.upstream):
        s = socket.socket(socket.AF_UNIX); s.bind(str(path)); s.close()
    with state.lock(): state.save({'proxy': {'inode': [0, 0]}})
    with pytest.raises(takeover.Unsafe, match='no official listener present'):
        state.restore()
    assert not state.target.exists() and not state.upstream.exists()
    assert not (state.load().get('proxy') or state.load().get('official'))


def test_symlink_is_not_socket(state, servers, tmp_path):
    other = tmp_path/'other.sock'; servers(other)
    state.target.symlink_to(other)
    with pytest.raises(takeover.Unsafe):
        state.restore()
    assert state.target.is_symlink()


def test_slow_readiness_and_true_deadline(state, servers):
    servers(state.target, 'slow')
    with state.lock():
        state.save({'proxy': takeover.snapshot(state.target)})
    start = time.monotonic()
    takeover.wait_ready(state, 4)
    assert 2.5 < time.monotonic()-start < 4.2
    start = time.monotonic()
    with pytest.raises(takeover.Unsafe):
        takeover.wait_ready(state, 0.3)
    assert time.monotonic()-start < 0.55


def test_readiness_retries_publication_identity_change(state, servers, tmp_path, monkeypatch):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    staged = tmp_path/'staged.sock'
    servers(staged)
    proxy = takeover.snapshot(staged)
    connect = takeover.connect
    snapshot = takeover.snapshot
    published = False
    rejected = []

    def publish_after_connect(path, timeout=0.4):
        nonlocal published
        connection, peer = connect(path, timeout)
        if path == state.target and not published:
            published = True
            try:
                state.publish(staged, proxy)
            except BaseException:
                connection.close()
                raise
        return connection, peer

    def observe_snapshot(path, timeout=0.9):
        try:
            return snapshot(path, timeout)
        except takeover.Unsafe as exc:
            rejected.append(str(exc))
            raise

    monkeypatch.setattr(takeover, 'connect', publish_after_connect)
    monkeypatch.setattr(takeover, 'snapshot', observe_snapshot)
    takeover.wait_ready(state, 3)
    assert rejected == ['socket changed during identity check']
    assert takeover.snapshot(state.target) == proxy
    assert takeover.snapshot(state.upstream) == original
    assert state.load()['proxy'] == proxy


def test_readiness_persistent_identity_change_fails_closed(state, monkeypatch):
    attempts = []

    def changed(path, timeout=0.9):
        attempts.append(path)
        raise takeover.Unsafe('socket changed during identity check')

    monkeypatch.setattr(takeover, 'snapshot', changed)
    with pytest.raises(takeover.Unsafe, match='readiness deadline exceeded: socket changed'):
        takeover.wait_ready(state, 0.05)
    assert attempts
    assert not state.target.exists()
    assert not state.upstream.exists()
    assert not state.file.exists()


def test_legacy_restore_recovery_full_flow(state, servers, tmp_path, monkeypatch):
    """Old proxy without livez: plan correlates it with the unit, then restores."""
    legacy_server = servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    legacy = takeover.snapshot(state.target)
    assert legacy['kind'] == 'unknown'
    assert takeover.snapshot(state.upstream)['kind'] == 'official'
    # Fake systemctl: the unit's MainPID is the legacy listener's kernel peer.
    main = legacy['process']['pid']

    def fake_systemctl(command, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(command, 0, stdout=f'{main}\n', stderr='')

    monkeypatch.setattr(takeover.subprocess, 'run', fake_systemctl)
    assert state.restore_plan() == 'proxy-recovery'
    with state.lock():
        recorded = state.load()
    assert recorded['proxy'] == dict(legacy, kind='proxy')
    # The plan may be repeated; identity and records stay consistent.
    assert state.restore_plan() == 'proxy-recovery'
    # Simulate `systemctl disable --now`: stop the listener, socket file remains.
    legacy_server.terminate(); legacy_server.wait(timeout=3)
    assert takeover.snapshot(state.target)['kind'] == 'stale'
    state.restore()
    assert takeover.snapshot(state.target) == takeover.snapshot(state.upstream) or not state.upstream.exists()
    assert not (state.load().get('proxy') or state.load().get('official'))


def test_legacy_restore_refuses_unverifiable_without_stop(state, servers, monkeypatch):
    servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    before = (takeover.snapshot(state.target), takeover.snapshot(state.upstream))

    def refuse(*args, **kwargs):
        raise subprocess.SubprocessError('systemctl unavailable')

    monkeypatch.setattr(takeover.subprocess, 'run', refuse)
    with pytest.raises(takeover.Unsafe):
        state.restore_plan()
    # Nothing stopped or changed; restore stays equally refused.
    assert (takeover.snapshot(state.target), takeover.snapshot(state.upstream)) == before
    with pytest.raises(takeover.Unsafe):
        state.restore_plan()
    assert not state.file.exists()


def test_foreign_legacy_listener_never_attributed(state, servers, monkeypatch):
    servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    foreign_dir = state.directory.parent / (state.directory.name + '.x')
    foreign_dir.mkdir(mode=0o700, exist_ok=True)
    foreign = servers(foreign_dir / 'other.sock')

    def fake_systemctl(command, capture_output, text, timeout, check):
        # Unit points at an unrelated process, not the socket's peer.
        return subprocess.CompletedProcess(command, 0, stdout=f'{foreign.pid}\n', stderr='')

    monkeypatch.setattr(takeover.subprocess, 'run', fake_systemctl)
    # Which refusal fires depends on whether a live fnmusic-ext.service cgroup
    # exists on the host: a missing cgroup -> "not the unit MainPID", an existing
    # one whose procs exclude the peer -> "outside the deployed unit". Both mean
    # the foreign listener is never attributed to this deployment.
    with pytest.raises(takeover.Unsafe, match='not the unit MainPID|outside the deployed unit'):
        state.restore_plan()
    assert takeover.snapshot(state.target)['kind'] == 'unknown'
    assert not state.file.exists()


def function(text, name):
    start = text.index(name+'() {')
    return text[start:text.index('\n}', start)+2]+'\n'


PROVIDER_FLAGS = {
    'musicdl': (1, 0, 0),
    'musicbox': (0, 1, 0),
    'lxmusic': (0, 0, 1),
}


@pytest.mark.parametrize('provider,flags', sorted(PROVIDER_FLAGS.items()))
def test_source_config_and_single_choice(tmp_path, provider, flags):
    # Extract actual shell function definitions; every external action is mocked.
    install = (BASE/'install.sh').read_text()
    script = 'set -euo pipefail\nlog_err() { printf "%s\\n" "$*" >&2; }\nlog_info() { :; }\n'
    script += function(install, 'parse_sources')
    script += function(install, 'cleanup_legacy_sources')
    script += '''remove_owned_container() { printf 'container %s\\n' "$1"; }
stop_owned_source_unit() { printf 'unit %s\\n' "$1"; }
'''
    script += f'parse_sources {provider}\nprintf "flags %s %s %s\\n" "$ENABLE_MUSICDL" "$ENABLE_MUSICBOX" "$ENABLE_LX"\ncleanup_legacy_sources\n'
    result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, check=True)
    rows = result.stdout.splitlines()
    assert rows[0] == 'flags '+' '.join(map(str, flags))
    # 旧 v1.x 三 unit + 三容器清理：单容器接管端口 8768/8770/8772 前全部回收
    for name in ('musicdl', 'musicbox', 'lxmusic'):
        assert f'unit fnmusic-{name}' in rows
        assert f'container fnmusic-{name}' in rows
    # Actual app config import under the selected provider.
    python = Path(sys.executable)
    env = os.environ.copy()
    env.update(FNMUSIC_HOME=str(tmp_path), FNMUSIC_DEPLOY_MODE='docker',
               FNMUSIC_MUSICDL_ENABLED=str(bool(flags[0])).lower(),
               FNMUSIC_NETEASE_ENABLED=str(bool(flags[1])).lower(),
               FNMUSIC_LX_ENABLED=str(bool(flags[2])).lower(), PYTHONDONTWRITEBYTECODE='1')
    for key in ('FNMUSIC_CACHE_DIR','FNMUSIC_FAV_DIR','FNMUSIC_RECOMMEND_DIR','FNMUSIC_PLAY_HISTORY_DIR','FNMUSIC_LIBRARY_DIR'):
        env[key] = str(tmp_path/key)
    env['FNMUSIC_MUSIC_DB'] = str(tmp_path/'unused.db')
    code = 'import app,json;print(json.dumps([app.CONF[k] for k in ("musicdl_enabled","netease_enabled","lx_enabled")]))'
    output = subprocess.check_output([str(python), '-B', '-c', code], env=env, cwd=BASE/'proxy', text=True)
    assert json.loads(output) == list(map(bool, flags))


def install_platform_block():
    """提取 install.sh 的平台表 + 解析函数块（菜单表 → 参数解析 while 之前）。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    start = text.index("SOURCE_PLATFORM_TABLE='")
    end = text.index('while [ $# -gt 0 ]')
    return text[start:end]


def run_install_bash(body):
    script = 'set -euo pipefail\nlog_err() { printf "%s\\n" "$*" >&2; }\n' + body
    return subprocess.run(['bash', '-c', script], text=True, capture_output=True)


def test_parse_sources_platform_tokens():
    block = install_platform_block()
    body = block + '''
parse_sources "netease"
printf "netease %s %s %s [%s] [%s] %s %s\\n" "$ENABLE_MUSICBOX" "$ENABLE_MUSICDL" "$ENABLE_LX" "$LX_PLATFORMS" "$MDL_PLATFORMS" "$LX_EXPLICIT" "$MDL_EXPLICIT"
parse_sources "musicdl-kuwo,mdl-49"
printf "mdl2 %s %s %s [%s] [%s]\\n" "$ENABLE_MUSICBOX" "$ENABLE_MUSICDL" "$ENABLE_LX" "$MDL_PLATFORMS" "$LX_PLATFORMS"
parse_sources "lxmusic-KuGou"
printf "alias [%s]\\n" "$LX_PLATFORMS"
parse_sources "2,3"
printf "ids23 %s %s %s [%s]\\n" "$ENABLE_MUSICDL" "$ENABLE_MUSICBOX" "$ENABLE_LX" "$MDL_PLATFORMS"
parse_sources "59,60"
printf "ids5960 [%s]\\n" "$LX_PLATFORMS"
parse_sources "49"
printf "id49 [%s]\\n" "$MDL_PLATFORMS"
parse_sources "64"
printf "id64 [%s]\\n" "$MDL_PLATFORMS"
parse_sources "14"
printf "id14 [%s]\\n" "$MDL_PLATFORMS"
parse_sources "lx-kw,lx-kg,lx-kw"
printf "union [%s]\\n" "$LX_PLATFORMS"
parse_sources "musicdl-all"
printf "all [%s] [%s]\\n" "$MDL_PLATFORMS" "$(mdl_short_to_full "$MDL_PLATFORMS")"
parse_sources "8"
printf "id8 %s %s [%s]\\n" "$ENABLE_MUSICBOX" "$ENABLE_MUSICDL" "$MDL_PLATFORMS"
parse_sources "musicbox"
printf "bare [%s] [%s] %s %s\\n" "$LX_PLATFORMS" "$MDL_PLATFORMS" "$LX_EXPLICIT" "$MDL_EXPLICIT"
'''
    result = run_install_bash(body)
    assert result.returncode == 0, result.stderr
    rows = result.stdout.splitlines()
    # 裸名字 token = 整源默认平台，不动平台键
    assert rows[0] == 'netease 1 0 0 [] [] 0 0'
    # 同源多平台并集（musicdl 短名 + 全局编号）
    assert rows[1] == 'mdl2 0 1 0 [kuwo,gequhai] []'
    # lx 别名归一
    assert rows[2] == 'alias [kg]'
    # 全局编号 2,3 = mdl-酷我 + mdl-酷狗（不再是三整源）
    assert rows[3] == 'ids23 1 0 0 [kuwo,kugou]'
    # 59,60 = lx-酷狗 + lx-网易
    assert rows[4] == 'ids5960 [kg,wy]'
    assert rows[5] == 'id49 [gequhai]'
    # 64 = yinyueku（musicdl 2.13.11 新增，表末追加）
    assert rows[6] == 'id64 [yinyueku]'
    # 14 = mdl-youtube（非精选，文档编号可直接输入）
    assert rows[7] == 'id14 [youtube]'
    # 同源多平台并集去重
    assert rows[8] == 'union [kw,kg]'
    # musicdl-all = 显式默认平台，短名 → 全名映射
    assert rows[9] == 'all [kuwo,migu] [KuwoMusicClient,MiguMusicClient]'
    # 8 = musicdl 的网易云客户端；名字 netease 仍指向 musicbox
    assert rows[10] == 'id8 0 1 [netease]'
    assert rows[11] == 'bare [] [] 0 0'


def test_parse_sources_rejects_cross_provider_mix():
    """v2.0.0 三源互斥：跨 provider 组合直接报错。"""
    block = install_platform_block()
    for raw in ('musicbox,musicdl', 'netease,lx-kw', 'musicdl,lxmusic',
                '1,2', '1,2,4,59,60,61,62', 'musicdl-kuwo,lx-kw'):
        result = run_install_bash(block + f'parse_sources "{raw}"')
        assert result.returncode != 0, raw
        assert '音源三选一' in result.stderr, raw
        assert '互斥' in result.stderr, raw


def test_parse_sources_rejects_unknown_platforms():
    block = install_platform_block()
    result = run_install_bash(block + 'parse_sources "lx-foo"')
    assert result.returncode != 0 and '未知 lx 平台' in result.stderr
    result = run_install_bash(block + 'parse_sources "musicdl-nope"')
    assert result.returncode != 0 and '未知 musicdl 平台' in result.stderr
    result = run_install_bash(block + 'parse_sources "bogus"')
    assert result.returncode != 0 and '未知音源' in result.stderr
    result = run_install_bash(block + 'parse_sources "0"')
    assert result.returncode != 0 and '未知音源' in result.stderr
    result = run_install_bash(block + 'parse_sources "65"')
    assert result.returncode != 0 and '未知音源' in result.stderr
    # 53 = zhuolin 已随上游 musicdl 2.13.11 下线退役，编号空缺不复用
    result = run_install_bash(block + 'parse_sources "53"')
    assert result.returncode != 0 and '未知音源' in result.stderr
    result = run_install_bash(block + 'parse_sources "64"')
    assert result.returncode == 0, result.stderr
    result = run_install_bash(block + 'parse_sources "mdl-1"')
    assert result.returncode != 0 and '未知 musicdl 平台' in result.stderr
    result = run_install_bash(block + 'parse_sources "15"')
    assert result.returncode == 0, result.stderr


def test_env_write_platform_keys_explicit_only():
    """平台键仅显式选择时写入并覆盖；裸 token 沿用 .env 既有值。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    assert 'ENV_EXPLICIT="${ENV_EXPLICIT},LX_SOURCES"' in text
    assert 'ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_ONLINE_SOURCES,MUSICDL_SOURCES"' in text
    # 显式分支：短名白名单（代理请求级）+ 全名白名单（musicdl 服务级，经 /repo/.env 生效）
    assert 'echo "FNMUSIC_ONLINE_SOURCES=\'$(dotenv_escape "${MDL_PLATFORMS}")\'"' in text
    assert 'echo "MUSICDL_SOURCES=\'$(dotenv_escape "$(mdl_short_to_full "${MDL_PLATFORMS}")")\'"' in text
    assert 'echo "LX_SOURCES=\'$(dotenv_escape "${LX_PLATFORMS}")\'"' in text
    # 非显式分支维持旧默认值（env_merge 沿用既有值）
    assert "echo \"FNMUSIC_ONLINE_SOURCES='MiguMusicClient,KuwoMusicClient'\"" in text
    # v2.0.0：host unit 模板已移除；compose 单容器经挂载的 /repo/.env 读取平台白名单
    compose = (BASE/'docker-compose.yml').read_text(encoding='utf-8')
    assert 'MUSICDL_SOURCES' not in compose and 'LX_SOURCES' not in compose
    assert './sources-data:/data' in compose and '.:/repo' in compose


def test_docker_only_gate_blocks_without_docker(tmp_path):
    """v2.0.0 仅 Docker：无 docker / daemon 未运行直接报错退出安装。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    block = text[text.index('while [ $# -gt 0 ]'):text.index('\nprecheck_environment')]
    stubs = 'log_err() { printf "%s\\n" "$*" >&2; }\nusage() { :; }\n'
    bash = shutil.which('bash')
    argv = lambda script: [bash, '-c', stubs + script, 'install.sh', '--sources', 'musicdl']
    # 1) PATH 里没有 docker
    empty = tmp_path/'empty'; empty.mkdir()
    result = subprocess.run(argv(block),
                            env={**os.environ, 'PATH': str(empty)}, capture_output=True, text=True)
    assert result.returncode != 0 and '未检测到 docker' in result.stderr
    # 2) docker 命令存在但 daemon 不通（PATH 仅含 stub，避免 sudo 找到真 docker 兜底）
    bindir = tmp_path/'bin'; bindir.mkdir()
    (bindir/'docker').write_text('#!/bin/sh\nexit 1\n'); (bindir/'docker').chmod(0o755)
    result = subprocess.run(argv(block),
                            env={**os.environ, 'PATH': str(bindir)},
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'docker 服务未运行' in result.stderr
    # 3) docker 可用：闸门放行
    (bindir/'docker').write_text('#!/bin/sh\nexit 0\n')
    result = subprocess.run(argv(block + '\nprintf "gate-passed\\n"'),
                            env={**os.environ, 'PATH': str(bindir)},
                            capture_output=True, text=True)
    assert result.returncode == 0 and 'gate-passed' in result.stdout, result.stderr


def test_host_mode_removed():
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    # --mode 参数直接报错（给出明确迁移指引）
    assert '--mode|--mode=*)' in text and 'host 模式已移除' in text
    for fn in ('install_musicdl_host', 'install_musicbox_host', 'install_lxmusic_host',
               'clear_opposite_mode', 'stop_unselected'):
        assert fn not in text, f'{fn} 应随 host 模式移除'


def test_lx_url_required_and_webui_choice_wiring():
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    # --lx-source-url 可选（无源安装，装后在管理页配置）；本机 .js 路径自动转 file://
    assert '--lx-source-url)' in text
    assert '无源安装' in text
    assert 'file:///data/lxmusic/uploads/' in text
    assert '洛雪源文件必须是 .js 后缀' in text
    assert '洛雪源地址必须是 http(s) URL、file:// URL 或本机 .js 文件路径' in text
    # 容器内校验必须 --json，安装脚本才能按 category 分类提示
    assert 'verify_source.py --json' in text
    # WebUI：CLI 双向开关 + 向导询问 + 非交互默认不装 + 写入 .env
    assert '--webui) WEBUI_CHOICE="yes"' in text
    assert '--no-webui) WEBUI_CHOICE="no"' in text
    assert '是否安装管理 Web UI? [y/N]' in text
    assert 'WEBUI_CHOICE="${WEBUI_CHOICE:-no}"' in text
    assert "echo \"FNMUSIC_WEBUI_ENABLED='${WEBUI_FLAG}'\"" in text
    assert "FNMUSIC_WEBUI_PASSWORD" not in text
    # 旧 .env 多源并存的升级检测
    assert '检测到旧版 .env 同时启用了多个音源' in text


def test_env_flags_written_before_container_up():
    """先写 .env 三源开关再 compose up：entrypoint 首启即按需加载正确进程集。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    env_write = text.index('env_merge.py')
    up_call = text.index('\ninstall_sources_container\n')
    assert env_write < up_call, '.env 开关必须先于单容器 up -d 写入'


def test_data_migration_musicbox_to_sources_data(tmp_path):
    """v1.x musicbox-data → v2.0.0 sources-data：登录态原样保留 + 权限收紧到位。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    start = text.index('# 数据卷迁移')
    end = text.index('find "${SOURCES_DATA_DIR}" -type f', start)
    end = text.index('|| true', end) + len('|| true')
    block = text[start:end]
    login = tmp_path/'musicbox-data/netease-musicbox/login.json'
    login.parent.mkdir(parents=True)
    login.write_text('{"cookie":"x"}', encoding='utf-8')
    script = f'set -euo pipefail\nlog_info() {{ :; }}\nBASE_DIR={tmp_path}\n{block}\n'
    subprocess.run(['bash', '-c', script], check=True)
    assert not (tmp_path/'musicbox-data').exists()
    assert (tmp_path/'sources-data/netease-musicbox/login.json').read_text(encoding='utf-8') == '{"cookie":"x"}'
    assert (tmp_path/'sources-data/lxmusic').is_dir()
    assert (tmp_path/'sources-data/cache/netease-musicbox').is_dir()
    assert (tmp_path/'sources-data/config/netease-musicbox').is_dir()
    # 权限收紧：目录 0700 / 文件 0600（沙箱非 root，chown 静默失败属预期，只断权限位）
    assert (tmp_path/'sources-data').stat().st_mode & 0o777 == 0o700
    assert (tmp_path/'sources-data/netease-musicbox').stat().st_mode & 0o777 == 0o700
    assert (tmp_path/'sources-data/netease-musicbox/login.json').stat().st_mode & 0o777 == 0o600


def test_sources_data_permissions_hardened():
    """回归守卫：sources-data 不许回退到 chmod -R 0755（凭据全局可读）的旧语义。"""
    install = (BASE/'install.sh').read_text(encoding='utf-8')
    extend = (BASE/'extend.sh').read_text(encoding='utf-8')
    assert 'chmod -R 0755 "${SOURCES_DATA_DIR}"' not in install
    assert 'chmod -R 0755 "${BASE_DIR}/sources-data"' not in extend
    assert 'chown -R 1000:1000 "${SOURCES_DATA_DIR}"' in install
    assert 'chown -R 1000:1000 "${BASE_DIR}/sources-data"' in extend


def test_extend_lx_user_source_probe_states():
    """extend.sh 洛雪用户源探测：ok / broken / unconfigured 三态输出。"""
    extend = (BASE/'extend.sh').read_text(encoding='utf-8')
    start = extend.index('# 洛雪用户源状态')
    esac = extend.index('esac', start)
    end = extend.index('fi', esac) + 2
    block = extend[start:end]
    stubs = ("set -uo pipefail\n"
             "log_info() { printf 'info %s\\n' \"$*\"; }\n"
             "log_warn() { printf 'warn %s\\n' \"$*\"; }\n"
             "curl() { printf '%s' \"$LX_JSON\"; }\n"
             "ENABLE_LX=1\nLX_URL=http://127.0.0.1:8772\n")
    cases = [
        ('{"user_source": {"initialized": true, "source": {"name": "Test Source", "version": "1.2"}}}',
         'info 洛雪用户自定义源已加载: Test Source 1.2'),
        ('{"user_source": {"configured": true, "initialized": false, "last_error": "init timeout"}}',
         'warn 洛雪用户源初始化失败: init timeout'),
        ('{"user_source": {"configured": false}}',
         'warn 尚未配置洛雪用户自定义源'),
        ('', 'warn 尚未配置洛雪用户自定义源'),
    ]
    for body, expected in cases:
        result = subprocess.run(['bash', '-c', stubs + block],
                                env={**os.environ, 'LX_JSON': body},
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout, (body, result.stdout)


def test_extend_docker_only_and_single_container_probe():
    text = (BASE/'extend.sh').read_text(encoding='utf-8')
    # 仅 Docker 路径：无 host 分支，容器名固定 fnmusic-sources
    assert '仅支持 Docker 部署' in text
    assert 'CONTAINER_NAME="fnmusic-sources"' in text
    assert 'DEPLOY_MODE' not in text
    assert 'ensure_source ' not in text.replace('source_healthy ', '')
    # 全就绪时校验容器归属，防止借用其他 checkout 的容器
    assert 'reclaim_container "${CONTAINER_NAME}"' in text
    # 改 .env 后重启容器让 entrypoint 重选进程集
    assert 'run_docker restart "${CONTAINER_NAME}"' in text
    # WebUI 探测与汇总提示
    assert 'FNMUSIC_WEBUI_ENABLED' in text
    assert '飞牛管理员打开的管理页' in text
    # v1.x 数据目录兜底迁移
    assert 'musicbox-data -> sources-data' in text


def test_extend_healthy_path_still_syncs_image_and_env(tmp_path):
    """升级语义：服务全健康时也要 compose up -d --build 同步新代码（git pull 后生效）；
    镜像未变且 .env 比容器启动新时才显式重启容器让 entrypoint 重读开关。"""
    extend = (BASE/'extend.sh').read_text(encoding='utf-8')
    common = (BASE/'proxy'/'install_common.sh').read_text(encoding='utf-8')
    start = extend.index('if [ "${need_start}" -eq 0 ]; then')
    end = extend.index('\nfi', extend.index('run_docker restart "${CONTAINER_NAME}"', start)) + 3
    block = extend[start:end]
    funcs = function(extend, 'ensure_image_current') + function(common, 'env_newer_than_container')
    env_file = tmp_path/'.env'
    env_file.write_text('FNMUSIC_NETEASE_ENABLED=true\n', encoding='utf-8')
    log = tmp_path/'docker.log'

    stub_tpl = """set -uo pipefail
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*"; }
need_start=0
CONTAINER_NAME=fnmusic-sources
BASE_DIR=__BASE__
BASE_IMAGE_ENSURED=1
STARTED_AT='__STARTED__'
DOCKER_LOG=__LOG__
run_docker() {
  printf '%s\\n' "$*" >> "${DOCKER_LOG}"
  case "$1" in
    container) return 0 ;;
    image) if [ "${IMAGE_PRESENT:-1}" = "1" ]; then return 0; else return 1; fi ;;
    inspect) if [ "$3" = '{{.Image}}' ]; then printf 'sha256:img'; else printf '%s' "${STARTED_AT}"; fi; return 0 ;;
    compose|restart) return 0 ;;
  esac
}
reclaim_container() { return 0; }
"""
    def run_case(started_at, image_present="1"):
        script = (stub_tpl + funcs + '\n' + block
                  ).replace('__BASE__', str(tmp_path)).replace('__STARTED__', started_at
                  ).replace('__LOG__', str(log))
        log.write_text('', encoding='utf-8')
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                env={**os.environ, 'IMAGE_PRESENT': image_present})
        assert result.returncode == 0, result.stderr
        return log.read_text(encoding='utf-8')

    # 容器比 .env 新（未改配置的例行运行）：重建校验执行，但不重启。
    # 离线模式（06db4c1）：镜像已存在（fpk 内置/历史构建）时绝不 --build——
    # 构建容器无外网时 apt/pip 步骤必挂，表现为安装"卡在 55%"；镜像在则 up -d --no-build。
    out = run_case('2100-01-01T00:00:00Z')
    assert 'up -d --no-build' in out
    assert 'up -d --build' not in out
    assert 'restart' not in out
    # .env 比容器启动新（安装/切源后）：重建之外还要重启容器
    out = run_case('2000-01-01T00:00:00Z')
    assert 'up -d --no-build' in out
    assert 'restart fnmusic-sources' in out
    # 镜像不存在（源码部署/git pull 后首次）：回退 up -d --build 同步新代码
    out = run_case('2100-01-01T00:00:00Z', image_present='0')
    assert 'up -d --build' in out


def test_install_aligns_container_process_set_before_healthz(tmp_path):
    """升级/切源场景：compose 对镜像与配置均未变的运行中容器不重启，而 entrypoint
    只在容器启动时读一次 .env——install.sh 必须在等待 healthz 之前对齐进程集：
    .env 更新且所选音源未运行时显式 restart；全部已就绪（同参数幂等重跑）或
    容器比 .env 新（全新安装）时不重启，保持快速路径。"""
    install = (BASE/'install.sh').read_text(encoding='utf-8')
    common = (BASE/'proxy'/'install_common.sh').read_text(encoding='utf-8')
    funcs = (function(install, 'sources_quick_ready')
             + function(common, 'env_newer_than_container'))
    start = install.index('if env_newer_than_container && ! sources_quick_ready; then')
    end = install.index('\n    fi', start) + len('\n    fi')
    block = install[start:end]
    env_file = tmp_path/'.env'
    env_file.write_text('FNMUSIC_MUSICDL_ENABLED=true\n', encoding='utf-8')
    log = tmp_path/'docker.log'

    stub_tpl = """set -uo pipefail
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*"; }
ENABLE_MUSICBOX=0
ENABLE_MUSICDL=1
ENABLE_LX=0
WEBUI_FLAG=true
CONTAINER_NAME=fnmusic-sources
BASE_DIR=__BASE__
STARTED_AT='__STARTED__'
HEALTHZ_RC=__HEALTHZ__
DOCKER_LOG=__LOG__
run_docker() {
  printf '%s\\n' "$*" >> "${DOCKER_LOG}"
  case "$1" in
    inspect) printf '%s' "${STARTED_AT}"; return 0 ;;
    restart) return 0 ;;
  esac
}
curl() { return "${HEALTHZ_RC}"; }
"""
    def run_case(started_at, healthz_rc):
        script = (stub_tpl + funcs + '\n' + block
                  ).replace('__BASE__', str(tmp_path)).replace('__STARTED__', started_at
                  ).replace('__HEALTHZ__', str(healthz_rc)
                  ).replace('__LOG__', str(log))
        log.write_text('', encoding='utf-8')
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return log.read_text(encoding='utf-8')

    # .env 更新 + 所选音源未运行（升级恢复了 musicdl，容器在跑别的源）→ 重启对齐
    out = run_case('2000-01-01T00:00:00Z', 7)
    assert 'restart fnmusic-sources' in out
    # .env 更新 + 所选服务全部已就绪（同参数幂等重跑）→ 不重启
    out = run_case('2000-01-01T00:00:00Z', 0)
    assert 'restart' not in out
    # 容器比 .env 新（全新安装/容器刚按新 .env 创建）→ 不重启
    out = run_case('2100-01-01T00:00:00Z', 7)
    assert 'restart' not in out


def test_healthz_timeout_dumps_container_diagnostics():
    """healthz 超时必须取证：install.sh 四个等待分支与 extend.sh 的 wait_source
    都调用 diagnose_sources_container（supervisor 状态 + 容器日志尾部随安装日志
    落盘），env_newer_than_container 移入共享库后 extend.sh 不再持有本地副本。"""
    install = (BASE/'install.sh').read_text(encoding='utf-8')
    extend = (BASE/'extend.sh').read_text(encoding='utf-8')
    common = (BASE/'proxy'/'install_common.sh').read_text(encoding='utf-8')
    assert install.count('diagnose_sources_container "${CONTAINER_NAME}"') == 4
    assert 'diagnose_sources_container "${CONTAINER_NAME}"' in extend
    assert 'supervisorctl' in common
    assert 'logs --tail' in common
    assert 'env_newer_than_container()' not in extend
    assert 'env_newer_than_container()' in common


def test_host_install_unit_restarts_and_propagates_failure(tmp_path):
    install = (BASE/'install.sh').read_text()
    script = 'set -euo pipefail\n'+function(install, 'install_unit')
    script += '''log_warn() { :; }; log_err() { :; }
sudo() { printf '%s\\n' "$*"; if [ "$1 $2" = 'systemctl restart' ]; then return 1; fi; }
rm() { :; }
'''
    script += f'BASE_DIR={tmp_path}\nif install_unit {tmp_path}/src {tmp_path}/unit; then exit 99; fi\n'
    result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, check=True)
    assert 'systemctl enable unit' in result.stdout
    assert 'systemctl restart unit' in result.stdout
    assert '--now' not in result.stdout


def test_foreign_container_never_removed(tmp_path):
    script = f'set -euo pipefail\nsource "{BASE}/proxy/install_common.sh"\nBASE_DIR={tmp_path}\n'
    script += '''log_err() { :; }
run_docker() { if [ "$1" = rm ]; then exit 99; fi; if [ "$#" -gt 3 ]; then printf /some/other/checkout; fi; }
if remove_owned_container fnmusic-lxmusic; then exit 88; fi
'''
    subprocess.run(['bash', '-c', script], check=True)


def test_unit_uses_canonical_template_and_explicit_interpreters(tmp_path):
    for script in ('install.sh', 'extend.sh', 'restore.sh', 'proxy/run_proxy.sh', 'proxy/install_common.sh'):
        subprocess.run(['bash', '-n', str(BASE/script)], check=True)
    output = subprocess.check_output([sys.executable, str(BASE/'proxy/takeover.py'), 'render-unit', '--base', str(BASE)], text=True)
    assert '@BASE_DIR@' not in output
    assert 'ExecStart=/bin/bash ' in output and 'ExecStartPost=/usr/bin/python3 ' in output
    assert '[ -S ' not in output and 'Restart=no' in output


def test_install_decline_recommend_never_wipes_llm_config():
    """安装向导答 N 不清空已保存的大模型配置：只有显式 --disable-recommend 才写入空值。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    # 默认初始化 + 仅显式关闭时置位
    assert 'LLM_CLEAR=0' in text
    assert '--disable-recommend) ENABLE_RECOMMEND="no"; LLM_CLEAR=1' in text
    # 空值清除只允许出现在 LLM_CLEAR 分支（env 输出与 explicit 列表各一处）
    assert text.count("FNMUSIC_LLM_API_KEY=''") == 1
    assert text.count('elif [ "${LLM_CLEAR}" -eq 1 ]') == 2
    # 关闭推荐时不再无条件加入 explicit（旧版会把密钥覆盖为空）
    assert 'ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL,FNMUSIC_LLM_API_KEY,FNMUSIC_LLM_MODEL"\nfi' in text
    assert text.count('ENV_EXPLICIT="${ENV_EXPLICIT},FNMUSIC_LLM_BASE_URL,FNMUSIC_LLM_API_KEY,FNMUSIC_LLM_MODEL"') == 1


def test_restore_default_removes_sources_and_full_purges_state(tmp_path):
    """restore.sh 语义：默认删除音源容器但保留配置数据；--full 才工厂级清理。"""
    text = (BASE/'restore.sh').read_text(encoding='utf-8')
    # 容器清理位于默认路径（FULL_RESTORE 判定之前）
    assert text.index('remove_owned_container "${unit}"') < text.index('if [ "${FULL_RESTORE}" -eq 1 ]')
    # 数据清理只在 --full 分支内调用
    assert 'purge_local_state\n' in text.split('if [ "${FULL_RESTORE}" -eq 1 ]')[1].split('fi')[0]

    # 行为验证：提取 purge_local_state 在临时目录执行，只删已知路径，代码保留
    base = tmp_path/'deploy'
    for name in ('.env', '.env.bak.20260101000000', 'musicbox-data/x', 'sources-data/y',
                 'cache/a.ref',
                 'online_favorites/u.json', 'play_history/u.json', 'recommend_cache/u/d.json',
                 '.venv-proxy/bin/python'):
        p = base/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('x', encoding='utf-8')
    for name in ('install.sh', 'restore.sh', 'proxy/app.py', 'musicdl_outputs/out.bin'):
        p = base/name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('code', encoding='utf-8')

    script = 'set -euo pipefail\nlog_info() { :; }\n'
    script += function(text, 'purge_local_state')
    script += f'BASE_DIR="{base}"\npurge_local_state\n'
    subprocess.run(['bash', '-c', script], check=True)

    for name in ('.env', '.env.bak.20260101000000', 'musicbox-data', 'sources-data', 'cache',
                 'online_favorites', 'play_history', 'recommend_cache', '.venv-proxy'):
        assert not (base/name).exists(), f'--full 应删除 {name}'
    for name in ('install.sh', 'restore.sh', 'proxy/app.py', 'musicdl_outputs/out.bin'):
        assert (base/name).exists(), f'--full 必须保留代码文件 {name}'


def test_restore_cleans_legacy_and_v2_containers():
    """restore 兼容新旧两种部署形态：v1.x 三容器与 v2 单容器 fnmusic-sources 一并清理。"""
    text = (BASE/'restore.sh').read_text(encoding='utf-8')
    block = text[text.index('# 3. 停止并移除音源容器'):text.index('# 4. --full')]
    script = ('set -euo pipefail\n'
              'log_info() { printf "info %s\\n" "$*"; }\n'
              'log_warn() { printf "warn %s\\n" "$*"; }\n'
              'remove_owned_container() { printf "container %s\\n" "$1"; }\n'
              'owned_source_unit() { return 1; }\n'
              'stop_owned_source_unit() { printf "unit %s\\n" "$1"; }\n'
              'sudo() { printf "sudo %s\\n" "$*"; }\n'
              'rm() { printf "rm %s\\n" "$*"; }\n'
              'run_docker() { return 0; }\n')
    result = subprocess.run(['bash', '-c', script + block], text=True, capture_output=True, check=True)
    for name in ('fnmusic-musicdl', 'fnmusic-musicbox', 'fnmusic-lxmusic', 'fnmusic-sources'):
        assert f'container {name}' in result.stdout, name
    assert '已清理完毕' in result.stdout


def test_preflight_invalid_config_never_touches_sockets_or_production(tmp_path):
    base = tmp_path/'checkout'; base.mkdir()
    (base/'proxy').symlink_to(BASE/'proxy', target_is_directory=True)
    (base/'.venv-proxy/bin').mkdir(parents=True)
    (base/'.venv-proxy/bin/python').symlink_to(sys.executable)
    (base/'.env').write_text("FNMUSIC_ONLINE_LIMIT='secret-invalid-number'\n")
    result = subprocess.run([sys.executable, str(BASE/'proxy/takeover.py'), 'preflight', '--base', str(base)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'secret-invalid-number' not in result.stdout+result.stderr
    assert not (base/'cache').exists()


def test_dotenv_is_data_not_executable(tmp_path):
    (tmp_path/'.env').write_text("FNMUSIC_LLM_API_KEY='one'\\''two'\nMALICIOUS='$(touch SHOULD_NOT_EXIST)'\n")
    env = takeover.environment(tmp_path)
    assert env['FNMUSIC_LLM_API_KEY'] == "one'two"
    assert env['MALICIOUS'] == '$(touch SHOULD_NOT_EXIST)'
    assert not (tmp_path/'SHOULD_NOT_EXIST').exists()


@pytest.mark.parametrize('action', ['term', 'child-kill', 'startup-fail'])
def test_real_supervisor_signal_and_crash_rollback(state, servers, tmp_path, action):
    servers(state.target, official=True)
    original = takeover.snapshot(state.target)
    base = tmp_path/'checkout'; (base/'proxy').mkdir(parents=True)
    (base/'.venv-proxy/bin').mkdir(parents=True)
    (base/'.venv-proxy/bin/python').symlink_to(sys.executable)
    (base/'proxy/recommend.py').write_text('')
    app = """import os
from fastapi import FastAPI
app = FastAPI()
@app.get('/_ext/livez')
def livez(): return {'service':'fnmusic-ext', 'pid':os.getpid()}
@app.get('/_ext/healthz')
def health(): return {'ok':True, 'upstream':'ok'}
"""
    if action == 'startup-fail':
        app += "\n@app.on_event('startup')\ndef fail(): raise ValueError('SECRET-never-log')\n"
    (base/'proxy/app.py').write_text(app)
    command = [sys.executable, str(BASE/'proxy/takeover.py'), 'run', '--base', str(base),
               '--target', str(state.target), '--upstream', str(state.upstream), '--state-dir', str(state.directory)]
    supervisor = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if action != 'startup-fail':
            takeover.wait_ready(state, 13)
            if action == 'term':
                supervisor.terminate()
            else:
                os.kill(state.load()['proxy']['process']['pid'], signal.SIGKILL)
        out, err = supervisor.communicate(timeout=12)
        assert 'SECRET-never-log' not in out+err
        assert takeover.snapshot(state.target) == original
        assert not state.upstream.exists()
        assert supervisor.returncode == (0 if action == 'term' else 1)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.communicate(timeout=12)


def test_official_restart_record_mismatch_is_adopted(state, servers, tmp_path):
    """A restarted official app must not deadlock install or restore."""
    official = servers(state.upstream, official=True)
    state.remember()
    official.terminate(); official.wait(timeout=3)
    state.upstream.rename(tmp_path/'old.sock')
    servers(state.upstream, official=True)
    restarted = takeover.snapshot(state.upstream)
    assert restarted['kind'] == 'official'
    state.remember()  # adopts the kernel-verified live identity
    assert state.load()['official'] == restarted
    state.restore()
    assert takeover.snapshot(state.target) == restarted
    assert not state.upstream.exists()


def _fake_checkout(tmp_path):
    """Minimal base directory the real supervisor can preflight and run."""
    base = tmp_path/'checkout'; (base/'proxy').mkdir(parents=True)
    (base/'.venv-proxy/bin').mkdir(parents=True)
    (base/'.venv-proxy/bin/python').symlink_to(sys.executable)
    (base/'proxy/recommend.py').write_text('')
    (base/'proxy/app.py').write_text("""import os
from fastapi import FastAPI
app = FastAPI()
@app.get('/_ext/livez')
def livez(): return {'service':'fnmusic-ext', 'pid':os.getpid()}
@app.get('/_ext/healthz')
def health(): return {'ok':True, 'upstream':'ok'}
""")
    return base


def test_supervisor_reports_primary_failure_and_rollback_failure(state, servers, tmp_path):
    """A rollback must never hide why the takeover failed."""
    servers(state.target, 'unknown')
    servers(state.upstream, official=True)
    base = _fake_checkout(tmp_path)
    command = [sys.executable, str(BASE/'proxy/takeover.py'), 'run', '--base', str(base),
               '--target', str(state.target), '--upstream', str(state.upstream), '--state-dir', str(state.directory)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert 'ambiguous/live socket topology; nothing removed' in result.stderr
    assert 'rollback failed: socket still live or indeterminate; preserved' in result.stderr
    # Both live listeners stay untouched when rollback also fails.
    assert takeover.snapshot(state.target)['kind'] == 'unknown'
    assert takeover.snapshot(state.upstream)['kind'] == 'official'


def test_record_symlink_and_permissions_rejected(state, tmp_path):
    with state.lock(): state.save({})
    state.file.chmod(0o666)
    with pytest.raises(takeover.Unsafe): state.load()
    state.file.unlink()
    other = tmp_path/'other.json'; other.write_text('{}')
    state.file.symlink_to(other)
    with pytest.raises(OSError): state.load()


def test_preflight_diagnostic_retains_only_exception_class():
    assert takeover.diagnostic('Traceback:\nValueError: SECRET_URL_AND_KEY') == 'ValueError'
    assert 'SECRET' not in takeover.diagnostic('SECRET arbitrary output')
    assert takeover.diagnostic("ModuleNotFoundError: No module named 'uvicorn'") == 'ModuleNotFoundError: missing module uvicorn'


def test_safe_child_logs_retain_outcomes_without_credentials(capsys):
    import io
    lines = (
        '2026-09-10 12:00:00,123 [WARNING] fnmusic_proxy: Failed to fetch online search from musicdl: ReadTimeout https://user:PASS@host/path?token=TOKEN api_key=KEY Cookie: COOKIE\n'
        'INFO:     Application startup complete.\n'
        '  File "/private/SECRET/path/app.py", line 42, in lifespan\n'
        "ModuleNotFoundError: No module named 'uvicorn'\n"
        'ValueError: SECRET_URL_AND_KEY\n'
        '2026-09-10 12:00:00,124 [INFO] fnmusic_proxy: llm_api_key = KEY\n'
        '2026-09-10 12:00:00,125 [WARNING] fnmusic_proxy.recommend: llm http 503\n'
    )
    takeover.drain_diagnostics(io.StringIO(lines), ('KEY',))
    output = capsys.readouterr().err
    assert 'WARNING Failed to fetch online search from musicdl: ReadTimeout' in output
    assert '2026-09-10 12:00:00,123' in output
    assert 'INFO Application startup complete.' in output
    assert 'traceback: app.py:42 in lifespan' in output
    assert 'missing module uvicorn' in output
    assert 'WARNING llm http 503' in output
    assert 'ValueError' in output
    for secret in ('PASS', 'TOKEN', 'KEY', 'COOKIE', 'SECRET', '/private', 'https://'):
        assert secret not in output


def test_safe_child_logs_retain_stream_probe_and_abort_lines(capsys):
    """流探针与流中断日志必须能进 journal：后台播放卡住排查时唯一的证据来源。"""
    import io
    lines = (
        "INFO:     stream probe: GET online:kuwo:123456 range='bytes=0-' cached=False tee_eligible=True\n"
        "2026-09-10 12:00:00,124 [WARNING] fnmusic_proxy: Stream aborted mid-way for online:kuwo:123456: ReadTimeout\n"
    )
    takeover.drain_diagnostics(io.StringIO(lines))
    output = capsys.readouterr().err
    assert 'stream probe: GET online:kuwo:123456' in output
    assert 'Stream aborted mid-way for online:kuwo:123456: ReadTimeout' in output


def test_safe_child_logs_retain_forward_unhandled_lines(capsys):
    """官方新端点取证日志必须能进 journal（应用中心模式下唯一可见途径）。

    官方 2026-09-25 更新后新增 forward-unhandled 采样日志，若被过滤器丢弃，
    官方升级引入新端点时用户日志里将无迹可循（review 发现的回归）。
    """
    import io
    lines = (
        "2026-09-28 15:00:00,001 [INFO] fnmusic_proxy: "
        "forward-unhandled GET /music/api/v1/download/track/detail (#1) —— 官方端点未被代理拦截，仅透传\n"
        "2026-09-28 15:00:01,002 [INFO] fnmusic_proxy: "
        "forward-unhandled POST /music/api/v1/search/index/rebuild (#50) —— 官方端点未被代理拦截，仅透传\n"
        "forward-unhandled EVIL /x password=SECRET\n"
    )
    takeover.drain_diagnostics(io.StringIO(lines), ('SECRET',))
    output = capsys.readouterr().err
    assert 'forward-unhandled GET /music/api/v1/download/track/detail (#1)' in output
    assert 'forward-unhandled POST /music/api/v1/search/index/rebuild (#50)' in output
    # 固定方法白名单之外的方法名不透传（EVIL 非 HTTP 方法），且秘密始终被抹除
    assert 'EVIL' not in output
    assert 'SECRET' not in output


def test_safe_child_logs_discard_oversized_line_tail(capsys):
    import io
    oversized = 'x' * 16384 + 'INFO:     Application startup complete.\n'
    takeover.drain_diagnostics(io.StringIO(oversized))
    output = capsys.readouterr().err
    assert 'oversized log line omitted' in output
    assert 'Application startup' not in output


def test_atomic_move_does_not_clobber_occupied_destination(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    a.write_text('source'); b.write_text('keep')
    with pytest.raises(takeover.Unsafe): takeover.move_no_replace(a, b)
    assert a.read_text() == 'source' and b.read_text() == 'keep'


def test_install_lock_reexec_preserves_uid_home_and_overrides(tmp_path):
    # Real unprivileged flock and re-exec; only privileged preparation is stubbed.
    common = (BASE/'proxy/install_common.sh').read_text().replace('/run/fnmusic-ext-install/operation.lock', str(tmp_path/'operation.lock'))
    helper = tmp_path/'common.sh'; helper.write_text(common)
    script = tmp_path/'install.sh'
    script.write_text(f"""#!/bin/bash
set -euo pipefail
BASE_DIR='{tmp_path}'
sudo() {{ :; }}
source '{helper}'
installation_lock "$@"
printf '%s|%s|%s|%s' "$(id -u)" "$HOME" "$PIP_INDEX" "$FNMUSIC_CUSTOM_TEST"
""")
    env = os.environ.copy()
    env.update(PIP_INDEX='custom-index', FNMUSIC_CUSTOM_TEST='retained', HOME=str(tmp_path))
    env.pop('FNMUSIC_INSTALL_LOCK_HELD', None)
    out = subprocess.check_output(['bash', str(script)], env=env, text=True)
    assert out == f'{os.getuid()}|{tmp_path}|custom-index|retained'


def test_checkout_ownership_accepts_symlinked_spelling(tmp_path):
    """The same checkout must never look foreign when reached via a symlink."""
    checkout = tmp_path/'vol2'/'checkout'
    (checkout/'musicdl-service').mkdir(parents=True)
    link = tmp_path/'home'/'checkout'
    link.parent.mkdir()
    link.symlink_to(checkout)
    other = tmp_path/'other'; other.mkdir()
    unit = tmp_path/'fnmusic-ext.service'
    unit.write_text(f'[Service]\nWorkingDirectory={checkout}\n')
    source = tmp_path/'fnmusic-musicdl.service'
    source.write_text(f'[Service]\nWorkingDirectory={checkout}/musicdl-service\n')
    helper = tmp_path/'common.sh'
    helper.write_text(
        (BASE/'proxy/install_common.sh').read_text()
        .replace('/etc/systemd/system/fnmusic-ext.service', str(unit))
        .replace('systemctl show fnmusic-ext.service -p WorkingDirectory --value', f"printf '%s\\n' '{checkout}'")
        .replace('/etc/systemd/system/${1}.service', str(source))
    )
    script = tmp_path/'check.sh'
    script.write_text(f"""#!/bin/bash
set -euo pipefail
BASE_DIR='{link}'
STUB_OWNER='{checkout}'
log_err() {{ printf 'refused: %s\\n' "$*"; }}
run_docker() {{ printf '%s\\n' "$STUB_OWNER"; }}
source '{helper}'
if check_proxy_unit_owner && owned_source_unit musicdl && reclaim_container fnmusic-musicdl; then
    printf 'accepted\\n'
else
    printf 'refused\\n'
fi
STUB_OWNER='{other}'
if reclaim_container fnmusic-musicdl; then printf 'foreign-accepted\\n'; else printf 'foreign-refused\\n'; fi
""")
    result = subprocess.run(['bash', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'accepted' in result.stdout
    assert 'foreign-refused' in result.stdout


def _run_proxy_unit_owner(tmp_path, *, unit_text, show_value, args=()):
    """check_proxy_unit_owner on a stubbed unit file / systemctl, BASE_DIR=checkout."""
    checkout = tmp_path/'checkout'
    checkout.mkdir(exist_ok=True)
    unit = tmp_path/'fnmusic-ext.service'
    unit.write_text(unit_text)
    helper = tmp_path/'common.sh'
    helper.write_text(
        (BASE/'proxy/install_common.sh').read_text()
        .replace('/etc/systemd/system/fnmusic-ext.service', str(unit))
        .replace('systemctl show fnmusic-ext.service -p WorkingDirectory --value',
                 f"printf '%s\\n' '{show_value}'")
    )
    script = tmp_path/'check.sh'
    script.write_text(f"""#!/bin/bash
set -euo pipefail
BASE_DIR='{checkout}'
log_err() {{ printf '[ERROR] %s\\n' "$*" >&2; }}
log_warn() {{ printf '[WARN] %s\\n' "$*" >&2; }}
source '{helper}'
check_proxy_unit_owner "$@"
""")
    return subprocess.run(['bash', str(script), *args], capture_output=True, text=True)


def test_proxy_unit_owner_adopts_when_owner_directory_deleted(tmp_path):
    """原部署目录已删除：放行接管（与 deployment_conflict 语义一致），不再死锁。"""
    gone = tmp_path/'gone'  # 从不创建
    result = _run_proxy_unit_owner(
        tmp_path,
        unit_text=f'[Service]\nWorkingDirectory={gone}\n',
        show_value=str(gone),
    )
    assert result.returncode == 0, result.stderr
    assert '已不存在' in result.stderr
    assert '直接接管' in result.stderr


def test_proxy_unit_owner_adopt_flag_bypasses_live_foreign_unit(tmp_path):
    """原目录仍存在：默认拒绝且报错给出双方路径与出路；--adopt 显式迁移放行。"""
    other = tmp_path/'other'
    other.mkdir()
    checkout = tmp_path/'checkout'
    kwargs = dict(
        unit_text=f'[Service]\nWorkingDirectory={other}\n',
        show_value=str(other),
    )
    refused = _run_proxy_unit_owner(tmp_path, **kwargs)
    assert refused.returncode != 0
    assert str(other) in refused.stderr
    assert str(checkout) in refused.stderr
    assert '--adopt' in refused.stderr
    assert 'restore.sh' in refused.stderr
    adopted = _run_proxy_unit_owner(tmp_path, **kwargs, args=('--adopt',))
    assert adopted.returncode == 0, adopted.stderr
    assert '--adopt 迁移' in adopted.stderr


def test_proxy_unit_owner_falls_back_to_unit_file_when_show_empty(tmp_path):
    """systemctl show 返回空：回退读 unit 文件本身，同目录仍判定为自己人。"""
    checkout = tmp_path/'checkout'
    result = _run_proxy_unit_owner(
        tmp_path,
        unit_text=f'[Service]\nWorkingDirectory={checkout}\n',
        show_value='',
    )
    assert result.returncode == 0, result.stderr


def test_proxy_unit_owner_rejects_when_working_directory_unreadable(tmp_path):
    """show 与 unit 文件都取不到 WorkingDirectory：明确报错而非静默失败。"""
    result = _run_proxy_unit_owner(
        tmp_path,
        unit_text='[Service]\nExecStart=/bin/true\n',
        show_value='',
    )
    assert result.returncode != 0
    assert '无法读取' in result.stderr
    assert 'fnmusic-ext.service' in result.stderr


def test_socket_mutation_lock_serializes_processes(state):
    with state.lock():
        command = [sys.executable, str(BASE/'proxy/takeover.py'), 'remember', '--target', str(state.target),
                   '--upstream', str(state.upstream), '--state-dir', str(state.directory)]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        assert child.poll() is None
    child.communicate(timeout=3)
    assert child.returncode == 0


def test_nested_startup_failure_runs_verified_rollback():
    extend = (BASE/'extend.sh').read_text()
    script = 'set -Eeuo pipefail\n'
    script += function(extend, 'rollback')
    script += """log_err() { printf 'error\n'; }; log_warn() { printf 'warn\n'; }; log_info() { printf 'info\n'; }
sudo() { printf 'sudo %s\n' "$*"; }
takeover() { printf 'takeover %s\n' "$*"; }
trap rollback ERR INT TERM
nested_startup() { false; }
nested_startup
"""
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'takeover remember' in result.stdout
    assert 'sudo systemctl stop fnmusic-ext.service' in result.stdout
    assert 'takeover restore' in result.stdout
    assert result.stdout.index('takeover remember') < result.stdout.index('sudo systemctl stop')


def test_official_only_restore_clears_old_record(state, servers):
    servers(state.target, official=True)
    with state.lock(): state.save({'proxy': {'inode': [0, 0]}})
    state.restore()
    assert 'proxy' not in state.load()


# ---------------------------------------------------------------- 网络脆弱性修复（v2.1.0）

def test_extend_reloads_version_without_read_eof_failure(tmp_path):
    """VERSION 无末尾换行时 read 会返回 1。extend 在 set -e 下必须改用 head。"""
    text = (BASE / "extend.sh").read_text(encoding="utf-8")
    assert "read -r FNMUSIC_VERSION" not in text
    version = tmp_path / "VERSION"
    version.write_bytes(b"9.9.9")
    script = (
        "set -euo pipefail\n"
        f'BASE_DIR="{tmp_path}"\n'
        'FNMUSIC_VERSION="stale-from-dotenv"\n'
        'FNMUSIC_VERSION="$(head -n 1 "${BASE_DIR}/VERSION" 2>/dev/null | tr -d \'[:space:]\' || true)"\n'
        'FNMUSIC_VERSION="${FNMUSIC_VERSION:-0.0.0}"\n'
        'printf "%s\\n" "${FNMUSIC_VERSION}"\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9.9.9"


def test_install_scripts_delegate_proxy_deps_to_fallback_helper():
    """宿主机代理依赖安装必须走 ensure_proxy_deps.sh 多源回退，不允许退回单源裸 pip。"""
    for name in ('install.sh', 'extend.sh'):
        text = (BASE/name).read_text(encoding='utf-8')
        assert 'ensure_proxy_deps.sh' in text, f'{name} 应调用 ensure_proxy_deps.sh'
        assert 'venv-proxy/bin/pip" install' not in text, f'{name} 不应残留单源裸 pip 安装'
    helper = (BASE/'ensure_proxy_deps.sh').read_text(encoding='utf-8')
    # 候选链：用户自定义 PIP_INDEX 永远第一位（默认腾讯云，H1.1 不限速），
    # 阿里云/清华/官方依次兜底
    assert 'https://mirrors.tencent.com/pypi/simple/' in helper
    assert 'https://mirrors.aliyun.com/pypi/simple/' in helper
    assert 'https://pypi.tuna.tsinghua.edu.cn/simple' in helper
    assert 'https://pypi.org/simple' in helper
    assert 'PIP_INDEX:-' in helper


def test_precheck_warns_when_host_dns_unusable_in_containers():
    """预检含宿主 DNS 形态检查：nameserver 全为本机时提示构建容器 DNS 兜底方案。"""
    text = (BASE/'install.sh').read_text(encoding='utf-8')
    block = text[text.index('precheck_environment() {'):text.index('ensure_docker_ready()')]
    assert 'resolv.conf' in block and 'nameserver' in block
    assert '223.5.5.5' in block


def _deploy_helper_with_stub_pip(tmp_path, fail_urls):
    """复制 ensure_proxy_deps.sh 到临时目录，伪造 venv 与 pip（按 -i 源决定成败）。"""
    deploy = tmp_path/'base'; deploy.mkdir()
    shutil.copy2(BASE/'ensure_proxy_deps.sh', deploy/'ensure_proxy_deps.sh')
    (deploy/'proxy').mkdir()
    (deploy/'proxy/requirements.txt').write_text('fastapi>=0.110\n')
    venv = tmp_path/'venv'; (venv/'bin').mkdir(parents=True)
    (venv/'bin/python').write_text('#!/bin/sh\nexit 0\n'); (venv/'bin/python').chmod(0o755)
    calls = tmp_path/'calls.log'
    # case 是整串匹配：URL 前后加 * 才是"参数里含该源即失败"
    cases = ''.join(f'    *{url}*) exit 1;;\n' for url in fail_urls)
    stub = venv/'bin/pip'
    stub.write_text('#!/bin/sh\n'
                    f'printf \'%s\\n\' "$*" >> "{calls}"\n'
                    f'case "$*" in\n{cases}*) exit 0;;\nesac\n')
    stub.chmod(0o755)
    return deploy, venv, calls


def test_ensure_proxy_deps_falls_back_across_indexes(tmp_path):
    """行为验证：首选源不可达时按 自定义→阿里云→清华→官方 链回退，最终成功退出 0。"""
    deploy, venv, calls = _deploy_helper_with_stub_pip(
        tmp_path,
        ['https://pypi.invalid/simple',
         'https://mirrors.aliyun.com/pypi/simple/',
         'https://pypi.tuna.tsinghua.edu.cn/simple'])
    env = os.environ.copy()
    env.update(PIP_INDEX='https://pypi.invalid/simple', FNMUSIC_VENV_DIR=str(venv))
    result = subprocess.run(['bash', str(deploy/'ensure_proxy_deps.sh')], env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    tried = calls.read_text()
    assert 'https://pypi.invalid/simple' in tried
    assert 'https://mirrors.aliyun.com/pypi/simple/' in tried
    assert 'https://pypi.tuna.tsinghua.edu.cn/simple' in tried
    assert 'https://pypi.org/simple' in tried
    assert '切换下一候选源' in result.stdout
    assert '代理依赖安装完成' in result.stdout


def test_ensure_proxy_deps_reports_guidance_when_all_indexes_fail(tmp_path):
    """全源失败：非零退出 + 带换源/排查指引的错误信息。"""
    deploy, venv, _ = _deploy_helper_with_stub_pip(
        tmp_path,
        ['https://pypi.invalid/simple',
         'https://mirrors.aliyun.com/pypi/simple/',
         'https://pypi.tuna.tsinghua.edu.cn/simple',
         'https://pypi.org/simple'])
    env = os.environ.copy()
    env.update(PIP_INDEX='https://pypi.invalid/simple', FNMUSIC_VENV_DIR=str(venv))
    result = subprocess.run(['bash', str(deploy/'ensure_proxy_deps.sh')], env=env,
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert '排查建议' in result.stderr
    assert 'PIP_INDEX=' in result.stderr
