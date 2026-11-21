"""Schedule Ubuntu housekeeping and coordinate a full local cleanup."""
from contextlib import contextmanager
import logging
import threading

from .log_retention import rotate_logs
from ..retention import cleanup_tasks, local_namespaces, prune_batches, record_cleanup
from ..storage import cleanup_lock, read_json


@contextmanager
def automatic_cleanup(api):
    if not api.settings['retention']['auto_cleanup']:
        yield
        return
    stop = threading.Event()

    def maintain():
        while not stop.is_set():
            try:
                cleanup(api)
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


def _prune_retired_instances(api, limit, removed, dry_run):
    retired = []
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

    selected = [path for _, path in sorted(retired)[:-limit]]
    if not dry_run:
        for path in selected:
            path.unlink()
    return [path.stem for path in selected]


def cleanup(api, dry_run=False):
    with cleanup_lock(api.data):
        namespaces = local_namespaces(api)
        logs = rotate_logs(api, dry_run)
        result = cleanup_tasks(api, namespaces, dry_run=dry_run)
        removed = set(result['task_ids'])
        limit = result['max_tasks']
        result.update(logs, batch_ids=prune_batches(api, limit, removed, dry_run),
                      retired_instance_ids=_prune_retired_instances(api, limit, removed, dry_run))
        if not dry_run and (removed or result['batch_ids'] or result['retired_instance_ids']):
            record_cleanup(api.data, result)
        return result
