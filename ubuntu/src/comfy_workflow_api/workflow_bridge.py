"""Executed by the existing Ubuntu venv over SSH; no additional dependencies."""
import base64
import importlib.util
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api.client import png_size
from comfy_workflow_api import batches
from comfy_workflow_api.contract import normalize, PRESETS
from comfy_workflow_api.metadata import metadata
from comfy_workflow_api.sampling import PROFILES as SAMPLING_PROFILES


def invoke(packet):
    api = WorkflowAPI(root=packet['root'])
    operation = packet['operation']
    if operation == 'info':
        presets = {alias: {name: {**values, 'cfg': 1} for name, values in modes.items()}
                   for alias, modes in PRESETS.items()}
        return {'workflows': api.list_workflows(), 'health': api.health(), 'presets': presets,
                'sampling_profiles': SAMPLING_PROFILES, 'seed_default': 0, 'prompt_max_chars': 8000}
    if operation == 'submit':
        spec = importlib.util.spec_from_file_location('generation_readiness_manager', Path(packet['root'])/'scripts/comfyui-service.py')
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        if manager.status().get('ready') is not True:
            raise ValueError('ComfyUI render readiness is unverified; call comfy_service action=ensure_ready and wait for ready=true before submitting. No generation was submitted.')
        alias = packet['workflow']
        request = normalize(packet['request'], alias)
        count = packet.get('count', 1)
        if type(count) is not int or not 1 <= count <= 4 or request['seed']+count-1 >= 2**64:
            raise ValueError('Invalid count or seed range')
        if alias not in ('qwen-image-2.1-edit', 'qwen-image-2.1-t2i'):
            raise ValueError('Unsupported workflow')
        blobs = packet.get('images', [])
        if alias.endswith('edit') and not 1 <= len(blobs) <= 10:
            raise ValueError('Editing requires 1..10 PNG images')
        if 'images' in request:
            raise ValueError('Remote paths are not accepted; transfer PNG bytes instead')
        incoming = api.data / 'incoming'
        incoming.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=incoming) as temporary:
            if blobs:
                request['images'] = []
                for index, blob in enumerate(blobs, 1):
                    data = base64.b64decode(blob, validate=True)
                    if len(data) > 20 * 1024**2:
                        raise ValueError('PNG exceeds 20 MiB')
                    png_size(data)
                    path = Path(temporary) / f'{index}.png'
                    path.write_bytes(data)
                    request['images'].append(str(path))
            return batches.submit(api, alias, request, count)
    if operation == 'task':
        task_id = packet['task_id']
        action = packet.get('action', 'status')
        if action not in ('status', 'result', 'cancel'):
            raise ValueError('action must be status, result or cancel')
        batch = (batches.directory(api, task_id)/'batch.json').exists()
        if action == 'cancel':
            return batches.cancel(api, task_id) if batch else api.cancel(task_id)
        get_status = (lambda: batches.status(api, task_id)) if batch else (lambda: api.status(task_id))
        state = get_status()
        wait = packet.get('wait_seconds', 0)
        if type(wait) is not int or not 0 <= wait <= 30:
            raise ValueError('wait_seconds must be 0..30')
        if action == 'result' and wait and state['state'] in ('queued', 'running'):
            started = time.monotonic()
            while state['state'] in ('queued', 'running') and time.monotonic()-started < wait:
                time.sleep(api.settings['poll_interval'])
                state = get_status()
        if action == 'status' or state['state'] not in ('completed', 'partial'):
            return state
        result = batches.fetch(api, task_id) if batch else api.fetch(task_id)
        for output in result['outputs']:
            data = Path(output['path']).read_bytes()
            output['png_base64'] = base64.b64encode(data).decode()
        return {'state': state['state'], **result}
    raise ValueError('Unknown operation')


def main():
    meta = {}
    try:
        packet = json.load(sys.stdin)
        meta = metadata(packet['root'])
        client = packet.get('client_metadata', {})
        stale = bool(meta.get('client_contract_error')) or client.get('plugin_hash') != meta['expected_plugin_hash'] or client.get('contract_version') != meta['contract_version']
        meta.update(client=client, stale_copy=stale)
        if stale and packet['operation'] == 'submit':
            raise ValueError('stale_copy: installed plugin differs from the Ubuntu client release manifest; update plugin and open a new Hermes session before submitting')
        result = invoke(packet)
        client_fields = {key: meta[key] for key in (
            'expected_plugin_version', 'expected_plugin_hash', 'client_contract_error')}
        for value in (result, result.get('workflows', {})):
            if isinstance(value.get('metadata'), dict):
                value['metadata'] = {**value['metadata'], **client_fields}
        print(json.dumps({'ok': True, 'result': result, 'metadata': meta}, ensure_ascii=False))
    except Exception as exc:
        error = {'type': type(exc).__name__, 'message': str(exc), 'retry_submission': False}
        match = re.search(r'Task ([0-9a-f-]{36}):', str(exc))
        if match:
            error['task_id'] = match.group(1)
        print(json.dumps({'ok': False, 'error': error, 'metadata': meta}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
