from contextlib import ExitStack
import fcntl
import importlib.util
import os
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from comfy_workflow_api import lifecycle, sleep_cycle as cycle, sleep_guard
from comfy_workflow_api.storage import write_json


class SleepCycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.api = SimpleNamespace(data=Path(self.temporary.name)/'data', root=Path(self.temporary.name),
                                   settings={'sleep_guard': {'enabled': True}})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cycle, 'boot_id', return_value='boot'))
        self.stack.enter_context(patch.object(cycle, '_event', side_effect=lambda api, kind, value: value))
        self.off = {'owned_process_running': False, 'listeners': [], 'ready': False}
        self.running = {'owned_process_running': True, 'listeners': [{'owners': [123]}],
                        'owned_listener': True, 'queue_known': True, 'service_instance_id': 'old'}
        self.manager = SimpleNamespace(status=Mock(return_value=self.off),
                                       queue_ids=Mock(return_value=[]), execute=Mock(return_value=self.off))

    def record(self, phase='sleeping', was_running=True, **kwargs):
        return cycle._save(self.api, {'cycle_token': 'token', 'boot_id': 'boot',
                           'was_running': was_running, 'stop_started': was_running,
                           'stop_confirmed': True, **kwargs}, phase)

    def test_status_is_read_only_and_does_not_create_paths(self):
        self.assertFalse(cycle.status(self.api)['active'])
        self.assertFalse(self.api.data.exists())

    def test_stale_boot_record_does_not_block_work(self):
        self.record(boot_id='previous')
        before = (cycle.home(self.api)/'state.json').read_bytes()
        cycle.assert_allowed(self.api)
        self.assertTrue(cycle.status(self.api)['stale_boot'])
        self.assertEqual(before, (cycle.home(self.api)/'state.json').read_bytes())

    def test_malformed_state_fails_closed(self):
        write_json(cycle.home(self.api)/'state.json', {'phase': 'sleeping'})
        with self.assertRaises(cycle.SleepPending):
            cycle.assert_allowed(self.api)

    def test_unknown_phase_fails_closed(self):
        write_json(cycle.home(self.api)/'state.json',
                   {'phase': 'unexpected', 'cycle_token': 'token', 'boot_id': 'boot'})
        with self.assertRaises(cycle.SleepPending):
            cycle.assert_allowed(self.api)

    def test_unmatched_live_process_is_not_proof_of_termination(self):
        self.manager.status.return_value = {**self.off, 'process': {'pid': 123}, 'processes_exited': False}
        with self.assertRaisesRegex(RuntimeError, 'Process or listener'):
            cycle.pre(self.api, self.manager)
        self.manager.execute.assert_not_called()

    def test_probe_quiesce_timeout_rolls_back_before_stop(self):
        with patch.object(cycle, 'quiesce_gpu', side_effect=TimeoutError('probe stuck')):
            with self.assertRaisesRegex(TimeoutError, 'probe stuck'):
                cycle.pre(self.api, self.manager)
        self.assertFalse(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_actual_probe_lock_is_not_waited_on_without_a_deadline(self):
        folder = self.api.data/'hardware-probe'
        folder.mkdir(parents=True)
        with (folder/'gpu.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TimeoutError, 'still running'):
                cycle.quiesce_gpu(self.api, timeout_seconds=0)

    def test_pre_deadline_records_uncertain_shutdown(self):
        self.manager.status.return_value = self.running
        self.manager.execute.side_effect = cycle.PrepareDeadline('pre deadline')
        with self.assertRaises(cycle.PrepareDeadline):
            cycle.pre(self.api, self.manager)
        self.assertEqual(cycle.status(self.api)['phase'], 'restore_failed')
        self.assertTrue(cycle.status(self.api)['active'])

    def test_live_inflight_request_prevents_sleep_without_creating_gate(self):
        write_json(self.api.data/'comfyui-service/inflight/op.json', {'owner': {'pid': 123}})
        with patch.object(sleep_guard, '_lease_owner_state', return_value=True):
            with self.assertRaisesRegex(cycle.SleepPending, 'in flight'):
                cycle.pre(self.api, self.manager)
        self.assertFalse(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_live_lease_prevents_sleep_without_stopping_comfy(self):
        with patch.object(sleep_guard, 'live_leases', return_value=[{'owner': {'pid': 123}}]):
            with self.assertRaisesRegex(cycle.SleepPending, 'sleep lease'):
                cycle.pre(self.api, self.manager)
        self.assertFalse(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_dead_operation_owner_cannot_leave_a_permanent_short_gate(self):
        gate = self.api.data/'comfyui-service/admission.json'
        write_json(gate, {'blocked': True, 'owner': {'pid': 123}})
        with patch.object(sleep_guard, '_lease_owner_state', return_value=False):
            self.assertEqual(cycle.pre(self.api, self.manager)['phase'], 'sleeping')
        self.assertFalse(gate.exists())

    def test_unknown_operation_owner_is_not_cleaned_up(self):
        gate = self.api.data/'comfyui-service/admission.json'
        write_json(gate, {'blocked': True, 'owner': {'pid': 123}})
        with patch.object(sleep_guard, '_lease_owner_state', return_value=None):
            with self.assertRaisesRegex(cycle.SleepPending, 'still holds admission'):
                cycle.pre(self.api, self.manager)
        self.assertTrue(gate.exists())

    def test_busy_and_unknown_queue_rollback_gate_without_stopping(self):
        for current, ids in ((self.running, ['job']), ({**self.running, 'queue_known': False}, [])):
            self.manager.status.return_value = current
            self.manager.queue_ids.return_value = ids
            with self.assertRaises(cycle.SleepPending):
                cycle.pre(self.api, self.manager)
            self.assertFalse(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_foreign_listener_never_gets_signalled(self):
        self.manager.status.return_value = {**self.running, 'owned_listener': False}
        with self.assertRaisesRegex(cycle.SleepPending, 'Foreign'):
            cycle.pre(self.api, self.manager)
        self.manager.execute.assert_not_called()
        self.assertFalse(cycle.status(self.api)['active'])

    def test_pre_stops_once_then_keeps_persistent_gate(self):
        self.manager.status.side_effect = [self.running, self.off]
        result = cycle.pre(self.api, self.manager)
        self.assertEqual(result['phase'], 'sleeping')
        self.assertTrue(result['was_running'])
        self.assertTrue(result['stop_confirmed'])
        self.manager.execute.assert_called_once_with('stop')
        with self.assertRaises(cycle.SleepPending):
            cycle.assert_allowed(self.api)

    def test_busy_after_real_stop_sent_sigint_keeps_gate(self):
        path = Path(__file__).parents[1]/'scripts/comfyui-service.py'
        spec = importlib.util.spec_from_file_location('busy_stop_manager', path)
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        folder = self.api.data/'comfyui-service'
        state_path = folder/'state.json'
        process = {'pid': 42, 'uid': os.getuid(), 'start_ticks': '1', 'command': ['synthetic']}
        write_json(state_path, {'phase': 'running', 'service_instance_id': 'old',
                              'process': process, 'process_group': 42, 'managed_processes': []})
        def current():
            state = json.loads(state_path.read_text())
            signalled = bool(state.get('signals'))
            return {**self.running, **state, 'ready': not signalled, 'recorded_phase': state['phase'],
                    'listeners': [{'owners': [process]}],
                    'queue': {'queue_running': [[0, 'late-job']] if signalled else [], 'queue_pending': []}}
        with patch.object(manager, 'DATA', folder), patch.object(manager, 'STATE', state_path), \
             patch.object(manager, 'status', side_effect=current), \
             patch.object(manager, '_known_members', return_value={42: process}), \
             patch.object(manager, '_collect_members', return_value={42: process}), \
             patch.object(manager, 'group', return_value=(42, 42)), \
             patch.object(manager, 'process_matches', return_value=True), \
             patch.object(manager, '_wait_for_shutdown', return_value=None), \
             patch.object(manager, '_signal_one') as send:
            with self.assertRaisesRegex(cycle.SleepPending, 'during shutdown'):
                cycle.pre(self.api, manager)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1].name, 'SIGINT')
        self.assertEqual(cycle.status(self.api)['phase'], 'restore_failed')
        self.assertTrue(cycle.status(self.api)['stop_started'])
        with self.assertRaises(cycle.SleepPending):
            cycle.assert_allowed(self.api)

    def test_busy_before_any_signal_can_release_gate_with_fresh_evidence(self):
        self.manager.status.return_value = self.running
        self.manager.execute.return_value = {**self.running, 'busy': True, 'ready': True,
                                            'recorded_phase': 'running', 'signals': []}
        with self.assertRaises(cycle.SleepPending):
            cycle.pre(self.api, self.manager)
        self.assertEqual(cycle.status(self.api)['phase'], 'aborted')
        self.assertFalse(cycle.status(self.api)['stop_started'])

    def prepare_child_root(self, enabled_guard=False):
        (self.api.root/'config').mkdir(exist_ok=True)
        (self.api.root/'config/settings.toml').write_text(
            'base_url="http://127.0.0.1:8188"\ndata_dir="data"\nrequest_timeout=2\n'
            '[sleep_guard]\nenabled='+str(enabled_guard).lower()+'\n')
        return {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1]/'src'),
                'COMFY_WORKFLOW_HOME': str(self.api.root)}

    def wait_child_marker(self, process, path):
        deadline = time.monotonic()+5
        while not path.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail('Child exited before barrier: '+str(process.communicate()))
            time.sleep(.01)
        self.assertTrue(path.exists(), 'Child did not reach its bounded barrier')

    def test_delayed_real_worker_cannot_acquire_lease_after_completed_sleep(self):
        environment = self.prepare_child_root(enabled_guard=True)
        operation = '12345678-1234-4234-8234-123456789abc'
        directory = self.api.data/'runtime-operations'/operation
        write_json(directory/'request.json', {'action': 'stop'})
        script = '''
import json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from comfy_workflow_api import sleep_cycle, sleep_guard, runtime_operation_worker as worker
root = Path(sys.argv[1])
sleep_cycle.boot_id = lambda: 'boot'
first = True
def status():
    global first
    if first:
        first = False
        (root/'worker-waiting').touch()
        deadline = time.monotonic()+8
        while not (root/'worker-release').exists():
            if time.monotonic() >= deadline: raise TimeoutError('worker barrier')
            time.sleep(.01)
    return {'ready': False, 'service_instance_id': 'synthetic', 'queue': {}}
def execute(*args, **kwargs):
    (root/'unsafe-mutation').touch()
    return {'ready': False}
manager = SimpleNamespace(status=status, execute=execute)
with patch.object(worker, 'manager_for', return_value=manager), \
     patch.object(worker, 'meta_for', return_value={'stale_copy': False}), \
     patch.object(sleep_guard, 'matches', return_value=True), \
     patch.object(sleep_guard, 'start', side_effect=lambda api: (root/'unsafe-inhibitor').touch()), \
     patch.object(sleep_guard, 'status', return_value={}):
    print(json.dumps(worker.execute(root, sys.argv[2])))
'''
        process = subprocess.Popen([sys.executable, '-c', script, str(self.api.root), operation],
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        write_json(directory/'receipt.json', {'worker': {'pid': process.pid},
                                              'admission_epoch': cycle.admission_epoch(self.api)})
        self.wait_child_marker(process, self.api.root/'worker-waiting')
        result = cycle.pre(self.api, self.manager)
        cycle.finalize(self.api, self.manager, result['cycle_token'])
        (self.api.root/'worker-release').touch()
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output+errors)
        response = json.loads(output)
        self.assertEqual(response['error']['type'], 'SleepPending')
        self.assertFalse((self.api.root/'unsafe-mutation').exists())
        self.assertFalse((self.api.root/'unsafe-inhibitor').exists())
        self.assertFalse(list((self.api.data/'sleep-guard/leases').glob('*.json')))

    def test_old_boot_dispatch_epoch_cannot_create_a_lease(self):
        epoch = cycle.admission_epoch(self.api)
        with patch.object(cycle, 'boot_id', return_value='new-boot'), patch.object(sleep_guard, 'start') as start:
            with self.assertRaisesRegex(cycle.SleepPending, 'changed after'):
                with sleep_guard.hold(self.api, expected_sleep_epoch=epoch):
                    self.fail('Old boot operation acquired a lease')
        start.assert_not_called()
        self.assertFalse(list((self.api.data/'sleep-guard/leases').glob('*.json')))

    def test_real_manager_waiting_on_control_lock_rechecks_dispatch_epoch(self):
        environment = self.prepare_child_root()
        path = Path(__file__).parents[1]/'scripts/comfyui-service.py'
        script = '''
import importlib.util, json, sys
from pathlib import Path
from comfy_workflow_api import sleep_cycle
root = Path(sys.argv[1])
sleep_cycle.boot_id = lambda: 'boot'
spec = importlib.util.spec_from_file_location('delayed_manager', sys.argv[2])
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
manager._stop = lambda: (root/'unsafe-mutation').touch()
epoch = json.loads((root/'dispatch-epoch.json').read_text())
(root/'manager-waiting').touch()
try:
    manager.execute('stop', expected_sleep_epoch=epoch)
except sleep_cycle.SleepPending:
    print('epoch rejected')
'''
        write_json(self.api.root/'dispatch-epoch.json', cycle.admission_epoch(self.api))
        folder = self.api.data/'comfyui-service'
        folder.mkdir(parents=True)
        with (folder/'control.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            process = subprocess.Popen([sys.executable, '-c', script, str(self.api.root), str(path)],
                                       env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            self.wait_child_marker(process, self.api.root/'manager-waiting')
            result = cycle.pre(self.api, self.manager)
            cycle.finalize(self.api, self.manager, result['cycle_token'])
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, output+errors)
        self.assertIn('epoch rejected', output)
        self.assertFalse((self.api.root/'unsafe-mutation').exists())

    def test_uncertain_partial_stop_keeps_gate_and_abort_cannot_clear_it(self):
        self.manager.status.return_value = self.running
        self.manager.execute.return_value = {**self.running, 'blocked': True}
        with self.assertRaisesRegex(RuntimeError, 'termination'):
            cycle.pre(self.api, self.manager)
        self.assertEqual(cycle.status(self.api)['phase'], 'restore_failed')
        with self.assertRaisesRegex(cycle.SleepPending, 'uncertain'):
            cycle.abort(self.api, self.manager)
        self.assertTrue(cycle.status(self.api)['active'])

    def test_abort_can_cancel_an_attempt_that_provably_sent_no_shutdown_signal(self):
        self.record('restore_failed', stop_confirmed=False, service_instance_id='old',
                    created_at='2026-11-01T01:00:00+00:00')
        self.manager.status.return_value = {**self.running, 'ready': True, 'recorded_phase': 'running'}
        result = cycle.abort(self.api, self.manager, 'token')
        self.assertEqual(result['phase'], 'aborted')
        self.assertTrue(result['aborted_before_signal'])
        self.assertFalse(result['needs_restore'])
        self.assertFalse(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_abort_does_not_guess_when_readiness_identity_or_stop_evidence_disagree(self):
        current = {**self.running, 'ready': True, 'recorded_phase': 'running'}
        for changes in ({'ready': False}, {'service_instance_id': 'other'}, {'recorded_phase': 'stopping'},
                        {'signals': [{'at': '2026-11-01T01:00:01+00:00', 'signal': 'SIGINT'}]},
                        {'graceful_shutdown': {'started_at': '2026-11-01T01:00:01+00:00'}},
                        {'signals': [{}]}, {'graceful_shutdown': {}}, {'graceful_shutdown': {'unknown': True}}):
            self.record('restore_failed', stop_confirmed=False, service_instance_id='old',
                        created_at='2026-11-01T01:00:00+00:00')
            self.manager.status.return_value = {**current, **changes}
            with self.assertRaises(cycle.SleepPending):
                cycle.abort(self.api, self.manager, 'token')
            self.assertTrue(cycle.status(self.api)['active'])

    def test_real_module_entrypoint_shares_authorization_with_real_manager_admission(self):
        # Host/process observation and the final stop action are synthetic. The
        # -m entrypoint, context token, execute(), admission(), and disk gate are
        # production code across a real subprocess boundary.
        root = self.api.root
        (root/'config').mkdir()
        (root/'config/settings.toml').write_text('base_url="http://127.0.0.1:8188"\ndata_dir="data"\nrequest_timeout=2\n')
        (root/'scripts').mkdir()
        production = Path(__file__).parents[1]/'scripts/comfyui-service.py'
        shim = '''import importlib.util
import sys
from pathlib import Path
from comfy_workflow_api import sleep_cycle
# Darwin has no /proc boot ID. Substitute only host identity, in both module
# namespaces, leaving their ContextVars and production admission untouched.
if not Path('/proc/sys/kernel/random/boot_id').exists():
    sleep_cycle.boot_id = lambda: 'test-boot'
    sys.modules['__main__'].boot_id = sleep_cycle.boot_id
spec = importlib.util.spec_from_file_location('real_manager', PRODUCTION)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
flag = Path(__file__).parents[1]/'stop-completed'
def status():
    if flag.exists():
        return {'owned_process_running': False, 'listeners': []}
    return {'owned_process_running': True, 'listeners': [{'owners': [123]}],
            'owned_listener': True, 'queue_known': True, 'service_instance_id': 'old',
            'queue': {'queue_running': [], 'queue_pending': []}}
def stop():
    flag.write_text('stopped inside real admitted execution')
    return status()
manager.status = status
manager._stop = stop
execute = manager.execute
queue_ids = manager.queue_ids
'''.replace('PRODUCTION', repr(str(production)))
        (root/'scripts/comfyui-service.py').write_text(shim)
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1]/'src'),
                       'COMFY_WORKFLOW_HOME': str(root)}
        result = subprocess.run([sys.executable, '-m', 'comfy_workflow_api.sleep_cycle',
                                 '--root', str(root), 'pre'], env=environment,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertNotIn('RuntimeWarning', result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['phase'], 'sleeping')
        self.assertTrue(receipt['stop_confirmed'])
        self.assertTrue((root/'stop-completed').exists())

    def test_off_service_remains_off_across_sleep(self):
        result = cycle.pre(self.api, self.manager)
        self.assertFalse(result['was_running'])
        final = cycle.finalize(self.api, self.manager, result['cycle_token'])
        self.assertEqual(final['phase'], 'complete')
        self.manager.execute.assert_not_called()

    def test_submit_and_fetch_lease_are_blocked_before_any_side_effect(self):
        self.record()
        with self.assertRaises(cycle.SleepPending):
            with lifecycle.submission(self.api, 'task', 'operation'):
                self.fail('submission entered')
        with patch.object(sleep_guard, 'start') as start:
            with self.assertRaises(cycle.SleepPending):
                with sleep_guard.hold(self.api, purpose='fetch'):
                    self.fail('fetch entered')
            start.assert_not_called()
        self.assertFalse(list((self.api.data/'comfyui-service/inflight').glob('*.json')))
        self.assertFalse(list((self.api.data/'sleep-guard/leases').glob('*.json')))

    def test_public_runtime_packet_cannot_authorize_itself_with_a_token(self):
        from comfy_workflow_api import runtime_bridge
        self.record()
        packet = {'action': 'ensure_ready', 'cycle_token': 'token', 'operation_id': 'operation'}
        with patch.object(runtime_bridge.subprocess, 'Popen') as spawn:
            with self.assertRaises(cycle.SleepPending):
                runtime_bridge.launch(self.api, self.api.root, packet, {})
            spawn.assert_not_called()

    def test_direct_manager_stop_obeys_gate_before_sending_signals(self):
        path = Path(__file__).parents[1]/'scripts/comfyui-service.py'
        spec = importlib.util.spec_from_file_location('gated_comfy_manager', path)
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        self.record()
        with patch.object(manager, 'DATA', self.api.data/'comfyui-service'), patch.object(manager, '_stop') as stop:
            with self.assertRaises(cycle.SleepPending):
                manager.execute('stop')
            stop.assert_not_called()

    def test_internal_authorization_cannot_leak_outside_its_context(self):
        self.record()
        with cycle.internal(self.api, 'token'):
            cycle.assert_allowed(self.api)
        with self.assertRaises(cycle.SleepPending):
            cycle.assert_allowed(self.api)
        with self.assertRaises(cycle.SleepPending):
            with cycle.internal(self.api, 'old-token'):
                self.fail('stale token entered')

    def test_internal_authorization_is_bound_to_data_directory(self):
        self.record()
        other = SimpleNamespace(data=self.api.data/'different')
        cycle._save(other, {'cycle_token': 'token', 'boot_id': 'boot'}, 'sleeping')
        with cycle.internal(self.api, 'token'):
            with self.assertRaises(cycle.SleepPending):
                cycle.assert_allowed(other)

    def test_resume_requires_three_consecutive_healthy_samples(self):
        self.record()
        self.manager.execute.return_value = {'ready': True, 'service_instance_id': 'new'}
        fake_probe = SimpleNamespace(gpu_probe=Mock(side_effect=[{'status': 'ok'}, {'status': 'unknown'},
                                                               {'status': 'ok'}, {'status': 'ok'}, {'status': 'ok'}]))
        with patch.dict('sys.modules', {'comfy_workflow_api.hardware_probe': fake_probe}), patch.object(cycle.time, 'sleep'):
            result = cycle.resume_owner(self.api, self.manager)
        self.assertEqual(fake_probe.gpu_probe.call_count, 5)
        self.assertEqual(result['phase'], 'restored')
        self.assertTrue(cycle.status(self.api)['active'])
        self.manager.execute.assert_called_once_with('ensure_ready')
        self.manager.status.return_value = {'ready': True, 'service_instance_id': 'new'}
        self.assertEqual(cycle.finalize(self.api, self.manager, 'token')['phase'], 'complete')

    def test_gpu_timeout_keeps_gate_and_never_starts_comfy(self):
        self.record()
        fake_probe = SimpleNamespace(gpu_probe=Mock(return_value={'status': 'unknown'}))
        with patch.dict('sys.modules', {'comfy_workflow_api.hardware_probe': fake_probe}):
            result = cycle.resume_owner(self.api, self.manager, gpu_wait_seconds=0)
        self.assertEqual(result['phase'], 'restore_failed')
        self.assertFalse(result['success'])
        self.assertTrue(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_internal_deadline_is_recorded_without_clearing_gate(self):
        self.record()
        fake_probe = SimpleNamespace(gpu_probe=Mock(side_effect=cycle.RestoreDeadline('deadline')))
        with patch.dict('sys.modules', {'comfy_workflow_api.hardware_probe': fake_probe}):
            result = cycle.resume_owner(self.api, self.manager)
        self.assertEqual(result['phase'], 'restore_failed')
        self.assertEqual(result['error'], 'deadline')

    def test_finalize_requires_matching_token_and_current_instance(self):
        self.record('restored', success=True, restored_instance_id='new')
        for token in (None, 'old-token'):
            with self.assertRaises((ValueError, cycle.SleepPending)):
                cycle.finalize(self.api, self.manager, token)
        self.manager.status.return_value = {'ready': True, 'service_instance_id': 'different'}
        with self.assertRaisesRegex(cycle.SleepPending, 'instance changed'):
            cycle.finalize(self.api, self.manager, 'token')
        self.assertTrue(cycle.status(self.api)['active'])

    def test_retry_preserves_gate_and_stops_failed_owned_instance_first(self):
        self.record('restore_failed')
        self.manager.status.side_effect = [self.running, self.off]
        result = cycle.retry(self.api, self.manager, 'token')
        self.assertTrue(result['success'])
        self.assertEqual(result['phase'], 'restoring')
        self.assertTrue(cycle.status(self.api)['active'])
        self.manager.execute.assert_called_once_with('stop')

    def test_new_sleep_cannot_overwrite_restoration_token(self):
        self.record('restoring')
        before = (cycle.home(self.api)/'state.json').read_bytes()
        with self.assertRaises(cycle.SleepPending):
            cycle.pre(self.api, self.manager)
        self.assertEqual(before, (cycle.home(self.api)/'state.json').read_bytes())

    def test_abort_after_successful_stop_requests_restore_without_releasing_gate(self):
        self.record()
        result = cycle.abort(self.api, self.manager, 'token')
        self.assertTrue(result['needs_restore'])
        self.assertTrue(result['success'])
        self.assertEqual(result['phase'], 'restoring')
        self.assertTrue(cycle.status(self.api)['active'])

    def test_coordinator_failure_keeps_gate_without_signalling(self):
        self.record('restoring')
        result = cycle.fail(self.api, 'token', 'owner start failed')
        self.assertEqual(result['phase'], 'restore_failed')
        self.assertEqual(result['error'], 'owner start failed')
        self.assertTrue(cycle.status(self.api)['active'])
        self.manager.execute.assert_not_called()

    def test_coordinator_failure_requires_matching_active_token(self):
        self.record('restoring')
        for token in (None, 'old-token'):
            with self.assertRaises((ValueError, cycle.SleepPending)):
                cycle.fail(self.api, token, 'failure')
        self.record('complete')
        with self.assertRaises(cycle.SleepPending):
            cycle.fail(self.api, 'token', 'late failure')
