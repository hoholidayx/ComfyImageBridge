"""A batch queues independent jobs; it never increases the GPU latent batch size."""
import copy
import re
import uuid
from .contract import normalize
from .metadata import metadata
from .storage import now, read_json, write_json


def directory(api, task_id):
    if str(uuid.UUID(task_id)) != task_id:
        raise ValueError('Invalid batch UUID')
    return api.data / 'batches' / task_id


def submit(api, alias, request, count=1):
    if type(count) is not int or not 1 <= count <= 4:
        raise ValueError('count must be an integer from 1 to 4')
    effective = normalize(request, alias)
    effective.setdefault('output_size', {'mode': 'follow_image'} if alias.endswith('edit') else {'mode': 'explicit', 'width': 1024, 'height': 1024})
    if alias.endswith('edit'):
        effective.setdefault('reference_resolution', 1024)
    if effective['seed'] + count - 1 >= 2**64:
        raise ValueError('seed + count - 1 exceeds unsigned 64-bit range')
    api.compile(alias, effective)  # reject the complete request before any job is submitted
    task_id = str(uuid.uuid4())
    folder = directory(api, task_id)
    folder.mkdir(parents=True)
    batch = {'task_id': task_id, 'workflow': alias, 'state': 'submitting', 'count': count,
             'created_at': now(), 'children': [], 'request_used': effective,
             'seed_used': effective['seed'], 'metadata': metadata(api.root)}
    write_json(folder/'batch.json', batch)
    for index in range(count):
        child_request = {**copy.deepcopy(effective), 'seed': effective['seed'] + index}
        try:
            child = api.submit(alias, child_request)
            batch['children'].append({'task_id': child['task_id'], 'seed_used': child_request['seed'], 'state': child['state']})
        except Exception as exc:
            match = re.search(r'Task ([0-9a-f-]{36}):', str(exc))
            if match:
                batch['children'].append({'task_id': match.group(1), 'seed_used': child_request['seed'], 'state': 'submission_uncertain'})
            batch['submission_error'] = str(exc)
            batch['submission_stopped'] = True
            write_json(folder/'batch.json', batch)
            break  # never submit remaining children after an uncertain response
        write_json(folder/'batch.json', batch)
    try:
        return status(api, task_id)
    except Exception as exc:
        raise RuntimeError(f'Task {task_id}: batch acceptance/state uncertain; query this parent ID, do not resubmit: {exc}') from exc


def status(api, task_id):
    folder = directory(api, task_id)
    batch = read_json(folder/'batch.json')
    for child in batch['children']:
        try:
            state = api.status(child['task_id'])
            child.update({key: state.get(key) for key in ('state', 'elapsed_seconds', 'queued_seconds', 'running_seconds', 'last_progress_at', 'stalled', 'stall_reason', 'observed_at', 'cancel_requested_at', 'cancellation_confirmed') if key in state})
            child.update(snapshot=state.get('snapshot'), model_config=state.get('model_config'), sampling_config=state.get('sampling_config'))
            if state.get('error'):
                child['error'] = state['error']
        except FileNotFoundError:
            child.update(state='expired', error='Child task removed by retention')
    states = [c['state'] for c in batch['children']]
    if any(s in ('queued', 'running', 'submitting', 'preparing', 'cancel_requested') for s in states):
        batch['state'] = 'running'
    elif states and all(s == 'cancelled' for s in states):
        batch['state'] = 'cancelled'
    elif states and all(s == 'interrupted_by_restart' for s in states):
        batch['state'] = 'interrupted_by_restart'
    elif 'submission_uncertain' in states:
        batch['state'] = 'submission_uncertain'
    elif len(states) == batch['count'] and all(s == 'completed' for s in states):
        batch['state'] = 'completed'
    elif 'completed' in states:
        batch['state'] = 'partial'
    else:
        batch['state'] = 'failed'
    from .task_diagnostics import seconds
    batch.update(observed_at=now(), elapsed_seconds=round(seconds(batch['created_at']), 3),
                 stalled=True if any(c.get('stalled') is True for c in batch['children']) else None if any(c.get('stalled') is None for c in batch['children']) else False)
    batch['updated_at'] = now()
    write_json(folder/'batch.json', batch)
    return batch


def fetch(api, task_id):
    batch = status(api, task_id)
    outputs = []
    for child in batch['children']:
        if child['state'] == 'completed':
            result = api.fetch(child['task_id'])
            child['snapshot'] = result['snapshot']
            for output in result['outputs']:
                outputs.append({**output, 'seed_used': child['seed_used'], 'child_task_id': child['task_id']})
    batch.update(outputs=outputs, metadata=metadata(api.root), cleanup={'max_tasks': api.settings['retention']['max_tasks']})
    write_json(directory(api, task_id)/'result.json', batch)
    return batch


def cancel(api, task_id):
    """Cancel only this batch's children; preserve all completed outputs."""
    folder = directory(api, task_id)
    batch = read_json(folder/'batch.json')
    batch['cancel_requested_at'] = now()
    write_json(folder/'batch.json', batch)
    from .task_diagnostics import TERMINAL
    for child in batch['children']:
        if child.get('state') not in TERMINAL:
            try:
                result = api.cancel(child['task_id'])
                child.update(state=result['state'], cancel_dispatched=result.get('cancel_dispatched'))
            except Exception as exc:
                child['cancel_error'] = str(exc)
            write_json(folder/'batch.json', batch)
    return status(api, task_id)
