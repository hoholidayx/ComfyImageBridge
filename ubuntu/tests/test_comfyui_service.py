from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/comfyui-service.py'
spec = importlib.util.spec_from_file_location('comfyui_service_test', SCRIPT)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        for name, value in [('DATA', self.data), ('STATE', self.data/'state.json'), ('LOG', self.data/'comfyui.log')]:
            p = patch.object(service, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.root = {'pid':123, 'uid':os.getuid(), 'start_ticks':'10', 'command':['uv'], 'boot_id':'boot'}
        self.child = {'pid':124, 'uid':os.getuid(), 'start_ticks':'11', 'command':['python'], 'boot_id':'boot'}
        self.state = {'process':self.root, 'managed_processes':[self.child], 'process_group':123,
                      'service_instance_id':'instance', 'phase':'running'}
        service.write_json(service.STATE, self.state)

    def current(self, queue=None):
        return {**self.state, 'owned_process_running':True, 'owned_listener':True,
                'listeners':[{'owners':[self.child]}], 'http_healthy':True, 'ready':False,
                'render_verified':False, 'queue_known':True,
                'queue':queue or {'queue_running':[], 'queue_pending':[]},
                'resume':{'resume_id':'boot:1', 'source':'boottime_minus_monotonic'}}

    def test_fingerprints_cover_the_configured_launch_script(self):
        import hashlib
        comfy = self.data/'ComfyUI'
        comfy.mkdir()
        for name in ('pyproject.toml', 'uv.lock'):
            (comfy/name).write_text(name)
        run = self.data/'custom-start.sh'
        run.write_text('custom configuration')
        with patch.object(service, 'COMFY', comfy), patch.object(service, 'RUN', run):
            observed = service.fingerprints()
        self.assertEqual(observed['run_script'], hashlib.sha256(run.read_bytes()).hexdigest())
        self.assertEqual(set(observed), {'pyproject.toml', 'uv.lock', 'run_script'})

    def test_identity_checks_boot_before_legacy_compatibility(self):
        with patch.object(service.service_readiness, 'boot_id', return_value='new-boot'), \
             patch.object(service.service_readiness, 'matches', return_value=True):
            self.assertFalse(service.process_matches(self.root))

    def test_complete_listener_inventory_retains_unknown_owner_and_ignores_timewait(self):
        proc = self.data/'proc'
        (proc/'net').mkdir(parents=True)
        header = 'header\n'
        row = lambda state,inode: f'0: 00000000:1FFC 00000000:0000 {state} 0 0 0 1000 0 {inode}\n'
        (proc/'net/tcp').write_text(header+row('0A','900')+row('06','901'))
        (proc/'net/tcp6').write_text(header)
        factory = lambda value: proc/str(value)[len('/proc/'): ] if str(value).startswith('/proc/') else proc if str(value)=='/proc' else Path(value)
        with patch.object(service.service_readiness, 'Path', side_effect=factory):
            inventory = service.listeners()
        self.assertEqual([v['inode'] for v in inventory], ['900'])
        self.assertEqual(inventory[0]['owners'], [])

    def test_unknown_listener_prevents_adoption_even_when_http_alive(self):
        value = self.current()
        value['owned_listener'] = False
        with patch.object(service, 'status', return_value=value), patch.object(service.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'refusing adoption'):
                service._start()
            spawn.assert_not_called()

    def test_legacy_owned_service_can_be_adopted_but_is_not_yet_ready(self):
        value = self.current()
        with patch.object(service, 'status', return_value=value):
            adopted = service._start()
        self.assertTrue(adopted['reused_existing'])
        self.assertFalse(adopted['ready'])

    def verified_status(self, resume='boot:1', expected_hash='hash', owners=True, verified_at='old'):
        state = {**self.state, 'launch_resume_id':'boot:1', 'verification':{
            'success':True, 'service_instance_id':'instance', 'resume_id':'boot:1', 'workflow_hash':'hash', 'verified_at':verified_at}}
        service.write_json(service.STATE, state)
        socket = {'owners':[self.child]} if owners else {'owners':[]}
        with patch.object(service, '_known_members', return_value={123:self.root}), \
             patch.object(service, 'listeners', return_value=[socket]), \
             patch.object(service, '_owned_owner', return_value=True), \
             patch.object(service, 'resume_evidence', return_value={'resume_id':resume, 'source':'test'}), \
             patch.object(service, 'workflow_hash', return_value=expected_hash), \
             patch.object(service, 'health', return_value={'http_healthy':True,'queue_known':True,'queue':{}}):
            return service.status()

    def test_http_liveness_is_not_render_readiness_after_resume(self):
        value = self.verified_status(resume='boot:12')
        self.assertTrue(value['http_healthy'])
        self.assertFalse(value['healthy'])
        self.assertTrue(value['crossed_resume'])
        self.assertTrue(value['stale']['render_verification'])

    def test_verified_instance_is_ready_and_status_does_not_write(self):
        value = self.verified_status()
        self.assertTrue(value['ready'])
        self.assertEqual(value['readiness_verified_at'], 'old')
        self.assertIn('observed_at', value)
        # No observation timestamp is persisted by status().
        self.assertNotIn('observed_at', service.read_json(service.STATE))

    def test_code_change_invalidates_readiness(self):
        self.assertFalse(self.verified_status(expected_hash='new-hash')['ready'])

    def test_known_execution_failure_invalidates_same_awake_epoch(self):
        service.write_json(self.data/'invalidations/instance.json', {'service_instance_id':'instance',
            'observed_at':'2026-11-06T01:00:00+00:00', 'reason':'execution_error', 'task_id':'job'})
        value = self.verified_status(verified_at='2026-11-06T00:00:00+00:00')
        self.assertFalse(value['render_verified'])
        self.assertEqual(value['readiness_invalidation']['reason'], 'execution_error')

    def test_new_successful_probe_supersedes_old_failure_marker(self):
        service.write_json(self.data/'invalidations/instance.json', {'service_instance_id':'instance',
            'observed_at':'2026-11-06T00:00:00+00:00', 'reason':'stalled'})
        value = self.verified_status(verified_at='2026-11-06T01:00:00+00:00')
        self.assertTrue(value['ready'])
        self.assertIsNone(value['readiness_invalidation'])

    def test_unknown_ownership_prevents_verified_readiness(self):
        self.assertFalse(self.verified_status(owners=False)['ready'])

    def test_image_admission_requires_current_owned_comfyui_listener(self):
        probe = service.service_readiness
        base_url = 'http://127.0.0.1:8188'
        api = SimpleNamespace(root=self.data, data=self.data, settings={'base_url': base_url},
                              client=SimpleNamespace(base_url=base_url))
        state = {**self.state, 'verification': {
            'success': True, 'service_instance_id': 'instance', 'resume_id': 'boot:1',
            'workflow_hash': 'hash',
        }}
        service.write_json(self.data/'comfyui-service/state.json', state)
        with patch.object(probe, 'resume_evidence', return_value={'resume_id': 'boot:1'}), \
             patch.object(probe, 'workflow_hash', return_value='hash'), \
             patch.object(probe, 'known_members', return_value={123: self.root}), \
             patch.object(probe, 'listeners', return_value=[{'owners': [self.child]}]), \
             patch.object(probe, 'owned_owner', side_effect=[True, False]), \
             patch.object(probe, 'health', return_value={'http_healthy': True}) as health:
            self.assertEqual(probe.ready_instance(api), 'instance')
            self.assertIsNone(probe.ready_instance(api))
        health.assert_called_once()

    def test_resume_clock_jitter_is_tolerated_but_unknown_is_not_proof(self):
        self.assertTrue(service.same_resume('boot:1.00', 'boot:1.01'))
        self.assertFalse(service.same_resume('boot:1.00', 'boot:2.00'))
        self.assertFalse(service.same_resume('old:1', 'new:1'))
        self.assertFalse(service.same_resume(None, None))

    def test_active_job_refuses_without_sending_signal(self):
        value=self.current({'queue_running':[[0,'job']],'queue_pending':[]})
        with patch.object(service,'status',return_value=value), \
             patch.object(service,'_known_members',return_value={123:self.root}), \
             patch.object(service,'group',return_value=(123,123)), \
             patch.object(service,'_signal_one') as send:
            result=service._stop()
        self.assertTrue(result['busy'])
        self.assertFalse(result['approval_required'])
        send.assert_not_called()

    def test_changed_group_refuses_all_signals(self):
        with patch.object(service, 'status', return_value=self.current()), \
             patch.object(service, '_known_members', return_value={123:self.root}), \
             patch.object(service, 'group', return_value=(999,999)), \
             patch.object(service, '_signal_one') as send:
            with self.assertRaisesRegex(RuntimeError, 'Session/process group'):
                service._stop()
            send.assert_not_called()

    def test_identity_change_immediately_before_signal_is_refused(self):
        with patch.object(service, 'process_matches', return_value=False), patch.object(service.os, 'kill') as send:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                service._signal_one(self.child, signal.SIGTERM)
            send.assert_not_called()

    def test_force_escalates_and_tracks_child_after_parent_exit(self):
        alive = [True]
        sent = []
        def signal_child(item, sig):
            sent.append((item['pid'], sig))
            if sig == signal.SIGTERM:
                alive[0] = False
        with patch.object(service, 'status', side_effect=lambda:self.current()), \
             patch.object(service, '_known_members', side_effect=lambda s:{124:self.child} if alive[0] else {}), \
             patch.object(service, '_collect_members', side_effect=lambda s:{124:self.child} if alive[0] else {}), \
             patch.object(service, 'process_matches', return_value=True), \
             patch.object(service, 'group', return_value=(123,123)), \
             patch.object(service, '_signal_one', side_effect=signal_child), \
             patch.object(service.time, 'monotonic', side_effect=iter(range(0,1000,11))), \
             patch.object(service.time, 'sleep'):
            service._stop(force=True)
        self.assertEqual(sent, [(124, signal.SIGINT),(124, signal.SIGTERM)])
        ledger = service.read_json(self.data/'retired-instances/instance.json')
        self.assertTrue(ledger['verified'])

    def test_busy_queue_never_enqueues_smoke(self):
        value = self.current({'queue_running':[[0,'other']], 'queue_pending':[]})
        with patch.object(service, 'status', return_value=value), patch.object(service.CLIENT, 'request') as request:
            result = service._smoke()
        self.assertTrue(result['busy'])
        request.assert_not_called()

    def test_transient_probe_read_timeout_is_retried_without_submitting(self):
        receipt={'service_instance_id':'instance'}
        with patch.object(service.CLIENT,'request',side_effect=[TimeoutError('model load blocks HTTP'),{'done':True}]) as request, \
             patch.object(service.time,'monotonic',return_value=10),patch.object(service.time,'sleep'):
            result=service._probe_read('/history/known-prompt',receipt,self.data,120)
        self.assertEqual(result,{'done':True})
        self.assertEqual(receipt['read_retries'],1)
        self.assertEqual([call.args[0] for call in request.call_args_list],['/history/known-prompt']*2)

    def test_probe_read_retry_is_bounded_by_execution_deadline(self):
        with patch.object(service.CLIENT,'request',side_effect=TimeoutError('blocked')) as request, \
             patch.object(service.time,'monotonic',return_value=121):
            with self.assertRaisesRegex(TimeoutError,'no generation was resubmitted'):
                service._probe_read('/history/known-prompt',{'service_instance_id':'instance'},self.data,120)
        self.assertEqual(request.call_count,1)

    def test_graceful_stop_waits_for_slow_exit_and_signals_python_only_once(self):
        clock = [0.0]
        members = {123:self.root, 124:self.child}
        def live(_):
            return members if clock[0] < 35 else {}
        with patch.object(service, 'status', side_effect=lambda: self.current()), \
             patch.object(service, '_known_members', side_effect=live), \
             patch.object(service, '_collect_members', return_value=members), \
             patch.object(service, 'process_matches', return_value=True), \
             patch.object(service, 'incarnation_exited', side_effect=lambda _:clock[0] >= 35), \
             patch.object(service, 'group', return_value=(123,123)), \
             patch.object(service, '_signal_one') as send, \
             patch.object(service.time, 'monotonic', side_effect=lambda:clock[0]), \
             patch.object(service.time, 'sleep', side_effect=lambda _:clock.__setitem__(0, clock[0]+5)):
            service._stop()
        self.assertGreater(clock[0], 10)
        self.assertLess(clock[0], service.GRACEFUL_STOP_SECONDS)
        self.assertEqual(send.call_args_list, [unittest.mock.call(self.child, signal.SIGINT)])
        ledger = service.read_json(self.data/'retired-instances/instance.json')
        self.assertEqual(ledger['signals'][0]['pids'], [124])
        self.assertTrue(ledger['verified'])

    def test_existing_graceful_shutdown_waits_without_second_interrupt_or_http(self):
        self.state.update(phase='stopping', graceful_shutdown={
            'service_instance_id':'instance', 'queue_prompt_ids':[],
            'deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=110)).isoformat()})
        service.write_json(service.STATE,self.state)
        clock=[0.0]
        current={**self.current(), 'http_healthy':False, 'queue_known':False}
        with patch.object(service,'status',return_value=current), \
             patch.object(service,'_known_members',side_effect=lambda _:{124:self.child} if clock[0]<4 else {}), \
             patch.object(service,'incarnation_exited',side_effect=lambda _:clock[0]>=4), \
             patch.object(service,'group',return_value=(123,123)), \
             patch.object(service,'_signal_one') as send, \
             patch.object(service.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(service.time,'sleep',side_effect=lambda _:clock.__setitem__(0,clock[0]+2)):
            service._stop()
        send.assert_not_called()
        self.assertTrue((self.data/'retired-instances/instance.json').exists())

    def test_graceful_timeout_automatically_escalates_without_confirmation(self):
        clock=[0.0]
        alive={123:self.root,124:self.child}
        sent=[]
        def signal_one(item,sig):
            sent.append((item['pid'],sig))
            if sig == signal.SIGKILL:
                alive.pop(item['pid'],None)
        with patch.object(service,'status',return_value=self.current()), \
             patch.object(service,'_known_members',side_effect=lambda _:dict(alive)), \
             patch.object(service,'_collect_members',side_effect=lambda _:dict(alive)), \
             patch.object(service,'process_matches',side_effect=lambda item:item['pid'] in alive), \
             patch.object(service,'incarnation_exited',side_effect=lambda item:item['pid'] not in alive), \
             patch.object(service,'group',return_value=(123,123)), \
             patch.object(service,'_signal_one',side_effect=signal_one), \
             patch.object(service.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(service.time,'sleep',side_effect=lambda _:clock.__setitem__(0,clock[0]+1)):
            service._stop()
        self.assertEqual(clock[0],125)
        self.assertEqual(sent,[(124,signal.SIGINT),(124,signal.SIGTERM),(124,signal.SIGKILL),(123,signal.SIGKILL)])
        ledger=service.read_json(self.data/'retired-instances/instance.json')
        self.assertEqual([step['signal'] for step in ledger['signals']],['SIGINT','SIGTERM','SIGKILL'])
        self.assertTrue(ledger['verified'])

    def test_start_retires_a_delayed_exit_before_attempting_a_normal_launch(self):
        dead={**self.current(),'owned_process_running':False,'owned_listener':False,
              'listeners':[],'http_healthy':False,'processes_exited':True}
        with patch.object(service,'status',return_value=dead), \
             patch.object(service,'incarnation_exited',return_value=True), \
             patch.object(service,'fingerprints',return_value={}), \
             patch.object(service.subprocess,'run',side_effect=RuntimeError('preflight reached')):
            with self.assertRaisesRegex(RuntimeError,'preflight reached'):
                service._start()
        self.assertEqual(service.read_json(service.STATE)['phase'],'stopped')
        self.assertTrue((self.data/'retired-instances/instance.json').exists())

    def test_exited_process_is_reported_stopped_without_mutating_historical_state(self):
        before=service.STATE.read_bytes()
        with patch.object(service,'_known_members',return_value={}), \
             patch.object(service,'listeners',return_value=[]), \
             patch.object(service,'health',return_value={'http_healthy':False}), \
             patch.object(service,'incarnation_exited',return_value=True), \
             patch.object(service,'resume_evidence',return_value={'resume_id':'boot:1','source':'test'}), \
             patch.object(service,'workflow_hash',return_value='hash'):
            result=service.status()
        self.assertEqual(result['phase'],'stopped')
        self.assertEqual(result['recorded_phase'],'running')
        self.assertTrue(result['processes_exited'])
        self.assertEqual(before,service.STATE.read_bytes())

    def test_resume_idle_restarts_before_probe(self):
        initial = {**self.current(), 'crossed_resume':True}
        after = {**self.current(), 'crossed_resume':False}
        events = []
        with patch.object(service, 'status', side_effect=[initial,after]), \
             patch.object(service, '_stop', side_effect=lambda:events.append('stop') or {}), \
             patch.object(service, '_start', side_effect=lambda:events.append('start')), \
             patch.object(service, '_smoke', side_effect=lambda cold:events.append(('smoke',cold)) or {'ready':True}):
            self.assertTrue(service._ensure_ready()['ready'])
        self.assertEqual(events, ['stop','start',('smoke',True)])

    def test_gate_blocks_unknown_inflight_owner_and_cleans_own_gate(self):
        service.write_json(self.data/'inflight/a.json', {'owner':None})
        with self.assertRaisesRegex(RuntimeError, 'in flight'):
            with service.admission('op'):
                self.fail('gate should refuse')
        (self.data/'inflight/a.json').unlink()
        with patch.object(service, 'process_identity', return_value=self.root):
            with service.admission('op'):
                self.assertTrue(service.read_json(self.data/'admission.json')['blocked'])
        self.assertFalse((self.data/'admission.json').exists())

    def smoke_failure(self, kind):
        from types import SimpleNamespace
        value = self.current()
        def respond(path, method='GET', body=None, **kwargs):
            if path == '/prompt':
                if kind == 'configuration':
                    raise service.APIError(400, 'missing model node')
                return {'prompt_id':body['prompt_id']}
            if path.startswith('/history/'):
                prompt_id = path.rsplit('/',1)[-1]
                messages = [['execution_cached', {'nodes':['458']}]] if kind == 'cached' else [
                    ['execution_start', {'timestamp':1000}], ['execution_success', {'timestamp':32001}]]
                return {prompt_id:{'status':{'completed':True, 'messages':messages}, 'outputs':{}}}
            self.fail('Unexpected request: '+path)
        api = SimpleNamespace(_snapshot=lambda alias:({},{}))
        with patch.object(service, 'status', return_value=value), \
             patch.object(service, 'workflow_hash', return_value='hash'), \
             patch.object(service, 'compile_graph', return_value={'458':{'class_type':'SamplerCustom'}}), \
             patch('comfy_workflow_api.api.WorkflowAPI', return_value=api), \
             patch.object(service.CLIENT, 'request', side_effect=respond):
            return service._smoke()

    def test_cached_sampler_does_not_pass_smoke(self):
        result = self.smoke_failure('cached')
        self.assertFalse(result['smoke']['success'])
        self.assertIn('Sampler was cached', result['smoke']['error'])
        self.assertFalse(service.read_json(service.STATE)['verification']['success'])

    def test_first_poll_completion_after_deadline_is_not_healthy(self):
        result = self.smoke_failure('late')
        self.assertFalse(result['smoke']['success'])
        self.assertIn('after its execution deadline', result['smoke']['error'])

    def test_graph_rejection_is_configuration_failure(self):
        result = self.smoke_failure('configuration')
        self.assertEqual(result['smoke']['failure_kind'], 'configuration')
        with patch.object(service, 'status', return_value=self.current()), \
             patch.object(service, '_ensure_ready', return_value=result), patch.object(service, '_stop') as stop:
            recovered = service._recover()
        self.assertTrue(recovered['blocked'])
        stop.assert_not_called()

    def test_explicit_recover_always_rechecks_cached_idle_readiness(self):
        value = {**self.current(), 'ready':True}
        with patch.object(service, 'status', return_value=value), \
             patch.object(service, '_ensure_ready', return_value=value), \
             patch.object(service, '_smoke', return_value={'ready':True}) as smoke:
            self.assertTrue(service._recover()['ready'])
        smoke.assert_called_once_with(cold=False)

    def test_recover_automatically_restarts_a_suspect_owned_instance(self):
        value={**self.current(),'busy':True,'ready':False}
        with patch.object(service,'status',return_value=value), \
             patch.object(service,'_ensure_ready',return_value=value), \
             patch.object(service,'_stop',return_value={}) as stop, \
             patch.object(service,'_start') as start, \
             patch.object(service,'_smoke',return_value={'ready':True}):
            self.assertTrue(service._recover()['ready'])
        stop.assert_called_once_with(force=True)
        start.assert_called_once()

    def test_healthy_active_generation_is_not_killed_by_recover(self):
        value={**self.current({'queue_running':[[0,'job']],'queue_pending':[]}), 'ready':True}
        with patch.object(service,'status',return_value=value),patch.object(service,'_stop') as stop:
            self.assertTrue(service._recover()['busy'])
        stop.assert_not_called()

    def test_recovery_never_loops_after_an_attempt_already_replaced_the_instance(self):
        before=self.current()
        after={**before,'service_instance_id':'replacement','ready':False,'smoke_failed':True}
        with patch.object(service,'status',side_effect=[before,after]), \
             patch.object(service,'_ensure_ready',return_value=after),patch.object(service,'_stop') as stop:
            self.assertTrue(service._recover()['blocked'])
        stop.assert_not_called()

    def test_retirement_needs_concrete_exit_proof_and_preserves_receipt(self):
        self.state.update(signals=[{'signal':'SIGINT'}], retirement_history_snapshot={'job':{'outputs':{}}})
        with patch.object(service, 'incarnation_exited', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'still present'):
                service._retire(self.state)
        with patch.object(service, 'incarnation_exited', return_value=True):
            service._retire(self.state)
        ledger = service.read_json(self.data/'retired-instances/instance.json')
        self.assertEqual(ledger['signals'], [{'signal':'SIGINT'}])
        self.assertIn('job', ledger['history_snapshot'])
        self.assertEqual(ledger['recorded_processes'], [self.root,self.child])


if __name__ == '__main__':
    unittest.main()
