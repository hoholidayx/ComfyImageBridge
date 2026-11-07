"""Exercise durable MCP use cases with the real workflow compiler and fake ComfyUI."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from comfy_workflow_api import APIError, WorkflowAPI
from comfy_workflow_api.image_jobs import ImageJobError, ImageJobs
from comfy_workflow_api.storage import write_json

ROOT = Path(__file__).resolve().parents[1]
ROBOT = (ROOT / 'examples/robot.png').read_bytes()
PALETTE = (ROOT / 'examples/palette.png').read_bytes()


class WorkerExit(BaseException):
    pass


class FakeComfyUI:
    base_url = 'http://127.0.0.1:8188'

    def __init__(self):
        self.posts = []
        self.uploads = []
        self.histories = {}
        self.lost_ack = False
        self.downloads = 0
        self.download_error = None

    def upload(self, path, task_id, index):
        self.uploads.append(Path(path).read_bytes())
        return f'comfy-workflow-api/{task_id}/{index}.png'

    def request(self, path, method='GET', body=None, **kwargs):
        if path == '/prompt':
            self.posts.append(body)
            if self.lost_ack:
                raise TimeoutError('Synthetic lost acknowledgement')
            return {'prompt_id': body['prompt_id']}
        if path.startswith('/history/'):
            prompt_id = path.split('/')[-1]
            return {prompt_id: self.histories[prompt_id]} if prompt_id in self.histories else {}
        if path == '/queue':
            pending = [[0, post['prompt_id']] for post in self.posts if post['prompt_id'] not in self.histories]
            return {'queue_running': [], 'queue_pending': pending}
        if path.startswith('/view?'):
            self.downloads += 1
            if self.download_error:
                raise self.download_error
            return ROBOT
        raise AssertionError(path)

    def complete(self, with_output=True):
        prompt_id = self.posts[-1]['prompt_id']
        outputs = {'save': {'images': [{'filename': 'robot.png', 'subfolder': '', 'type': 'output'}]}}
        self.histories[prompt_id] = {
            'status': {'completed': True, 'status_str': 'success'},
            'outputs': outputs if with_output else {},
        }


class ImageJobsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        for name in ('config', 'workflows'):
            shutil.copytree(ROOT / name, root / name)
        client_contract = root / 'config/client-contract.json'
        client_contract.unlink()
        path = root / 'config/settings.toml'
        path.write_text(path.read_text().replace('enabled = true', 'enabled = false'))
        self.client = FakeComfyUI()
        self.api = WorkflowAPI(root, client=self.client)
        self.api.settings['poll_interval'] = 0
        self.api.settings['retention']['max_tasks'] = 1
        self.ready = True
        self.jobs = self.reopen()

    def reopen(self):
        return ImageJobs(self.api, lambda: self.ready)

    def submit(self, **kwargs):
        task_id = str(uuid.uuid4())
        self.jobs.submit(task_id, 'A geometric robot', **kwargs)
        return task_id

    def test_concurrent_duplicates_are_one_durable_job(self):
        request_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as threads:
            results = list(threads.map(lambda _: self.jobs.submit(request_id, 'A colored cube'), range(4)))
        self.assertEqual({item['task_id'] for item in results}, {request_id})
        self.jobs = self.reopen()
        self.assertTrue(self.jobs.tick())
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.jobs.status(request_id)['state'], 'queued')
        self.assertEqual(self.jobs.submit(request_id, 'A colored cube')['state'], 'queued')
        with self.assertRaises(ImageJobError) as conflict:
            self.jobs.submit(request_id, 'A different cube')
        self.assertEqual(conflict.exception.code, 'request_conflict')

    def test_admission_checks_backend_again_before_post(self):
        self.ready = False
        with self.assertRaises(ImageJobError) as unavailable:
            self.submit()
        self.assertEqual(unavailable.exception.code, 'backend_unavailable')
        self.ready = True
        task_id = self.submit()
        self.ready = False
        self.jobs.tick()
        self.assertEqual(self.jobs.status(task_id), {
            'task_id': task_id, 'state': 'failed', 'error_code': 'backend_unavailable'})
        self.assertFalse(self.client.posts)

    def test_unknown_ack_reconciles_without_second_post_and_returns_verified_png(self):
        task_id = self.submit()
        self.client.lost_ack = True
        self.jobs.tick()
        self.assertEqual(self.jobs.status(task_id)['state'], 'submission_uncertain')
        self.client.complete()
        self.jobs = self.reopen()
        self.jobs.tick()
        metadata, png = self.jobs.result(task_id)
        self.assertEqual(metadata['seed_used'], '0')
        self.assertEqual(png, ROBOT)
        self.assertEqual(self.jobs.result(task_id), (metadata, png))
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.downloads, 1)

    def test_backend_status_outage_releases_queue_slot_after_reconciliation_window(self):
        self.api.settings['mcp']['max_pending_tasks'] = 1
        self.api.settings['wait_timeout'] = 10
        self.jobs = self.reopen()
        with patch('comfy_workflow_api.image_jobs.time.time', return_value=100) as clock:
            task_id = self.submit()
            self.jobs.tick()
            self.assertEqual(self.jobs.status(task_id)['state'], 'queued')
            with patch.object(self.client, 'request', side_effect=TimeoutError):
                self.jobs.tick()
                self.assertEqual(self.jobs.status(task_id), {
                    'task_id': task_id, 'state': 'submission_uncertain',
                    'error_code': 'backend_status_unavailable',
                })
                with self.assertRaises(ImageJobError) as full:
                    self.submit()
                self.assertEqual(full.exception.code, 'queue_full')
                self.jobs = self.reopen()
                clock.return_value = 110
                self.jobs.tick()
                self.assertFalse(self.jobs.tick())
            self.assertEqual(len(self.client.posts), 1)
            self.assertEqual(self.jobs.submit(task_id, 'A geometric robot')['state'], 'submission_uncertain')
            self.assertEqual(self.jobs.status(self.submit())['state'], 'pending')

    def test_worker_restart_retries_only_preparation_before_dispatch(self):
        task_id = self.submit(images=(ROBOT,))
        with patch.object(self.client, 'upload', side_effect=WorkerExit):
            with self.assertRaises(WorkerExit):
                self.jobs.tick()
        self.assertEqual(self.jobs.status(task_id)['state'], 'submitting')
        self.assertFalse(self.client.posts)
        self.jobs = self.reopen()
        self.jobs.tick()
        self.jobs.tick()
        self.assertEqual(self.jobs.status(task_id)['state'], 'queued')
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.uploads, [ROBOT])

    def test_edit_keeps_order_and_auto_latent_and_replaces_reference_seed(self):
        t2i = self.submit(seed=str(2**64 - 1))
        self.jobs.tick()
        self.client.complete()
        self.jobs.tick()
        self.assertEqual(self.jobs.result(t2i)[0]['seed_used'], str(2**64 - 1))
        self.submit(images=(PALETTE, ROBOT), seed=str(2**64 - 1))
        with patch('comfy_workflow_api.image_jobs.secrets.randbits', return_value=123):
            self.jobs.tick()
        self.assertEqual(self.client.uploads, [PALETTE, ROBOT])
        graph = self.client.posts[-1]['prompt']
        encoder = graph['474']['inputs']
        self.assertEqual(encoder['images.image_1'], ['input_1', 0])
        self.assertEqual(encoder['images.image_2'], ['input_2', 0])
        self.assertEqual(encoder['resolution'], 1024)
        self.assertFalse(graph['468']['inputs']['switch'])
        self.assertEqual(graph['468']['inputs']['on_false'], ['474', 2])
        self.assertEqual(graph['458']['inputs']['latent_image'], ['468', 0])
        self.assertEqual(graph['pruna_sigmas']['inputs']['latent'], ['468', 0])
        self.assertEqual(graph['458']['inputs']['noise_seed'], 123)

    def test_saved_output_survives_cleanup_after_worker_crashes_before_archive(self):
        task_id = self.submit()
        self.jobs.tick()
        self.client.complete()
        with patch.object(self.jobs, '_finish', side_effect=WorkerExit):
            with self.assertRaises(WorkerExit):
                self.jobs.tick()
        with self.jobs._db() as db:
            backend_id = db.execute('SELECT backend_id FROM jobs WHERE task_id=?', (task_id,)).fetchone()[0]
        backend = self.api.data / 'runs' / backend_id
        self.assertTrue((backend / '.result-pinned').is_file())
        # Force SDK retention to run with another eligible task present.
        other_id = str(uuid.uuid4())
        other = self.api.data / 'runs' / other_id
        write_json(other / 'task.json', {'task_id': other_id, 'base_url': self.client.base_url,
                                        'state': 'failed', 'created_at': '2020-01-01'})
        write_json(other / 'history.json', {})
        write_json(other / 'result.json', {})
        for kind in ('input', 'output'):
            folder = self.api.root / 'fake' / kind / 'comfy-workflow-api'
            folder.mkdir(parents=True)
            self.api.settings['retention'][f'comfy_{kind}_dir'] = str(folder)
        self.assertNotIn(backend_id, self.api.cleanup(dry_run=True)['task_ids'])
        self.jobs = self.reopen()
        self.jobs.tick()
        self.assertEqual(self.jobs.result(task_id)[1], ROBOT)
        self.assertEqual(self.client.downloads, 1)
        self.assertFalse((backend / '.result-pinned').exists())

    def test_missing_output_fails_without_repeating_the_post(self):
        for with_output, error in ((False, None), (True, APIError(404, 'gone')), (True, TimeoutError())):
            with self.subTest(error=error):
                self.jobs.reconcile_seconds = 0
                task_id = self.submit()
                self.jobs.tick()
                self.client.complete(with_output)
                self.client.download_error = error
                self.jobs.tick()
                self.assertEqual(self.jobs.status(task_id)['error_code'], 'output_unavailable')
                self.assertFalse(self.jobs.tick())
        self.assertEqual(len(self.client.posts), 3)


if __name__ == '__main__':
    unittest.main()
