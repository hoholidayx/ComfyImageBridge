"""Detached, on-demand GNOME inhibitor; no changes to ComfyUI or power policy."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from .client import Client
from .storage import now, read_json, write_json
from .lifecycle import ANY_EPOCH


def home(api):
    path = api.data / 'sleep-guard'
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def control(api):
    with (home(api)/'control.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def identity(pid):
    path = Path('/proc')/str(pid)
    try:
        fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start_ticks': fields[19], 'uid': path.stat().st_uid,
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'command': (path/'cmdline').read_bytes().decode().rstrip('\0').split('\0')}
    except (OSError, ValueError, IndexError):
        return None


def matches(saved):
    if not saved or saved.get('uid') != os.getuid():
        return False
    current = identity(saved['pid'])
    if not current:
        return False
    # Compatibility for pre-upgrade records. New identities always carry boot_id;
    # the service manager separately verifies boot identity before adoption.
    if 'boot_id' not in saved:
        current = {key: value for key, value in current.items() if key != 'boot_id'}
    return current == saved


def state(api):
    path = home(api)/'state.json'
    return read_json(path) if path.exists() else {}


def _lease_owner_state(owner):
    if not isinstance(owner, dict) or not owner.get('pid'):
        return None
    if matches(owner):
        return True
    proc = Path('/proc')/str(owner['pid'])
    try:
        process_state = (proc/'stat').read_text().rsplit(')',1)[1].split()[0]
        if process_state == 'Z':
            return False
        # A readable changed identity proves the old owner ended or execed.
        return False if identity(owner['pid']) is not None else None
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, ValueError, IndexError):
        return None


def live_leases(api):
    result = []
    for path in (home(api)/'leases').glob('*.json'):
        try:
            value = read_json(path)
        except (OSError, ValueError) as exc:
            result.append({'lease_id':path.stem,'owner_state':'unknown','error':str(exc)})
            continue
        if not isinstance(value, dict):
            result.append({'lease_id':path.stem, 'owner_state':'unknown', 'error':'Lease is not an object'})
            continue
        owner = value.get('owner')
        if matches(owner):
            result.append(value)
        elif _lease_owner_state(owner) is False:
            path.unlink(missing_ok=True)
        else:
            result.append({**value, 'owner_state':'unknown'})
    return result


def status(api):
    current = state(api)
    running = matches(current.get('supervisor')) and matches(current.get('worker'))
    result = {**current, 'running': running, 'inhibited': False,
              'log': str(home(api)/'guard.log')}
    from .task_diagnostics import seconds
    lease_details = []
    for path in (home(api)/'leases').glob('*.json'):
        try:
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError('Lease is not an object')
            alive = _lease_owner_state(value.get('owner'))
            age = round(seconds(value.get('created_at')), 3)
            lease_details.append({**value, 'owner_alive': alive, 'age_seconds': age,
                                  'age_warning': age >= 600})
        except (OSError, ValueError) as exc:
            lease_details.append({'lease_id':path.stem, 'owner_alive':None, 'error':str(exc)})
    result.update(observed_at=now(), lease_details=lease_details,
                  leases=sum(v.get('owner_alive') is not False for v in lease_details),
                  stale=not running, heartbeat_at=current.get('heartbeat'))
    if current.get('token'):
        try:
            result['inhibited'] = inhibited(current['token'])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result['inhibited'] = None
            result['health_error'] = str(exc)
    return result


def environment():
    return {**os.environ, 'DBUS_SESSION_BUS_ADDRESS': os.environ.get('DBUS_SESSION_BUS_ADDRESS') or f'unix:path=/run/user/{os.getuid()}/bus'}


def inhibited(token):
    result = subprocess.run(['/usr/bin/gnome-session-inhibit', '--list'], env=environment(),
                            text=True, capture_output=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f'GNOME inhibitor query failed ({result.returncode}): {result.stderr.strip()[:500]}')
    return f'comfy-workflow-api-{token}:' in result.stdout


def start(api):
    deadline = time.monotonic()+12
    launched_token = None
    while time.monotonic() < deadline:
        with control(api):
            current = state(api)
            active = matches(current.get('supervisor'))
            if launched_token and current.get('token') == launched_token and not active:
                raise RuntimeError('Sleep inhibitor exited before becoming ready; check GNOME session and '+str(home(api)/'guard.log'))
            if active and current.get('phase') in ('watching', 'starting'):
                if current['base_url'] != api.client.base_url:
                    raise RuntimeError('Sleep guard is bound to a different API URL')
                token = current['token']
            elif active:
                token = None  # let an exiting guard finish before replacing it
            else:
                token = uuid.uuid4().hex
                command = ['/usr/bin/gnome-session-inhibit', '--app-id', f'comfy-workflow-api-{token}',
                    '--reason', 'ComfyUI API requests or queued/running generation tasks', '--inhibit', 'suspend:idle',
                    sys.executable, '-m', 'comfy_workflow_api._sleep_guard_worker', '--root', str(api.root),
                    '--base-url', api.client.base_url, '--worker', '--token', token]
                with (home(api)/'guard.log').open('ab', buffering=0) as log:
                    process = subprocess.Popen(['/usr/bin/nohup', *command], cwd=api.root,
                        env=environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True)
                # Wait for nohup to exec while holding the control lock. A concurrent
                # caller must never mistake the short-lived launcher for a stale PID.
                launched = time.monotonic()
                while True:
                    saved = identity(process.pid)
                    if saved and saved['command'] == command:
                        break
                    if process.poll() is not None or time.monotonic()-launched > 3:
                        raise RuntimeError('Sleep guard launcher failed; see '+str(home(api)/'guard.log'))
                    time.sleep(.02)
                write_json(home(api)/'state.json', {'token': token, 'phase': 'starting', 'base_url': api.client.base_url,
                    'supervisor': saved, 'created_at': now(), 'ready': False})
                launched_token = token
        if token:
            current = status(api)
            if current.get('token') == token and current.get('ready') and current['running']:
                if not current['inhibited']:
                    raise RuntimeError('GNOME did not grant the requested sleep/idle inhibitor')
                return current
        time.sleep(.1)
    raise RuntimeError('Sleep guard did not become ready; no job submitted. See '+str(home(api)/'guard.log'))


@contextmanager
def hold(api, operation_id=None, task_id=None, service_instance_id=None, purpose=None,
         expected_sleep_epoch=ANY_EPOCH):
    from .sleep_cycle import admission_lock, assert_allowed
    if not api.settings.get('sleep_guard', {}).get('enabled', False):
        with admission_lock(api):
            assert_allowed(api, expected_sleep_epoch)
        yield
        return
    lease = uuid.uuid4().hex
    path = home(api)/'leases'/f'{lease}.json'
    with admission_lock(api):
        assert_allowed(api, expected_sleep_epoch)
        with control(api):
            write_json(path, {'lease_id': lease, 'owner': identity(os.getpid()), 'created_at': now(),
                'operation_id': operation_id or str(uuid.uuid4()), 'task_id': task_id,
                'service_instance_id': service_instance_id, 'purpose': purpose or 'request'})
    try:
        start(api)
        yield
    finally:
        with control(api):
            path.unlink(missing_ok=True)


def ticket(api, task_id, remove=False):
    if api.settings.get('sleep_guard', {}).get('enabled', False):
        with control(api):
            path = home(api)/'tickets'/f'{task_id}.json'
            if remove:
                path.unlink(missing_ok=True)
            else:
                from .lifecycle import instance_id
                write_json(path, {'task_id': task_id, 'created_at': now(), 'service_instance_id': instance_id(api)})


def stop(api, force=False):
    with control(api):
        current = status(api)
        if not current['running']:
            if current.get('phase') not in (None, 'stopped'):
                raise RuntimeError('Recorded process identity no longer matches; refusing to signal any PID')
            return current
        queue = api.client.request('/queue') if not force else {}
        if not force and (live_leases(api) or any(queue.get(k) for k in ('queue_running', 'queue_pending'))):
            raise RuntimeError('Guard protects active work; stop refused (explicit --force stops only the guard)')
        # Exact PID, birth time, UID and argv were checked immediately before signalling.
        if not matches(current['worker']):
            raise RuntimeError('Worker identity changed; refusing to signal')
        os.kill(current['worker']['pid'], signal.SIGTERM)
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        if not matches(current['worker']) and not matches(current['supervisor']):
            return status(api)
        time.sleep(.1)
    raise RuntimeError('Guard did not stop within 10 seconds; no unrelated processes were signalled')


def worker(api, token):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    stopping = False
    def on_stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    with (home(api)/'worker.lock').open('a') as singleton:
        fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with control(api):
            current = state(api)
            if current.get('token') != token or not matches(current.get('supervisor')):
                raise RuntimeError('Launcher identity or token mismatch')
            current.update(phase='watching', ready=True, worker=identity(os.getpid()), heartbeat=now())
            write_json(home(api)/'state.json', current)
            print(json.dumps({'time': now(), 'started': True, 'token': token,
                              'worker': current['worker']['pid'], 'base_url': api.client.base_url}), flush=True)
        settings = api.settings['sleep_guard']
        client = Client(api.client.base_url, timeout=3)
        last_busy = time.monotonic()
        saw_busy = False
        from . import lifecycle, task_diagnostics
        observed_instance = lifecycle.instance_id(api)
        last_observation = 0
        reason = 'stopped_by_operator'
        try:
            while not stopping:
                with control(api):
                    leases = live_leases(api)
                    tickets = list((home(api)/'tickets').glob('*.json'))
                    # Only verified termination of the exact bound instance can
                    # retire queue memory. Network errors or lease age cannot.
                    if lifecycle.retired(api, observed_instance):
                        saw_busy = False
                        observed_instance = lifecycle.instance_id(api)
                    retained_tickets = []
                    for path in tickets:
                        try:
                            value = read_json(path)
                            if lifecycle.retired(api, value.get('service_instance_id')):
                                path.unlink(missing_ok=True)
                            else:
                                retained_tickets.append(path)
                        except (OSError, ValueError):
                            retained_tickets.append(path)
                    tickets = retained_tickets
                    error = None
                    try:
                        queue = client.request('/queue')
                        busy = any(queue.get(k) for k in ('queue_running', 'queue_pending'))
                        saw_busy = busy
                        observed_instance = lifecycle.instance_id(api)
                        if busy and time.monotonic()-last_observation >= 5:
                            task_diagnostics.observe(api, queue)
                            last_observation = time.monotonic()
                        if not busy and not leases:
                            for path in tickets:
                                path.unlink(missing_ok=True)
                            tickets = []
                    except Exception as exc:
                        busy = None
                        error = str(exc)
                    protected = bool(leases or busy or (busy is None and (tickets or saw_busy)))
                    if protected:
                        last_busy = time.monotonic()
                    current = state(api)
                    if current.get('token') != token:
                        raise RuntimeError('Guard token replaced unexpectedly')
                    current.update(heartbeat=now(), service_instance_id=observed_instance, leases=len(leases), queued_or_running=busy,
                        outstanding_tickets=len(tickets), queue_error=error)
                    if not protected and time.monotonic()-last_busy >= settings['idle_grace_seconds']:
                        reason = 'queue_empty_and_no_requests'
                        current.update(phase='exiting', ready=False)
                        write_json(home(api)/'state.json', current)
                        break
                    write_json(home(api)/'state.json', current)
                time.sleep(settings['poll_seconds'])
        finally:
            with control(api):
                current = state(api)
                if current.get('token') == token:
                    current.update(phase='stopped', ready=False, stopped_at=now(), reason=reason)
                    write_json(home(api)/'state.json', current)
            print(json.dumps({'time': now(), 'released': True, 'reason': reason}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--worker', action='store_true', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    from .api import WorkflowAPI
    worker(WorkflowAPI(args.root, args.base_url), args.token)
