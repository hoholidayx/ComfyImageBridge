"""Persistent admission gate for one system-managed sleep/resume cycle.

The system runner owns systemctl. This module runs as the ComfyUI user and
never exposes its internal authorization through the remote tool contract.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import time
import uuid

from .storage import now, read_json, write_json
from .lifecycle import ANY_EPOCH

ACTIVE = {'preparing', 'sleeping', 'restoring', 'restored', 'restore_failed'}
_authorization = ContextVar('sleep_cycle_authorization', default=None)


class SleepPending(RuntimeError):
    pass


class RestoreDeadline(BaseException):
    """Unwind management locks before systemd's outer startup timeout."""


class PrepareDeadline(BaseException):
    """Record uncertain shutdown before the root runner's outer timeout."""


class RetryDeadline(BaseException):
    """Leave a failed cycle gated if owned-process cleanup takes too long."""


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def home(api):
    return api.data/'sleep-cycle'


def status(api):
    """Read only; never clean up or infer completion from a PID or age."""
    path = home(api)/'state.json'
    if not path.exists():
        return {'success': True, 'active': False, 'state': 'idle', 'phase': 'idle'}
    try:
        value = read_json(path)
        if not isinstance(value, dict) or not value.get('cycle_token') or not value.get('boot_id'):
            raise ValueError('Malformed sleep cycle record')
    except (OSError, ValueError) as exc:
        return {'success': False, 'active': True, 'state': 'unknown', 'phase': 'unknown', 'error': str(exc)}
    current_boot = boot_id()
    stale = bool(current_boot and value['boot_id'] != current_boot)
    phase = value.get('phase', value.get('state', 'unknown'))
    return {**value, 'state': phase, 'phase': phase, 'stale_boot': stale,
            'active': not stale and phase not in ('complete', 'aborted')}


@contextmanager
def admission_lock(api):
    folder = api.data/'comfyui-service'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'admission.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


@contextmanager
def control(api):
    home(api).mkdir(parents=True, exist_ok=True)
    with (home(api)/'control.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SleepPending('Another sleep-cycle operation is still running') from exc
        yield


def admission_epoch(api):
    """Fence delayed work across both completed sleep cycles and kernel boots."""
    return {'boot_id': boot_id(), 'cycle_token': status(api).get('cycle_token')}


def assert_allowed(api, expected_epoch=ANY_EPOCH):
    current = status(api)
    if expected_epoch is not ANY_EPOCH and expected_epoch != {
            'boot_id': boot_id(), 'cycle_token': current.get('cycle_token')}:
        raise SleepPending('Sleep cycle changed after this operation was dispatched; submit a new operation')
    if not current['active']:
        return
    expected = (str(api.data.resolve()), current.get('cycle_token'))
    if _authorization.get() != expected or not current.get('cycle_token'):
        raise SleepPending('Sleep cycle blocks new work: '+current['phase'])


@contextmanager
def internal(api, token):
    current = status(api)
    if not token or not current['active'] or current.get('cycle_token') != token:
        raise SleepPending('Sleep cycle token is no longer current')
    marker = _authorization.set((str(api.data.resolve()), token))
    try:
        yield
    finally:
        _authorization.reset(marker)


def _save(api, value, phase, **updates):
    value = {**value, **updates, 'phase': phase, 'state': phase, 'updated_at': now()}
    write_json(home(api)/'state.json', value)
    return {**value, 'active': phase in ACTIVE}


def _event(api, kind, value):
    try:
        from .hardware_monitor import queue_event
        queue_event(api.root, kind, {key: value.get(key) for key in
                    ('cycle_token', 'phase', 'was_running', 'error', 'service_instance_id')})
    except Exception as exc:
        value['notification_error'] = str(exc)
        with admission_lock(api):
            current = status(api)
            if current.get('cycle_token') == value.get('cycle_token'):
                write_json(home(api)/'state.json', {**current, 'notification_error': str(exc)})
    return value


def _current(api, token=None):
    value = status(api)
    if not value['active'] or not value.get('cycle_token'):
        raise SleepPending('No current sleep cycle')
    if token is not None and token != value['cycle_token']:
        raise SleepPending('Sleep cycle token changed; no action taken')
    return value


def _stopped(current):
    recorded = bool(current.get('process') or current.get('managed_processes'))
    return (not current.get('owned_process_running') and not current.get('listeners')
            and (not recorded or current.get('processes_exited') is True))


def quiesce_gpu(api, timeout_seconds=6):
    """Wait for an already-running probe, never synchronously reap it."""
    folder = api.data/'hardware-probe'
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(folder/'gpu.lock', os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic()+timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('GPU probe is still running; sleep refused without waiting for a stuck driver')
                time.sleep(min(.05, remaining))
    finally:
        os.close(descriptor)


def pre(api, manager):
    from . import sleep_guard
    with control(api):
        with admission_lock(api):
            if status(api)['active']:
                raise SleepPending('Previous sleep cycle has not finished')
            folder = api.data/'comfyui-service'
            gate = folder/'admission.json'
            if gate.exists() and (prior := read_json(gate)).get('blocked', True):
                if sleep_guard._lease_owner_state(prior.get('owner')) is False:
                    gate.unlink()
                else:
                    raise SleepPending('A service operation still holds admission')
            for path in (folder/'inflight').glob('*.json'):
                reservation = read_json(path)
                if sleep_guard._lease_owner_state(reservation.get('owner')) is not False:
                    raise SleepPending('SDK submission is still in flight')
            with sleep_guard.control(api):
                if sleep_guard.live_leases(api):
                    raise SleepPending('An active request still holds a sleep lease')
            identity = boot_id()
            if not identity:
                raise RuntimeError('Cannot identify the current kernel boot')
            value = _save(api, {'cycle_token': str(uuid.uuid4()), 'boot_id': identity,
                               'created_at': now(), 'success': False, 'was_running': False,
                               'stop_started': False, 'stop_confirmed': False}, 'preparing')
        try:
            quiesce_gpu(api)
            current = manager.status()
            if current.get('listeners') and not current.get('owned_listener'):
                raise SleepPending('Foreign or unreadable listener; sleep refused')
            running = bool(current.get('owned_process_running'))
            value = _save(api, value, 'preparing', was_running=running,
                          service_instance_id=current.get('service_instance_id'))
            if running and (not current.get('queue_known') or manager.queue_ids(current) != []):
                raise SleepPending('ComfyUI queue is busy or unknown; sleep refused')
            if running:
                value = _save(api, value, 'preparing', stop_started=True)
                with internal(api, value['cycle_token']):
                    stopped = manager.execute('stop')
                if stopped.get('busy'):
                    if _shutdown_never_started(value, stopped):
                        value = _save(api, value, 'preparing', stop_started=False)
                    raise SleepPending('New generation appeared during shutdown; sleep refused')
                if stopped.get('blocked') or not _stopped(stopped):
                    raise RuntimeError('Managed process termination was not confirmed; sleep refused')
            if not _stopped(manager.status()):
                raise RuntimeError('Process or listener appeared during sleep preparation')
            value = _save(api, value, 'sleeping', stop_confirmed=True, success=True)
            return value
        except BaseException as exc:
            # An uncertain partial shutdown must keep clients out. No age-based
            # cleanup can prove the old process or GPU context ended.
            with admission_lock(api):
                phase = 'restore_failed' if value.get('stop_started') else 'aborted'
                value = _save(api, value, phase, success=False, error=str(exc) or type(exc).__name__)
            _event(api, 'sleep_prepare_failed', value)
            raise


def _gpu_ready(probe):
    return probe.get('status') == 'ok' and not probe.get('paused', False)


def resume_owner(api, manager, token=None, gpu_wait_seconds=120):
    from .hardware_probe import gpu_probe
    with control(api):
        value = _current(api, token)
        if value['phase'] == 'restored':
            return value
        if value['phase'] not in ('sleeping', 'restoring'):
            raise SleepPending('Recovery requires an explicit retry after failure')
        value = _save(api, value, 'restoring', success=False, restore_started_at=now())
        try:
            if not value.get('stop_confirmed'):
                raise RuntimeError('Pre-sleep process termination was not confirmed')
            if not value['was_running']:
                return _save(api, value, 'restored', success=True)
            deadline = time.monotonic()+gpu_wait_seconds
            consecutive = 0
            while time.monotonic() < deadline:
                sample = gpu_probe(api.root, timeout_seconds=min(5, deadline-time.monotonic()),
                                   allow_during_restore=True)
                consecutive = consecutive+1 if _gpu_ready(sample) else 0
                value = _save(api, value, 'restoring', gpu_sample=sample,
                              consecutive_healthy_samples=consecutive)
                if consecutive >= 3:
                    break
                time.sleep(min(3, max(0, deadline-time.monotonic())))
            else:
                raise TimeoutError('GPU did not produce three healthy samples within the resume deadline')
            with internal(api, value['cycle_token']):
                result = manager.execute('ensure_ready')
            if not result.get('ready'):
                raise RuntimeError(result.get('message') or 'Fresh ComfyUI render verification failed')
            return _save(api, value, 'restored', success=True,
                         restored_instance_id=result.get('service_instance_id'), restored_at=now())
        except BaseException as exc:
            value = _save(api, value, 'restore_failed', success=False,
                          error=str(exc) or type(exc).__name__)
            return _event(api, 'sleep_restore_failed', value)


def finalize(api, manager, token):
    if not token:
        raise ValueError('finalize requires --token from the matching pre result')
    with control(api), admission_lock(api):
        value = _current(api, token)
        current = manager.status()
        if value['was_running']:
            if value['phase'] != 'restored' or not value.get('success') or not current.get('ready'):
                raise SleepPending('Resume owner has not completed current render verification')
            if current.get('service_instance_id') != value.get('restored_instance_id'):
                raise SleepPending('Restored service instance changed before finalization')
        elif not value.get('stop_confirmed') or not _stopped(current):
            raise SleepPending('Originally stopped service unexpectedly became active')
        value = _save(api, value, 'complete', success=True, finished_at=now())
    return _event(api, 'sleep_restore_complete', value)


def retry(api, manager, token=None):
    with control(api):
        value = _current(api, token)
        if value['phase'] != 'restore_failed':
            raise SleepPending('Only a failed restoration can be retried')
        current = manager.status()
        if current.get('listeners') and not current.get('owned_listener'):
            raise SleepPending('Foreign or unreadable listener prevents retry')
        if current.get('owned_process_running'):
            with internal(api, value['cycle_token']):
                stopped = manager.execute('stop')
            if stopped.get('busy') or stopped.get('blocked') or not _stopped(stopped):
                raise SleepPending('Old owned instance could not be stopped safely')
        with admission_lock(api):
            if not _stopped(manager.status()):
                raise SleepPending('Old instance termination is not confirmed')
            return _save(api, value, 'restoring', success=True, stop_confirmed=True,
                         retry_requested_at=now(), needs_restore=bool(value['was_running']))


def fail(api, token, reason):
    """Coordinator failure receipt only; never signal or release admission."""
    if not token:
        raise ValueError('fail requires --token from the matching pre result')
    with control(api), admission_lock(api):
        value = _current(api, token)
        value = _save(api, value, 'restore_failed', success=False,
                      error=str(reason or 'Recovery coordinator failed')[:2000])
    return _event(api, 'sleep_restore_failed', value)


def abort(api, manager, token=None):
    with control(api), admission_lock(api):
        value = _current(api, token)
        current = manager.status()
        if value.get('stop_started') and not value.get('stop_confirmed'):
            if _shutdown_never_started(value, current):
                return _save(api, value, 'aborted', success=True, needs_restore=False,
                             stop_started=False, aborted_before_signal=True)
            raise SleepPending('Shutdown is uncertain; cannot release admission or start another instance')
        if value.get('was_running') and value.get('stop_confirmed'):
            if not _stopped(current):
                raise SleepPending('A process is still running; cannot dispatch another restoration')
            return _save(api, value, 'restoring', success=True, needs_restore=True,
                         abort_requested_at=now())
        return _save(api, value, 'aborted', success=True, needs_restore=False)


def _shutdown_never_started(value, current):
    """Only fresh same-instance readiness can disprove an attempted shutdown."""
    if (current.get('ready') is not True or current.get('recorded_phase') != 'running'
            or not value.get('service_instance_id')
            or current.get('service_instance_id') != value['service_instance_id']):
        return False
    try:
        beginning = datetime.fromisoformat(value['created_at'])
        signals = current.get('signals', [])
        if not isinstance(signals, list):
            return False
        for sent in signals:
            if datetime.fromisoformat(sent['at']) >= beginning:
                return False
        shutdown = current.get('graceful_shutdown')
        if shutdown is not None and datetime.fromisoformat(shutdown['started_at']) >= beginning:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def main():
    from .api import WorkflowAPI
    from .runtime_bridge import manager_for
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--token')
    parser.add_argument('--reason')
    parser.add_argument('action', choices=('pre', 'resume-owner', 'finalize', 'status', 'retry', 'abort', 'fail'))
    args = parser.parse_args()
    api = WorkflowAPI(args.root)
    if args.action == 'status':
        print(json.dumps(status(api), ensure_ascii=False))
        return 0
    previous_alarm = None
    if args.action in ('resume-owner', 'pre', 'retry'):
        def deadline(_signal, _frame):
            if args.action == 'pre':
                raise PrepareDeadline('Sleep preparation exceeded its 150 second internal budget')
            if args.action == 'retry':
                raise RetryDeadline('Recovery cleanup exceeded its 150 second internal budget')
            raise RestoreDeadline('Resume exceeded its 540 second internal budget')
        previous_alarm = signal.signal(signal.SIGALRM, deadline)
        signal.alarm(540 if args.action == 'resume-owner' else 150)
    try:
        manager = manager_for(api.root)
        if args.action == 'pre':
            result = pre(api, manager)
        elif args.action == 'resume-owner':
            result = resume_owner(api, manager, args.token)
        elif args.action == 'finalize':
            result = finalize(api, manager, args.token)
        elif args.action == 'retry':
            result = retry(api, manager, args.token)
        elif args.action == 'fail':
            result = fail(api, args.token, args.reason)
        else:
            result = abort(api, manager, args.token)
        print(json.dumps(result, ensure_ascii=False))
        # Handled restore failures intentionally keep the owner cgroup active.
        return 0 if args.action == 'resume-owner' or result.get('success', False) else 1
    except (Exception, PrepareDeadline, RestoreDeadline, RetryDeadline) as exc:
        if isinstance(exc, RetryDeadline):
            fail(api, args.token, str(exc))
        print(json.dumps({**status(api), 'success': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if previous_alarm is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_alarm)


if __name__ == '__main__':
    # -m first executes this file as __main__. Management imports the canonical
    # module; run its main too so both sides share the same authorization token.
    from comfy_workflow_api.sleep_cycle import main as canonical_main
    raise SystemExit(canonical_main())
