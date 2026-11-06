"""Short admission transactions shared by generation and the service manager."""
from contextlib import contextmanager
import fcntl
import os
from .storage import now, read_json, write_json

# Shared without importing the sleep CLI during package initialization.
ANY_EPOCH = object()


def service_state(api):
    path = api.data/'comfyui-service/state.json'
    try:
        return read_json(path) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def instance_id(api):
    return service_state(api).get('service_instance_id')


def retired(api, service_instance_id):
    if not service_instance_id:
        return None
    for path in (api.data/'comfyui-service/retired-instances').glob('*.json'):
        try:
            value = read_json(path)
            if value.get('service_instance_id') == service_instance_id and value.get('verified') is True:
                return value
        except (OSError, ValueError):
            continue
    return None


@contextmanager
def submission(api, task_id, operation_id):
    from .sleep_guard import identity
    folder = api.data/'comfyui-service'
    folder.mkdir(parents=True, exist_ok=True)
    reservation = folder/'inflight'/f'{operation_id}.json'
    with (folder/'admission.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        from .sleep_cycle import assert_allowed
        assert_allowed(api)
        gate = folder/'admission.json'
        if gate.exists():
            value = read_json(gate)
            if value.get('blocked', True):
                raise RuntimeError(f"Service management in progress ({value.get('operation_id', 'unknown')}); no generation submitted")
        service_instance_id = instance_id(api)
        write_json(reservation, {'operation_id': operation_id, 'task_id': task_id,
            'owner': identity(os.getpid()), 'created_at': now(), 'service_instance_id': service_instance_id})
    try:
        yield service_instance_id
    finally:
        with (folder/'admission.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            reservation.unlink(missing_ok=True)


def invalidate(api, state, reason):
    """Record fresh failed-render evidence without racing service state writes."""
    service_instance_id = state.get('service_instance_id')
    if not isinstance(service_instance_id, str) or not service_instance_id or '/' in service_instance_id or '\\' in service_instance_id or '\0' in service_instance_id:
        return None
    folder = api.data/'comfyui-service'
    folder.mkdir(parents=True, exist_ok=True)
    observed = now()
    value = {'service_instance_id':service_instance_id, 'observed_at':observed,
             'task_id':state.get('task_id'), 'prompt_id':state.get('prompt_id'), 'reason':reason}
    with (folder/'invalidation.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        path = folder/'invalidations'/f'{service_instance_id}.json'
        previous = read_json(path) if path.exists() else {}
        if not previous.get('observed_at') or previous['observed_at'] <= observed:
            write_json(path, value)
    return value
