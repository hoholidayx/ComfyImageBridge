import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import uuid
import zlib


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'comfy_job_tests'
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
from comfy_job_tests.jobs import JobService, Store, normalize, scope_matches
from comfy_job_tests.transport import Transport
from comfy_job_tests.worker import Runner, manage


ORIGIN = {'profile': 'default', 'bot': '123', 'platform': 'telegram', 'chat_id': '10',
          'thread_id': '', 'user_id': '20', 'session_key': 'test', 'session_id': 'test-session'}


def png(width=64, height=64):
    def chunk(name, value):
        return struct.pack('>I', len(value))+name+value+struct.pack('>I', zlib.crc32(name+value))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            +chunk(b'IDAT', zlib.compress((b'\0'+b'\xff\0\0'*width)*height))+chunk(b'IEND', b''))


class Context:
    def get_config(self, key, default=None):
        return default


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Runtime:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [{'ready': True}])

    def run(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.results.pop(0)


class FakeTransport(Transport):
    def __init__(self, root, responses):
        super().__init__(Context(), root)
        self.responses = list(responses)
        self.calls = []

    def call(self, packet):
        self.calls.append(packet)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class JobTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = JobService(Context(), self.root)
        self.store = self.service.store

    def tearDown(self):
        self.temp.cleanup()

    def create(self, args=None, origin=None):
        with patch('comfy_job_tests.worker.launch', return_value={'started': True}):
            return self.service.create(args or {'prompt': 'a geometric robot'}, origin or ORIGIN)['task_id']

    def test_normalization_and_safe_defaults(self):
        value = normalize({'prompt': 'a robot'})
        self.assertEqual((value['preset'], value['seed'], value['count']), ('fast', 0, 1))
        self.assertEqual(value['output_size'], {'mode': 'explicit', 'width': 1024, 'height': 1024})
        self.assertEqual(normalize({'prompt': 'x', 'images': ['img_000001']})['output_size'], {'mode': 'follow_image'})
        self.assertEqual(normalize({'prompt': 'x', 'size': '9:16'})['output_size']['aspect_ratio'], '9:16')
        self.assertEqual(normalize({'prompt': 'x', 'size': '576x1024'})['output_size']['width'], 576)
        for request in ({'prompt': 'x', 'steps': 7}, {'prompt': 'x', 'cfg': 2}, {'prompt': 'x'*8001},
                        {'prompt': 'x', 'count': 0}, {'prompt': 'x', 'seed': 2**64-1, 'count': 2},
                        {'prompt': 'x', 'size': '511x512'}, {'prompt': 'x', 'preset': 'slow'},
                        {'prompt': 'x', 'title': 'x'*81}):
            with self.assertRaises(ValueError):
                normalize(request)

    def test_input_copy_order_and_isolation(self):
        paths = []
        for index in (1, 2):
            path = self.root/f'reference-{index}.png'
            path.write_bytes(png(64*index))
            paths.append(str(path))
        task_id = self.create({'prompt': 'combine geometric shapes', 'images': paths})
        job = self.store.get_job(task_id)
        self.assertEqual([Path(p).read_bytes() for p in job['input_paths']], [Path(p).read_bytes() for p in paths])
        Path(paths[0]).unlink()
        self.assertTrue(Path(job['input_paths'][0]).exists())
        with self.assertRaises(PermissionError):
            self.service.status(task_id, origin={**ORIGIN, 'chat_id': 'other'})
        for method in (lambda: self.service.status(task_id), lambda: self.service.list(), lambda: self.service.cancel(task_id)):
            with self.assertRaises(ValueError):
                method()

    def test_edit_auto_submits_original_first_image_size_before_reference_resize(self):
        source = self.root/'original-512.png'
        source.write_bytes(png(512, 512))
        task_id = self.create({'prompt': 'change robot color', 'images': [str(source)], 'size': 'auto'})
        job = self.store.get_job(task_id)
        self.assertEqual(job['request']['size'], 'auto')
        self.assertEqual(job['request']['output_size'], {'mode': 'explicit', 'width': 512, 'height': 512})
        self.assertEqual(json.loads((self.store.directory(task_id)/'request.json').read_text())['size'], 'auto')
        transport = FakeTransport(self.root, [{'success': True, 'result': {'task_id': str(uuid.uuid4()), 'state': 'queued'}}])
        runner = Runner(self.store, task_id, transport=transport, runtime=Runtime())
        self.assertTrue(runner.submit())
        self.assertEqual(transport.calls[0]['request']['output_size'], {'mode': 'explicit', 'width': 512, 'height': 512})
        self.assertNotIn('reference_resolution', transport.calls[0]['request'])

    def test_edit_auto_rejects_unsupported_original_dimensions_with_size_guidance(self):
        for width, height in ((32, 64), (70, 64), (2080, 64)):
            source = self.root/f'{width}x{height}.png'
            source.write_bytes(png(width, height))
            with self.assertRaisesRegex(ValueError, 'choose an explicit size or aspect ratio'):
                self.create({'prompt': 'edit robot', 'images': [str(source)], 'size': 'auto'})
        task_id = self.create({'prompt': 'edit robot', 'images': [str(source)], 'size': '512x512'})
        self.assertEqual(self.store.get_job(task_id)['request']['output_size']['width'], 512)

    def test_outbox_claim_and_uncertain_recovery(self):
        task_id = self.create()
        first = self.store.enqueue(task_id, 'completed', {'text': 'done'}, revision='1')
        self.store.enqueue(task_id, 'completed', {'text': 'same'}, revision='1')
        self.assertEqual(len(self.store.pending_outbox()), 1)
        self.assertTrue(self.store.claim_outbox(first['event_id']))
        self.assertFalse(self.store.claim_outbox(first['event_id']))
        self.assertEqual(self.store.recover_outbox({'profile': 'other'}), 0)
        self.assertEqual(self.store.recover_outbox({'profile': 'default', 'platform': 'telegram'}), 1)
        self.assertEqual(self.store.pending_outbox(), [])
        value = self.store.mark_outbox(first['event_id'], 'delivery_uncertain')
        self.assertEqual(value['state'], 'delivery_uncertain')

    def test_terminal_state_and_notification_survive_receipt_write_failure(self):
        task_id = self.create()
        with patch('comfy_job_tests.jobs.atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.update_with_event(task_id, {'state': 'completed'}, 'completed', {'text': 'done'}, revision='done')
        recovered = Store(self.root)
        self.assertEqual(recovered.get_job(task_id)['state'], 'completed')
        self.assertEqual(len(recovered.pending_outbox()), 1)

    def test_resume_preparation_queries_original_operation(self):
        task_id = self.create()
        operation_id = str(uuid.uuid4())
        self.store.update_job(task_id, state='preparing', runtime_operation_id=operation_id)
        runtime = Runtime([{'ready': False, 'state': 'failed'}])
        Runner(self.store, task_id, transport=FakeTransport(self.root, []), runtime=runtime).run()
        self.assertEqual(runtime.calls[0][0], 'status')
        self.assertEqual(runtime.calls[0][1]['operation_id'], operation_id)

    def test_concurrent_manager_lock_waits_read_only_then_accepts_healthy_busy_service(self):
        task_id = self.create()
        clock = Clock()
        runtime = Runtime([
            {'success': False, 'operation_id': str(uuid.uuid4()), 'error': {'type': 'BlockingIOError'}},
            {'success': True, 'ready': False, 'service': {'phase': 'starting'}},
            {'success': True, 'ready': True, 'busy': True},
        ])
        runner = Runner(self.store, task_id, transport=FakeTransport(self.root, []), runtime=runtime,
                        clock=clock, sleep=clock.sleep)
        self.assertTrue(runner.prepare())
        self.assertEqual([call[0] for call in runtime.calls], ['ensure_ready', 'status', 'status'])
        self.assertTrue(all('operation_id' not in call[1] for call in runtime.calls[1:]))
        self.assertFalse(self.store.get_job(task_id)['submission_attempted'])

    def test_concurrent_manager_wait_is_bounded_and_never_restarts_service(self):
        task_id = self.create()
        clock = Clock()
        runtime = Runtime([{'error': {'type': 'BlockingIOError'}}] + [{'ready': False}]*3)
        runner = Runner(self.store, task_id, transport=FakeTransport(self.root, []), runtime=runtime,
                        clock=clock, sleep=clock.sleep)
        with patch('comfy_job_tests.worker.PREPARE_SECONDS', 4):
            runner.run()
        self.assertEqual([call[0] for call in runtime.calls], ['ensure_ready', 'status', 'status', 'status'])
        self.assertEqual(self.store.get_job(task_id)['state'], 'failed')
        self.assertFalse(self.store.get_job(task_id)['submission_attempted'])

    def test_terminal_elapsed_is_frozen_and_queue_diagnostics_are_exposed(self):
        task_id = self.create()
        self.store.update_job(task_id, state='completed', created_at='2026-01-01T00:00:00+00:00',
                              finished_at='2026-01-01T00:00:12+00:00',
                              last_remote_status={'queued_seconds': 2, 'running_seconds': 10, 'stalled': False,
                                                  'model_config': {'private': 'internal'}})
        with patch('comfy_job_tests.jobs.time.time', return_value=2**40):
            result = self.service.status(task_id, origin=ORIGIN)
        self.assertEqual(result['elapsed_seconds'], 12)
        self.assertEqual(result['queued_seconds'], 2)
        self.assertEqual(result['running_seconds'], 10)
        self.assertIs(result['stalled'], False)
        self.assertNotIn('model_config', result)

    def test_input_retention_keeps_title_and_marks_old_inputs_unavailable(self):
        source = self.root/'ref.png'
        source.write_bytes(png())
        first = self.create({'prompt': 'one', 'images': [str(source)]})
        self.store.update_job(first, state='failed', retry_allowed=True)
        second = self.create({'prompt': 'two', 'images': [str(source)]})
        self.store.update_job(second, state='completed')
        self.store.reconcile_retention(max_jobs=1)
        self.assertFalse(Path(self.store.get_job(first)['input_paths'][0]).exists())
        self.assertTrue(self.store.get_job(first)['inputs_expired'])
        self.assertEqual(self.store.get_job(first)['title'], 'one')
        self.assertTrue(Path(self.store.get_job(second)['input_paths'][0]).exists())

    def test_actions_scope_expiry_and_one_time_consumption(self):
        task_id = self.create()
        token = self.store.create_action(task_id, 'cancel')
        with self.assertRaises(PermissionError):
            self.store.consume_action(token, {**ORIGIN, 'user_id': 'intruder'})
        self.assertEqual(self.store.consume_action(token, ORIGIN)['task_id'], task_id)
        with self.assertRaisesRegex(ValueError, 'consumed'):
            self.store.consume_action(token, ORIGIN)
        token = self.store.create_action(task_id, 'status')
        with patch('comfy_job_tests.jobs.time.time', return_value=2**40):
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.store.consume_action(token, ORIGIN)

    def test_artifact_ids_resolve_expire_and_keep_title(self):
        task_id = self.create({'prompt': 'a red geometric robot', 'title': '红色机器人'})
        path = self.root/'artifact.png'
        path.write_bytes(png())
        artifacts = self.store.register_outputs(task_id, [{'path': str(path), 'width': 64, 'height': 64}])
        image_id = artifacts[0]['image_id']
        self.assertEqual(artifacts[0]['title'], '红色机器人')
        self.assertEqual(self.store.register_outputs(task_id, [{'path': str(path)}])[0]['image_id'], image_id)
        edit_id = self.create({'prompt': 'change to blue', 'images': [image_id]})
        self.assertEqual(self.store.get_job(edit_id)['reference_ids'], [image_id])
        path.unlink()
        self.assertTrue(self.store.get_artifact(image_id)['expired'])
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.create({'prompt': 'edit it', 'images': [image_id]})
        self.assertEqual(self.store.artifacts(origin=ORIGIN)[0]['title'], '红色机器人')

    def test_worker_preserves_task_on_response_loss_and_resume(self):
        task_id = self.create()
        runtime = Runtime()
        transport = FakeTransport(self.root, [TimeoutError('response lost')])
        clock = Clock()
        Runner(self.store, task_id, transport=transport, runtime=runtime, clock=clock, sleep=clock.sleep).run()
        self.assertEqual(self.store.get_job(task_id)['state'], 'submission_uncertain')
        self.assertEqual(sum(p['operation'] == 'submit' for p in transport.calls), 1)
        Runner(self.store, task_id, transport=transport, runtime=runtime, clock=clock, sleep=clock.sleep).run()
        self.assertEqual(len(transport.calls), 1)
        with self.assertRaises(ValueError):
            self.service.retry(task_id, origin=ORIGIN)

    def test_full_runner_downloads_and_registers_without_model(self):
        task_id = self.create({'prompt': 'a geometric robot', 'title': '机器人', 'size': '64x64'})
        remote_id = str(uuid.uuid4())
        data = png()
        import hashlib
        output = {'path': '/remote/result.png', 'width': 64, 'height': 64,
                  'sha256': hashlib.sha256(data).hexdigest(), 'png_base64': base64.b64encode(data).decode()}
        transport = FakeTransport(self.root, [
            {'success': True, 'result': {'task_id': remote_id, 'state': 'queued'}},
            {'success': True, 'result': {'task_id': remote_id, 'state': 'running'}},
            {'success': True, 'result': {'task_id': remote_id, 'state': 'completed', 'outputs': [output]}},
        ])
        runtime = Runtime([{'in_progress': True, 'operation_id': str(uuid.uuid4())}, {'ready': True}])
        clock = Clock()
        Runner(self.store, task_id, transport=transport, runtime=runtime, clock=clock, sleep=clock.sleep).run()
        job = self.store.get_job(task_id)
        self.assertEqual(job['state'], 'completed')
        self.assertEqual(Path(job['artifacts'][0]['path']).read_bytes(), data)
        self.assertEqual(job['artifacts'][0]['title'], '机器人')
        self.assertEqual([c[0] for c in runtime.calls], ['ensure_ready', 'status'])
        self.assertEqual(sum(p['operation'] == 'submit' for p in transport.calls), 1)
        self.assertEqual([e['kind'] for e in self.store.pending_outbox()].count('completed'), 1)

    def test_stall_and_observer_restart_never_resubmit(self):
        task_id = self.create()
        remote_id = str(uuid.uuid4())
        self.store.update_job(task_id, submission_attempted=True, remote_task_id=remote_id, state='running')
        transport = FakeTransport(self.root, [{'success': True, 'result': {'state': 'running', 'stalled': True}}])
        clock = Clock()
        Runner(self.store, task_id, transport=transport, runtime=Runtime(), clock=clock, sleep=clock.sleep).run()
        self.assertEqual(self.store.get_job(task_id)['state'], 'stalled')
        self.assertFalse(any(p['operation'] == 'submit' for p in transport.calls))
        with self.assertRaises(ValueError):
            self.service.retry(task_id, origin=ORIGIN)

    def test_remote_completion_download_failure_remains_resumable(self):
        task_id = self.create()
        remote_id = str(uuid.uuid4())
        self.store.update_job(task_id, submission_attempted=True, remote_task_id=remote_id, state='running')
        transport = FakeTransport(self.root, [{'success': True, 'result': {'state': 'completed', 'outputs': []}}])
        with patch.object(transport, 'download', side_effect=OSError('local download interrupted')):
            Runner(self.store, task_id, transport=transport, runtime=Runtime()).run()
        self.assertEqual(self.store.get_job(task_id)['state'], 'observation_failed')
        self.assertEqual(self.store.get_job(task_id)['remote_state'], 'completed')
        self.assertFalse(any(p['operation'] == 'submit' for p in transport.calls))

    def test_pid_reuse_stop_refuses_signal(self):
        task_id = self.create()
        self.store.update_job(task_id, worker={'pid': 888, 'uid': 1, 'start': 'old'})
        with patch('comfy_job_tests.worker.matches', return_value=False), patch('comfy_job_tests.worker.identity', return_value={'pid': 888}), patch('comfy_job_tests.worker.os.kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'reused'):
                manage('stop', task_id, self.root)
            kill.assert_not_called()

    def test_real_detached_worker_ignores_hup_and_stops_with_identity_check(self):
        task_id = self.create()
        # A copied dependency replaces Runtime with a sleeping stub: this verifies
        # real OS process/session behavior without any network, WoL or GPU work.
        copied = self.root/'fixture'/'comfy-workflows'
        copied.mkdir(parents=True)
        for name in ('jobs.py', 'worker.py', 'transport.py', 'metadata.py', 'plugin.yaml'):
            shutil.copyfile(ROOT/name, copied/name)
        dependency = copied.parent/'ubuntu-runtime'
        dependency.mkdir()
        (dependency/'runtime.py').write_text('import time\nclass Runtime:\n def __init__(self,*args): pass\n def run(self,*args,**kwargs):\n  time.sleep(30)\n  return {"ready":False}\n')
        package = types.ModuleType('detached_comfy_fixture')
        package.__path__ = [str(copied)]
        sys.modules[package.__name__] = package
        spec = importlib.util.spec_from_file_location(package.__name__+'.worker', copied/'worker.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        launched = module.launch(self.store, task_id)
        saved = launched['worker']
        try:
            deadline = time.monotonic()+5
            while self.store.get_job(task_id)['state'] != 'preparing' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(self.store.get_job(task_id)['state'], 'preparing',
                             (self.store.directory(task_id)/'worker.log').read_text())
            self.assertEqual(os.getsid(saved['pid']), saved['pid'])
            self.assertEqual(os.getpgid(saved['pid']), saved['pid'])
            os.kill(saved['pid'], signal.SIGHUP)
            time.sleep(.05)
            self.assertTrue(module.matches(saved))
            stopped = module.manage('stop', task_id, self.root)
            self.assertFalse(stopped['running'])
            self.assertFalse(self.store.get_job(task_id)['submission_attempted'])
        finally:
            if module.matches(saved):
                os.kill(saved['pid'], signal.SIGKILL)

    def test_retry_explicitly_creates_new_id_only_after_confirmed_failure(self):
        task_id = self.create()
        self.store.update_job(task_id, state='failed', retry_allowed=True, submission_attempted=True, remote_state='failed')
        with patch('comfy_job_tests.worker.launch', return_value={'started': True}):
            retried = self.service.retry(task_id, origin=ORIGIN)
        self.assertNotEqual(retried['task_id'], task_id)
        self.assertEqual(self.store.get_job(retried['task_id'])['retry_of'], task_id)

    def test_reconnect_recovers_known_remote_observer_only_once(self):
        task_id = self.create()
        remote_id = str(uuid.uuid4())
        self.store.update_job(task_id, state='finalizing', remote_task_id=remote_id, submission_attempted=True)
        binding = {'profile': ORIGIN['profile'], 'platform': 'telegram', 'bot': ORIGIN['bot']}
        with patch('comfy_job_tests.worker.launch', return_value={'started': True}) as launch:
            self.assertEqual(self.service.recover_observers(binding), [task_id])
            self.assertEqual(self.service.recover_observers(binding), [])
        launch.assert_called_once()
        self.assertEqual(self.store.get_job(task_id)['remote_task_id'], remote_id)
        self.assertEqual(self.store.get_job(task_id)['observer_recovery_count'], 1)

    def test_reconnect_excludes_stopped_stalled_unknown_and_other_bot_jobs(self):
        cases = [
            {'state': 'running', 'observer_stopped_at': 'explicit-stop'},
            {'state': 'stalled'}, {'state': 'submission_uncertain'},
            {'state': 'preparing', 'remote_task_id': None}, {'state': 'completed'},
            {'state': 'running', 'origin': {**ORIGIN, 'bot': 'another-bot'}},
        ]
        for fields in cases:
            task_id = self.create()
            self.store.update_job(task_id, **{'remote_task_id': str(uuid.uuid4()), 'submission_attempted': True, **fields})
        binding = {'profile': ORIGIN['profile'], 'platform': 'telegram', 'bot': ORIGIN['bot']}
        with patch('comfy_job_tests.worker.launch') as launch:
            self.assertEqual(self.service.recover_observers(binding), [])
            launch.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.recover_observers({'profile': 'default'})


if __name__ == '__main__':
    unittest.main()
