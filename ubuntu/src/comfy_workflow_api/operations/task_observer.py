"""Collect host progress evidence for the generation status reader."""
from pathlib import Path

from ..storage import now, read_json, write_json
from ..task_diagnostics import seconds


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
