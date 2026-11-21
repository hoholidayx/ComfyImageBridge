"""Count-based cleanup confined to this module's task namespaces."""
from contextlib import contextmanager
import fcntl
import logging
import shutil
import threading
from pathlib import Path
from .storage import is_job_id, read_json
from .log_retention import record_cleanup, rotate_logs


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


@contextmanager
def automatic_cleanup(api):
    if not api.settings['retention']['auto_cleanup']:
        yield
        return
    stop = threading.Event()

    def maintain():
        while not stop.is_set():
            try:
                api.cleanup()
            except (OSError, ValueError, RuntimeError) as exc:
                logging.getLogger(__name__).warning('Retention deferred: %s', exc)
            stop.wait(300)

    thread = threading.Thread(target=maintain, name='bridge-retention')
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def prune_probes(home, outputs, active, keep=10):
    if active is None or home.resolve() != home or outputs.resolve() != outputs:
        return
    candidates = []
    for folder in home.glob('*'):
        if folder.is_symlink() or not folder.is_dir() or not is_job_id(folder.name):
            continue
        try:
            receipt = read_json(folder/'receipt.json')
            if (receipt['probe_id'] == folder.name and is_job_id(receipt['prompt_id']) and
                    receipt['prompt_id'] not in active and isinstance(receipt.get('created_at'), str) and
                    (receipt.get('success') or receipt.get('failed_at'))):
                candidates.append((receipt['created_at'], folder))
        except (ValueError, KeyError, OSError, TypeError):
            continue
    for _, folder in sorted(candidates)[:-keep]:
        output = outputs/folder.name
        if output.is_symlink():
            continue
        if output.is_dir():
            shutil.rmtree(output)
        shutil.rmtree(folder)


def _prune_records(api, limit, removed, dry_run):
    batches = []
    retired = []
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

    references = set()
    service = api.data/'comfyui-service'
    try:
        if (service/'state.json').exists():
            references.add(read_json(service/'state.json').get('service_instance_id'))
        for path in (api.data/'runs').glob('*/task.json'):
            if path.parent.name not in removed:
                references.add(read_json(path).get('service_instance_id'))
        for path in (service/'retired-instances').glob('*.json'):
            if path.resolve() != path or path.stem in references:
                continue
            item = read_json(path)
            if (item.get('verified') is True and item.get('service_instance_id') == path.stem
                    and isinstance(item.get('ended_at'), str)):
                retired.append((item['ended_at'], path))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        retired.clear()

    selected_batches = [folder for _, folder in sorted(batches)[:-limit]]
    selected_instances = [path for _, path in sorted(retired)[:-limit]]
    if not dry_run:
        for folder in selected_batches:
            shutil.rmtree(folder)
        for path in selected_instances:
            path.unlink()
    return {'batch_ids': [folder.name for folder in selected_batches],
            'retired_instance_ids': [path.stem for path in selected_instances]}


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
    logs = rotate_logs(api, dry_run) if protect_task is None else {}
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
    if protect_task is None:
        result.update(logs)
        result.update(_prune_records(api, limit, set(removed), dry_run))
    if not dry_run and (removed or result.get('batch_ids') or result.get('retired_instance_ids')):
        record_cleanup(api.data, result)
    return result
