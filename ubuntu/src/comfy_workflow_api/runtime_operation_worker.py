"""One bounded operation, detached from SSH; never a resident service."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from .runtime_bridge import manager_for, meta_for, service_summary, read_json
from comfy_workflow_api import WorkflowAPI, sleep_guard
from comfy_workflow_api.storage import now, write_json


class OperationDeadline(BaseException):
    """Cannot be swallowed by lower-level retry/diagnostic exception handlers."""


def execute(root, operation_id):
    root = Path(root).resolve()
    operation_id = str(uuid.UUID(operation_id))
    api = WorkflowAPI(root)
    directory = api.data/'runtime-operations'/operation_id
    # The launcher records identity before this worker touches its receipt.
    for _ in range(50):
        receipt = read_json(directory/'receipt.json')
        if receipt.get('worker'):
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Launcher did not persist worker identity')
    if not sleep_guard.matches(receipt['worker']) or receipt['worker']['pid'] != os.getpid():
        raise RuntimeError('Worker identity mismatch')
    packet = read_json(directory/'request.json')
    manager = manager_for(root)
    meta = meta_for(root, packet)
    receipt['before'] = service_summary(manager)
    write_json(directory/'receipt.json', receipt)
    try:
        if meta['stale_copy']:
            raise RuntimeError('stale_copy: source changed before operation started')
        with sleep_guard.hold(api, operation_id=operation_id, purpose='service_'+packet['action'], service_instance_id=receipt['before'].get('service_instance_id')):
            receipt['guard_during_operation'] = sleep_guard.status(api)
            write_json(directory/'receipt.json', receipt)
            changed = manager.execute(packet['action'], operation_id=operation_id)
        unverified = packet['action'] != 'stop' and not changed.get('ready')
        result = {'success':not unverified and not changed.get('busy',False) and not changed.get('blocked',False) and changed.get('state') != 'blocked',
                  'power_state':'online','host_awake':True,'ssh_reachable':True,'service':service_summary(manager),
                  'sleep_guard':sleep_guard.status(api),'metadata':meta,
                  **{key:changed[key] for key in ('ready','healthy','state','busy','next_action','message','blocked','reused_existing','startup_seconds','error') if key in changed}}
        result.setdefault('ready',False)
        result.setdefault('state', 'busy' if changed.get('busy') else ('blocked' if changed.get('blocked') else ('unverified' if unverified else ('ready' if changed.get('ready') else 'stopped'))))
        if unverified and changed.get('smoke_failed'):
            result.update(error={'type':'RenderVerificationFailed', 'message':changed.get('smoke',{}).get('error',changed.get('message','Render not verified')), 'retry_automatically':False}, next_action='recover')
    except (Exception, OperationDeadline) as exc:
        result = {'success':False,'ready':False,'state':'failed','power_state':'online','ssh_reachable':True,
                  'error':{'type':type(exc).__name__,'message':str(exc),'retry_automatically':False},'metadata':meta}
        try:
            result['service'] = service_summary(manager)
        except Exception:
            pass
    receipt.update(finished_at=now(), response=result)
    write_json(directory/'receipt.json', receipt)
    return result


def main():
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    def timeout(_signal, _frame):
        raise OperationDeadline('Runtime operation exceeded 600 seconds; inspect service before any retry')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(600)
    execute(sys.argv[1],sys.argv[2])


if __name__ == '__main__':
    main()
