#!/usr/bin/env python3
"""Linux-only, fail-closed socket takeover. No business response is an identity."""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import re
import threading
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time


class Unsafe(RuntimeError):
    pass


def move_no_replace(source, destination):
    # Atomic no-clobber publication even if the official daemon recreates a path.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        raise Unsafe('renameat2 unavailable; refusing non-atomic replacement')
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise Unsafe('no-replace socket move refused (errno=' + str(error) + ')')


def inode(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(st.st_mode):
        raise Unsafe("non-socket or symlink at socket path; preserved")
    return [st.st_dev, st.st_ino]


def process(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return {"pid": pid, "start": text[text.rindex(')') + 2:].split()[19],
                "exe": os.readlink(f"/proc/{pid}/exe")}
    except (OSError, ValueError, IndexError):
        return None


def process_info(pid):
    """Kernel identity for installer-lock holders: pid/ppid/pgid + full cmdline.

    pgid lets the shell wrapper terminate a whole hung installer tree (the
    flock wrapper and every child it spawned); cmdline lets it verify the
    holder actually runs one of this project's scripts before signalling.
    Values come from /proc only, never from user input.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rindex(')') + 2:].split()
        # fields[0]=state, [1]=ppid, [2]=pgrp; starttime is field 22 overall.
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ''
        return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]),
                "start": fields[19], "cwd": cwd,
                "cmdline": cmdline.replace(b'\0', b' ').decode('utf-8', 'replace').strip()}
    except (OSError, ValueError, IndexError):
        return None


def connect(path, timeout=0.4):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        pid, _, _ = struct.unpack('3i', s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return s, process(pid)
    except BaseException:
        s.close()
        raise


def request(path, route, timeout=4):
    # Socket timeouts alone are per-read, not a true overall deadline.
    deadline = time.monotonic() + timeout
    s, peer = connect(path, timeout)
    with s:
        s.sendall(f"GET {route} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            s.settimeout(remaining)
            data = s.recv(65536)
            if not data:
                break
            chunks.extend(data)
            if len(chunks) > 262144:
                raise Unsafe("probe response too large")
    headers, body = bytes(chunks).split(b'\r\n\r\n', 1)
    if headers.split()[1] != b'200':
        raise Unsafe("probe HTTP status not 200")
    return json.loads(body), peer


def snapshot(path, timeout=0.9):
    deadline = time.monotonic() + timeout
    ino = inode(path)
    if ino is None:
        return {"kind": "absent"}
    try:
        s, peer = connect(path, min(0.4, timeout))
        s.close()
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED and inode(path) == ino:
            return {"kind": "stale", "inode": ino}
        return {"kind": "unknown", "inode": ino}
    if inode(path) != ino or not peer:
        raise Unsafe("socket changed during identity check")
    # Kernel peer executable, never INVALID TOKEN forwarded by a proxy.
    if Path(peer['exe']).name == 'trim-music':
        kind = 'official'
    else:
        kind = 'unknown'
        try:
            body, live_peer = request(path, '/_ext/livez', max(0.001, deadline-time.monotonic()))
            if (body.get('service') == 'fnmusic-ext' and
                    body.get('pid') == peer['pid'] and live_peer == peer):
                kind = 'proxy'
        except (OSError, ValueError, Unsafe):
            pass
    return {"kind": kind, "inode": ino, "process": peer}


def remember_service(target, upstream, directory, unit='fnmusic-ext.service'):
    """Legacy migration: record a live proxy that predates /_ext/livez.

    The socket's kernel peer must be the deployed unit's MainPID (or a verified
    member of its cgroup). Kernel identity decides; systemd only correlates the
    listener with this deployment. Returns the recorded snapshot.
    """
    snap = snapshot(target)
    if snap['kind'] == 'proxy':
        return snap
    if snap['kind'] != 'unknown':
        raise Unsafe('live proxy identity unverifiable')
    peer = snap.get('process')
    if not peer:
        raise Unsafe('legacy proxy has no kernel peer identity')
    try:
        main_pid = int(subprocess.run(['systemctl', 'show', unit, '-p', 'MainPID', '--value'],
                                      capture_output=True, text=True, timeout=10,
                                      check=True).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise Unsafe('legacy proxy cannot be correlated with the deployed unit')
    if peer['pid'] != main_pid:
        try:
            cgroup = Path(f'/sys/fs/cgroup/system.slice/{unit}/cgroup.procs').read_text().split()
        except OSError:
            raise Unsafe('legacy proxy is not the unit MainPID')
        if str(peer['pid']) not in cgroup:
            raise Unsafe('legacy proxy is outside the deployed unit')
    current = process(peer['pid'])
    if current is None or current['start'] != peer['start'] or current['exe'] != peer['exe']:
        raise Unsafe('legacy proxy exited during ownership verification')
    if inode(target) != snap['inode']:
        raise Unsafe('legacy proxy socket replaced during verification')
    with State(target, upstream, directory).lock():
        data = State(target, upstream, directory).load()
        recorded = data.get('proxy')
        # Compare identity (inode/process), not the recorded role label: a
        # legacy listener is probed as 'unknown' but recorded as proxy.
        if recorded and {k: v for k, v in recorded.items() if k != 'kind'} != {k: v for k, v in snap.items() if k != 'kind'}:
            raise Unsafe('recorded proxy ownership differs from legacy listener')
        # Verify the attribution once more against the current live peer before
        # writing the proxy role; the record must satisfy restore's checks.
        verified = snapshot(target)
        if verified['kind'] != 'unknown' or verified != snap:
            raise Unsafe('legacy proxy identity changed during recording')
        attributed = dict(verified, kind='proxy')
        data['proxy'] = attributed
        upstream_snap = snapshot(upstream)
        if upstream_snap['kind'] == 'official':
            prior = data.get('official')
            if prior and prior != upstream_snap:
                raise Unsafe('official upstream ownership changed')
            data['official'] = upstream_snap
        State(target, upstream, directory).save(data)
    return attributed


def remember_official(path):
    """Record a positively identified official listener, else None."""
    snap = snapshot(path)
    return snap if snap['kind'] == 'official' else None


class State:
    def __init__(self, target, upstream, directory):
        self.target, self.upstream = Path(target), Path(upstream)
        self.directory = Path(directory)
        self.file = self.directory / 'ownership.json'

    @contextlib.contextmanager
    def lock(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            raise Unsafe('unsafe state directory')
        fd = os.open(self.directory / 'socket.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            # Never hold this lock across systemctl or service lifetime.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self):
        try:
            fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd) as file:
            st = os.fstat(file.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise Unsafe('unsafe ownership record')
            data = json.load(file)
        if data.get('target') != str(self.target) or data.get('upstream') != str(self.upstream):
            raise Unsafe('ownership record paths differ')
        return data

    def save(self, data):
        data.update(target=str(self.target), upstream=str(self.upstream))
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(data, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def remember(self):
        with self.lock():
            data = self.load()
            for key, path in [('official', self.upstream), ('proxy', self.target)]:
                snap = snapshot(path)
                if snap['kind'] == key:
                    if data.get(key) and data[key] != snap:
                        # Both roles are kernel-verified live listeners, so the
                        # record is a cache: adopt the new identity (an official
                        # restart must never deadlock install or restore).
                        print('[takeover] note: ' + key + ' identity changed; adopting verified listener',
                              file=sys.stderr, flush=True)
                    data[key] = snap
            self.save(data)

    def remove_vacant(self, path, record=None):
        """Reclaim a socket file that is provably unreachable.

        A stream connect() that returns ECONNREFUSED proves no listener accepts
        on that path, so the file is a leftover artifact and unlinking it cannot
        disconnect any client. Requiring a matching ownership record here turned
        every aborted takeover into a permanent deadlock (a stale file blocked
        both re-installation and restoration), so a *verified vacant* socket is
        reclaimed even when unrecorded. A live or indeterminate socket, an
        unverifiable file type, and any inode change during the check are still
        preserved untouched.
        """
        current = snapshot(path)
        if current['kind'] == 'absent':
            return
        if current['kind'] != 'stale':
            # Live sockets, including a known proxy, stay owned by their process.
            raise Unsafe('socket still live or indeterminate; preserved')
        if record and record.get('inode') != current.get('inode'):
            # The recorded owner is elsewhere (crashed, killed or replaced); the
            # vacant path is unrelated garbage, still provably unreachable.
            print('[takeover] note: recorded owner inode differs; reclaiming vacant socket',
                  file=sys.stderr, flush=True)
        # Vacancy must be observed twice, on the same inode, immediately before unlink.
        if inode(path) != current['inode'] or snapshot(path)['kind'] != 'stale':
            raise Unsafe('socket changed before unlink')
        os.unlink(path)
        print('[takeover] reclaimed vacant socket file: ' + str(path), file=sys.stderr, flush=True)

    def remove_vacant_any(self, data):
        """Reclaim vacant files on both known paths; live sockets are never touched."""
        for path, role in ((self.upstream, 'official'), (self.target, 'proxy')):
            if snapshot(path)['kind'] == 'stale':
                self.remove_vacant(path, data.get(role))

    def remove_live_upstream(self, target_snapshot, data):
        """Reclaim a live upstream listener while the official app owns target.

        The upstream path exists only because a takeover renamed the official
        socket onto it; no client ever connects there. With target
        kernel-verified as the official listener, a live upstream occupant is
        an orphan from an aborted cycle (typically an official restart that
        left the pre-restart process bound there); restarting the official app
        never clears it, and preserving it deadlocks restore and install
        forever. Unlinking is a deliberate exception to fail-closed
        preservation: it drops only the pathname (the orphan keeps its inode,
        and the printed pid lets an administrator retire the process).
        """
        current = snapshot(self.upstream)
        if current['kind'] in ('absent', 'stale'):
            return current
        # The kernel-verified official listener must still be exactly the one
        # that justified the reclaim at the moment of unlink.
        if snapshot(self.target) != target_snapshot or inode(self.target) != target_snapshot['inode']:
            raise Unsafe('official target changed before upstream reclaim')
        if inode(self.upstream) != current['inode']:
            raise Unsafe('upstream changed before reclaim')
        peer = current.get('process')
        pid = peer['pid'] if peer else '?'
        os.unlink(self.upstream)
        print('[takeover] note: reclaimed live upstream orphan (pid ' + str(pid) +
              '); target is verified official', file=sys.stderr, flush=True)
        return snapshot(self.upstream)

    def restore_plan(self, unit='fnmusic-ext.service'):
        """Pre-flight: may this stop be followed by verified recovery?

        Called by restore.sh BEFORE disabling the service. Returns the positive
        future role of the target; raises Unsafe while recovery is unsupported
        or ambiguous, so the stop never produces an unrecoverable layout. A
        vacant (unreachable) target or upstream file is never a blocker: it is
        reclaimable, so an aborted takeover stays restorable.
        """
        t, u = snapshot(self.target), snapshot(self.upstream)
        if t['kind'] == 'official':
            if u['kind'] in ('absent', 'stale'):
                return 'official-direct'
            # The official app already serves clients on target; any live
            # upstream listener is an unreachable orphan and gets reclaimed
            # during restore instead of blocking it forever.
            return 'official-reclaim'
        if u['kind'] == 'absent':
            raise Unsafe('no positively identified official upstream; refuse stop')
        if u['kind'] not in ('official', 'unknown'):
            raise Unsafe('upstream is not official; refuse stop')
        if u['kind'] == 'unknown':
            if remember_official(self.upstream) is None:
                raise Unsafe('upstream identity unverifiable; refuse stop')
        if t['kind'] == 'stale':
            return 'vacant-target-repair'
        if t['kind'] == 'proxy':
            return 'proxy-recovery'
        current = t
        recorded = remember_service(self.target, self.upstream, self.directory, unit=unit)
        live = snapshot(self.target)
        # The legacy peer stays 'unknown' to live probes; the verified
        # record grants it the proxy role, matching the exact live identity.
        if recorded['kind'] != 'proxy' or live not in (recorded, current):
            raise Unsafe('legacy proxy identity unverifiable; refuse stop')
        return 'proxy-recovery'

    def restore(self):
        with self.lock():
            data = self.load()
            t, u = snapshot(self.target), snapshot(self.upstream)
            if t['kind'] == 'official':
                if u['kind'] == 'stale':
                    self.remove_vacant(self.upstream, data.get('official'))
                elif u['kind'] != 'absent':
                    self.remove_live_upstream(t, data)
                self.save({})
                print('[takeover] official socket already in place', flush=True)
                return
            if u['kind'] == 'stale':
                # Vacant upstream is unreachable garbage: clear it, then report
                # honestly that nothing is listening for clients.
                self.remove_vacant(self.upstream, data.get('official'))
                u = snapshot(self.upstream)
                if u['kind'] == 'absent':
                    # Leave both canonical paths clean: a restarting official app
                    # must be able to bind them again without stale leftovers.
                    if snapshot(self.target)['kind'] == 'stale':
                        self.remove_vacant(self.target, data.get('proxy'))
                    self.save({})
                    raise Unsafe('no official listener present; restart the official music app')
            if u['kind'] != 'official':
                raise Unsafe('no positively identified official upstream; preserved')
            recorded = data.get('official')
            if recorded and (recorded.get('inode') != u['inode'] or recorded.get('process') != u['process']):
                # The official app restarted while taken over: the live kernel
                # identity wins, and the move below re-verifies the end state.
                print('[takeover] note: official identity changed; adopting verified listener',
                      file=sys.stderr, flush=True)
            self.remove_vacant(self.target, data.get('proxy'))
            if inode(self.upstream) != u['inode'] or inode(self.target) is not None:
                raise Unsafe('socket changed before restore')
            move_no_replace(self.upstream, self.target)
            if snapshot(self.target) != u:
                raise Unsafe('restoration verification failed')
            self.save({})
            print('[takeover] official socket restoration verified', flush=True)

    def publish(self, staged, proxy):
        with self.lock():
            data = self.load()
            # Vacant leftovers never block publication, whichever path holds them.
            self.remove_vacant_any(data)
            t, u = snapshot(self.target), snapshot(self.upstream)
            if t['kind'] == 'official' and u['kind'] not in ('absent', 'stale'):
                # Orphaned pre-restart official listener: without this reclaim
                # a live upstream file blocks re-installation forever.
                u = self.remove_live_upstream(t, data)
            if t['kind'] == 'official' and u['kind'] == 'absent':
                data['official'] = t
                data['proxy'] = proxy
                self.save(data)  # journal BEFORE moving official
                if inode(self.target) != t['inode']:
                    raise Unsafe('target changed before takeover')
                move_no_replace(self.target, self.upstream)
            elif u['kind'] == 'official' and t['kind'] in ('absent', 'stale'):
                prior = data.get('official')
                if prior and prior != u:
                    # The official app restarted between installs; the live
                    # kernel identity is authoritative, not the cached record.
                    print('[takeover] note: upstream identity changed; adopting verified listener',
                          file=sys.stderr, flush=True)
                data.update(official=u, proxy=proxy)
                self.save(data)
            else:
                raise Unsafe('ambiguous/live socket topology; nothing removed')
            if inode(self.target) is not None or snapshot(staged) != proxy:
                raise Unsafe('socket changed before proxy publication')
            os.chmod(staged, 0o666)
            move_no_replace(staged, self.target)


def wait_ready(state, seconds):
    deadline = time.monotonic() + seconds
    reason = 'not probed'
    while time.monotonic() < deadline:
        try:
            data = state.load()
            current = snapshot(state.target, min(0.9, max(0.001, deadline-time.monotonic())))
            if current['kind'] != 'proxy' or current != data.get('proxy'):
                raise Unsafe('target is not the recorded live proxy')
            body, peer = request(state.target, '/_ext/healthz', min(4, max(0.01, deadline-time.monotonic())))
            if body.get('ok') is True and body.get('upstream') == 'ok' and peer == current['process']:
                return
            keys = ('upstream', 'musicdl', 'musicbox', 'lxmusic')
            allowed = ('ok', 'error', 'disabled', 'timeout', 'unavailable')
            reason = 'dependencies: ' + ','.join(k+'='+ (str(body.get(k)) if body.get(k) in allowed else 'not-ready') for k in keys)
        except (OSError, ValueError, Unsafe) as exc:
            reason = str(exc) if isinstance(exc, Unsafe) else type(exc).__name__
            # Unsafe messages here are fixed local strings, never response/env data.
        time.sleep(min(0.2, max(0, deadline-time.monotonic())))
    raise Unsafe('readiness deadline exceeded: ' + reason)


def environment(base):
    # Parse the installer's shell-quoted dotenv as data; do not execute arbitrary shell.
    import shlex
    env = os.environ.copy()
    file = base / '.env'
    if file.exists():
        for line in file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            key, sep, value = line.partition('=')
            if not sep or not key.replace('_', 'a').isalnum():
                raise Unsafe('invalid dotenv assignment')
            words = shlex.split(value, comments=True)
            if len(words) > 1:
                raise Unsafe('dotenv value must be quoted')
            env[key] = words[0] if words else ''
    env.update(FNMUSIC_HOME=str(base), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1',
               FNMUSIC_BACKGROUND_JOBS='1')
    return env


def diagnostic(text):
    # Exception values stay private; missing dependency names are actionable.
    text = text[-16384:]
    classes = re.findall(r'^([A-Za-z][A-Za-z0-9_]*(?:Error|Exception)):', text, re.M)
    missing = re.findall(r"No module named ['\"]([A-Za-z_][A-Za-z0-9_.]{0,100})['\"]", text)
    if missing:
        return 'ModuleNotFoundError: missing module ' + missing[-1]
    return classes[-1] if classes else 'no-safe-exception-class'


# Retain fixed operational prefixes, not arbitrary exception text, response
# dictionaries, config values or URLs appended to them. This deliberately small
# allowlist covers source/search/stream/auth/recommend failures in the current app.
_SAFE_OUTCOME = re.compile(
    r'(?:\[dl-capture\] [ -~]{0,600}|'
    r'audio decode check failed for [!-~]{1,120} \(ext=[a-z0-9]{1,8} bytes=[0-9]{1,12}\)|'
    r'retrying [!-~]{1,120} with mp3 tier after corrupt lossless stream|'
    r'tee finalize rejected corrupt lossless for [!-~]{1,120}|'
    r'Background full fetch failed for [!-~]{1,120}: [A-Za-z_]{1,40}|'
    r'Failed to fetch online search from (?:musicdl|lxmusic)|'
    r'Failed to fetch musicbox search|musicdl search partial errors|'
    r'Suggest musicdl error|Stream startup failed|'
    r'Stream aborted mid-way for [!-~]{1,140}: [A-Za-z_][A-Za-z0-9_.]{0,60}|'
    r'playback failed, marked unplayable for [!-~]{1,140}|'
    r'prefetch marked unplayable: [ -~]{1,200}|'
    r'stream probe: (?:GET|HEAD) [!-~]{1,140} range=[!-~]{0,80} '
    r'cached=(?:True|False) tee_eligible=(?:True|False)|'
    r'tee (?:diag|finalize|metadata) [ -~]{0,400}|'
    r'album detail not synthesizable: source=[!-~]{1,40}|'
    r'forward-unhandled (?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) /[!-~]{1,140}(?: \(\#[0-9]+\))?|'
    r'Upstream auth probe failed|'
    r'(?:musicbox|lxmusic|musicdl)(?: /info| lyric fetch(?: in _online_info)?)? failed|'
    r'lyric sidecar fetch failed|resolve_(?:lx|netease)_url error|'
    r'llm call failed|daily recommend (?:peek|list inject|llm branch|fallback branch) failed|'
    r'Failed to (?:read|write|load|save|parse|remember)|failed to (?:read|write|load|map|purge)|'
    r'(?:favorite|play history) list degraded to official-only|'
    r'client request (?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) /[!-~]{1,180} status=[1-5][0-9]{2} ua=[ -~]{1,100})',
    re.I,
)


def safe_child_log(line, secrets=()):
    # Known configured secrets are removed before considering even safe fields.
    for value in secrets:
        if value:
            line = line.replace(value, '[redacted]')
    line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
    # Preserve traceback module/function/line, never source code or full paths.
    frame = re.fullmatch(r'File "[^"\n]*[/\\]([A-Za-z_][A-Za-z0-9_]*\.py)", line ([0-9]+), in ([A-Za-z_][A-Za-z0-9_]*|<module>)', line)
    if frame:
        return 'traceback: ' + frame[1] + ':' + frame[2] + ' in ' + frame[3]
    kind = diagnostic(line)
    if kind != 'no-safe-exception-class':
        return kind
    record = re.match(r'(?:(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,\.]\d+) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] [A-Za-z0-9_.]+: |(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+)(.*)', line)
    if not record:
        return None
    prefix = ((record[1] + ' ') if record[1] else '') + (record[2] or record[3]) + ' '
    message = record[4]
    # Uvicorn lifecycle events contain no request/config values.
    lifecycle = re.fullmatch(r'(?:Started server process \[[0-9]+\]|Finished server process \[[0-9]+\]|Waiting for application (?:startup|shutdown)\.|Application (?:startup|shutdown) complete\.|Application startup failed\. Exiting\.|Shutting down)', message)
    if lifecycle:
        return prefix + lifecycle[0]
    http = re.fullmatch(r'llm http ([1-5][0-9]{2})', message)
    if http:
        return prefix + http[0]
    outcome = _SAFE_OUTCOME.match(message)
    if outcome:
        # Retain known error categories only; never arbitrary exception values.
        categories = re.findall(r'\b(?:ReadTimeout|ConnectTimeout|ConnectError|ConnectionRefusedError|TimeoutError|HTTPStatusError|ValueError|OSError|timeout|timed out)\b', message)
        return prefix + outcome[0] + (': ' + ','.join(dict.fromkeys(categories)) if categories else '')
    return None


def drain_diagnostics(pipe, secrets=()):
    # Each physical line is bounded; discard overlong lines INCLUDING their tail
    # so a credential split over reads cannot be reinterpreted as a fresh log.
    while True:
        line = pipe.readline(16384)
        if not line:
            break
        if len(line) == 16384 and not line.endswith('\n'):
            while line and not line.endswith('\n'):
                line = pipe.readline(16384)
            print('[takeover] child diagnostic: oversized log line omitted', file=sys.stderr, flush=True)
            continue
        safe = safe_child_log(line, secrets)
        if safe:
            print('[takeover] child: ' + safe, file=sys.stderr, flush=True)
    pipe.close()


def preflight(base, env):
    python = base / '.venv-proxy/bin/python'
    # Imports may create storage directories; all known storage locations go to temp.
    with tempfile.TemporaryDirectory(prefix='fnmusic-preflight-') as temp:
        isolated = env.copy()
        for key in ('HOME', 'FNMUSIC_HOME', 'FNMUSIC_CACHE_DIR', 'FNMUSIC_FAV_DIR',
                    'FNMUSIC_PLAY_HISTORY_DIR', 'FNMUSIC_RECOMMEND_DIR', 'FNMUSIC_LIBRARY_DIR',
                    'FNMUSIC_TEE_SAVE_DIR',
                    'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
            isolated[key] = temp
        isolated['FNMUSIC_MUSIC_DB'] = temp + '/nonexistent.db'
        isolated['FNMUSIC_UPSTREAM_SOCK'] = temp + '/nonexistent.socket'
        for command in ([str(python), '-m', 'uvicorn', '--version'],
                        [str(python), '-B', '-c', 'import app; import recommend; assert callable(app.app)']):
            with tempfile.TemporaryFile() as errors:
                result = subprocess.run(command, cwd=base / 'proxy', env=isolated,
                                        stdout=subprocess.DEVNULL, stderr=errors, timeout=20)
                if result.returncode:
                    size = errors.tell()
                    errors.seek(max(0, size-16384))
                    kind = diagnostic(errors.read().decode('utf-8', 'replace'))
                    stage = 'uvicorn module' if '-m' in command else 'app/recommend imports'
                    raise Unsafe(f'preflight {stage}: exit={result.returncode}, exception={kind} (values suppressed)')


def wait_official_target(state, budget):
    """Boot race: the official app daemon may bind the target socket after us.

    Neither path holding a live listener means 'official app not started yet'
    (e.g. our unit raced ahead during system boot), not ambiguity: wait a
    bounded time for the official daemon to appear before publishing, so the
    takeover survives reboot without manual restart.
    """
    deadline = time.monotonic() + budget
    while True:
        t, u = snapshot(state.target), snapshot(state.upstream)
        if not (t['kind'] in ('absent', 'stale') and u['kind'] in ('absent', 'stale')):
            return
        if time.monotonic() >= deadline:
            raise Unsafe('official app socket did not appear within wait budget')
        time.sleep(0.5)


def supervise(state, base):
    env = environment(base)
    env['FNMUSIC_UPSTREAM_SOCK'] = str(state.upstream)
    preflight(base, env)
    child = None
    log_thread = None
    changed = False
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        raise InterruptedError('service stopping')

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Private staging prevents uvicorn from unlinking an existing target on startup.
    with tempfile.TemporaryDirectory(prefix='proxy-', dir=state.directory) as temp:
        staged = Path(temp) / 'listen.sock'
        failure = None
        try:
            child = subprocess.Popen([str(base / '.venv-proxy/bin/python'), '-m', 'uvicorn',
                                      'app:app', '--app-dir', str(base / 'proxy'), '--uds', str(staged),
                                      '--no-access-log'], cwd=base / 'proxy', env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            secrets = tuple(v for k, v in env.items() if v and re.search(r'key|token|secret|password|cookie', k, re.I))
            log_thread = threading.Thread(target=drain_diagnostics, args=(child.stdout, secrets), daemon=True)
            log_thread.start()
            deadline = time.monotonic() + 25
            proxy = None
            while time.monotonic() < deadline and child.poll() is None:
                snap = snapshot(staged)
                if snap['kind'] == 'proxy' and snap['process']['pid'] == child.pid:
                    proxy = snap
                    break
                time.sleep(0.1)
            if not proxy:
                raise Unsafe('child failed liveness before takeover')
            changed = True  # publish may fail after moving official
            wait_official_target(state, 40.0)
            state.publish(staged, proxy)
            # Readiness is the acceptance gate; keep this window below the unit's
            # 65s ExecStartPost deadline so a failure reports the real dependency
            # state instead of racing systemd's own timeout.
            wait_ready(state, 55)
            print('[takeover] proxy identity and readiness verified', flush=True)
            code = child.wait()
            failure = Unsafe(f'proxy exited (status {code})')
        except InterruptedError as exc:
            if not stopping:
                failure = exc
        except BaseException as exc:
            failure = exc
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if log_thread:
                log_thread.join(timeout=1)
        if changed:
            # Rollback failure must never hide why the service failed: report both.
            try:
                state.restore()
            except BaseException as rollback:
                if failure is not None:
                    raise Unsafe(str(failure) + '; rollback failed: ' + str(rollback)) from rollback
                raise
            print('[takeover] rolled back to the official socket', flush=True)
        if failure is not None:
            raise failure


def prepare_install_lock():
    directory = Path('/run/fnmusic-ext-install')
    directory.mkdir(mode=0o755, exist_ok=True)
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        raise Unsafe('unsafe installer lock directory')
    os.chmod(directory, 0o755)
    fd = os.open(directory / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o666)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1:
            raise Unsafe('unsafe installer lock file')
        os.fchmod(fd, 0o666)  # stable root-owned inode, no data; unprivileged flock
    finally:
        os.close(fd)


def lock_holders(path='/run/fnmusic-ext-install/operation.lock'):
    """Print which processes hold the installer operation lock, as JSON.

    Read-only /proc scan (open fds pointing at the lock inode) so a freshly
    started install/restore can NAME a hung holder instead of failing
    silently. Nothing is signalled here; the shell wrapper decides. Without
    root, other users' processes are simply invisible and go unreported.
    """
    lock = Path(path)
    holders = []
    try:
        wanted = (os.stat(lock).st_dev, os.stat(lock).st_ino)
    except OSError:
        print(json.dumps({'holders': holders}))
        return
    if not Path('/proc').is_dir():
        # Non-Linux platforms have no /proc; report no holders instead of
        # failing (the shell wrapper then reports it cannot identify any).
        print(json.dumps({'holders': holders}))
        return
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd_link in (entry / 'fd').iterdir():
                try:
                    if os.readlink(fd_link) != str(lock):
                        continue
                    # Match the inode too: a recreated lock file must not
                    # misreport holders of the previous generation.
                    st = os.stat(fd_link)
                    if (st.st_dev, st.st_ino) != wanted:
                        continue
                except OSError:
                    continue
                info = process_info(int(entry.name))
                if info:
                    holders.append(info)
                break
        except OSError:
            continue
    print(json.dumps({'holders': holders}))


# Which checkout owns the machine-wide deployment. The unit name, container
# names and installer lock are all global; ownership checks on those resources
# only work while the resource exists. This record survives their absence so a
# second checkout cannot silently become the deployment without notice.
DEPLOYMENT_FILE = Path('/var/lib/fnmusic-ext/deployment')


def deployment_remember(base):
    """Record this checkout as the active deployment (requires root)."""
    record = {'base': str(Path(base).resolve()),
              'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    DEPLOYMENT_FILE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Explicit chmod: the install chain may run under `umask 077` (install.sh
    # protects .env writes), which would silently tighten a fresh mkdir to
    # 0700 and keep unprivileged deployment-check out of the directory.
    # Same hardening prepare_install_lock() applies to /run/fnmusic-ext-install.
    os.chmod(DEPLOYMENT_FILE.parent, 0o755)
    fd, name = tempfile.mkstemp(dir=str(DEPLOYMENT_FILE.parent))
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(record, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o644)  # world-readable: unprivileged checks must work
        os.replace(name, DEPLOYMENT_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def deployment_conflict(base, file=None):
    """Return a conflict dict when another live checkout owns the deployment.

    None means this checkout may proceed: no record, the record names this
    very directory (compared by realpath, so /home/x and /vol/home/x spellings
    of one checkout agree), or the recorded directory was deleted (a moved or
    removed checkout cannot be protected any more).
    """
    path = Path(file) if file else DEPLOYMENT_FILE
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    recorded = str(record.get('base') or '')
    here = str(Path(base).resolve())
    if not recorded or os.path.realpath(recorded) == os.path.realpath(here):
        return None
    if not os.path.isdir(recorded):
        # The recorded deployment directory is gone; nothing left to protect.
        print('[takeover] note: recorded deployment directory no longer exists; '
              'this checkout may adopt the deployment', file=sys.stderr, flush=True)
        return None
    return {'conflict': recorded, 'here': here}


def deployment_clear():
    """Forget the deployment record (restore returns the machine to stock)."""
    try:
        os.unlink(DEPLOYMENT_FILE)
    except FileNotFoundError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'preflight', 'remember', 'restore-plan', 'restore', 'ready', 'status', 'render-unit', 'prepare-install-lock', 'lock-holders', 'deployment-remember', 'deployment-check', 'deployment-clear'])
    parser.add_argument('--base', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--target', default='/var/run/trim_music.socket')
    parser.add_argument('--upstream', default='/var/run/trim_music_upstream.socket')
    parser.add_argument('--state-dir', default='/run/fnmusic-ext')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--lock-file', default='/run/fnmusic-ext-install/operation.lock',
                        help='lock path for lock-holders (testing)')
    args = parser.parse_args()
    state = State(args.target, args.upstream, args.state_dir)
    try:
        if args.command == 'prepare-install-lock':
            prepare_install_lock()
        elif args.command == 'lock-holders':
            lock_holders(args.lock_file)
        elif args.command == 'deployment-remember':
            deployment_remember(args.base)
        elif args.command == 'deployment-check':
            conflict = deployment_conflict(args.base)
            if conflict:
                print(json.dumps(conflict))
                return 2
        elif args.command == 'deployment-clear':
            deployment_clear()
        elif args.command == 'preflight':
            preflight(args.base, environment(args.base))
        elif args.command == 'render-unit':
            base = str(args.base.resolve())
            if any(c in base for c in '\n\r\"\\%$'):
                raise Unsafe('unsupported unit path characters')
            print((args.base / 'fnmusic-ext.service').read_text().replace('@BASE_DIR@', base), end='')
        elif args.command == 'ready':
            wait_ready(state, args.timeout)
        elif args.command == 'status':
            print(json.dumps({'target': snapshot(state.target), 'upstream': snapshot(state.upstream)}))
        elif args.command == 'restore-plan':
            plan = state.restore_plan()
            print(json.dumps({'plan': plan}))
        elif args.command == 'run':
            with state.lock():
                pass
            # Lifetime supervisor lock is separate from mutation and installer locks.
            with open(state.directory / 'supervisor.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                supervise(state, args.base)
        else:
            getattr(state, args.command)()
    except Exception as exc:
        message = str(exc) if isinstance(exc, Unsafe) else type(exc).__name__
        print('[takeover] ERROR: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
