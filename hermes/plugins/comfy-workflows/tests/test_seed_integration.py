"""Exercise seed protection at the real job and legacy transport boundaries."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

from test_jobs import Context, ORIGIN, FakeTransport, Runtime, png
from comfy_job_tests.jobs import JobService, Store
from comfy_job_tests.transport import Transport
from comfy_job_tests.worker import Runner


class SeedIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = JobService(Context(), self.root)
        self.store = self.service.store
        self.path = self.root/'reference.png'
        self.path.write_bytes(png())

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **values):
        args = {'prompt': 'edit a geometric robot', **values}
        with patch('comfy_job_tests.worker.launch'), patch(
                'comfy_job_tests.local_delivery.prepare_origin', side_effect=lambda ctx, value: value):
            return self.service.create(args, ORIGIN)

    def register(self, seed):
        job = self.create(seed=seed)
        return self.store.register_outputs(job['task_id'], [{
            'path': str(self.path), 'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
            'width': 64, 'height': 64, 'seed_used': seed}])[0]

    def test_known_conflict_changes_real_packet_and_exposes_policy(self):
        image = self.register(7)
        task = self.create(images=[image['image_id']], seed=7, count=2)
        policy = task['seed_policy']
        self.assertEqual(policy['requested_seed'], 7)
        self.assertEqual(policy['reason'], 'reference_seed_conflict')
        self.assertNotIn(7, range(task['seed_used'], task['seed_used']+2))
        runtime = Runtime()
        transport = FakeTransport(self.root, [{'success': True, 'result': {'task_id': str(uuid.uuid4())}}])
        runner = Runner(self.store, task['task_id'], runtime=runtime, transport=transport)
        self.assertTrue(runner.prepare())
        self.assertTrue(runner.submit())
        self.assertEqual(transport.calls[0]['request']['seed'], task['seed_used'])
        self.assertEqual(self.store.get_job(task['task_id'])['seed_policy'], policy)
        self.assertTrue(any('7' in e['payload'].get('message', '') and str(task['seed_used'])
                            in e['payload'].get('message', '') for e in self.store.pending_outbox()))

    def test_edit_random_is_frozen_and_unknown_explicit_is_retained(self):
        task = self.create(images=[str(self.path)])
        self.assertEqual(task['seed_policy']['reason'], 'edit_random')
        self.assertEqual(task['seed_policy']['unknown_reference_count'], 1)
        self.store.prepare_seed_inputs(task['task_id'])
        self.store.prepare_seed_inputs(task['task_id'])
        self.assertEqual(self.store.get_job(task['task_id'])['request']['seed'], task['seed_used'])
        explicit = self.create(images=[str(self.path)], seed=13)
        self.assertEqual(explicit['seed_used'], 13)
        self.assertEqual(explicit['seed_policy']['reason'], 'explicit')
        self.assertEqual(self.create()['seed_used'], 0)

    def test_newly_registered_conflict_is_rechecked_before_wake(self):
        task = self.create(images=[str(self.path)], seed=17)
        self.register(17)
        runtime = Runtime()
        runner = Runner(self.store, task['task_id'], runtime=runtime, transport=FakeTransport(self.root, []))
        self.assertTrue(runner.prepare())
        current = self.store.get_job(task['task_id'])
        self.assertNotEqual(current['request']['seed'], 17)
        self.assertEqual(current['seed_policy']['requested_seed'], 17)
        self.assertEqual(current['seed_policy']['reason'], 'reference_seed_conflict')

    def test_tampered_copied_input_never_wakes_or_submits(self):
        task = self.create(images=[str(self.path)], seed=4)
        copied = Path(self.store.get_job(task['task_id'])['input_paths'][0])
        copied.write_bytes(png(96, 64))
        runtime, transport = Runtime(), FakeTransport(self.root, [])
        runner = Runner(self.store, task['task_id'], runtime=runtime, transport=transport)
        with self.assertRaisesRegex(ValueError, 'changed'):
            runner.prepare()
        with self.assertRaisesRegex(ValueError, 'changed'):
            runner.submit()
        self.assertEqual(runtime.calls, [])
        self.assertEqual(transport.calls, [])

    def test_legacy_blob_guard_and_download_registration(self):
        self.register(2)
        transport = Transport(Context(), self.root)
        blobs = transport.images([str(self.path)])
        request, policy = transport.select_edit_seed({'prompt': 'robot', 'seed': 2}, blobs, 1)
        self.assertEqual(policy['reason'], 'reference_seed_conflict')
        self.assertNotEqual(request['seed'], 2)
        raw = png(96, 64)
        transport.download({'task_id': str(uuid.uuid4()), 'workflow': 'qwen-image-2.1-t2i',
            'outputs': [{'png_base64': base64.b64encode(raw).decode(),
                'sha256': hashlib.sha256(raw).hexdigest(), 'path': '/example/output.png',
                'width': 96, 'height': 64, 'seed_used': 19}]})
        request, policy = transport.select_edit_seed({'prompt': 'robot', 'seed': 19},
                                                     [base64.b64encode(raw).decode()], 1)
        self.assertNotEqual(request['seed'], 19)
        self.assertEqual(policy['known_reference_count'], 1)

    def test_partial_batch_without_child_seed_does_not_invent_provenance(self):
        task = self.create(seed=10, count=3)
        image = self.store.register_outputs(task['task_id'], [{'path': str(self.path)}])[0]
        self.assertIsNone(image['seed_used'])
        edit = self.create(images=[str(self.path)], seed=10)
        self.assertEqual(edit['seed_used'], 10)
        self.assertEqual(edit['seed_policy']['unknown_reference_count'], 1)

    def test_transport_partial_batch_single_success_does_not_use_batch_base(self):
        raw = png()
        transport = Transport(Context(), self.root)
        transport.download({'task_id': str(uuid.uuid4()), 'workflow': 'qwen-image-2.1-edit',
            'count': 3, 'seed_used': 10,
            'outputs': [{'png_base64': base64.b64encode(raw).decode(),
                'sha256': hashlib.sha256(raw).hexdigest(), 'path': '/example/output.png',
                'width': 64, 'height': 64}]})
        request, policy = transport.select_edit_seed({'prompt': 'robot', 'seed': 10},
                                                     [base64.b64encode(raw).decode()], 1)
        self.assertEqual(request['seed'], 10)
        self.assertEqual(policy['unknown_reference_count'], 1)

    def test_expired_artifact_keeps_seed_constraint_for_copied_file(self):
        image = self.register(23)
        copy = self.root/'renamed.png'
        copy.write_bytes(self.path.read_bytes())
        self.path.unlink()
        self.store.reconcile_retention()
        task = self.create(images=[str(copy)], seed=23)
        self.assertNotEqual(task['seed_used'], 23)
        self.assertTrue(self.store.get_artifact(image['image_id'])['expired'])

    def test_retry_and_resume_preserve_selected_seed(self):
        task = self.create(images=[str(self.path)])
        self.store.update_job(task['task_id'], state='failed', retry_allowed=True)
        with patch('comfy_job_tests.worker.launch'), patch(
                'comfy_job_tests.local_delivery.prepare_origin', side_effect=lambda ctx, value: value):
            retried = self.service.retry(task['task_id'], ORIGIN)
        self.assertEqual(retried['seed_used'], task['seed_used'])
        self.store.update_job(retried['task_id'], submission_attempted=True, remote_task_id=str(uuid.uuid4()))
        runner = Runner(self.store, retried['task_id'], runtime=Runtime(), transport=FakeTransport(self.root, []))
        with patch.object(runner, 'prepare') as prepare, patch.object(runner, 'submit') as submit, \
                patch.object(runner, 'observe') as observe:
            runner.run()
        prepare.assert_not_called()
        submit.assert_not_called()
        observe.assert_called_once()

    def test_legacy_tool_cannot_bypass_guard_and_public_policy_is_not_pruned(self):
        self.register(31)
        name = '_seed_guard_legacy_handler'
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(name, root/'__init__.py', submodule_search_locations=[str(root)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[name] = plugin
        spec.loader.exec_module(plugin)
        storage = types.ModuleType('plugins.plugin_storage')
        storage.plugin_data_dir = lambda _name: self.root
        plugins = types.ModuleType('plugins')
        plugins.__path__ = []
        packets = []

        def call(_transport, packet):
            packets.append(packet)
            return {'success': True, 'result': {'task_id': str(uuid.uuid4()),
                    'request_used': packet['request'], 'seed_used': packet['request']['seed']}}

        with patch.dict(sys.modules, {'plugins': plugins, 'plugins.plugin_storage': storage}), \
                patch(name+'.transport.Transport.call', call):
            result = json.loads(plugin._handle_legacy(Context(), 'comfy_edit', {
                'prompt': 'a geometric robot', 'images': [str(self.path)], 'seed': 31}))
        self.assertTrue(result['success'])
        self.assertNotEqual(packets[0]['request']['seed'], 31)
        policy = result['seed_policy']
        self.assertEqual(policy['requested_seed'], 31)
        self.assertEqual(plugin._public_job({'seed_policy': policy})['seed_policy'], policy)
        self.assertEqual(list((self.root/'seed-intents').glob('*.json')), [])

        def lose_response(_transport, packet):
            intents = list((self.root/'seed-intents').glob('*.json'))
            self.assertEqual(len(intents), 1)
            saved = json.loads(intents[0].read_text())
            self.assertEqual(saved['seed_policy']['selected_seed'], packet['request']['seed'])
            self.assertNotIn('prompt', saved)
            raise TimeoutError('Lost remote response; never resubmit automatically')

        with patch.dict(sys.modules, {'plugins': plugins, 'plugins.plugin_storage': storage}), \
                patch(name+'.transport.Transport.call', lose_response):
            failed = json.loads(plugin._handle_legacy(Context(), 'comfy_edit', {
                'prompt': 'a geometric robot', 'images': [str(self.path)], 'seed': 31}))
        self.assertFalse(failed['success'])
        intent = json.loads((self.root/'seed-intents'/(failed['seed_intent_id']+'.json')).read_text())
        self.assertEqual(failed['seed_used'], intent['seed_policy']['selected_seed'])
        self.assertFalse(failed['error']['retry_submission'])


if __name__ == '__main__':
    unittest.main()
