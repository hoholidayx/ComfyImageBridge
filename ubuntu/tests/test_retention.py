import shutil
import tempfile
import threading
from unittest.mock import patch
import unittest
import uuid
from pathlib import Path
from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api.storage import read_json, write_json
from comfy_workflow_api.retention import automatic_cleanup, prune_probes
import test_workflows as fixtures


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        shutil.copytree(fixtures.ROOT / 'config', self.root / 'config')
        self.api = WorkflowAPI(self.root, client=fixtures.FakeClient())
        self.output = self.root / 'ComfyUI/output/comfy-workflow-api'
        self.inputs = self.root / 'ComfyUI/input/comfy-workflow-api'
        self.output.mkdir(parents=True)
        self.inputs.mkdir(parents=True)
        self.api.settings['retention'].update(max_tasks=2, comfy_output_dir=str(self.output), comfy_input_dir=str(self.inputs))

    def tearDown(self):
        self.temp.cleanup()

    def task(self, age, state='completed', flat=False):
        task_id = str(uuid.uuid4())
        run = self.api._directory(task_id)
        run.mkdir(parents=True)
        write_json(run / 'task.json', {'task_id': task_id, 'state': state,
            'base_url': self.api.client.base_url, 'prompt_id': str(uuid.uuid4()),
            'created_at': f'2026-01-{age:02d}T00:00:00+00:00'})
        filename = task_id + '_00001.png' if flat else 'image_00001.png'
        subfolder = 'comfy-workflow-api' if flat else 'comfy-workflow-api/' + task_id
        image = {'filename': filename, 'subfolder': subfolder, 'type': 'output'}
        write_json(run / 'result.json', {'outputs': [{'server_file': image}]})
        write_json(run / 'history.json', {'outputs': {'save': {'images': [image]}}})
        for base in (self.api.data / 'inputs', self.inputs):
            (base / task_id).mkdir(parents=True)
            (base / task_id / '1.png').write_bytes(b'test')
        destination = self.output / filename if flat else self.output / task_id / filename
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(b'test')
        return task_id, destination

    def test_count_cleanup_removes_only_old_owned_tasks(self):
        old, flat = self.task(1, flat=True)
        second, nested = self.task(2)
        third, _ = self.task(3)
        fourth, _ = self.task(4)
        active, _ = self.task(5, state='running')
        unrelated = self.output.parent / 'manual-image.png'
        unrelated.write_bytes(b'keep')
        foreign = self.output / 'my-personal-image.png'
        foreign.write_bytes(b'keep')
        preview = self.api.cleanup(dry_run=True)
        self.assertEqual(preview['task_ids'], [old, second])
        self.assertTrue(flat.exists())
        result = self.api.cleanup()
        self.assertEqual(result['task_ids'], [old, second])
        self.assertFalse(flat.exists())
        self.assertFalse(nested.exists())
        for task_id in (old, second):
            self.assertFalse(self.api._directory(task_id).exists())
            self.assertFalse((self.inputs / task_id).exists())
            self.assertFalse((self.api.data / 'inputs' / task_id).exists())
        for task_id in (third, fourth, active):
            self.assertTrue(self.api._directory(task_id).exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(foreign.exists())

    def test_skip_queued_and_not_downloaded(self):
        old, _ = self.task(1)
        second, _ = self.task(2)
        newest, _ = self.task(3)
        queued = fixtures.read_json(self.api._directory(old) / 'task.json')['prompt_id']
        self.api.client.prompt_id = queued
        self.assertEqual(self.api.cleanup()['task_ids'], [])
        self.api.client.prompt_id = None
        write_json(self.api._directory(old) / 'result.json', {'outputs': []})
        self.assertEqual(self.api.cleanup()['task_ids'], [])

    def test_symlink_refused(self):
        old, _ = self.task(1)
        self.task(2)
        self.task(3)
        target = self.root / 'outside'
        target.mkdir()
        (target / 'keep.txt').write_text('keep')
        shutil.rmtree(self.inputs / old)
        (self.inputs / old).symlink_to(target, target_is_directory=True)
        result = self.api.cleanup()
        self.assertEqual(result['skipped_task_ids'], [old])
        self.assertTrue((target / 'keep.txt').exists())

    def test_confirmed_terminal_tasks_count_but_cancel_request_is_protected(self):
        old, _ = self.task(1, state='cancelled')
        interrupted, _ = self.task(2, state='interrupted_by_restart')
        self.task(3)
        self.task(4)
        pending, _ = self.task(5, state='cancel_requested')
        observation = self.api.data / 'task-observations' / (old + '.json')
        write_json(observation, {'task_id': old})
        self.assertEqual(self.api.cleanup()['task_ids'], [old, interrupted])
        self.assertFalse(observation.exists())
        self.assertTrue(self.api._directory(pending).exists())

    def test_malformed_queue_is_unknown_and_never_deletes(self):
        old, _ = self.task(1)
        self.task(2)
        self.task(3)
        self.api.client.request = lambda *a, **k: {'error': 'queue unavailable'}
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.api.cleanup()
        self.assertTrue(self.api._directory(old).exists())

    def test_unreachable_queue_does_not_delete(self):
        old, _ = self.task(1)
        self.task(2)
        self.task(3)
        def fail(*args, **kwargs):
            raise ConnectionError('offline')
        self.api.client.request = fail
        with self.assertRaises(ConnectionError):
            self.api.cleanup()
        self.assertTrue(self.api._directory(old).exists())


    def test_periodic_maintenance_cleans_failures_without_a_fetch(self):
        old, _ = self.task(1, state='failed')
        self.task(2, state='failed')
        self.task(3, state='failed')
        done = threading.Event()
        def sweep():
            WorkflowAPI.cleanup(self.api)
            done.set()
        with patch.object(self.api, 'cleanup', side_effect=sweep) as cleanup:
            self.api.settings['retention']['auto_cleanup'] = False
            with automatic_cleanup(self.api):
                cleanup.assert_not_called()
            self.api.settings['retention']['auto_cleanup'] = True
            with automatic_cleanup(self.api):
                self.assertTrue(done.wait(3))
        self.assertFalse(self.api._directory(old).exists())

    def test_active_log_descriptor_rotates_with_a_bounded_history(self):
        self.api.settings['retention'].update(log_max_bytes=4, log_backups=2)
        path = self.api.data/'mcp-service/mcp.log'
        path.parent.mkdir(parents=True)
        with path.open('ab', buffering=0) as writer:
            writer.write(b'prefix1111')
            inode = path.stat().st_ino
            self.assertEqual(self.api.cleanup(dry_run=True)['rotated_logs'], ['mcp-service/mcp.log'])
            self.assertEqual(path.read_bytes(), b'prefix1111')
            for payload in (b'2222', b'3333', b'new'):
                self.api.cleanup()
                writer.write(payload)
            self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(path.read_bytes(), b'new')
        self.assertEqual(path.with_suffix('.log.1').read_bytes(), b'3333')
        self.assertEqual(path.with_suffix('.log.2').read_bytes(), b'2222')
        self.assertFalse(path.with_suffix('.log.3').exists())

    def test_offline_queue_rotates_only_owned_logs(self):
        old, _ = self.task(1)
        self.task(2)
        self.task(3)
        self.api.settings['retention'].update(log_max_bytes=4)
        outside = self.root/'foreign.log'
        outside.write_bytes(b'private')
        (self.api.data/'cleanup.log').symlink_to(outside)
        path = self.api.data/'comfyui-service/comfyui.log'
        path.parent.mkdir()
        path.write_bytes(b'old log')
        with patch.object(self.api.client, 'request', side_effect=ConnectionError):
            with self.assertRaises(ConnectionError):
                self.api.cleanup()
        self.assertTrue(self.api._directory(old).is_dir())
        self.assertEqual(outside.read_bytes(), b'private')
        self.assertEqual(path.read_bytes(), b'')

    def test_bad_receipt_does_not_block_next_eligible_task_or_write_through_log_link(self):
        broken, _ = self.task(1)
        other, _ = self.task(2)
        self.task(3)
        self.task(4)
        (self.api._directory(broken)/'history.json').write_text('{')
        target = self.root/'foreign.log'
        target.write_text('original')
        (self.api.data/'cleanup.log').symlink_to(target)
        with self.assertLogs('comfy_workflow_api.log_retention', level='WARNING'):
            result = self.api.cleanup()
        self.assertIn(broken, result['skipped_task_ids'])
        self.assertIn(other, result['task_ids'])
        self.assertEqual(target.read_text(), 'original')

    def test_only_unreferenced_batches_and_retired_instances_expire(self):
        self.api.settings['retention']['max_tasks'] = 1
        live, _ = self.task(1, state='running')
        path = self.api._directory(live)/'task.json'
        write_json(path, {**read_json(path), 'service_instance_id': 'needed'})
        instances = self.api.data/'comfyui-service/retired-instances'
        for age, sid in enumerate(('old', 'needed', 'new')):
            write_json(instances/(sid+'.json'), {'service_instance_id':sid, 'verified':True, 'ended_at':str(age)})
        parents = []
        for age, child in enumerate((str(uuid.uuid4()), live, str(uuid.uuid4()))):
            uid = str(uuid.uuid4())
            parents.append(uid)
            write_json(self.api.data/'batches'/uid/'batch.json', {'task_id':uid, 'count':1, 'created_at':str(age),
                                                                 'children':[{'task_id':child}]})
        for preview in (True, False):
            result = self.api.cleanup(dry_run=preview)
            self.assertEqual(result['batch_ids'], parents[:1])
            self.assertEqual(result['retired_instance_ids'], ['old'])
            self.assertEqual((instances/'old.json').exists(), preview)
        self.assertTrue((instances/'needed.json').exists())
        self.assertTrue((self.api.data/'batches'/parents[1]).exists())

    def test_failed_probe_outputs_expire_only_after_the_queue_is_known(self):
        folders = self.api.data/'comfyui-service/health'
        outputs = self.output/'health'
        probes = []
        for age in range(3):
            uid, prompt = str(uuid.uuid4()), str(uuid.uuid4())
            probes.append((uid, prompt))
            write_json(folders/uid/'receipt.json', {'probe_id':uid, 'prompt_id':prompt,
                        'created_at':str(age), 'failed_at':'now', 'success':False})
            (outputs/uid).mkdir(parents=True)
        prune_probes(folders, outputs, None, keep=1)
        self.assertTrue((folders/probes[1][0]).exists())
        prune_probes(folders, outputs, {probes[0][1]}, keep=1)
        self.assertTrue((outputs/probes[0][0]).exists())
        self.assertFalse((outputs/probes[1][0]).exists())
        self.assertFalse((folders/probes[1][0]).exists())


if __name__ == '__main__':
    unittest.main()
