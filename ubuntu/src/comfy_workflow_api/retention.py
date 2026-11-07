"""Count-based cleanup confined to this module's task namespaces."""
from contextlib import contextmanager
import fcntl
import json
import shutil
import uuid
from pathlib import Path
from .storage import now, read_json


@contextmanager
def cleanup_lock(data):
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.cleanup.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def result_pin(directory):
    return directory / '.result-pinned'


def _safe_root(path, parent):
    path = Path(path).absolute()
    if path.name != 'comfy-workflow-api' or path.parent.name != parent or path.resolve() != path:
        raise ValueError(f'Cleanup requires a real {parent}/comfy-workflow-api directory: {path}')
    return path


def cleanup(api, dry_run=False, protect_task=None):
    settings = api.settings['retention']
    limit = settings['max_tasks']
    if type(limit) is not int or limit < 1:
        raise ValueError('retention.max_tasks must be a positive integer')
    # The API may target another box; filesystem cleanup must target this host.
    from urllib.parse import urlparse
    if api.client.base_url != api.settings['base_url'] or urlparse(api.client.base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Local cleanup requires the configured loopback ComfyUI URL')
    output = _safe_root(settings['comfy_output_dir'], 'output')
    inputs = _safe_root(settings['comfy_input_dir'], 'input')
    queue = api.client.request('/queue')  # fail closed when queue cannot be checked
    if not isinstance(queue, dict) or any(not isinstance(queue.get(k), list) for k in ('queue_running', 'queue_pending')):
        raise ValueError('Queue state is unknown; cleanup refused')
    active = {row[1] for key in ('queue_running', 'queue_pending') for row in queue.get(key, [])}
    candidates = []
    for directory in (api.data / 'runs').glob('*'):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            task_id = directory.name
            if str(uuid.UUID(task_id)) != task_id:
                continue
            task = read_json(directory / 'task.json')
            if task.get('task_id') != task_id or task.get('base_url') != api.client.base_url:
                continue
            if (task.get('state') not in ('completed', 'failed', 'cancelled', 'interrupted_by_restart')
                    or task.get('prompt_id') in active or result_pin(directory).exists()):
                continue
            # Keep completed jobs that have not yet been downloaded.
            if task['state'] == 'completed' and not read_json(directory / 'result.json').get('outputs'):
                continue
            candidates.append((task['created_at'], task_id, directory))
        except (ValueError, KeyError, OSError):
            continue
    excess = max(0, len(candidates) - limit)
    selected = [item for item in sorted(candidates) if item[1] != protect_task][:excess]
    removed = []
    skipped = []
    for _, task_id, directory in selected:
        paths = [directory, api.data / 'inputs' / task_id, inputs / task_id, output / task_id,
                 api.data / 'task-observations' / (task_id + '.json')]
        # Compatibility with the previous flat filename prefix. No broad globbing.
        for name in ('history.json', 'result.json'):
            path = directory / name
            if not path.exists():
                continue
            value = read_json(path)
            images = value.get('outputs', {}).get('save', {}).get('images', []) if name == 'history.json' else [o.get('server_file', {}) for o in value.get('outputs', [])]
            for image in images:
                filename = image.get('filename', '')
                if image.get('subfolder') == 'comfy-workflow-api' and image.get('type') == 'output' and Path(filename).name == filename and filename.startswith(task_id + '_'):
                    paths.append(output / filename)
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
    if not dry_run and removed:
        with (api.data / 'cleanup.log').open('a') as stream:
            stream.write(json.dumps({'time': now(), **result}) + '\n')
    return result
