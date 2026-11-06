import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api.storage import write_json
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


if __name__ == '__main__':
    unittest.main()
