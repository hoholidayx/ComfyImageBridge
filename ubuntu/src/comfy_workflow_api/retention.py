"""Retention of workflow tasks, batch records and their verified artifacts."""
import json
import logging
import os
from pathlib import Path
import shutil
import stat
from urllib.parse import urlparse

from .storage import is_job_id, now, read_json


def result_pin(directory):
    return directory / '.result-pinned'


def _safe_root(path, parent):
    path = Path(path).absolute()
    if path.name != 'comfy-workflow-api' or path.parent.name != parent or path.resolve() != path:
        raise ValueError(f'Cleanup requires a real {parent}/comfy-workflow-api directory: {path}')
    return path


def record_cleanup(data, entry):
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with os.fdopen(os.open(data / 'cleanup.log', flags, 0o600), 'a') as stream:
            info = os.fstat(stream.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                stream.write(json.dumps({'time': now(), **entry}) + '\n')
    except OSError as exc:
        logging.getLogger(__name__).warning('Unable to append cleanup log: %s', exc)


def prune_batches(api, limit, removed, dry_run):
    batches = []
    for path in (api.data / 'batches').glob('*/batch.json'):
        if not is_job_id(path.parent.name) or path.resolve() != path:
            continue
        try:
            batch = read_json(path)
            children = [child['task_id'] for child in batch['children']]
            if (batch['task_id'] == path.parent.name and isinstance(batch.get('created_at'), str) and
                    (len(children) == batch['count'] or batch.get('submission_stopped')) and
                    all(is_job_id(child) and (child in removed or not (api.data/'runs'/child).exists()) for child in children)):
                batches.append((batch['created_at'], path.parent))
        except (OSError, ValueError, KeyError, TypeError):
            continue

    selected = [folder for _, folder in sorted(batches)[:-limit]]
    if not dry_run:
        for folder in selected:
            shutil.rmtree(folder)
    return [folder.name for folder in selected]


def local_namespaces(api):
    settings = api.settings['retention']
    limit = settings['max_tasks']
    if type(limit) is not int or limit < 1:
        raise ValueError('retention.max_tasks must be a positive integer')
    # The API may target another box; filesystem cleanup must target this host.
    if api.client.base_url != api.settings['base_url'] or urlparse(api.client.base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Local cleanup requires the configured loopback ComfyUI URL')
    output = _safe_root(settings['comfy_output_dir'], 'output')
    inputs = _safe_root(settings['comfy_input_dir'], 'input')
    return output, inputs


def cleanup_tasks(api, namespaces, *, dry_run=False, protect_task=None):
    output, inputs = namespaces
    limit = api.settings['retention']['max_tasks']
    queue = api.client.request('/queue', timeout=3)  # fail closed when queue cannot be checked
    if not isinstance(queue, dict) or any(not isinstance(queue.get(k), list) for k in ('queue_running', 'queue_pending')):
        raise ValueError('Queue state is unknown; cleanup refused')
    rows = queue['queue_running'] + queue['queue_pending']
    if any(not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[1], str) for row in rows):
        raise ValueError('Queue state is unknown; cleanup refused')
    active = {row[1] for row in rows}
    candidates = []
    for directory in (api.data / 'runs').glob('*'):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            task_id = directory.name
            if not is_job_id(task_id):
                continue
            task = read_json(directory / 'task.json')
            if (task.get('task_id') != task_id or task.get('base_url') != api.client.base_url
                    or not isinstance(task.get('created_at'), str)):
                continue
            if (task.get('state') not in ('completed', 'failed', 'cancelled', 'interrupted_by_restart')
                    or task.get('prompt_id') in active or result_pin(directory).exists()):
                continue
            # Keep completed jobs that have not yet been downloaded.
            if (task['state'] == 'completed' and not (directory / '.result-discarded').is_file()
                    and not read_json(directory / 'result.json').get('outputs')):
                continue
            candidates.append((task['created_at'], task_id, directory))
        except (ValueError, KeyError, OSError, TypeError, AttributeError):
            continue
    excess = max(0, len(candidates) - limit)
    selected = [item for item in sorted(candidates) if item[1] != protect_task][:excess]
    removed = []
    skipped = []
    for _, task_id, directory in selected:
        paths = [directory, api.data / 'inputs' / task_id, inputs / task_id, output / task_id,
                 api.data / 'task-observations' / (task_id + '.json')]
        # Compatibility with the previous flat filename prefix. No broad globbing.
        try:
            for name in ('history.json', 'result.json'):
                path = directory / name
                if not path.exists():
                    continue
                if path.is_symlink():
                    raise ValueError('Linked receipt')
                value = read_json(path)
                images = value.get('outputs', {}).get('save', {}).get('images', []) if name == 'history.json' else [o.get('server_file', {}) for o in value.get('outputs', [])]
                for image in images:
                    filename = image.get('filename', '')
                    if isinstance(filename, str) and image.get('subfolder') == 'comfy-workflow-api' and image.get('type') == 'output' and Path(filename).name == filename and filename.startswith(task_id + '_'):
                        paths.append(output / filename)
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            skipped.append(task_id)
            continue
        if any(path.is_symlink() or (path.exists() and path.resolve() != path.absolute()) for path in paths):
            skipped.append(task_id)
            continue
        if not dry_run:
            # Delete remote copies first; keep the task manifest if any deletion fails.
            for path in list(dict.fromkeys(paths[1:])) + [directory]:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
        removed.append(task_id)
    result = {'dry_run': dry_run, 'max_tasks': limit, 'eligible_tasks': len(candidates),
              'task_ids': removed, 'skipped_task_ids': skipped}
    return result


def cleanup_after_fetch(api, result, task_id):
    if api.settings.get('retention', {}).get('auto_cleanup', False):
        try:
            result['cleanup'] = cleanup_tasks(api, local_namespaces(api), protect_task=task_id)
            if result['cleanup']['task_ids']:
                record_cleanup(api.data, result['cleanup'])
        except Exception as exc:
            result['cleanup'] = {'error': str(exc)}
            record_cleanup(api.data, result['cleanup'])
