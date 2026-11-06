"""Conservative progress diagnostics. Missing telemetry is never zero utilization."""
from datetime import datetime
from pathlib import Path
from .storage import now, read_json, write_json

TERMINAL = frozenset(('completed', 'failed', 'cancelled', 'interrupted_by_restart'))


def seconds(start, end=None):
    if not start:
        return 0.0
    try:
        return max(0.0, (datetime.fromisoformat(end or now()) - datetime.fromisoformat(start)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def decorate(api, state):
    observed = now()
    end = state.get('completed_at') or state.get('ended_at') or observed
    evidence_path = api.data/'task-observations'/f"{state['task_id']}.json"
    evidence = read_json(evidence_path) if evidence_path.exists() else {}
    same = (evidence.get('prompt_id') == state.get('prompt_id') and
            evidence.get('service_instance_id') == state.get('service_instance_id'))
    if same and evidence.get('running_since'):
        if not state.get('running_since') or evidence['running_since'] < state['running_since']:
            state['running_since'] = evidence['running_since']
    started = state.get('running_since')
    state.update(observed_at=observed,
                 elapsed_seconds=round(seconds(state.get('created_at'), end), 3),
                 queued_seconds=round(seconds(state.get('submitted_at'), started or end), 3),
                 running_seconds=round(seconds(started, end), 3),
                 last_progress_at=state.get('last_progress_at'), stalled=None)
    if state.get('state') not in ('running', 'cancel_requested'):
        state['stalled'] = False
    elif seconds(started, observed) < 300:
        state['stalled'] = False
    elif (evidence.get('prompt_id') == state.get('prompt_id') and
          evidence.get('service_instance_id') == state.get('service_instance_id') and
          seconds(evidence.get('observed_at'), observed) <= 15):
        state['stall_evidence'] = evidence
        state['last_progress_at'] = evidence.get('last_progress_at') or state.get('last_progress_at')
        if evidence.get('gpu_utilization_percent') is not None and evidence.get('log_available'):
            state['stalled'] = bool(evidence.get('low_gpu_samples', 0) >= 3 and
                seconds(evidence.get('low_gpu_since'), observed) >= 120 and
                seconds(evidence.get('last_progress_at'), observed) >= 120)
    state['stall_reason'] = ('suspected_stall; not proof of a dead GPU and never authorizes termination'
                             if state['stalled'] else 'insufficient_evidence' if state['stalled'] is None else None)
    return state


def gpu_utilization():
    values, errors = [], []
    # Only AMD cards exposing the appropriate metric are considered. Read failures
    # (notably EBUSY after resume) remain unknown, not 0%.
    paths = list(Path('/sys/class/drm').glob('card[0-9]*/device/gpu_busy_percent'))
    for path in paths:
        try:
            value = int(path.read_text().strip())
            if not 0 <= value <= 100:
                raise ValueError('GPU utilization outside 0..100')
            values.append(value)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    return (max(values) if values and not errors else None), errors or (['GPU metric unavailable'] if not values else [])


def observe(api, queue):
    """Called by the existing inhibitor worker; never launches another observer."""
    gpu, errors = gpu_utilization()
    log = api.data/'comfyui-service/comfyui.log'
    try:
        info = log.stat()
        log_mark = [info.st_ino, info.st_size, info.st_mtime_ns]
    except OSError:
        log_mark = None
    running = {row[1] for row in queue.get('queue_running', [])}
    for folder in (api.data/'runs').glob('*'):
        path = folder/'task.json'
        try:
            task = read_json(path)
            if task.get('prompt_id') not in running:
                continue
            evidence_path = api.data/'task-observations'/f"{task['task_id']}.json"
            previous = read_json(evidence_path) if evidence_path.exists() else {}
            at = now()
            same = previous.get('prompt_id') == task['prompt_id'] and previous.get('service_instance_id') == task.get('service_instance_id')
            unchanged = same and previous.get('log_mark') == log_mark and log_mark is not None
            recent = same and seconds(previous.get('observed_at'), at) <= 15
            low = gpu is not None and gpu <= 1
            low_continues = low and recent and previous.get('gpu_utilization_percent') is not None and previous['gpu_utilization_percent'] <= 1
            value = {'task_id': task['task_id'], 'prompt_id': task['prompt_id'],
                'service_instance_id': task.get('service_instance_id'), 'observed_at': at,
                'running_since': previous.get('running_since', at) if same else at,
                'last_progress_at': previous.get('last_progress_at', at) if unchanged else at,
                'log_mark': log_mark, 'log_available': log_mark is not None,
                'progress_source': 'managed_log_change; conservative whole-service signal',
                'gpu_utilization_percent': gpu, 'gpu_errors': errors,
                'low_gpu_since': previous.get('low_gpu_since') if low_continues else at if low else None,
                'low_gpu_samples': previous.get('low_gpu_samples', 0)+1 if low_continues else 1 if low else 0}
            write_json(evidence_path, value)
        except (OSError, ValueError, KeyError, TypeError):
            continue
