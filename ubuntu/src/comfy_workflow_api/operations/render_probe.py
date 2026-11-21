"""Submit an isolated diagnostic render and record evidence for this service instance."""
from functools import partial
import hashlib
import secrets
import shutil
import time
import urllib.parse
import uuid

from ..client import APIError, png_size
from ..compiler import compile_graph
from .service_readiness import instance_id, queue_ids
from ..snapshots import read_snapshot
from ..storage import is_job_id, now, read_json, write_json


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


def execution_seconds(item):
    messages = item.get('status', {}).get('messages', [])
    starts = [data.get('timestamp') for kind, data in messages if kind == 'execution_start']
    ends = [data.get('timestamp') for kind, data in messages if kind == 'execution_success']
    if starts and ends and isinstance(starts[0], (int,float)) and isinstance(ends[-1], (int,float)) and ends[-1] >= starts[0]:
        return (ends[-1]-starts[0])/1000
    return None


def _probe_read(client, state_file, path, receipt, folder, deadline, **kwargs):
    """Retry observations, never generation submission, within the probe budget."""
    while True:
        try:
            return client.request(path, **kwargs)
        except (OSError, APIError) as exc:
            if isinstance(exc, APIError) and exc.status < 500:
                raise
            receipt['read_retries'] = receipt.get('read_retries', 0)+1
            receipt['last_read_error'] = str(exc)
            write_json(folder/'receipt.json',receipt)
            if time.monotonic() >= deadline:
                raise TimeoutError('Smoke observation unavailable until its execution deadline; no generation was resubmitted') from exc
            if instance_id(read_json(state_file)) != receipt['service_instance_id']:
                raise RuntimeError('Service instance changed during smoke observation') from exc
            time.sleep(.25)


def run_probe(*, root, data, comfy_dir, client, status, workflow_hash, cold=False):
    state_file = data/'state.json'
    probe_read = partial(_probe_read, client, state_file)
    current = status()
    if not current['http_healthy'] or not current['owned_listener']:
        return {**current, 'ready':False, 'smoke_failed':True, 'message':'Smoke requires an HTTP-alive owned service'}
    if queue_ids(current):
        return {**current, 'busy': True, 'message': 'Queue is busy; no smoke job was enqueued'}
    if queue_ids(current) is None:
        return {**current, 'ready': False, 'healthy': False, 'message': 'Queue is unknown; smoke was not submitted'}
    probe_hash = workflow_hash()
    snapshot, manifest = read_snapshot(root, data.parent, 'qwen-image-2.1-t2i')
    if workflow_hash() != probe_hash:
        return {**current, 'ready': False, 'healthy': False, 'smoke_failed': True,
                'message': 'Render configuration changed while preparing smoke'}
    probe_id = str(uuid.uuid4())
    seed = secrets.randbits(64)
    folder = data/'health'/probe_id
    folder.mkdir(parents=True)
    request = {'prompt': 'A red cube and a blue sphere on a white background. Diagnostic '+probe_id,
               'preset':'quality', 'seed':seed, 'output_size':{'mode':'explicit','width':64,'height':64}}
    prefix = 'comfy-workflow-api/health/'+probe_id+'/image'
    graph = compile_graph('qwen-image-2.1-t2i', snapshot, request, prefix=prefix)
    prompt_id = str(uuid.uuid4())
    receipt = {'probe_id':probe_id, 'prompt_id':prompt_id, 'service_instance_id': current['service_instance_id'],
               'resume_id':current['resume']['resume_id'], 'workflow_hash':probe_hash, 'snapshot':manifest,
               'request':request, 'created_at':now(), 'deadline_seconds':120 if cold else 30, 'cold':cold,
               'success':False, 'submission_uncertain':True}
    write_json(folder/'prompt.json', graph)
    write_json(folder/'receipt.json', receipt)  # Persist ID before POST, never retry.
    try:
        result = client.request('/prompt','POST', {'prompt':graph,'client_id':probe_id,'prompt_id':prompt_id})
        if result.get('node_errors') or result.get('prompt_id') != prompt_id:
            raise ValueError('Smoke graph rejected: '+str(result))
        receipt.update(submission_uncertain=False, submitted_at=now())
        write_json(folder/'receipt.json', receipt)
        started = None
        queued_at = time.monotonic()
        while True:
            if instance_id(read_json(state_file)) != receipt['service_instance_id']:
                raise RuntimeError('Service instance changed during smoke')
            deadline = (started if started is not None else queued_at)+receipt['deadline_seconds']
            item = probe_read('/history/'+prompt_id, receipt, folder, deadline).get(prompt_id)
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
                    payload = probe_read('/view?'+urllib.parse.urlencode(query), receipt, folder, deadline, binary=True)
                    if png_size(payload) != (64,64):
                        raise RuntimeError('Smoke output is not 64x64')
                    server_path = comfy_dir/'output'/image['subfolder']/image['filename']
                    if not server_path.is_file() or server_path.read_bytes() != payload:
                        raise RuntimeError('Smoke PNG was not durably saved at the server output path')
                    (folder/'output.png').write_bytes(payload)
                    receipt.update(success=True, verified_at=now(), server_file=query,
                                   sha256=hashlib.sha256(payload).hexdigest(),
                                   execution_seconds=receipt['execution_seconds'])
                    break
            queue = probe_read('/queue', receipt, folder, deadline)
            running = any(row[1] == prompt_id for row in queue.get('queue_running', []))
            pending = any(row[1] == prompt_id for row in queue.get('queue_pending', []))
            if running and started is None:
                started = time.monotonic()
                receipt['execution_started_at'] = now()
            # Never count other jobs' wait as GPU timeout. Withdraw only our own pending smoke.
            if pending and any(row[1] != prompt_id for row in queue.get('queue_running', [])):
                client.request('/api/jobs/'+prompt_id+'/cancel','POST',{})
                receipt['cancel_requested'] = True
                raise RuntimeError('Another job started first; smoke withdrawn without interrupting it')
            if started is not None and time.monotonic()-started >= receipt['deadline_seconds']:
                try:
                    client.request('/api/jobs/'+prompt_id+'/cancel','POST',{})
                    receipt['cancel_requested'] = True
                except Exception as exc:
                    receipt['cancel_error'] = str(exc)
                raise TimeoutError('Smoke execution exceeded deadline; cooperative cancellation requested, completion unconfirmed')
            if started is None and time.monotonic()-queued_at > 30:
                try:
                    client.request('/api/jobs/'+prompt_id+'/cancel', 'POST', {})
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
    state = read_json(state_file)
    if instance_id(state) == receipt['service_instance_id']:
        state.update(service_instance_id=receipt['service_instance_id'], verification=receipt)
        write_json(state_file, state)
    result = status()
    prune_probes(data/'health', comfy_dir/'output/comfy-workflow-api/health', queue_ids(result))
    result['smoke'] = receipt
    if not receipt['success']:
        result.update(ready=False, healthy=False, smoke_failed=True)
    return result
