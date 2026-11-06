"""Execute bounded service actions in the independent Ubuntu SDK venv."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from comfy_workflow_api import WorkflowAPI, sleep_guard
from comfy_workflow_api.contract import VERSION as SDK_VERSION
from comfy_workflow_api.metadata import client_contract
from comfy_workflow_api.storage import now, write_json


def command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=2,
                                env={**os.environ, 'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'})
        return {'exit_code': result.returncode, 'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()[:500]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'error': str(exc)}


def text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def power_status():
    session = command(['loginctl', 'show-user', str(os.getuid()), '-p', 'Display', '--value']).get('stdout', '')
    desktop = command(['loginctl', 'show-session', session, '-p', 'IdleHint', '-p', 'IdleSinceHint', '-p', 'State', '-p', 'Type']) if re.fullmatch(r'[a-zA-Z0-9]+', session) else {'message': 'No graphical session found'}
    settings = {}
    for key in ('sleep-inactive-ac-type', 'sleep-inactive-ac-timeout'):
        settings[key] = command(['gsettings', 'get', 'org.gnome.settings-daemon.plugins.power', key])
    gpus = []
    for device in sorted(Path('/sys/class/drm').glob('card[0-9]*/device')):
        if text(device/'vendor') != '0x1002':
            continue
        gpu = {'observed_at':now(), 'stale':False, 'device': str(device.resolve()), 'runtime_status': text(device/'power/runtime_status')}
        for field, filename, divisor in (
            ('vram_used_bytes', 'mem_info_vram_used', 1),
            ('vram_total_bytes', 'mem_info_vram_total', 1),
            ('gpu_busy_percent', 'gpu_busy_percent', 1),
        ):
            value = text(device/filename)
            gpu[field] = int(value)//divisor if value and value.isdigit() else None
        for sensor in ('power1_average', 'power1_input'):
            files = list((device/'hwmon').glob('hwmon*/'+sensor))
            value = text(files[0]) if files else None
            if value and value.isdigit():
                gpu['power_watts'] = int(value)/1_000_000
                gpu['power_sensor'] = sensor
                break
        gpus.append(gpu)
    return {'observed_at':now(),'stale':False,'boot_id': text('/proc/sys/kernel/random/boot_id'), 'uptime': text('/proc/uptime'),
            'mem_sleep': text('/sys/power/mem_sleep'), 'desktop': desktop,
            'automatic_suspend': settings, 'system_inhibitors': command(['systemd-inhibit', '--list', '--no-pager']),
            'gnome_inhibitors': command(['/usr/bin/gnome-session-inhibit', '--list']), 'gpus': gpus}


def service_summary(manager):
    current = manager.status()
    summary = {key: value for key, value in current.items() if key not in ('queue', 'system')}
    queue = current.get('queue')
    summary['queue'] = {key: len(queue[key]) if isinstance(queue, dict) and isinstance(queue.get(key), list) else None
                        for key in ('queue_running', 'queue_pending')}
    summary['system'] = {key: current.get('system', {}).get('system', {}).get(key)
                         for key in ('comfyui_version', 'pytorch_version')}
    return summary


def manager_for(root):
    spec = importlib.util.spec_from_file_location('managed_comfyui_service', root/'scripts/comfyui-service.py')
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    return manager


def meta_for(root, packet):
    expected = client_contract(root, 'ubuntu-runtime')
    return {'plugin_version': '0.3.1', 'plugin_hash': packet.get('client_metadata', {}).get('plugin_hash'),
            'expected_plugin_hash': expected['hash'], 'expected_plugin_version': expected['version'],
            'client_contract_error': expected['error'], 'sdk_version': SDK_VERSION,
            'stale_copy': bool(expected['error']) or packet.get('client_metadata', {}).get('plugin_hash') != expected['hash'],
            'service_manager_sha256': hashlib.sha256((root/'scripts/comfyui-service.py').read_bytes()).hexdigest()}


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def operation_status(api, operation_id):
    directory = api.data/'runtime-operations'/str(uuid.UUID(operation_id))
    receipt = read_json(directory/'receipt.json')
    if not receipt:
        return {'success': False, 'ready': False, 'state': 'unknown_operation', 'operation_id': operation_id}
    result = dict(receipt.get('response') or {'success': True, 'ready': False, 'state': 'in_progress'})
    if not receipt.get('finished_at') and not sleep_guard.matches(receipt.get('worker')):
        result.update(success=False, ready=False, state='outcome_unknown',
                      error={'type':'WorkerLost', 'message':'Operation worker ended without a final receipt; inspect status before recovery', 'retry_automatically':False})
    result.update(operation_id=operation_id, observed_at=now(), operation_started_at=receipt.get('started_at'), operation_finished_at=receipt.get('finished_at'), receipt_path=str(directory/'receipt.json'))
    return result


def reconcile_confirmation(result, live, manager):
    """A historical escalation request must not block an ordinary fresh start."""
    request = result.get('recovery_request')
    if not result.get('approval_required') or not isinstance(request, dict):
        return
    processes = request.get('processes', [])
    ended = bool(processes) and hasattr(manager, 'incarnation_exited') and all(
        manager.incarnation_exited(item) for item in processes)
    changed_instance = request.get('service_instance_id') != live.get('service_instance_id')
    obsolete = ended or changed_instance or live.get('ready')
    expired = False
    try:
        from datetime import datetime, timezone
        expired = datetime.fromisoformat(request['expires_at']) <= datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        expired = True
    result['operation_state'] = result.get('state')
    result['operation_recovery_request'] = request
    result['approval_required'] = False
    result['approval_obsolete'] = True
    result['recovery_policy'] = 'automatic_owned_instance_timeout_escalation'
    result['confirmation_expired'] = expired
    result['operation_result_stale'] = True
    result.update(ready=bool(live.get('ready')), healthy=bool(live.get('ready')))
    for key in ('recovery_request', 'confirmation_command', 'error', 'approval_scope'):
        result.pop(key, None)
    if live.get('ready'):
        result.update(success=True, state='ready', next_action=None,
                      message='Ubuntu and ComfyUI are ready; the previous confirmation is obsolete.')
    elif obsolete:
        conflict = bool(live.get('listeners') and not live.get('owned_listener'))
        changing = live.get('owned_process_running') and live.get('phase') in ('starting','stopping')
        result.update(success=True, state='blocked' if conflict else 'in_progress' if changing else 'unverified' if live.get('owned_process_running') else 'stopped',
                      next_action='inspect_port_conflict' if conflict else 'status' if changing else 'ensure_ready',
                      message='Ubuntu is already awake. The old process escalation no longer applies; normal ensure_ready needs no confirmation.' if not conflict else
                              'Old escalation is obsolete; a foreign listener must be inspected. No process will be killed.')
    else:
        result.update(success=False, state='recovery_required', next_action='recover',
                      message='Ubuntu is already awake. Manual recovery confirmation is no longer used; call recover to reset the suspect owned instance.')


def launch(api, root, packet, meta):
    operations = api.data/'runtime-operations'
    operations.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (operations/'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        directory = operations/packet['operation_id']
        if (directory/'receipt.json').exists():
            return operation_status(api, packet['operation_id'])
        active = read_json(operations/'active.json')
        if active.get('operation_id'):
            prior = operation_status(api, active['operation_id'])
            if prior.get('state') == 'in_progress' or (prior.get('state') == 'outcome_unknown' and packet['action'] != 'recover'):
                return {**prior, 'existing_operation': True}
        directory.mkdir(mode=0o700)
        write_json(directory/'request.json', packet)
        os.chmod(directory/'request.json', 0o600)
        argv = [sys.executable, '-m', 'comfy_workflow_api.runtime_operation_worker', str(root), packet['operation_id']]
        with (directory/'worker.log').open('ab', buffering=0) as log:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       cwd=root, start_new_session=True, close_fds=True)
        identity = sleep_guard.identity(process.pid)
        receipt = {'operation_id': packet['operation_id'], 'action':packet['action'], 'started_at':now(),
                   'worker':identity, 'session_id':os.getsid(process.pid), 'process_group_id':os.getpgid(process.pid), 'command':argv, 'log':str(directory/'worker.log'), 'metadata':meta,
                   'response':{'success':True,'ready':False,'state':'in_progress','metadata':meta}}
        write_json(directory/'receipt.json', receipt)
        write_json(operations/'active.json', {'operation_id':packet['operation_id']})
    return operation_status(api, packet['operation_id'])


def invoke(packet):
    action = packet['action']
    if action not in ('status', 'ensure_ready', 'stop', 'restart', 'recover'):
        raise ValueError('Unsupported runtime action')
    root = Path(packet['root']).resolve()
    packet['operation_id'] = str(uuid.UUID(packet['operation_id']))
    wait = packet.get('wait_seconds', 30)
    if type(wait) is not int or not 0 <= wait <= 30:
        raise ValueError('wait_seconds must be 0..30')
    manager, api = manager_for(root), WorkflowAPI(root)
    meta = meta_for(root, packet)
    if meta['stale_copy'] and action != 'status':
        return {'success':False,'ready':False,'error':{'type':'StaleCopy','message':'Installed plugin differs from the Ubuntu client release manifest; update and open a new Hermes session','retry_automatically':False},'metadata':meta}
    if action == 'status':
        if packet.get('query_operation_id'):
            result = operation_status(api, packet['query_operation_id'])
            historical = result.get('service', {})
            live = service_summary(manager)
            result['operation_result_observed_at'] = historical.get('observed_at')
            result['operation_result_stale'] = (historical.get('service_instance_id') != live.get('service_instance_id') or historical.get('resume',{}).get('resume_id') != live.get('resume',{}).get('resume_id'))
            result['service'] = live
            result['sleep_guard'] = sleep_guard.status(api)
            result['ready'] = bool(live.get('ready')) and result.get('state') not in ('in_progress','outcome_unknown')
            result['healthy'] = result['ready']
            reconcile_confirmation(result, live, manager)
            result['metadata'] = meta
            return result
        return {'success':True,'ready':manager.status().get('ready',False),'power_state':'online',
                'ssh_reachable':True,'service':service_summary(manager),'sleep_guard':sleep_guard.status(api),
                'ubuntu':power_status(),'observed_at':now(),'metadata':meta}
    result = launch(api, root, packet, meta)
    deadline = time.monotonic()+wait
    while result.get('state') == 'in_progress' and time.monotonic() < deadline:
        time.sleep(.25)
        result = operation_status(api, result['operation_id'])
    return result


def main():
    try:
        response = invoke(json.load(sys.stdin))
    except Exception as exc:
        response = {'success':False,'ready':False,'error':{'type':type(exc).__name__,'message':str(exc),'retry_automatically':False}}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == '__main__':
    main()
