#!/usr/bin/env python3
"""GPT egress check + independent read-only standby scout. Standard library only."""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

GROUP = 'AI-AUTO'
URL = 'https://chatgpt.com/backend-api/me'
PROXY = 'http://127.0.0.1:7897'
TTL = 120
REFRESH = 60
COOLDOWN = 120
SCOUT_LABEL = 'com.b612.ai-egress-scout'
CACHE_VERSION = 2
BACKGROUND_CONCURRENCY = 2
RECOVERY_CONCURRENCY = 6
STANDBY_RETRY_DELAY = 2


def subscriptions(path):
    """Explicit exact node-to-subscription mapping; never infer from names."""
    value = load(path)
    return {node: source for node, source in value.items()
            if isinstance(node, str) and node and isinstance(source, str) and source.strip()}


def numeric(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def valid_delay(value):
    return numeric(value) and value >= 0


class ControlError(Exception):
    pass


class SelectionChanged(Exception):
    pass


class ContextChanged(Exception):
    pass


class Interrupted(Exception):
    pass


def load(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name+'.', delete=False) as f:
            name = f.name
            json.dump(value, f, ensure_ascii=False)
            f.write('\n')
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class Guard:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.root = self.home / '.dsh/state'
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.root / 'ai-egress-standbys.json'
        self.subscriptions_path = self.root / 'ai-egress-subscriptions.json'
        self.state_path = self.root / 'ai-egress.json'
        self.log_path = self.home / 'Library/Logs/ai-egress-guard.log'
        sockets = glob.glob('/var/folders/*/*/T/verge-mihomo.sock')
        self.sock = sockets[0] if sockets else ''
        self.origin = self.last = ''
        self.inflight = ''
        self.restore_needed = False
        self.epoch = ''
        self.members = []
        self.recovery_scan_stop = None

    @contextmanager
    def lock(self, name, blocking=False, shared=False):
        with (self.root / name).open('a') as f:
            try:
                fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def run(self, args, timeout=10):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(args, 1, '', '')

    def api(self, path, body=None, timeout=3):
        if not self.sock:
            raise ControlError('Clash socket missing')
        args = ['curl', '-q', '-fsS', '--connect-timeout', '1', '-m', str(timeout),
                '--noproxy', '*', '--unix-socket', self.sock,
                '-H', 'Authorization: Bearer ' + os.environ.get('CLASH_API_SECRET', 'set-your-secret')]
        if body is not None:
            args += ['-X', 'PUT', '-H', 'Content-Type: application/json', '--data-binary', json.dumps(body)]
        result = self.run(args + ['http://localhost/' + path], timeout+1)
        if result.returncode:
            raise ControlError('Clash request failed')
        if body is not None:
            return {}
        try:
            return json.loads(result.stdout)
        except ValueError as e:
            raise ControlError('Invalid Clash response') from e

    def group(self):
        data = self.api('proxies/' + quote(GROUP, safe=''))
        if not isinstance(data, dict) or not data.get('now') or not isinstance(data.get('all'), list):
            raise ControlError('AI-AUTO missing selection/members')
        return data

    def current(self):
        return self.group()['now']

    def network_identity(self):
        # Includes active IP/interface topology and default gateway, but no probe history.
        nwi = self.run(['/usr/sbin/scutil', '--nwi'], 2)
        route = self.run(['/sbin/route', '-n', 'get', 'default'], 2)
        if nwi.returncode or route.returncode:
            raise ContextChanged('Cannot determine network identity')
        stable_route = '\n'.join(x.strip() for x in route.stdout.splitlines()
                                 if x.strip().startswith(('gateway:', 'interface:')))
        # Wake timestamp invalidates even a short sleep on an unchanged network.
        wake = self.run(['/usr/sbin/sysctl', '-n', 'kern.waketime'], 2)
        return [nwi.stdout.strip(), stable_route, wake.stdout.strip() if not wake.returncode else '']

    def context(self):
        group = self.group()
        cfg = self.home / 'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
        try:
            text = (cfg/'profiles.yaml').read_text()
            match = re.search(r'^current:\s*[\"\']?([^\s\"\']+)', text, re.M)
            profile = match.group(1) if match else 'unknown'
            p = cfg/'profiles'/(profile + '.yaml')
            stamp = [p.stat().st_mtime_ns, p.stat().st_size] if p.exists() else []
        except OSError:
            profile, stamp = 'unknown', []
        members = list(dict.fromkeys(n for n in group['all'] if isinstance(n, str)
                                     and n not in ('DIRECT', 'REJECT', 'GLOBAL', GROUP)))
        value = [self.network_identity(), profile, stamp, members, self.sock,
                 subscriptions(self.subscriptions_path)]
        epoch = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        latest = self.group()
        if latest['all'] != group['all']:
            raise ContextChanged('Members changed while reading context')
        return epoch, members, latest['now']

    def probe(self):
        r = self.run(['curl', '-q', '-sS', '--connect-timeout', '3', '-m', '6',
                      '-x', PROXY, '--noproxy', '', '-o', '/dev/null', '-w', '%{http_code}', URL], 7)
        return r.returncode == 0 and r.stdout in ('200', '401')

    def base_network_ok(self):
        tests = [('https://captive.apple.com/hotspot-detect.html',
                  '<html><head><title>success</title></head><body>success</body></html>'),
                 ('https://www.msftconnecttest.com/connecttest.txt', 'Microsoft Connect Test')]
        for url, expected in tests:
            r = self.run(['curl', '-q', '-fsS', '--connect-timeout', '2', '-m', '4',
                          '--proxy', '', '--noproxy', '*', '-w', '\n%{http_code}', url], 5)
            body, _, code = r.stdout.rpartition('\n')
            normalize = lambda s: ''.join(s.lower().split())
            if not r.returncode and code == '200' and normalize(body) == normalize(expected):
                return True
        return False

    def delay(self, node):
        try:
            d = self.api('proxies/' + quote(node, safe='') + '/delay?timeout=5000&url=' + quote(URL, safe=''), timeout=7)
            value = d.get('delay') if isinstance(d, dict) else None
            return value if valid_delay(value) else None
        except ControlError:
            return None

    def log(self, event, **fields):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open('a') as f:
            f.write(time.strftime('%F %T ') + event + ' ｜ ' + json.dumps(fields, ensure_ascii=False) + '\n')

    def record(self, status, node):
        old = load(self.state_path)
        now = int(time.time())
        atomic(self.state_path, dict(version=1, status=status, node=node, checked_at=now,
                                    success_at=now if status == 'healthy' else old.get('success_at', 0)))
        if old.get('status') != status or old.get('node') != node:
            self.log(status, node=node)
        if status == 'healthy':
            for name in ('ai-egress.idle', 'ai-egress.slow'):
                (self.root/name).write_text('0\n')

    def ordered(self, nodes, members, current):
        mapping = subscriptions(self.subscriptions_path)
        source = mapping.get(current)
        # Only explicitly known different subscriptions receive priority.
        return sorted(nodes, key=lambda n: (0 if source and mapping.get(n)
                                            and mapping[n] != source else 1,
                                            members.index(n)))

    def ranked(self, records, members, current, failed=None):
        now = time.time()
        failed = failed if isinstance(failed, dict) else {}
        if not isinstance(records, list):
            return []
        valid = []
        for d in records:
            if not isinstance(d, dict):
                continue
            n, at = d.get('node'), d.get('success_at')
            start = d.get('started_at', at)
            if (n not in members or n == current or not numeric(at) or not numeric(start)
                    or not 0 <= now-at < TTL or not valid_delay(d.get('delay_ms'))):
                continue
            failure = failed.get(n, 0)
            if numeric(failure) and failure >= start:
                continue
            valid.append(d)
        mapping = subscriptions(self.subscriptions_path)
        source = mapping.get(current)
        valid.sort(key=lambda d: (0 if source and mapping.get(d['node'])
                                  and mapping[d['node']] != source else 1,
                                  d['delay_ms'], -d['success_at'], members.index(d['node'])))
        unique = []
        for d in valid:
            if d['node'] not in [v['node'] for v in unique]:
                unique.append(d)
        return unique[:2]

    def valid(self, cache, epoch, members, current):
        if cache.get('version') != CACHE_VERSION or cache.get('epoch') != epoch:
            return []
        return self.ranked(cache.get('candidates', []), members, current, cache.get('failed_at'))

    def cache_update(self, epoch, update, background=False):
        # Slow local control/network sampling must NEVER hold the shared write lock.
        # Observation order prevents a late old-epoch sample replacing a new epoch.
        observed_ns = time.time_ns()
        live_epoch, live_members, live_current = self.context()
        if live_epoch != epoch:
            raise ContextChanged('Stale cache writer')
        with self.lock('ai-egress-cache.lock', blocking=True), ExitStack() as transaction:
            if background and not transaction.enter_context(self.lock('ai-egress-recovery.lock', shared=True)):
                raise ContextChanged('Recovery preempted scout commit')
            cache = load(self.cache_path)
            last_observed = cache.get('context_observed_ns', 0)
            if not numeric(last_observed) or last_observed > time.time_ns():
                # Durable wall-clock ordering survives reboot; discard a future
                # marker after a clock correction rather than wedging all writers.
                last_observed = 0
            if cache.get('epoch') != epoch and last_observed > observed_ns:
                raise ContextChanged('Newer cache generation already committed')
            if cache.get('epoch') != epoch or cache.get('version') != CACHE_VERSION:
                # Old format has no latency; never manufacture a zero-ms score.
                cache = dict(version=CACHE_VERSION, epoch=epoch, candidates=[], failed_at={}, next_refresh=0)
            if not isinstance(cache.get('candidates'), list):
                cache['candidates'] = []
            failed = cache.get('failed_at', {})
            cache['failed_at'] = {n: at for n, at in failed.items() if numeric(at)} if isinstance(failed, dict) else {}
            update(cache)
            cache['context_observed_ns'] = max(last_observed, observed_ns)
            cache['candidates'] = self.ranked(cache['candidates'], live_members, live_current, cache['failed_at'])
            if cache.get('status') in ('ready', 'insufficient'):
                cache['status'] = 'ready' if len(cache['candidates']) == 2 else 'insufficient'
            atomic(self.cache_path, cache)

    def invalidate(self, node):
        def change(c):
            records = c.get('candidates', [])
            c['candidates'] = [x for x in records if isinstance(x, dict) and x.get('node') != node] if isinstance(records, list) else []
            if not isinstance(c.get('failed_at'), dict):
                c['failed_at'] = {}
            c['failed_at'][node] = time.time()
            c['next_refresh'] = 0
        self.cache_update(self.epoch, change)

    def kick_scout(self):
        # launchd owns the worker; no untracked long-running subprocesses.
        self.run(['/bin/launchctl', 'kickstart', 'gui/' + str(os.getuid()) + '/' + SCOUT_LABEL], 2)

    def check_selection(self):
        epoch, _, current = self.context()
        if current != self.last:
            raise SelectionChanged(current)
        if epoch != self.epoch:
            raise ContextChanged('Network/subscription changed')

    def switch(self, node):
        # Recheck immediately before PUT (Clash has no atomic compare-and-set).
        self.check_selection()
        self.restore_needed = True
        self.inflight = node  # Keep last confirmed selection until PUT is acknowledged.
        self.api('proxies/' + quote(GROUP, safe=''), {'name': node})
        if self.current() != node:
            raise SelectionChanged(self.current())
        self.last = node
        self.inflight = ''
        self.check_selection()
        self.log('candidate_selected', node=node)

    def restore(self):
        if not self.restore_needed:
            return
        try:
            epoch, _, current = self.context()
            if current == self.origin:
                self.restore_needed = False
                return
            if current not in (self.last, self.inflight):
                self.record('selection_changed', current)
                return
            if epoch != self.epoch:
                self.record('network_unknown', current)
                return
            self.api('proxies/' + quote(GROUP, safe=''), {'name': self.origin})
            if self.current() != self.origin:
                self.record('restore_failed', self.current())
            else:
                self.log('origin_restored', node=self.origin)
        except (ControlError, ContextChanged):
            self.record('restore_failed', self.last)

    def check(self, readonly=False):
        # Same lock name as old shell: deployment cannot overlap an old recovery scan.
        with self.lock('ai-egress.lock') as acquired, ExitStack() as recovery:
            if not acquired:
                return
            started = time.monotonic()
            try:
                self.epoch, self.members, self.origin = self.context()
                self.last = self.origin
                if self.probe():
                    self.check_selection()
                    self.record('healthy', self.origin)
                    if not readonly:
                        self.kick_scout()
                    return
                time.sleep(2)
                if self.probe():
                    self.check_selection()
                    self.record('healthy', self.origin)
                    if not readonly:
                        self.kick_scout()
                    return
                self.check_selection()
                if not self.base_network_ok():
                    (self.root/'ai-egress.idle').write_text('0\n')
                    self.record('network_unknown', self.origin)
                    return
                if readonly:
                    self.record('probe_failed', self.origin)
                    return
                try:
                    idle = float((self.root/'ai-egress.idle').read_text())
                except (ValueError, OSError):
                    idle = 0
                if 0 < time.time()-idle < COOLDOWN:
                    self.record('unavailable', self.origin)
                    return
                if not recovery.enter_context(self.lock('ai-egress-recovery.lock', blocking=True)):
                    return
                confirmed = time.monotonic()
                self.record('scanning', self.origin)
                self.log('fault_confirmed', node=self.origin, confirmation_s=round(confirmed-started, 3))
                self.invalidate(self.origin)
                tried = {self.origin}
                while True:
                    # In-flight scout results can invalidate/re-rank the second
                    # standby while the first is being truly verified.
                    cached = self.valid(load(self.cache_path), self.epoch, self.members, self.last)
                    cached = [d for d in cached if d['node'] not in tried]
                    if not cached:
                        break
                    d = cached[0]
                    if self.try_candidate(d['node'], tried, confirmed, started, d['delay_ms'], 'standby'):
                        return
                # No wait for the scout: use a separate bounded pool for on-demand coarse checks.
                remaining = [n for n in self.members if n not in tried]
                scan = self.coarse_candidates(remaining)
                try:
                    for d in scan:
                        if self.try_candidate(d['node'], tried, confirmed, started, d['delay_ms'], 'live'):
                            return
                finally:
                    scan.close()
                self.check_selection()
                if not self.base_network_ok():
                    (self.root/'ai-egress.idle').write_text('0\n')
                    self.record('network_unknown', self.origin)
                    return
                (self.root/'ai-egress.idle').write_text(str(time.time())+'\n')
                self.record('unavailable', self.origin)
            except SelectionChanged as e:
                self.restore_needed = False
                self.record('selection_changed', str(e))
            except ContextChanged:
                self.record('network_unknown', self.last)
            except ControlError:
                self.record('control_error', self.last)
            except Interrupted:
                self.record('interrupted', self.last)
            finally:
                self.restore()

    def try_candidate(self, node, tried, confirmed, started, delay_ms=None, source='live'):
        tried.add(node)
        if not self.base_network_ok():
            raise ContextChanged('Base network unavailable during recovery')
        self.log('candidate_verifying', node=node, delay_ms=delay_ms, source=source)
        self.switch(node)
        verification_started = time.monotonic()
        passed = self.probe()
        self.log('candidate_verified', node=node, passed=passed,
                 duration_s=round(time.monotonic()-verification_started, 3))
        if passed:
            self.check_selection()
            if self.recovery_scan_stop is not None:
                self.recovery_scan_stop.set()
            self.restore_needed = False
            self.record('healthy', node)
            self.invalidate(node)  # Current node is not a standby.
            self.log('recovered', node=node, scan_s=round(time.monotonic()-confirmed, 3),
                     observed_s=round(time.monotonic()-started, 3))
            self.kick_scout()
            return True
        self.invalidate(node)
        return False

    def coarse_candidates(self, nodes, background=False, on_result=None):
        # Workers continue dispatching while the caller serially verifies a node.
        # Only measured results enter ready; unstarted/cancelled work is not failure.
        nodes = list(dict.fromkeys(nodes))
        iterator = iter(nodes)
        stop = threading.Event()
        if not background:
            self.recovery_scan_stop = stop
        condition = threading.Condition()
        ready, errors = [], []
        workers = BACKGROUND_CONCURRENCY if background else RECOVERY_CONCURRENCY
        active = workers
        dispatched = completed = 0
        preempted = False
        round_id = ('bg-' if background else 'fg-') + str(os.getpid()) + '-' + str(time.monotonic_ns())
        round_started = time.monotonic()

        def foreground_idle():
            with self.lock('ai-egress-recovery.lock', shared=True) as idle:
                return idle

        def should_stop():
            nonlocal preempted
            if stop.is_set():
                return True
            if background and not foreground_idle():
                preempted = True
                stop.set()
                return True
            return False

        def worker():
            nonlocal active, dispatched, completed
            try:
                while not should_stop():
                    result = None
                    # Background can occupy only slots 0/1; recovery can always
                    # start on the other four even while those requests drain.
                    for slot in range(workers):
                        with self.lock('ai-egress-delay-' + str(slot) + '.lock') as acquired:
                            if not acquired:
                                continue
                            with condition:
                                if should_stop():
                                    return
                                node = next(iterator, None)
                                if node is None:
                                    return
                                dispatched += 1
                            started_at = time.time()
                            delay = self.delay(node)
                            result = dict(node=node, delay_ms=delay if valid_delay(delay) else None,
                                          started_at=started_at, success_at=time.time(),
                                          completed_at=time.monotonic())
                        # Release the scarce global slot before publishing/cache IO.
                        break
                    if result is None:
                        stop.wait(0.05)
                        continue
                    self.log('coarse_result', round_id=round_id, background=background,
                             node=result['node'], delay_ms=result['delay_ms'],
                             started_at=result['started_at'], completed_at=result['success_at'],
                             failure=None if result['delay_ms'] is not None else 'probe_or_delay_invalid')
                    if on_result:
                        on_result(result)
                    with condition:
                        completed += 1
                        if result['delay_ms'] is not None:
                            ready.append(result)
                        condition.notify_all()
            except Exception as e:
                with condition:
                    errors.append(e)
                    stop.set()
                    condition.notify_all()
            finally:
                with condition:
                    active -= 1
                    condition.notify_all()

        self.log('coarse_scan_started', round_id=round_id, background=background, concurrency=workers)
        threads = [threading.Thread(target=worker, name=round_id+'-'+str(i)) for i in range(workers)]
        try:
            for thread in threads:
                thread.start()
            while True:
                with condition:
                    if errors:
                        raise errors[0]
                    if should_stop():
                        break
                    if ready:
                        # Merge ALL completed results before each next candidate.
                        result = min(ready, key=lambda d: (d['delay_ms'], d['completed_at'], nodes.index(d['node'])))
                        ready.remove(result)
                    elif active:
                        condition.wait(0.1)
                        continue
                    else:
                        break
                yield result
        finally:
            stop.set()
            # At most six single bounded requests finish; no whole-round drain.
            # Callback writes finish before the scout releases its lifecycle lock.
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
            if not background:
                self.recovery_scan_stop = None
            self.log('coarse_scan_ended', round_id=round_id, background=background,
                     dispatched=dispatched, completed=completed, preempted=preempted,
                     duration_s=round(time.monotonic()-round_started, 3))

    def scout(self, force=False):
        with self.lock('ai-egress-scout.lock') as acquired:
            if not acquired:
                return
            # Healthy checks must not starve scouting every ten seconds. Only an
            # actual recovery transaction blocks new background probes.
            with self.lock('ai-egress-recovery.lock', shared=True) as foreground_idle:
                if not foreground_idle:
                    return
            try:
                epoch, members, current = self.context()
                cache = load(self.cache_path)
                now = time.time()
                due = cache.get('next_refresh', 0)
                if not numeric(due):
                    due = 0
                records = cache.get('candidates', [])
                retained = self.valid(cache, epoch, members, current)
                invalidated = not isinstance(records, list) or len(retained) < len(records)
                state = load(self.state_path)
                network_recovered = (cache.get('status') == 'network_unknown'
                                     and state.get('status') == 'healthy'
                                     and state.get('checked_at', 0) >= cache.get('checked_at', 0))
                if (not force and cache.get('version') == CACHE_VERSION and cache.get('epoch') == epoch
                        and not invalidated and not network_recovered and 0 < due-now <= REFRESH):
                    return
                if not self.base_network_ok():
                    self.cache_update(epoch, lambda c: c.update(candidates=[], next_refresh=now+REFRESH,
                                                               status='network_unknown', checked_at=now), background=True)
                    return
                started = time.monotonic()
                existing = self.valid(cache, epoch, members, current)
                first = [d['node'] for d in existing]
                scanned = successful = 0

                def batch(nodes):
                    # A fixed-size batch prevents eager worker dispatch from scanning
                    # the whole pool while the consumer is deciding whether to stop.
                    nonlocal scanned, successful
                    results = []
                    scan = self.coarse_candidates(nodes, background=True, on_result=results.append)
                    try:
                        for _ in scan:
                            pass
                    finally:
                        scan.close()
                    with self.lock('ai-egress-recovery.lock', shared=True) as idle:
                        if not idle:
                            raise ContextChanged('Recovery preempted scout')
                    live_epoch, _, live_current = self.context()
                    if live_epoch != epoch or live_current != current or len(results) != len(nodes):
                        raise ContextChanged('Scout context changed or preempted')
                    scanned += len(results)
                    successful += sum(r['delay_ms'] is not None for r in results)
                    return results

                def publish(results):
                    # Merge against the latest cache, never against a stale local
                    # snapshot: a foreground failure marker dominates old probes.
                    with self.lock('ai-egress-recovery.lock', shared=True) as idle:
                        if not idle:
                            raise ContextChanged('Recovery preempted scout')
                    live_epoch, _, live_current = self.context()
                    if live_epoch != epoch or live_current != current:
                        raise ContextChanged('Scout selection changed')
                    def change(c):
                        records = {r['node']: r for r in c['candidates']
                                   if isinstance(r, dict) and isinstance(r.get('node'), str)}
                        for result in results:
                            node = result['node']
                            previous = records.get(node)
                            if previous and previous.get('started_at', 0) > result['started_at']:
                                continue
                            if result['delay_ms'] is None:
                                c['failed_at'][node] = max(c['failed_at'].get(node, 0), result['success_at'])
                                records.pop(node, None)
                            elif c['failed_at'].get(node, 0) < result['started_at']:
                                records[node] = result
                        c['candidates'] = list(records.values())
                        c['status'] = 'ready' if len(self.ranked(c['candidates'], members, current,
                                                                c['failed_at'])) == 2 else 'insufficient'
                    self.cache_update(epoch, change, background=True)

                # Recheck only the current standbys, and confirm failures before
                # evicting them. No replacement scan until these checks finish.
                if first:
                    results = batch(first)
                    failed = [r['node'] for r in results if r['delay_ms'] is None]
                    if failed:
                        time.sleep(STANDBY_RETRY_DELAY)
                        retry = batch(failed)
                        retried = {r['node']: r for r in retry}
                        results = [retried.get(r['node'], r) for r in results]
                    publish(results)

                attempted = set(first)
                rest = self.ordered([n for n in members if n != current and n not in attempted],
                                    members, current)
                while rest:
                    current_cache = load(self.cache_path)
                    count = len(self.valid(current_cache, epoch, members, current))
                    if count >= 2:
                        break
                    # Never dispatch more than the number still needed.
                    nodes, rest = rest[:min(BACKGROUND_CONCURRENCY, 2-count)], rest[min(BACKGROUND_CONCURRENCY, 2-count):]
                    results = batch(nodes)
                    # New candidates have not been retained before; one failure is
                    # enough to skip them for this round, not to evict a standby.
                    publish(results)

                with self.lock('ai-egress-recovery.lock', shared=True) as idle:
                    if not idle:
                        return
                live_epoch, _, live_current = self.context()
                if live_epoch != epoch or live_current != current:
                    return
                def commit(c):
                    c.update(next_refresh=time.time()+REFRESH,
                             status='ready' if len(self.ranked(c['candidates'], members, current,
                                                               c['failed_at'])) == 2 else 'insufficient',
                             checked_at=time.time())
                self.cache_update(epoch, commit, background=True)
                result = load(self.cache_path)
                self.log('standbys_refreshed', count=len(result.get('candidates', [])),
                         nodes=[d['node'] for d in result.get('candidates', [])],
                         delays_ms=[d['delay_ms'] for d in result.get('candidates', [])],
                         scanned=scanned, successful=successful,
                         duration_s=round(time.monotonic()-started, 3))
            except (ControlError, ContextChanged):
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'scout'], nargs='?', default='check')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--force', action='store_true', help='Refresh scout immediately (never selects nodes)')
    args = parser.parse_args()
    def interrupt(*_):
        raise Interrupted()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupt)
    guard = Guard()
    if args.mode == 'scout':
        guard.scout(force=args.force)
    else:
        guard.check(readonly=args.check_only)


if __name__ == '__main__':
    main()
