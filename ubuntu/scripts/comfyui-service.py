"""Verified lifecycle of the ComfyUI instance launched by run_comfyui.sh.

HTTP liveness is deliberately separate from a fresh render in this process.
Owned-instance shutdown timeouts escalate automatically under the user policy.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid

from comfy_workflow_api.client import APIError, Client, png_size
from comfy_workflow_api.compiler import compile_graph
from comfy_workflow_api.sleep_guard import identity, matches
from comfy_workflow_api.storage import now, read_json, write_json
from comfy_workflow_api.settings import load_settings, module_root, service_paths

ROOT = module_root(os.environ.get('COMFY_WORKFLOW_HOME') or Path(__file__).resolve().parents[1])
SETTINGS = load_settings(ROOT)
COMFY, RUN, UV = service_paths(ROOT, SETTINGS)
DATA = Path(SETTINGS['data_dir'])/'comfyui-service'
STATE = DATA/'state.json'
LOG = DATA/'comfyui.log'
CLIENT = Client(SETTINGS['base_url'], timeout=2)
SERVICE_URL = urllib.parse.urlparse(SETTINGS['base_url'])
if SERVICE_URL.scheme != 'http' or SERVICE_URL.hostname not in ('127.0.0.1', 'localhost', '::1'):
    raise ValueError('Service management requires a configured loopback http ComfyUI URL')
PORT = SERVICE_URL.port or 80
GRACEFUL_STOP_SECONDS = 120


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def process_identity(pid):
    item = identity(pid)
    if item:
        item['boot_id'] = boot_id()
    return item


def process_matches(item):
    return bool(item and (not item.get('boot_id') or item['boot_id'] == boot_id()) and matches({k:v for k,v in item.items() if k != 'boot_id'}))


@contextmanager
def lock(name='control.lock'):
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA/name).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def group(pid):
    try:
        fields = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[2]), int(fields[3])
    except (OSError, ValueError):
        return None


def listeners():
    """Keep every LISTEN socket, including ones with unreadable process ownership."""
    sockets = {}
    for name in ('tcp', 'tcp6'):
        try:
            lines = (Path('/proc/net')/name).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for line in lines:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == PORT:
                sockets[fields[9]] = {'inode': fields[9], 'family': name,
                                      'address': fields[1], 'state': 'LISTEN', 'owners': []}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            found = set()
            for fd in (proc/'fd').iterdir():
                target = os.readlink(fd)
                if target.startswith('socket:['):
                    inode = target[8:-1]
                    if inode in sockets:
                        found.add(inode)
            owner = process_identity(int(proc.name)) if found else None
            for inode in found:
                if owner:
                    sockets[inode]['owners'].append(owner)
        except (OSError, ProcessLookupError):
            continue
    return list(sockets.values())


def resume_evidence():
    # BOOTTIME-MONOTONIC increases on suspend even when the kernel journal is private.
    # Round milliseconds to avoid measurement jitter; kernel boot identity scopes it.
    evidence = {'boot_id': boot_id(), 'resume_id': None, 'source': 'unknown', 'observed_at': now()}
    if hasattr(time, 'CLOCK_BOOTTIME'):
        offset = time.clock_gettime(time.CLOCK_BOOTTIME)-time.clock_gettime(time.CLOCK_MONOTONIC)
        evidence.update(suspend_offset_seconds=round(offset, 2),
                        resume_id=f"{evidence['boot_id']}:{round(offset, 2)}", source='boottime_minus_monotonic')
    try:
        result = subprocess.run(['journalctl', '-b', '-k', '-g', 'PM: suspend exit', '-n', '1',
                                 '-o', 'json', '--no-pager'], capture_output=True, text=True, timeout=2)
        entries = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        if result.returncode == 0 and entries:
            item = entries[-1]
            evidence.update(last_resume_at=item.get('__REALTIME_TIMESTAMP'), journal_cursor=item.get('__CURSOR'))
            if item.get('__CURSOR'):

                if evidence['resume_id'] is None:
                    evidence.update(resume_id=f"{evidence['boot_id']}:{item['__CURSOR']}", source='kernel_journal')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return evidence


def same_resume(left, right):
    if left is None or right is None:
        return False
    if left == right:
        return True
    # Clock subtraction is not atomic. Tolerate measurement jitter without
    # masking a practical suspend interval, and keep it scoped to one boot.
    try:
        boot_a, value_a = left.split(':', 1)
        boot_b, value_b = right.split(':', 1)
        return boot_a == boot_b and abs(float(value_a)-float(value_b)) <= .05
    except (AttributeError, ValueError):
        return False


def instance_id(state):
    if state.get('service_instance_id'):
        return state['service_instance_id']
    process = state.get('process') or {}
    if not process.get('pid'):
        return None
    raw = [process.get('boot_id') or boot_id(), process.get('pid'), process.get('start_ticks'), process.get('uid')]
    return 'legacy-'+hashlib.sha256(json.dumps(raw).encode()).hexdigest()[:24]


def fingerprints():
    paths = {name: COMFY/name for name in ('pyproject.toml', 'uv.lock')}
    paths['run_script'] = RUN
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def workflow_hash():
    paths = [ROOT/'workflows/manifest.json', Path(__file__), RUN] + sorted((ROOT/'src/comfy_workflow_api').glob('*.py'))
    value = hashlib.sha256()
    for path in paths:
        value.update(str(path.name).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def _known_members(state):
    items = [state.get('process')] + state.get('managed_processes', [])
    return {item['pid']: item for item in items if item and process_matches(item)}


def _owned_owner(owner, state, live):
    root = state.get('process') or {}
    pgid = state.get('process_group', root.get('pid'))
    return bool(owner and owner.get('uid') == os.getuid() and process_matches(owner) and
                group(owner['pid']) == (pgid, pgid) and
                (owner['pid'] in live or (live and int(owner.get('start_ticks', 0)) >= int(root.get('start_ticks', 0)))))


def health():
    observed = now()
    try:
        system = CLIENT.request('/system_stats')
        queue = CLIENT.request('/queue')
        queue_known = isinstance(queue, dict) and all(isinstance(queue.get(k), list) for k in ('queue_running','queue_pending'))
        return {'http_healthy': isinstance(system, dict) and 'system' in system and queue_known,
                'system': system, 'queue': queue, 'queue_known': queue_known,
                'http_observed_at': observed}
    except Exception as exc:
        return {'http_healthy': False, 'queue_known': False, 'error': str(exc), 'http_observed_at': observed}


def status():
    state = read_json(STATE) if STATE.exists() else {}
    live = _known_members(state)
    sockets = listeners()
    owned_listener = bool(sockets and live and all(item['owners'] and all(
        _owned_owner(owner, state, live) for owner in item['owners']) for item in sockets))
    resume = resume_evidence()
    current = {**state, **health(), 'service_instance_id': instance_id(state),
               'owned_process_running': bool(live), 'owned_listener': owned_listener,
               'listeners': sockets, 'log': str(LOG), 'observed_at': now(), 'resume': resume}
    recorded = [item for item in [state.get('process')] + state.get('managed_processes', []) if item]
    current['processes_exited'] = bool(recorded) and all(incarnation_exited(item) for item in recorded)
    current['recorded_phase'] = state.get('phase')
    if current['processes_exited']:
        current['phase'] = 'stopped'
    shutdown = state.get('graceful_shutdown') or {}
    current['shutdown_pending'] = bool(live and state.get('phase') == 'stopping' and shutdown)
    if current['shutdown_pending']:
        try:
            current['shutdown_remaining_seconds'] = max(0, round(datetime.fromisoformat(shutdown['deadline_at']).timestamp()-time.time(), 1))
        except (KeyError, ValueError, TypeError):
            current['shutdown_remaining_seconds'] = None
    verification = state.get('verification') or {}
    try:
        same_workflow = verification.get('workflow_hash') == workflow_hash()
    except OSError:
        same_workflow = False
    verified = bool(verification.get('success') and verification.get('service_instance_id') == instance_id(state)
                    and same_resume(verification.get('resume_id'), resume.get('resume_id')) and same_workflow)
    invalidation = None
    invalidated = False
    invalidation_path = DATA/'invalidations'/f"{current.get('service_instance_id')}.json"
    if invalidation_path.exists():
        try:
            invalidation = read_json(invalidation_path)
            if invalidation.get('service_instance_id') == current.get('service_instance_id'):
                invalidated = datetime.fromisoformat(invalidation['observed_at']).timestamp() >= datetime.fromisoformat(verification.get('verified_at', '')).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            invalidated = True  # Unknown invalidation freshness cannot certify rendering.
    verified = verified and not invalidated
    current.update(readiness_invalidation=invalidation if invalidated else None, render_verified=verified,
                   readiness_verified_at=verification.get('verified_at'),
                   ready=bool(current['http_healthy'] and owned_listener and verified and not current['shutdown_pending']),
                   crossed_resume=bool((state.get('launch_resume_id') or verification.get('resume_id')) is not None and
                                       not same_resume(state.get('launch_resume_id') or verification.get('resume_id'), resume.get('resume_id'))),
                   stale={'instance_metadata': False, 'http': False, 'render_verification': not verified,
                          'resume': resume.get('source') == 'unknown'})
    current['healthy'] = current['ready']  # Compatibility, never HTTP-only liveness.
    return current


def queue_ids(current):
    if not current.get('queue_known'):
        return None
    return sorted({row[1] for key in ('queue_running','queue_pending')
                   for row in current.get('queue', {}).get(key, []) if len(row) > 1})


def _busy(current, message='Generation is active; wait or cancel its task before normal stop/restart'):
    return {**current, 'ready':False, 'healthy':False, 'busy':True,
            'state':'busy', 'approval_required':False, 'host_awake':True,
            'message':message, 'next_action':'wait_or_cancel_task'}


def _collect_members(state):
    live = _known_members(state)
    root = state.get('process') or {}
    pgid = state.get('process_group', root.get('pid'))
    # Only expand an existing verified session while an owned anchor is alive.
    if live:
        for proc in Path('/proc').iterdir():
            if proc.name.isdigit() and group(int(proc.name)) == (pgid, pgid):
                candidate = process_identity(int(proc.name))
                if candidate and candidate['uid'] == os.getuid() and int(candidate.get('start_ticks', 0)) >= int(root.get('start_ticks', 0)):
                    live[candidate['pid']] = candidate
    return live


def _signal_one(item, sig):
    if not process_matches(item):
        raise RuntimeError('Process identity changed immediately before signal')
    if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
        fd = os.pidfd_open(item['pid'])
        try:
            if not process_matches(item):
                raise RuntimeError('Process identity changed before pidfd signal')
            signal.pidfd_send_signal(fd, sig)
        finally:
            os.close(fd)
    else:
        os.kill(item['pid'], sig)


def incarnation_exited(item):
    if not item:
        return True
    if item.get('boot_id') and item['boot_id'] != boot_id():
        return True
    proc = Path('/proc')/str(item['pid'])
    try:
        fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
        return (fields[0] == 'Z' or fields[19] != str(item['start_ticks']) or proc.stat().st_uid != item['uid'])
    except (FileNotFoundError, ProcessLookupError):
        return True
    except (OSError, ValueError, KeyError, IndexError):
        return False  # Unreadable is not proof of termination.


def _snapshot_history(state, current):
    """Preserve already-finished SDK jobs before this in-memory history disappears."""
    state['active_prompt_ids'] = queue_ids(current)
    targets = set()
    for path in (DATA.parent/'runs').glob('*/task.json'):
        try:
            task = read_json(path)
            if task.get('service_instance_id') == instance_id(state) and task.get('prompt_id') and task.get('state') not in ('completed','failed','cancelled','interrupted_by_restart'):
                targets.add(task['prompt_id'])
        except (OSError, ValueError):
            continue
    state['retirement_history_snapshot'] = {}
    state['history_snapshot_complete'] = not targets
    if not targets:
        return
    try:
        history = CLIENT.request('/history')
        state['retirement_history_snapshot'] = {key:value for key,value in history.items() if key in targets}
        state['history_snapshot_at'] = now()
        state['history_snapshot_complete'] = True
    except Exception as exc:
        state['history_snapshot_error'] = str(exc)


def _retire(state):
    recorded = [state.get('process')] + state.get('managed_processes', [])
    if not all(incarnation_exited(item) for item in recorded):
        raise RuntimeError('Process incarnation is still present or unreadable; refusing retirement')
    ended = now()
    sid = instance_id(state)
    if sid:
        write_json(DATA/'retired-instances'/f'{sid}.json',
                   {**state, 'service_instance_id': sid, 'ended_at': ended, 'verified': True,
                    'exit_verified_at': ended, 'recorded_processes': recorded,
                    'history_snapshot': state.get('retirement_history_snapshot', {})})
    state.update(phase='stopped', stopped_at=ended, service_instance_id=sid, verification=None)
    write_json(STATE, state)


def _signal_targets(members, current, sig, root_pid):
    """Signal the application once; a non-TTY uv parent forwards INT/TERM.

    Signalling both child and uv delivers two interrupts to Python and can
    interrupt its atexit cleanup. The wrapper exits naturally with its child.
    KILL is not forwarded and must cover every verified owned member.
    """
    if sig == signal.SIGKILL:
        return list(members.values())
    owners = {owner['pid'] for listener in current.get('listeners', [])
              for owner in listener.get('owners', []) if owner.get('pid') in members}
    children = {pid for pid in members if pid != root_pid}
    targets = (owners & children) or owners or children or set(members)
    return [members[pid] for pid in sorted(targets)]


def _wait_for_shutdown(state, seconds):
    deadline = time.monotonic()+max(0, seconds)
    while True:
        if not _known_members(state):
            _retire(state)  # independently proves every recorded incarnation ended
            result = status()
            if result.get('listeners'):
                result.update(blocked=True, port_conflict=True,
                              message='Old instance ended; foreign listener prevents restart. No signal sent.')
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(.2)


def _stop(force=False):
    current = status()
    if not current['owned_process_running']:
        if current.get('process') and current.get('recorded_phase', current.get('phase')) in ('starting','running','stopping'):
            _retire(read_json(STATE))
            result = status()
            if result.get('listeners'):
                result.update(blocked=True, port_conflict=True, message='Old instance ended; foreign listener prevents restart. No signal sent.')
            return result
        if current.get('http_healthy') or current.get('listeners'):
            raise RuntimeError('No matching owned process; refusing to signal any PID')
        return current
    state = read_json(STATE)
    root = state.get('process') or {}
    pgid = state.get('process_group', root.get('pid'))
    if any(group(item['pid']) != (pgid, pgid) for item in _known_members(state).values()):
        raise RuntimeError('Session/process group mismatch; refusing to signal')
    if current['listeners'] and not current['owned_listener']:
        raise RuntimeError('Listener ownership mismatch; refusing to signal')
    shutdown = state.get('graceful_shutdown') or {}
    waiting = bool(state.get('phase') == 'stopping' and
                   shutdown.get('service_instance_id') == instance_id(state) and
                   (shutdown.get('queue_prompt_ids') == [] or shutdown.get('force_stop')))
    if not waiting and not force and queue_ids(current) != []:
        return _busy(current)
    state['service_instance_id'] = instance_id(state)
    original_jobs = shutdown.get('queue_prompt_ids') if waiting else queue_ids(current)
    steps = []
    if waiting:
        try:
            remaining = max(0, min(GRACEFUL_STOP_SECONDS, datetime.fromisoformat(shutdown['deadline_at']).timestamp()-time.time()))
        except (KeyError, TypeError, ValueError):
            remaining = 0
        ended = _wait_for_shutdown(state, remaining)
        if ended is not None:
            return ended
    else:
        steps.append((signal.SIGINT, 10 if force else GRACEFUL_STOP_SECONDS))
    # User policy: owned-instance shutdown timeout escalates automatically.
    # There is no per-incident approval or model-provided permission flag.
    steps += [(signal.SIGTERM, 5), (signal.SIGKILL, 5)]
    for sig, seconds in steps:
        current = status()
        if not _known_members(state):
            _retire(state)
            result = status()
            if result.get('listeners'):
                result.update(blocked=True, port_conflict=True, message='Old instance ended; foreign listener prevents restart')
            return result
        if current['listeners'] and not current['owned_listener']:
            raise RuntimeError('Listener ownership changed; refusing further signals')
        actual = queue_ids(current)
        if not force and actual is not None and set(actual)-set(original_jobs or []):
            return _busy(current, 'New generation appeared during shutdown; no further signal sent')
        members = _collect_members(state)
        if not members:
            _retire(state)
            return status()
        if any(group(item['pid']) != (pgid, pgid) for item in members.values()):
            raise RuntimeError('Process group changed before signal')
        state['managed_processes'] = list(members.values())
        if sig == signal.SIGINT:
            _snapshot_history(state, current)
        targets = _signal_targets(members, current, sig, root.get('pid'))
        state['phase'] = 'stopping'
        if sig == signal.SIGINT:
            state['graceful_shutdown'] = {
                'service_instance_id':state['service_instance_id'], 'started_at':now(),
                'deadline_at':datetime.fromtimestamp(time.time()+seconds,timezone.utc).isoformat(),
                'queue_prompt_ids':original_jobs, 'force_stop':force, 'signal_targets':targets}
        state.setdefault('signals', []).append({'at':now(), 'signal':sig.name,
            'pids':sorted(item['pid'] for item in targets), 'force_recovery':force,
            'policy':'automatic_owned_instance_timeout_escalation'})
        write_json(STATE,state)
        for item in sorted(targets,key=lambda item:item['pid']==root.get('pid')):
            if process_matches(item):
                try:
                    _signal_one(item,sig)
                except ProcessLookupError:
                    pass
        ended = _wait_for_shutdown(state,seconds)
        if ended is not None:
            return ended
    return {**status(), 'blocked':True, 'ready':False, 'healthy':False,
            'state':'blocked', 'approval_required':False,
            'message':'Owned process did not exit after SIGKILL; possible driver/kernel wait. No repeated restart or reboot.'}


def _start():
    current = status()
    if current.get('processes_exited') and STATE.exists():
        saved = read_json(STATE)
        if saved.get('phase') != 'stopped':
            _retire(saved)
            current = status()
    if current['http_healthy']:
        if not current['owned_listener']:
            raise RuntimeError('HTTP service is not an owned listener; refusing adoption')
        saved = read_json(STATE)
        if not saved.get('service_instance_id') or not saved.get('process', {}).get('boot_id'):
            saved['service_instance_id'] = instance_id(saved)
            saved['process'] = process_identity(saved['process']['pid'])
            saved['managed_processes'] = list(_collect_members(saved).values())
            write_json(STATE, saved)
            current = status()
        return {**current, 'reused_existing': True}
    if current['owned_process_running']:
        raise RuntimeError('Owned process is alive but HTTP-unhealthy; inspect or request recovery')
    if current['listeners']:
        raise RuntimeError(f'Port {PORT} has a LISTEN socket; refusing to launch or kill any process')
    before = fingerprints()
    for command in ([str(UV),'lock','--check','--offline'], [str(UV),'sync','--inexact','--dry-run','--locked','--offline']):
        result = subprocess.run(command, cwd=COMFY, text=True, capture_output=True, check=True, timeout=45)
        with (DATA/'preflight.log').open('a') as stream:
            stream.write(result.stdout+result.stderr)
        if 'Would make no changes' not in result.stderr+result.stdout and 'sync' in command:
            raise RuntimeError('uv preflight would change the environment; refusing to start')
    command = ['/usr/bin/nohup', str(RUN)]
    env = {**os.environ, 'PATH': str(UV.parent)+':'+os.environ.get('PATH','')}
    started = time.monotonic()
    retries_started = None
    while True:
        offset = LOG.stat().st_size if LOG.exists() else 0
        with LOG.open('ab', buffering=0) as log:
            process = subprocess.Popen(command, cwd=COMFY, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        original = process_identity(process.pid)
        if not original:
            raise RuntimeError('Launcher exited immediately; see '+str(LOG))
        state = {'phase':'starting', 'service_instance_id': str(uuid.uuid4()), 'started_at': now(),
                 'launch_resume_id': resume_evidence().get('resume_id'), 'process': original,
                 'managed_processes': [], 'process_group': process.pid, 'launch_command': command,
                 'cwd': str(COMFY), 'log': str(LOG), 'before_sha256': before}
        write_json(STATE, state)
        retry = False
        while time.monotonic()-started < 180:
            if process.poll() is not None:
                state.update(phase='failed', exit_code=process.returncode)
                write_json(STATE, state)
                with LOG.open('rb') as stream:
                    stream.seek(offset)
                    tail = stream.read()[-12000:].lower()
                address_in_use = b'address already in use' in tail or b'errno 98' in tail
                if address_in_use and not listeners():
                    retries_started = time.monotonic() if retries_started is None else retries_started
                    if time.monotonic()-retries_started < 15:
                        time.sleep(.5)
                        retry = True
                        break
                raise RuntimeError('ComfyUI launcher exited; see '+str(LOG))
            live = process_identity(process.pid)
            if not live or any(live[k] != original[k] for k in ('pid','uid','start_ticks','boot_id')):
                raise RuntimeError('Launcher identity changed; no signal sent')
            state['process'] = live  # exec may legitimately replace uv/nohup argv, never PID/start identity.
            state['managed_processes'] = list(_collect_members(state).values())
            write_json(STATE, state)
            result = status()
            if result['http_healthy'] and result['owned_listener']:
                after = fingerprints()
                state.update(phase='running', ready_at=now(), startup_seconds=round(time.monotonic()-started,3),
                             files_unchanged=before == after, after_sha256=after)
                write_json(STATE, state)
                if before != after:
                    raise RuntimeError('Original script changed protected project files; inspect state.json')
                return status()
            time.sleep(.5)
        if not retry:
            raise TimeoutError('No healthy owned listener after 180 seconds; process retained, inspect log')


def execution_seconds(item):
    messages = item.get('status', {}).get('messages', [])
    starts = [data.get('timestamp') for kind, data in messages if kind == 'execution_start']
    ends = [data.get('timestamp') for kind, data in messages if kind == 'execution_success']
    if starts and ends and isinstance(starts[0], (int,float)) and isinstance(ends[-1], (int,float)) and ends[-1] >= starts[0]:
        return (ends[-1]-starts[0])/1000
    return None


def _probe_read(path, receipt, folder, deadline, **kwargs):
    """Retry observations, never generation submission, within the probe budget."""
    while True:
        try:
            return CLIENT.request(path, **kwargs)
        except (OSError, APIError) as exc:
            if isinstance(exc, APIError) and exc.status < 500:
                raise
            receipt['read_retries'] = receipt.get('read_retries', 0)+1
            receipt['last_read_error'] = str(exc)
            write_json(folder/'receipt.json',receipt)
            if time.monotonic() >= deadline:
                raise TimeoutError('Smoke observation unavailable until its execution deadline; no generation was resubmitted') from exc
            if instance_id(read_json(STATE)) != receipt['service_instance_id']:
                raise RuntimeError('Service instance changed during smoke observation') from exc
            time.sleep(.25)


def _smoke(cold=False):
    current = status()
    if not current['http_healthy'] or not current['owned_listener']:
        return {**current, 'ready':False, 'smoke_failed':True, 'message':'Smoke requires an HTTP-alive owned service'}
    if queue_ids(current):
        return {**current, 'busy': True, 'message': 'Queue is busy; no smoke job was enqueued'}
    if queue_ids(current) is None:
        return {**current, 'ready': False, 'healthy': False, 'message': 'Queue is unknown; smoke was not submitted'}
    from comfy_workflow_api.api import WorkflowAPI
    api = WorkflowAPI(ROOT)
    snapshot, manifest = api._snapshot('qwen-image-2.1-t2i')
    probe_id = str(uuid.uuid4())
    seed = secrets.randbits(64)
    folder = DATA/'health'/probe_id
    folder.mkdir(parents=True)
    request = {'prompt': 'A red cube and a blue sphere on a white background. Diagnostic '+probe_id,
               'preset':'fast', 'seed':seed, 'output_size':{'mode':'explicit','width':64,'height':64}}
    prefix = 'comfy-workflow-api/health/'+probe_id+'/image'
    graph = compile_graph('qwen-image-2.1-t2i', snapshot, request, prefix=prefix)
    prompt_id = str(uuid.uuid4())
    receipt = {'probe_id':probe_id, 'prompt_id':prompt_id, 'service_instance_id': current['service_instance_id'],
               'resume_id':current['resume']['resume_id'], 'workflow_hash':workflow_hash(), 'snapshot':manifest,
               'request':request, 'created_at':now(), 'deadline_seconds':120 if cold else 30, 'cold':cold,
               'success':False, 'submission_uncertain':True}
    write_json(folder/'prompt.json', graph)
    write_json(folder/'receipt.json', receipt)  # Persist ID before POST, never retry.
    try:
        result = CLIENT.request('/prompt','POST', {'prompt':graph,'client_id':probe_id,'prompt_id':prompt_id})
        if result.get('node_errors') or result.get('prompt_id') != prompt_id:
            raise ValueError('Smoke graph rejected: '+str(result))
        receipt.update(submission_uncertain=False, submitted_at=now())
        write_json(folder/'receipt.json', receipt)
        started = None
        queued_at = time.monotonic()
        while True:
            if instance_id(read_json(STATE)) != receipt['service_instance_id']:
                raise RuntimeError('Service instance changed during smoke')
            deadline = (started if started is not None else queued_at)+receipt['deadline_seconds']
            item = _probe_read('/history/'+prompt_id, receipt, folder, deadline).get(prompt_id)
            if item:
                write_json(folder/'history.json', item)
                messages = item.get('status', {}).get('messages', [])
                cached = [node for kind, data in messages if kind == 'execution_cached' for node in data.get('nodes', [])]
                if '458' in cached:
                    raise RuntimeError('Sampler was cached; smoke does not prove GPU execution')
                if item.get('status', {}).get('status_str') == 'error':
                    raise RuntimeError('Smoke execution failed; see history')
                if item.get('status', {}).get('completed'):
                    duration = execution_seconds(item)
                    duration = duration if duration is not None else time.monotonic()-(started if started is not None else queued_at)
                    if duration > receipt['deadline_seconds']:
                        raise TimeoutError('Smoke finished after its execution deadline')
                    receipt['execution_seconds'] = round(duration,3)
                    images = [image for value in item.get('outputs', {}).values() for image in value.get('images', [])]
                    if not images:
                        raise RuntimeError('Smoke completed without a saved PNG')
                    image = images[0]
                    if probe_id not in image.get('subfolder',''):
                        raise RuntimeError('Smoke output does not belong to this probe')
                    query = {key:image[key] for key in ('filename','subfolder','type')}
                    payload = _probe_read('/view?'+urllib.parse.urlencode(query), receipt, folder, deadline, binary=True)
                    if png_size(payload) != (64,64):
                        raise RuntimeError('Smoke output is not 64x64')
                    server_path = COMFY/'output'/image['subfolder']/image['filename']
                    if not server_path.is_file() or server_path.read_bytes() != payload:
                        raise RuntimeError('Smoke PNG was not durably saved at the server output path')
                    (folder/'output.png').write_bytes(payload)
                    receipt.update(success=True, verified_at=now(), server_file=query,
                                   sha256=hashlib.sha256(payload).hexdigest(),
                                   execution_seconds=receipt['execution_seconds'])
                    break
            queue = _probe_read('/queue', receipt, folder, deadline)
            running = any(row[1] == prompt_id for row in queue.get('queue_running', []))
            pending = any(row[1] == prompt_id for row in queue.get('queue_pending', []))
            if running and started is None:
                started = time.monotonic()
                receipt['execution_started_at'] = now()
            # Never count other jobs' wait as GPU timeout. Withdraw only our own pending smoke.
            if pending and any(row[1] != prompt_id for row in queue.get('queue_running', [])):
                CLIENT.request('/api/jobs/'+prompt_id+'/cancel','POST',{})
                receipt['cancel_requested'] = True
                raise RuntimeError('Another job started first; smoke withdrawn without interrupting it')
            if started is not None and time.monotonic()-started >= receipt['deadline_seconds']:
                try:
                    CLIENT.request('/api/jobs/'+prompt_id+'/cancel','POST',{})
                    receipt['cancel_requested'] = True
                except Exception as exc:
                    receipt['cancel_error'] = str(exc)
                raise TimeoutError('Smoke execution exceeded deadline; cooperative cancellation requested, completion unconfirmed')
            if started is None and time.monotonic()-queued_at > 30:
                try:
                    CLIENT.request('/api/jobs/'+prompt_id+'/cancel', 'POST', {})
                    receipt['cancel_requested'] = True
                except Exception as exc:
                    receipt['cancel_error'] = str(exc)
                raise TimeoutError('Smoke did not start within 30 seconds; GPU health unknown, own-job cancellation requested')
            if not running and not pending and not item:
                raise RuntimeError('Smoke disappeared from queue/history; submission outcome unknown')
            write_json(folder/'receipt.json', receipt)
            time.sleep(.25)
    except Exception as exc:
        configuration_failure = isinstance(exc, (ValueError, FileNotFoundError)) or (isinstance(exc, APIError) and 400 <= exc.status < 500)
        receipt.update(error=str(exc), failed_at=now(), failure_kind='configuration' if configuration_failure else 'execution_or_unknown')
    write_json(folder/'receipt.json', receipt)
    state = read_json(STATE)
    if instance_id(state) == receipt['service_instance_id']:
        state.update(service_instance_id=receipt['service_instance_id'], verification=receipt)
        write_json(STATE, state)
    # Retain only this private namespace; no user task/output cleanup is performed here.
    folders = sorted((DATA/'health').iterdir(), key=lambda p:p.stat().st_mtime, reverse=True)
    import shutil
    for expired in folders[10:]:
        if expired.is_dir() and (expired/'receipt.json').exists():
            old = read_json(expired/'receipt.json')
            if old.get('success'):
                server_dir = COMFY/'output/comfy-workflow-api/health'/expired.name
                if server_dir.is_dir() and server_dir.name == expired.name:
                    shutil.rmtree(server_dir)
            shutil.rmtree(expired)
    result = status()
    result['smoke'] = receipt
    if not receipt['success']:
        result.update(ready=False, healthy=False, smoke_failed=True)
    return result


def _ensure_ready():
    current = status()
    cold = not current['render_verified']
    if current.get('shutdown_pending'):
        stopped = _stop()
        if stopped.get('busy') or stopped.get('blocked'):
            return stopped
        _start()
        cold = True
    elif current['owned_process_running'] and current.get('crossed_resume'):
        if queue_ids(current) is None or queue_ids(current):
            return {**current, 'busy':True, 'ready':False, 'healthy':False,
                    'message':'Owned instance predates resume but queue is busy/unknown; no restart or smoke'}
        stopped = _stop()
        if stopped.get('busy') or stopped.get('blocked'):
            return stopped
        _start()
        cold = True
    else:
        _start()
    current = status()
    if current['ready']:
        return {**current, 'reused_existing':True}
    if queue_ids(current):
        return {**current, 'busy':True, 'message':'Queue is busy; readiness not yet verified'}
    return _smoke(cold=cold)


def _recover():
    before = status()
    if before.get('ready') and queue_ids(before):
        return _busy(before, 'Healthy generation is active; wait or cancel the task before recovery')
    try:
        result = _ensure_ready()
    except Exception as exc:
        current = status()
        if isinstance(exc, (ValueError, FileNotFoundError, subprocess.CalledProcessError)) or not current.get('owned_process_running') or current.get('listeners') and not current.get('owned_listener'):
            return {**current, 'blocked':True, 'ready':False, 'healthy':False, 'failure_kind':'configuration_or_ownership', 'message':str(exc)}
        result = {**current,'ready':False,'message':str(exc)}
    if result.get('ready') and 'smoke' not in result:
        if queue_ids(result) != []:
            return _busy(result)
        result = _smoke(cold=False)
    if result.get('ready'):
        return result
    if result.get('smoke',{}).get('failure_kind') == 'configuration':
        return {**result, 'blocked':True, 'message':'Smoke configuration rejected; fix workflow/nodes rather than restarting'}
    current = status()
    if current.get('service_instance_id') != before.get('service_instance_id'):
        return {**result, 'blocked':True, 'message':'One startup/restart was already attempted; no repeated recovery'}
    if not current.get('owned_process_running'):
        return {**result, 'blocked':True, 'message':'No owned live process remains; query status and use ensure_ready'}
    stopped = _stop(force=True)
    if stopped.get('blocked'):
        return stopped
    _start()
    result = _smoke(cold=True)
    if not result.get('ready'):
        result.update(blocked=True, message='One automatic recovery attempt completed but rendering still fails; no repeated restart/reboot')
    return result


@contextmanager
def admission(operation_id):
    gate = DATA/'admission.json'
    with lock('admission.lock'):
        if gate.exists():
            prior = read_json(gate)
            if prior.get('blocked',True) and prior.get('operation_id') != operation_id:
                if not prior.get('owner') or process_matches(prior['owner']):
                    raise RuntimeError('Another service operation blocks new submissions')
        inflight = []
        for path in (DATA/'inflight').glob('*.json'):
            reservation = read_json(path)
            if not reservation.get('owner') or process_matches(reservation['owner']):
                inflight.append(reservation)
        if inflight:
            raise RuntimeError('SDK submissions are in flight; retry recovery after their bounded operations complete')
        write_json(gate, {'blocked':True, 'operation_id':operation_id, 'owner':process_identity(os.getpid()), 'created_at':now()})
    try:
        yield
    finally:
        with lock('admission.lock'):
            if gate.exists() and read_json(gate).get('operation_id') == operation_id:
                gate.unlink()


def execute(action, operation_id=None):
    if action == 'status':
        return status()
    if action not in ('start','stop','restart','ensure_ready','recover'):
        raise ValueError('Unsupported service action')
    operation_id = operation_id or str(uuid.uuid4())
    with lock(), admission(operation_id):
        if STATE.exists():
            state = read_json(STATE)
            state['operation_id'] = operation_id
            write_json(STATE,state)
        if action == 'start':
            return _ensure_ready()
        if action == 'stop':
            return _stop()
        if action == 'restart':
            result = _stop()
            if result.get('busy') or result.get('blocked'):
                return result
            _start()
            return _smoke(cold=True)
        if action == 'ensure_ready':
            return _ensure_ready()
        return _recover()


def start():
    return execute('start')


def stop():
    return execute('stop')


def ensure_ready():
    return execute('ensure_ready')


def recover():
    return execute('recover')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start','stop','restart','status','ensure_ready','recover'])
    args = parser.parse_args()
    try:
        print(json.dumps(execute(args.action), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'error':str(exc),'log':str(LOG)}), file=sys.stderr)
        sys.exit(1)
