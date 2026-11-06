from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid

from comfy_workflow_api import runtime_bridge as bridge
from comfy_workflow_api import runtime_operation_worker as worker
from comfy_workflow_api import hardware_monitor, sleep_cycle


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.api = SimpleNamespace(data=self.root/'data')
        self.manager = SimpleNamespace(status=lambda:{'ready':True,'healthy':True,'queue':{},'service_instance_id':'new','resume':{'resume_id':'resume2'},'observed_at':'current'})
        self.packet = {'root':str(self.root),'action':'ensure_ready','operation_id':str(uuid.uuid4()),'wait_seconds':0,
                       'client_metadata':{'plugin_hash':'current'}}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self):
        with patch.object(bridge,'manager_for',return_value=self.manager),patch.object(bridge,'WorkflowAPI',return_value=self.api),patch.object(bridge,'meta_for',return_value={'stale_copy':False}),patch.object(bridge.sleep_guard,'status',return_value={}),patch.object(bridge,'power_status',return_value={}):
            return bridge.invoke(self.packet)

    def test_readonly_status_never_launches_or_writes(self):
        self.packet['action']='status'
        with patch.object(bridge,'launch') as launch:
            self.assertTrue(self.invoke()['ready'])
        launch.assert_not_called()
        self.assertFalse(self.api.data.exists())

    def test_stale_copy_blocks_mutations(self):
        with patch.object(bridge,'manager_for',return_value=self.manager),patch.object(bridge,'WorkflowAPI',return_value=self.api),patch.object(bridge,'meta_for',return_value={'stale_copy':True}),patch.object(bridge,'launch') as launch:
            self.assertFalse(bridge.invoke(self.packet)['success'])
        launch.assert_not_called()

    def test_launch_is_detached_and_records_identity_command_log(self):
        with patch.object(bridge.subprocess,'Popen',return_value=SimpleNamespace(pid=123)) as popen,patch.object(bridge.sleep_guard,'identity',return_value={'pid':123}),patch.object(bridge.sleep_guard,'matches',return_value=True),patch.object(bridge.os,'getsid',return_value=123),patch.object(bridge.os,'getpgid',return_value=123):
            result=bridge.launch(self.api,self.root,self.packet,{})
            self.assertEqual(result['state'],'in_progress')
            self.assertFalse(result['ready'])
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(popen.call_args.kwargs['stdin'],bridge.subprocess.DEVNULL)
            again=bridge.launch(self.api,self.root,self.packet,{})
            self.assertEqual(again['operation_id'],self.packet['operation_id'])
            self.assertEqual(popen.call_count,1)
        receipt=json.loads((self.api.data/'runtime-operations'/self.packet['operation_id']/'receipt.json').read_text())
        self.assertEqual(receipt['worker']['pid'],123)
        self.assertTrue(receipt['command'])
        self.assertTrue(receipt['log'].endswith('worker.log'))
        self.assertEqual(receipt['admission_epoch'], sleep_cycle.admission_epoch(self.api))

    def test_power_status_uses_gpu_cache_without_reading_gpu_sysfs(self):
        cached = {'stale': False, 'gpu': {'status': 'ok', 'observed_at': 'cached-time', 'gpus': [
            {'pci': '0000:03:00.0', 'utilization_percent': 42, 'vram_used_bytes': 123,
             'vram_total_bytes': 456, 'runtime_status': 'active',
             'power_watts': 46.5, 'power_sensor': 'power1_average'}]}}
        def read_system(path):
            self.assertNotIn('/sys/class/drm', str(path))
            self.assertNotIn('/sys/bus/pci', str(path))
            return None
        with patch.object(hardware_monitor, 'status', return_value=cached) as status, \
             patch.object(bridge, 'command', return_value={}), \
             patch.object(bridge, 'text', side_effect=read_system), \
             patch.object(Path, 'glob', side_effect=AssertionError('GPU enumeration is forbidden')):
            result = bridge.power_status(self.root)
        status.assert_called_once_with(self.root)
        self.assertEqual(result['gpu_status'], 'ok')
        self.assertEqual(result['gpus'][0]['gpu_busy_percent'], 42)
        self.assertEqual(result['gpus'][0]['observed_at'], 'cached-time')
        self.assertFalse(result['gpus'][0]['stale'])
        self.assertEqual(result['gpus'][0]['power_watts'], 46.5)
        self.assertEqual(result['gpus'][0]['runtime_status'], 'active')

    def test_missing_gpu_snapshot_is_unknown_and_read_only(self):
        (self.root/'config').mkdir()
        (self.root/'config/settings.toml').write_text('data_dir="data"\n')
        with patch.object(bridge, 'command', return_value={}), \
             patch.object(bridge, 'text', return_value=None), \
             patch.object(Path, 'glob', side_effect=AssertionError('GPU enumeration is forbidden')):
            result = bridge.power_status(self.root)
        self.assertEqual(result['gpu_status'], 'unknown')
        self.assertTrue(result['gpu_stale'])
        self.assertIsNone(result['gpu_observed_at'])
        self.assertEqual(result['gpus'], [])
        self.assertFalse(self.api.data.exists())

    def test_old_gpu_cache_keeps_its_observation_time_and_is_unknown(self):
        cached = {'stale': True, 'gpu': {'status': 'ok', 'observed_at': 'old-time',
                                      'gpus': [{'pci': '0000:03:00.0', 'utilization_percent': 0}]}}
        with patch.object(hardware_monitor, 'status', return_value=cached), \
             patch.object(bridge, 'command', return_value={}), patch.object(bridge, 'text', return_value=None):
            result = bridge.power_status(self.root)
        self.assertEqual(result['gpu_status'], 'unknown')
        self.assertTrue(result['gpus'][0]['stale'])
        self.assertEqual(result['gpus'][0]['observed_at'], 'old-time')

    def test_dead_worker_has_explicit_recover_escape_without_duplicate_retry(self):
        operations=self.api.data/'runtime-operations'
        operations.mkdir(parents=True)
        (operations/'active.json').write_text(json.dumps({'operation_id':str(uuid.uuid4())}))
        unknown={'success':False,'state':'outcome_unknown','operation_id':'old'}
        with patch.object(bridge,'operation_status',return_value=unknown),patch.object(bridge.subprocess,'Popen',return_value=SimpleNamespace(pid=123)) as popen,patch.object(bridge.sleep_guard,'identity',return_value={'pid':123}),patch.object(bridge.os,'getsid',return_value=123),patch.object(bridge.os,'getpgid',return_value=123):
            self.assertTrue(bridge.launch(self.api,self.root,self.packet,{})['existing_operation'])
            popen.assert_not_called()
            self.packet['action']='recover'
            bridge.launch(self.api,self.root,self.packet,{})
            popen.assert_called_once()

    def test_operation_query_cannot_resurrect_historical_ready(self):
        self.packet.update(action='status',query_operation_id=str(uuid.uuid4()))
        self.manager.status=lambda:{'ready':False,'service_instance_id':'new','resume':{'resume_id':'resume2'},'queue':{}}
        historical={'success':True,'healthy':True,'ready':True,'state':'ready','service':{'ready':True,'service_instance_id':'old','resume':{'resume_id':'resume1'}}}
        with patch.object(bridge,'operation_status',return_value=historical):
            result=self.invoke()
        self.assertFalse(result['ready'])
        self.assertFalse(result['healthy'])
        self.assertTrue(result['operation_result_stale'])
        self.assertFalse(result['service']['ready'])

    def test_unknown_queue_counts_are_null_not_empty(self):
        self.manager.status=lambda:{'ready':False,'error':'timeout'}
        self.assertIsNone(bridge.service_summary(self.manager)['queue']['queue_running'])

    def confirmation_result(self, expires_seconds=300):
        from datetime import datetime,timezone,timedelta
        return {'success':False,'ready':False,'state':'approval_required','approval_required':True,
                'recovery_request':{'request_id':str(uuid.uuid4()),'service_instance_id':'new',
                    'processes':[{'pid':123}],
                    'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=expires_seconds)).isoformat()},
                'service':{'service_instance_id':'new','resume':{'resume_id':'resume2'}}}

    def test_old_approval_cannot_block_after_its_process_has_exited(self):
        self.packet.update(action='status',query_operation_id=str(uuid.uuid4()))
        self.manager.status=lambda:{'ready':False,'phase':'stopped','service_instance_id':'new',
                                    'resume':{'resume_id':'resume2'},'listeners':[]}
        self.manager.incarnation_exited=lambda _:True
        with patch.object(bridge,'operation_status',return_value=self.confirmation_result()),patch.object(bridge,'launch') as launch:
            result=self.invoke()
        self.assertFalse(result['approval_required'])
        self.assertTrue(result['approval_obsolete'])
        self.assertNotIn('recovery_request',result)
        self.assertEqual(result['next_action'],'ensure_ready')
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(result['operation_state'],'approval_required')
        self.assertFalse(self.api.data.exists())
        launch.assert_not_called()

    def test_legacy_live_confirmation_now_directs_automatic_recover(self):
        result=self.confirmation_result()
        self.manager.incarnation_exited=lambda _:False
        bridge.reconcile_confirmation(result,{'ready':False,'service_instance_id':'new'},self.manager)
        self.assertFalse(result['approval_required'])
        self.assertNotIn('recovery_request',result)
        self.assertEqual(result['next_action'],'recover')

    def test_expired_live_escalation_requests_new_diagnosis_not_old_command(self):
        result=self.confirmation_result(-1)
        self.manager.incarnation_exited=lambda _:False
        bridge.reconcile_confirmation(result,{'ready':False,'service_instance_id':'new'},self.manager)
        self.assertTrue(result['confirmation_expired'])
        self.assertFalse(result['approval_required'])
        self.assertNotIn('recovery_request',result)
        self.assertEqual(result['next_action'],'recover')

    def test_new_ready_instance_invalidates_old_confirmation(self):
        result=self.confirmation_result()
        bridge.reconcile_confirmation(result,{'ready':True,'service_instance_id':'newer'},self.manager)
        self.assertTrue(result['ready'])
        self.assertTrue(result['success'])
        self.assertFalse(result['approval_required'])
        self.assertEqual(result['state'],'ready')

    def test_old_confirmation_does_not_report_a_new_stopping_instance_as_stopped(self):
        result=self.confirmation_result()
        self.manager.incarnation_exited=lambda _:True
        live={'ready':False,'service_instance_id':'newer','owned_process_running':True,'phase':'stopping'}
        bridge.reconcile_confirmation(result,live,self.manager)
        self.assertEqual(result['state'],'in_progress')
        self.assertEqual(result['next_action'],'status')
        self.assertFalse(result['approval_required'])

    def test_worker_finally_releases_lease_and_persists_error(self):
        directory=self.api.data/'runtime-operations'/self.packet['operation_id']
        directory.mkdir(parents=True)
        (directory/'request.json').write_text(json.dumps(self.packet))
        (directory/'receipt.json').write_text(json.dumps({'worker':{'pid':worker.os.getpid()},
            'admission_epoch': sleep_cycle.admission_epoch(self.api)}))
        events=[]
        @contextmanager
        def hold(api,**kwargs):
            events.append('acquire')
            try:yield
            finally:events.append('release')
        manager=SimpleNamespace(status=self.manager.status,execute=Mock(side_effect=TimeoutError('smoke exceeded')))
        with patch.object(worker,'WorkflowAPI',return_value=self.api),patch.object(worker,'manager_for',return_value=manager),patch.object(worker,'meta_for',return_value={'stale_copy':False}),patch.object(worker.sleep_guard,'matches',return_value=True),patch.object(worker.sleep_guard,'hold',side_effect=hold),patch.object(worker.sleep_guard,'status',return_value={}):
            result=worker.execute(self.root,self.packet['operation_id'])
        self.assertEqual(events,['acquire','release'])
        self.assertFalse(result['success'])
        self.assertFalse(result['error']['retry_automatically'])
        final=json.loads((directory/'receipt.json').read_text())
        self.assertTrue(final['finished_at'])
        manager.execute.assert_called_once()

    def test_legacy_worker_without_dispatch_epoch_fails_before_lease_or_mutation(self):
        directory = self.api.data/'runtime-operations'/self.packet['operation_id']
        directory.mkdir(parents=True)
        (directory/'request.json').write_text(json.dumps(self.packet))
        (directory/'receipt.json').write_text(json.dumps({'worker': {'pid': worker.os.getpid()}}))
        self.api.settings = {'sleep_guard': {'enabled': True}}
        manager = SimpleNamespace(status=self.manager.status, execute=Mock())
        with patch.object(worker, 'WorkflowAPI', return_value=self.api), \
             patch.object(worker, 'manager_for', return_value=manager), \
             patch.object(worker, 'meta_for', return_value={'stale_copy': False}), \
             patch.object(worker.sleep_guard, 'matches', return_value=True), \
             patch.object(worker.sleep_guard, 'start') as start, \
             patch.object(worker.sleep_guard, 'status', return_value={}):
            result = worker.execute(self.root, self.packet['operation_id'])
        self.assertEqual(result['error']['type'], 'SleepPending')
        manager.execute.assert_not_called()
        start.assert_not_called()
        self.assertFalse(list((self.api.data/'sleep-guard/leases').glob('*.json')))

    def test_failed_smoke_cannot_be_reported_as_successful_finished_operation(self):
        from contextlib import nullcontext
        directory=self.api.data/'runtime-operations'/self.packet['operation_id']
        directory.mkdir(parents=True)
        (directory/'request.json').write_text(json.dumps(self.packet))
        (directory/'receipt.json').write_text(json.dumps({'worker':{'pid':worker.os.getpid()},
            'admission_epoch': sleep_cycle.admission_epoch(self.api)}))
        manager=SimpleNamespace(status=self.manager.status,
            execute=Mock(return_value={'ready':False,'smoke_failed':True,'smoke':{'error':'observation timed out'}}))
        with patch.object(worker,'WorkflowAPI',return_value=self.api), \
             patch.object(worker,'manager_for',return_value=manager), \
             patch.object(worker,'meta_for',return_value={'stale_copy':False}), \
             patch.object(worker.sleep_guard,'matches',return_value=True), \
             patch.object(worker.sleep_guard,'hold',return_value=nullcontext()), \
             patch.object(worker.sleep_guard,'status',return_value={}):
            result=worker.execute(self.root,self.packet['operation_id'])
        self.assertFalse(result['success'])
        self.assertFalse(result['ready'])
        self.assertEqual(result['state'],'unverified')
        self.assertEqual(result['next_action'],'recover')
        self.assertEqual(result['error']['type'],'RenderVerificationFailed')
