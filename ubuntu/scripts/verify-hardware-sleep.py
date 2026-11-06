#!/usr/bin/env python3
"""Opt-in, two-cycle Ubuntu sleep acceptance with an external Wake-on-LAN peer.

Default invocation only prints the plan. With --run, sudo is required and the
Mac operator must wake this host after each confirmed sleep. No driver reset,
machine reboot, force-stop, or automatic repeated sleep is performed.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid


USER_CODE = r'''
import json, sys
from comfy_workflow_api.api import WorkflowAPI
from comfy_workflow_api.runtime_bridge import manager_for
from comfy_workflow_api import sleep_cycle, sleep_guard
from comfy_workflow_api.storage import write_json
root, action, payload = sys.argv[1:]
api = WorkflowAPI(root)
if action == 'hardware-check':
    from comfy_workflow_api.hardware_monitor import check, status
    value = check(api.root)
    monitored = status(api.root)
    value['monitor_evidence'] = {key: monitored.get(key) for key in ('status', 'kernel_error', 'incidents')}
    print(json.dumps(value))
elif action == 'write':
    path = api.data/'hardware-monitor/sleep-verification.json'
    write_json(path, json.loads(payload))
    print(json.dumps({'success': True, 'receipt_path': str(path)}))
else:
    manager = manager_for(api.root)
    if action in ('stop', 'ensure_ready'):
        if action == 'ensure_ready':
            with sleep_guard.hold(api, purpose='sleep_acceptance_start'):
                manager.execute(action)
        else:
            manager.execute(action)
    current = manager.status()
    queue = current.get('queue', {})
    guard = sleep_guard.status(api)
    summary = {key: current.get(key) for key in ('service_instance_id', 'ready',
        'owned_process_running', 'owned_listener', 'http_healthy', 'queue_known',
        'phase', 'processes_exited', 'process', 'managed_processes')}
    summary['listeners_present'] = bool(current.get('listeners'))
    summary['queue_running'] = len(queue.get('queue_running', [])) if current.get('queue_known') else None
    summary['queue_pending'] = len(queue.get('queue_pending', [])) if current.get('queue_known') else None
    summary['sleep_cycle'] = sleep_cycle.status(api)
    summary['inhibitor'] = {'running': guard.get('running'), 'inhibited': guard.get('inhibited'),
                            'leases': guard.get('leases')}
    print(json.dumps(summary))
'''


def now():
    return datetime.now(timezone.utc).isoformat()


def plan():
    return {'run': False, 'steps': [
        'Record the current owned ComfyUI state; refuse busy, unknown, or foreign instances.',
        'Refresh the installed root controller and units, restart and verify the hardware monitor.',
        'Stop ComfyUI normally; suspend once as root; the Mac peer wakes Ubuntu after 15 seconds offline.',
        'Require a new completed sleep token and actual suspended-time increase; ComfyUI must remain off.',
        'Start through the existing manager and verify a real render; suspend once more and await the Mac wake.',
        'Require a new ready service instance and process, then restore the original running/off state.',
    ], 'receipt': 'Configured data_dir/hardware-monitor/sleep-verification.json',
        'execute': 'sudo /usr/bin/python3 scripts/verify-hardware-sleep.py --run'}


def load_controller():
    path = Path(__file__).resolve().with_name('hardware-service.py')
    spec = importlib.util.spec_from_file_location('sleep_acceptance_controller', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sdk(controller, config, action='status', payload=None, timeout=15):
    """The SDK and configured data paths are touched only after dropping UID."""
    argv = ['/usr/sbin/runuser', '-u', config['user'], '--', '/usr/bin/env',
            'HOME='+config['home'], 'COMFY_WORKFLOW_HOME='+config['root'],
            'PATH='+config['path'], config['python'], '-c', USER_CODE,
            config['root'], action, json.dumps(payload) if payload is not None else '{}']
    result = controller.command(argv, timeout=timeout, check=False)
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError('User-side '+action+' returned no JSON: '+result.stderr[-1000:]) from exc
    if result.returncode or not isinstance(value, dict):
        raise RuntimeError('User-side '+action+' failed: '+result.stderr[-1000:])
    return value


def elapsed_clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def sleep_offset():
    return time.clock_gettime(time.CLOCK_BOOTTIME)-time.clock_gettime(time.CLOCK_MONOTONIC)


def assert_idle_owned(current):
    if current.get('listeners_present') and not current.get('owned_listener'):
        raise RuntimeError('A foreign or unreadable listener is present; validation refused')
    if current.get('owned_process_running'):
        if not current.get('queue_known'):
            raise RuntimeError('ComfyUI queue is unknown; validation refused')
        if current.get('queue_running') or current.get('queue_pending'):
            raise RuntimeError('ComfyUI generation is busy; validation refused')
    elif current.get('process') and current.get('processes_exited') is not True:
        raise RuntimeError('A recorded process has no confirmed exit; validation refused')
    if current.get('sleep_cycle', {}).get('active'):
        raise RuntimeError('A sleep cycle is already active; finish its recovery first')


def assert_off(current):
    assert_idle_owned(current)
    if current.get('owned_process_running') or current.get('listeners_present'):
        raise RuntimeError('ComfyUI did not remain stopped')


class Verification:
    def __init__(self, controller, config, timeout=660):
        self.controller, self.config, self.timeout = controller, config, timeout
        self.receipt = {'schema_version': 1, 'run_id': str(uuid.uuid4()), 'started_at': now(),
                        'success': False, 'cycles': [], 'events': []}
        self.original = None

    def progress(self, stage, **details):
        event = {'at': now(), 'stage': stage, **details}
        self.receipt['stage'] = stage
        self.receipt['events'] = (self.receipt['events']+[event])[-200:]
        print(json.dumps(event, ensure_ascii=False), flush=True)
        result = sdk(self.controller, self.config, 'write', self.receipt)
        self.receipt['receipt_path'] = result['receipt_path']

    def current(self):
        return sdk(self.controller, self.config)

    def stop(self):
        current = self.current()
        assert_idle_owned(current)
        if current.get('owned_process_running'):
            current = sdk(self.controller, self.config, 'stop', timeout=150)
        assert_off(current)
        return current

    def start(self):
        assert_idle_owned(self.current())
        current = sdk(self.controller, self.config, 'ensure_ready', timeout=540)
        assert_idle_owned(current)
        if not current.get('ready'):
            raise RuntimeError('Existing manager did not verify a real render')
        return current

    def wait_inhibitor_release(self):
        deadline = elapsed_clock()+35
        while elapsed_clock() < deadline:
            current = self.current()
            assert_idle_owned(current)
            guard = current.get('inhibitor', {})
            if guard.get('leases'):
                raise RuntimeError('A new request acquired a sleep lease; validation refused')
            if guard.get('inhibited') is False:
                return current
            time.sleep(1)
        raise RuntimeError('GNOME inhibitor did not release; validation refused')

    def gpu_check(self):
        deadline = elapsed_clock()+15
        last = {}
        while elapsed_clock() < deadline:
            last = sdk(self.controller, self.config, 'hardware-check',
                       timeout=max(.1, min(10, deadline-elapsed_clock())))
            gpu = last.get('gpu', {})
            if gpu.get('status') == 'ok' and not gpu.get('rocm_error') and not gpu.get('paused'):
                return last
            time.sleep(min(1, max(0, deadline-elapsed_clock())))
        raise RuntimeError('First-cycle fresh GPU health check failed: '+json.dumps(last.get('gpu', {})))

    def one_cycle(self, number, expect_running):
        before = self.wait_inhibitor_release()
        if expect_running and not before.get('ready'):
            raise RuntimeError('Second cycle must begin with verified rendering')
        if not expect_running:
            assert_off(before)
        previous_token = before.get('sleep_cycle', {}).get('cycle_token')
        baseline = sleep_offset()
        deadline = elapsed_clock()+self.timeout
        item = {'cycle_index': number, 'expected_running': expect_running,
                'before': before, 'before_offset_seconds': baseline, 'started_at': now()}
        self.receipt['cycles'].append(item)
        self.progress('suspend_requested', cycle_index=number, expected_running=expect_running,
                      message='Mac peer: wait until Ubuntu is offline for 15 seconds, then send three Wake-on-LAN packets.')
        # Invoke the normal systemd path so the installed pre/post units do the work.
        self.controller.systemctl('suspend', timeout=175)
        last_progress = 0
        while elapsed_clock() < deadline:
            current = self.current()
            offset_delta = sleep_offset()-baseline
            item.update(last_observed=current, suspended_seconds=round(offset_delta, 3))
            state = current.get('sleep_cycle', {})
            changed = bool(state.get('cycle_token') and state.get('cycle_token') != previous_token)
            if changed and state.get('phase') == 'restore_failed':
                raise RuntimeError('Sleep recovery failed: '+str(state.get('error', 'unknown')))
            if changed and state.get('phase') == 'complete' and not state.get('active') and offset_delta >= 2:
                if bool(state.get('was_running')) != expect_running:
                    raise RuntimeError('Pre-sleep running state does not match the validation cycle')
                assert_idle_owned(current)
                if expect_running:
                    if not current.get('ready'):
                        raise RuntimeError('Recovered service has no current real-render verification')
                    old_pid = (before.get('process') or {}).get('pid')
                    new_pid = (current.get('process') or {}).get('pid')
                    if current.get('service_instance_id') == before.get('service_instance_id') or not new_pid or new_pid == old_pid:
                        raise RuntimeError('Recovery did not produce a new service instance and PID')
                else:
                    assert_off(current)
                    item['hardware_check'] = self.gpu_check()
                item.update(success=True, completed_at=now(), after=current)
                self.progress('cycle_complete', cycle_index=number, cycle_token=state['cycle_token'],
                              suspended_seconds=item['suspended_seconds'])
                return current
            if elapsed_clock()-last_progress >= 10:
                self.progress('awaiting_resume_completion', cycle_index=number,
                              cycle_phase=state.get('phase'), suspended_seconds=item['suspended_seconds'])
                last_progress = elapsed_clock()
            time.sleep(2)
        raise TimeoutError('Cycle '+str(number)+' exceeded its '+str(self.timeout)+' second deadline; no further sleep requested')

    def restore_original(self):
        if self.original is None:
            return
        self.receipt['restoration'] = {'attempted': True, 'success': False}
        self.progress('restoring_original_state', original_running=self.original['owned_process_running'])
        if self.original['owned_process_running']:
            final = self.start()
        else:
            final = self.stop()
            self.controller.stop_empty_owner()
            if self.controller.owner_members():
                raise RuntimeError('Recovery owner cgroup was not empty after normal stop')
        self.receipt['restoration'].update(success=True, final=final, completed_at=now())

    def run(self):
        failure = None
        try:
            original = self.current()
            assert_idle_owned(original)
            self.original = original
            self.receipt['original'] = original
            self.progress('initial_state_recorded')
            self.controller.install(self.config)
            self.controller.systemctl('restart', self.controller.MONITOR, timeout=20)
            monitor = self.controller.verify_monitor(self.config)
            self.receipt['monitor'] = {'main_pid': monitor['main_pid'], 'unit': monitor['unit']}
            self.progress('monitor_verified', **self.receipt['monitor'])
            self.stop()
            self.one_cycle(1, False)
            self.start()
            self.progress('rendering_verified_before_second_cycle')
            self.one_cycle(2, True)
        except BaseException as exc:
            failure = str(exc) or type(exc).__name__
            self.receipt['error'] = failure
        finally:
            try:
                self.restore_original()
            except BaseException as exc:
                self.receipt['restoration_error'] = str(exc) or type(exc).__name__
                failure = failure or self.receipt['restoration_error']
            self.receipt.update(success=failure is None, finished_at=now())
            try:
                self.progress('passed' if failure is None else 'failed', error=failure)
            except Exception as exc:
                self.receipt['receipt_error'] = str(exc)
                print(json.dumps(self.receipt, ensure_ascii=False), flush=True)
                failure = failure or str(exc)
        return 0 if failure is None else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Actually perform two suspend/wake acceptance cycles')
    parser.add_argument('--config', type=Path, default=Path('/etc/myhermes2comfy/service.json'))
    args = parser.parse_args()
    if not args.run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return 0
    controller = load_controller()
    controller.require_root()
    config = controller.load_config(args.config)
    if Path(config['root']).resolve() != Path(__file__).resolve().parents[1]:
        raise RuntimeError('Run the verifier from the configured Ubuntu package')
    def interrupted(_number, _frame):
        raise KeyboardInterrupt('Verification interrupted; attempting only normal original-state restoration')
    signal.signal(signal.SIGTERM, interrupted)
    return Verification(controller, config).run()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
