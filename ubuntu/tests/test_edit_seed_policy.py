"""Public-entry regressions; capture ComfyUI POSTs without a GPU or network."""
from contextlib import nullcontext, redirect_stdout
import copy
import hashlib
import io
import itertools
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api import cli
from comfy_workflow_api.image_jobs import ImageJobs
from comfy_workflow_api.storage import read_json
from test_image_jobs import FakeComfyUI, ROBOT, ROOT

EDIT = 'qwen-image-2.1-edit'
T2I = 'qwen-image-2.1-t2i'
RNG = 'comfy_workflow_api.seed_policy.secrets.randbits'


class OfflineRuntime:
    def require_ready(self, check):
        if check is not None:
            assert check()

    def admission_ready(self, instance, check):
        return True

    def submitting(self, task_id, readiness):
        return nullcontext('offline-test-instance')

    def fetching(self, task_id):
        return nullcontext()

    def ticket(self, task_id, *, remove=False):
        pass

    def retired(self, instance):
        return None

    def invalidate(self, state, reason):
        return None


class EditSeedPolicyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('config', 'workflows'):
            shutil.copytree(ROOT/name, self.root/name)
        self.client = FakeComfyUI()
        self.api = WorkflowAPI(self.root, client=self.client, runtime_factory=lambda _: OfflineRuntime())
        self.api.settings['retention']['auto_cleanup'] = False
        self.api.settings['retention']['max_tasks'] = 100
        self.api.settings['poll_interval'] = 0
        self.request = {'images': [str(ROOT/'examples/robot.png')], 'prompt': 'A synthetic robot folding a paper crane',
                        'preset': 'quality', 'seed': 42,
                        'output_size': {'mode': 'explicit', 'width': 384, 'height': 320}}

    def assert_graph_seed(self, graph, seed):
        slots = [(node_id, key, value) for node_id, node in graph.items()
                 for key, value in node['inputs'].items() if key in ('seed', 'noise_seed', 'rng_seed')]
        self.assertTrue(slots)
        self.assertTrue(all(type(value) is int and value == seed for _, _, value in slots), slots)
        self.assertEqual(graph['458']['inputs']['seed'], seed)

    def assert_receipts(self, task, seed, original_seed=42):
        directory = self.api._directory(task['task_id'])
        self.assertEqual(read_json(directory/'original-request.json')['seed'], original_seed)
        for filename in ('request.json', 'effective-request.json'):
            self.assertEqual(read_json(directory/filename)['seed'], seed)
        for filename in ('sampling.json',):
            self.assertEqual(read_json(directory/filename)['seed'], seed)
        state = read_json(directory/'task.json')
        self.assertEqual((state['seed_used'], state['request_used']['seed'], state['seed_policy']),
                         (seed, seed, 'random_per_edit'))
        self.assert_graph_seed(read_json(directory/'prompt.json'), seed)

    def test_edit_preview_replaces_fixed_seed_once_and_reports_actual_graph(self):
        original = copy.deepcopy(self.request)
        with patch(RNG, side_effect=[42, 111, 222]) as rng:
            previews = [self.api.compile(EDIT, self.request) for _ in range(2)]
        self.assertEqual(rng.call_count, 3)  # Reject the caller's seed if entropy returns it.
        for preview, seed in zip(previews, (111, 222)):
            self.assertFalse(preview['submitted'])
            self.assertEqual((preview['seed_used'], preview['request_used']['seed'], preview['seed_policy']),
                             (seed, seed, 'random_per_edit'))
            self.assert_graph_seed(preview['prompt'], seed)
        self.assertEqual(self.request, original)
        self.assertFalse(self.client.posts)
        self.assertFalse((self.api.data/'runs').exists())

    def test_two_identical_edit_dispatches_use_fresh_graph_and_stable_receipts_for_both_presets(self):
        for preset in ('quality', 'fast'):
            with self.subTest(preset=preset), patch(RNG, side_effect=itertools.count(1001)) as rng:
                request = {**self.request, 'preset': preset, 'images': self.request['images']*2}
                original = copy.deepcopy(request)
                seeds = []
                for index in range(2):
                    task = self.api.submit(EDIT, request)
                    seed = 1001 + index
                    seeds.append(task['seed_used'])
                    self.assert_graph_seed(self.client.posts[-1]['prompt'], seed)
                    self.assert_receipts(task, seed)
                    for _ in range(2):
                        self.assertEqual(self.api.status(task['task_id'])['seed_used'], seed)
                    self.client.complete()
                    result = self.api.fetch(task['task_id'])
                    self.assertEqual((result['seed_used'], result['outputs'][0]['seed_used'],
                                      result['sampling_config']['seed'], result['seed_policy']),
                                     (seed, seed, seed, 'random_per_edit'))
                    self.assertEqual(self.api.status(task['task_id'])['seed_used'], seed)
                self.assertEqual(seeds, [1001, 1002])
                self.assertEqual(rng.call_count, 2)  # Validation/upload/final compile/status/fetch do not redraw.
                self.assertEqual(request, original)

    def test_text_to_image_fixed_seed_and_default_remain_unchanged(self):
        with patch(RNG) as rng:
            for seed in (None, 42, 42, 2**64-1):
                request = {'prompt': 'A synthetic geometric robot',
                           'output_size': {'mode': 'explicit', 'width': 384, 'height': 320}}
                if seed is not None:
                    request['seed'] = seed
                expected = seed if seed is not None else 0
                preview = self.api.compile(T2I, request)
                task = self.api.submit(T2I, request)
                self.assertEqual(preview['seed_used'], expected)
                self.assert_graph_seed(preview['prompt'], expected)
                self.assert_graph_seed(self.client.posts[-1]['prompt'], expected)
                self.assertEqual(task['seed_used'], expected)
                self.assertEqual(task['seed_policy'], 'fixed')
            rng.assert_not_called()

    def test_edit_batch_records_actual_child_seeds_and_does_not_overflow_ignored_seed(self):
        request = {**self.request, 'seed': 2**64-1}
        with patch(RNG, side_effect=itertools.count(500)) as rng:
            batch = self.api.submit_batch(EDIT, request, count=2)
            self.assertEqual(batch['seed_policy'], 'random_per_edit')
            self.assertIsNone(batch['seed_used'])
            self.assertEqual([c['seed_used'] for c in batch['children']], [501, 502])
            self.assertEqual(rng.call_count, 3)  # An unreserved preview plus two real admissions.
            for child, post in zip(batch['children'], self.client.posts):
                self.assert_graph_seed(post['prompt'], child['seed_used'])
                self.assert_receipts(child, child['seed_used'], 2**64-1)
                self.client.histories[post['prompt_id']] = {
                    'status': {'completed': True, 'status_str': 'success'},
                    'outputs': {'save': {'images': [{'filename': 'robot.png', 'subfolder': '', 'type': 'output'}]}}}
            result = self.api.batch_fetch(batch['task_id'])
            self.assertEqual([o['seed_used'] for o in result['outputs']], [501, 502])
            self.api.batch_status(batch['task_id'])
            self.assertEqual(rng.call_count, 3)

    def test_uncertain_dispatch_status_and_history_never_redraw_or_repost(self):
        self.client.lost_ack = True
        with patch(RNG, return_value=777) as rng:
            with self.assertRaisesRegex(RuntimeError, 'submission_uncertain'):
                self.api.submit(EDIT, self.request)
            directory = next((self.api.data/'runs').iterdir())
            task = read_json(directory/'task.json')
            self.assert_receipts(task, 777)
            for _ in range(3):
                self.assertEqual(self.api.status(task['task_id'])['seed_used'], 777)
            self.client.complete()
            self.assertEqual(self.api.fetch(task['task_id'])['seed_used'], 777)
            self.assertEqual(len(self.client.posts), 1)
            self.assertEqual(rng.call_count, 1)

    def test_uncertain_edit_batch_stops_and_recovers_actual_child_seed(self):
        self.client.lost_ack = True
        with patch(RNG, side_effect=itertools.count(600)) as rng:
            batch = self.api.submit_batch(EDIT, self.request, count=2)
            self.assertTrue(batch['submission_stopped'])
            self.assertEqual(len(batch['children']), 1)
            self.assertEqual(batch['children'][0]['seed_used'], 601)
            self.assert_graph_seed(self.client.posts[0]['prompt'], 601)
            self.api.batch_status(batch['task_id'])
            self.assertEqual(len(self.client.posts), 1)
            self.assertEqual(rng.call_count, 2)

    def test_mcp_adapter_forces_random_seed_excludes_known_reference_and_journals_actual_seed(self):
        jobs = ImageJobs(self.api, lambda: True)
        reference_sha = hashlib.sha256(ROBOT).hexdigest()
        with jobs._db() as db:
            db.execute('INSERT INTO image_seeds VALUES (?,?)', (reference_sha, '9001'))
        task_id = str(uuid.uuid4())
        with patch(RNG, side_effect=[42, 9001, 9002, 9003]) as rng:
            jobs.submit(task_id, 'A synthetic robot', seed='42', images=(ROBOT,))
            jobs.tick()
            with jobs._db() as db:
                row = db.execute('SELECT backend_id,chosen_seed,prompt_id FROM jobs WHERE task_id=?', (task_id,)).fetchone()
            self.assertEqual(row['chosen_seed'], '9002')
            self.assert_graph_seed(self.client.posts[-1]['prompt'], 9002)
            jobs.submit(task_id, 'A synthetic robot', seed='42', images=(ROBOT,))
            jobs.status(task_id)
            jobs.tick()
            self.assertEqual(len(self.client.posts), 1)
            self.assertEqual(rng.call_count, 3)
            self.client.complete()
            jobs.tick()
            self.assertEqual(jobs.result(task_id)[0]['seed_used'], '9002')
            second = str(uuid.uuid4())
            jobs.submit(second, 'A synthetic robot', seed='42', images=(ROBOT,))
            jobs.tick()
            self.assert_graph_seed(self.client.posts[-1]['prompt'], 9003)
            self.assertEqual(rng.call_count, 4)
            self.assertEqual(len(self.client.posts), 2)

    def test_cli_public_main_uses_same_policy_for_preview_and_dispatch(self):
        request_file = self.root/'request.json'
        request_file.write_text(json.dumps(self.request))
        replies = []
        with patch('comfy_workflow_api.api.WorkflowAPI', return_value=self.api), \
                patch(RNG, side_effect=itertools.count(800)) as rng:
            for command in ('compile', 'compile', 'submit', 'submit'):
                output = io.StringIO()
                with patch('sys.argv', ['comfy-workflow', '--root', str(self.root), command, EDIT,
                                        '--request', str(request_file)]), redirect_stdout(output):
                    self.assertEqual(cli.main(), 0)
                replies.append(json.loads(output.getvalue()))
            self.assertEqual([r['seed_used'] for r in replies], [800, 801, 802, 803])
            self.assertEqual(rng.call_count, 4)
            self.assertEqual(len(self.client.posts), 2)
            for post, seed in zip(self.client.posts, (802, 803)):
                self.assert_graph_seed(post['prompt'], seed)

    def test_invalid_seed_is_still_rejected_before_entropy_or_dispatch(self):
        with patch(RNG) as rng:
            for invalid in (-1, 2**64, True, '42', None):
                for method in (self.api.compile, self.api.submit):
                    with self.subTest(invalid=invalid, method=method.__name__), self.assertRaises(ValueError):
                        method(EDIT, {**self.request, 'seed': invalid})
            rng.assert_not_called()
        self.assertFalse(self.client.posts)


if __name__ == '__main__':
    unittest.main()
