import copy
from datetime import datetime, timedelta, timezone
import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api import sleep_guard, task_diagnostics, lifecycle, batches
from comfy_workflow_api.client import Client
from comfy_workflow_api.storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    base_url = 'http://127.0.0.1:8188'
    def __init__(self):
        self.prompts = []
        self.pending, self.running, self.history, self.cancels = [], [], {}, []
        self.cooperate = True
    def request(self, path, method='GET', body=None, **kwargs):
        if path == '/prompt':
            self.prompts.append(body['prompt_id'])
            self.pending.append(body['prompt_id'])
            return {'prompt_id': body['prompt_id']}
        if path.startswith('/view?'):
            return (ROOT/'examples/robot.png').read_bytes()
        if path == '/queue':
            return {'queue_running': [[0,p] for p in self.running], 'queue_pending': [[0,p] for p in self.pending]}
        if path.startswith('/history/'):
            p = path.rsplit('/',1)[-1]
            return {p:self.history[p]} if p in self.history else {}
        if path.endswith('/cancel'):
            p = path.split('/')[-2]
            self.cancels.append(p)
            if p in self.pending:
                self.pending.remove(p)
            if self.cooperate and p in self.running:
                self.running.remove(p)
                self.history[p] = {'status': {'completed':False, 'messages':[['execution_interrupted',{}]]}}
            return {'cancelled':True}
        raise AssertionError(path)


class TaskReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for name in ('config', 'workflows'):
            shutil.copytree(ROOT/name, self.root/name)
        p = self.root/'config/settings.toml'
        p.write_text(p.read_text().replace('enabled = true','enabled = false'))
        self.client = FakeClient()
        self.api = WorkflowAPI(self.root, client=self.client)
    def tearDown(self):
        self.tmp.cleanup()
    def submit(self):
        return self.api.submit('qwen-image-2.1-t2i', {'prompt':'colored cube','output_size':{'mode':'explicit','width':64,'height':64}})
    def test_admission_blocks_without_post_and_reservations_are_short(self):
        gate = self.api.data/'comfyui-service/admission.json'
        write_json(gate, {'operation_id':'recovery', 'blocked':True})
        with self.assertRaisesRegex(RuntimeError, 'management in progress'):
            self.submit()
        self.assertFalse(self.client.prompts)
        gate.unlink()
        with lifecycle.submission(self.api,'test-task','test-operation'):
            self.assertTrue((gate.parent/'inflight/test-operation.json').exists())
        self.assertFalse(list((gate.parent/'inflight').glob('*.json')))
    def test_polling_does_not_advance_progress_and_queued_is_not_stalled(self):
        task = self.submit()
        first = self.api.status(task['task_id'])
        second = self.api.status(task['task_id'])
        self.assertEqual(first['last_progress_at'], second['last_progress_at'])
        self.assertFalse(second['stalled'])
        self.assertIn('elapsed_seconds',second)
        self.assertEqual(second['running_seconds'],0)
    def test_targeted_cancel_pending_is_confirmed_without_global_interrupt(self):
        a, b = self.submit(), self.submit()
        result = self.api.cancel(a['task_id'])
        self.assertEqual(result['state'],'cancelled')
        self.assertTrue(result['cancellation_confirmed'])
        self.assertIn(b['prompt_id'],self.client.pending)
        self.assertEqual(self.client.cancels,[a['prompt_id']])
    def test_running_cancel_is_request_until_history_confirms(self):
        a = self.submit()
        self.client.pending.remove(a['prompt_id'])
        self.client.running.append(a['prompt_id'])
        self.client.cooperate = False
        state = self.api.cancel(a['task_id'])
        self.assertEqual(state['state'],'cancel_requested')
        self.assertFalse(state['cancellation_confirmed'])
        self.api.cancel(a['task_id'])
        self.assertEqual(len(self.client.cancels),1)
        self.client.running.clear()
        self.client.history[a['prompt_id']] = {'status':{'messages':[['execution_interrupted',{}]]}}
        self.assertEqual(self.api.status(a['task_id'])['state'],'cancelled')
    def test_retired_instance_requires_positive_proof(self):
        service = self.api.data/'comfyui-service'
        write_json(service/'state.json', {'service_instance_id':'old-instance'})
        a = self.submit()
        write_json(service/'retired-instances/old.json',{'service_instance_id':'old-instance','verified':False})
        self.assertEqual(self.api.status(a['task_id'])['state'],'queued')
        write_json(service/'retired-instances/old.json',{'service_instance_id':'old-instance','verified':True,'active_prompt_ids':[a['prompt_id']],'ended_at':datetime.now(timezone.utc).isoformat()})
        self.assertEqual(self.api.status(a['task_id'])['state'],'interrupted_by_restart')
        self.assertEqual(len(self.client.prompts),1)
    def test_completed_unpolled_history_survives_service_restart_and_can_fetch(self):
        service = self.api.data/'comfyui-service'
        write_json(service/'state.json', {'service_instance_id':'old-instance'})
        a = self.submit()
        history = {'status':{'completed':True,'status_str':'success'},
                   'outputs':{'save':{'images':[{'filename':'saved.png','subfolder':'','type':'output'}]}}}
        write_json(service/'retired-instances/old.json',{'service_instance_id':'old-instance','verified':True,
                   'ended_at':datetime.now(timezone.utc).isoformat(),'active_prompt_ids':[],
                   'history_snapshot':{a['prompt_id']:history}})
        self.assertEqual(self.api.status(a['task_id'])['state'],'completed')
        result = self.api.fetch(a['task_id'])
        self.assertEqual(len(result['outputs']),1)
        self.assertTrue(Path(result['outputs'][0]['path']).exists())
    def test_retired_without_history_or_active_proof_is_unknown(self):
        service = self.api.data/'comfyui-service'
        write_json(service/'state.json', {'service_instance_id':'old-instance'})
        a = self.submit()
        write_json(service/'retired-instances/old.json',{'service_instance_id':'old-instance','verified':True,
                   'ended_at':datetime.now(timezone.utc).isoformat(),'active_prompt_ids':[]})
        self.assertEqual(self.api.status(a['task_id'])['state'],'submission_uncertain')
        self.assertEqual(len(self.client.prompts),1)

    def test_stall_requires_fresh_continuous_valid_telemetry(self):
        a = self.submit()
        at = datetime.now(timezone.utc)
        ago = lambda seconds:(at-timedelta(seconds=seconds)).isoformat()
        a.update(state='running',running_since=ago(400),last_progress_at=ago(200))
        path = self.api.data/'task-observations'/f"{a['task_id']}.json"
        evidence = {'prompt_id':a['prompt_id'],'service_instance_id':a.get('service_instance_id'),
                    'observed_at':at.isoformat(),'log_available':True,'gpu_utilization_percent':0,
                    'low_gpu_since':ago(150),'low_gpu_samples':30,'last_progress_at':ago(150)}
        write_json(path,evidence)
        self.assertTrue(task_diagnostics.decorate(self.api,copy.deepcopy(a))['stalled'])
        for change in ({'gpu_utilization_percent':None},{'log_available':False},{'observed_at':ago(60)}):
            write_json(path,{**evidence,**change})
            self.assertIsNone(task_diagnostics.decorate(self.api,copy.deepcopy(a))['stalled'])
        write_json(path,{**evidence,'low_gpu_since':ago(20)})
        self.assertFalse(task_diagnostics.decorate(self.api,copy.deepcopy(a))['stalled'])
    def test_batch_cancel_only_own_children_preserves_completed(self):
        parent = self.api.submit_batch('qwen-image-2.1-t2i',{'prompt':'cube'},2)
        outsider = self.submit()
        p = read_json(self.api._directory(parent['children'][0]['task_id'])/'task.json')['prompt_id']
        self.client.pending.remove(p)
        self.client.history[p] = {'status':{'completed':True,'status_str':'success'}}
        result = self.api.batch_cancel(parent['task_id'])
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['children'][0]['state'],'completed')
        self.assertNotIn(p,self.client.cancels)
        self.assertIn(outsider['prompt_id'],self.client.pending)
    def test_execution_error_invalidates_only_bound_service_epoch(self):
        service = self.api.data/'comfyui-service'
        write_json(service/'state.json', {'service_instance_id':'bound-instance'})
        task = self.submit()
        self.client.history[task['prompt_id']] = {'status':{'status_str':'error','completed':False}}
        self.assertEqual(self.api.status(task['task_id'])['state'],'failed')
        marker = read_json(service/'invalidations/bound-instance.json')
        self.assertEqual(marker['reason'],'execution_error')
        self.assertEqual(marker['task_id'],task['task_id'])
        before = marker['observed_at']
        self.api.status(task['task_id'])
        self.assertEqual(read_json(service/'invalidations/bound-instance.json')['observed_at'],before)
    def test_only_confident_stall_writes_readiness_invalidation(self):
        service = self.api.data/'comfyui-service'
        write_json(service/'state.json', {'service_instance_id':'bound-instance'})
        task = self.submit()
        self.client.pending.remove(task['prompt_id'])
        self.client.running.append(task['prompt_id'])
        state_path = self.api._directory(task['task_id'])/'task.json'
        state = read_json(state_path)
        at = datetime.now(timezone.utc)
        ago = lambda seconds:(at-timedelta(seconds=seconds)).isoformat()
        state.update(state='running',running_since=ago(400),last_progress_at=ago(200))
        write_json(state_path,state)
        evidence_path = self.api.data/'task-observations'/f"{task['task_id']}.json"
        evidence = {'prompt_id':task['prompt_id'],'service_instance_id':'bound-instance',
                    'observed_at':at.isoformat(),'log_available':True,'gpu_utilization_percent':None,
                    'low_gpu_since':ago(150),'low_gpu_samples':30,'last_progress_at':ago(150)}
        write_json(evidence_path,evidence)
        self.assertIsNone(self.api.status(task['task_id'])['stalled'])
        self.assertFalse((service/'invalidations/bound-instance.json').exists())
        write_json(evidence_path,{**evidence,'gpu_utilization_percent':0})
        self.assertTrue(self.api.status(task['task_id'])['stalled'])
        self.assertEqual(read_json(service/'invalidations/bound-instance.json')['reason'],'suspected_stall_with_valid_evidence')

    def test_http_trickle_has_absolute_body_deadline(self):
        response = SimpleNamespace(read1=lambda n:b'x')
        with patch('comfy_workflow_api.client.time.monotonic',side_effect=[0,1,3]):
            with self.assertRaisesRegex(TimeoutError,'total request deadline'):
                Client._read(response,2)
    def test_http_headers_trickle_cannot_extend_request_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    for byte in b'HTTP/1.1 200 OK\r\nX-Slow: value\r\n\r\n{}':
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                Client(f'http://127.0.0.1:{server.server_port}',timeout=.08).request('/slow')
            self.assertLess(time.monotonic()-started,.5)
        finally:
            server.shutdown()
            server.server_close()

    def test_malformed_lease_is_reported_and_retained_conservatively(self):
        path = sleep_guard.home(self.api)/'leases/broken.json'
        path.parent.mkdir(parents=True)
        path.write_text('{broken')
        leases = sleep_guard.live_leases(self.api)
        self.assertEqual(leases[0]['owner_state'],'unknown')
        self.assertTrue(path.exists())
        result = sleep_guard.status(self.api)
        self.assertEqual(result['leases'],1)
        self.assertIsNone(result['lease_details'][0]['owner_alive'])

    def test_lease_age_warns_without_deleting_live_owner(self):
        self.api.settings['sleep_guard']['enabled'] = True
        old = (datetime.now(timezone.utc)-timedelta(seconds=700)).isoformat()
        path = sleep_guard.home(self.api)/'leases/lease.json'
        write_json(path,{'owner':{'pid':123},'created_at':old,'purpose':'submit','task_id':'test'})
        with patch.object(sleep_guard,'matches',side_effect=lambda v:bool(v)):
            # running state is absent, so status must not invoke GNOME or mutate.
            value = sleep_guard.status(self.api)
            self.assertTrue(value['lease_details'][0]['age_warning'])
            self.assertTrue(path.exists())

if __name__ == '__main__':
    unittest.main()
