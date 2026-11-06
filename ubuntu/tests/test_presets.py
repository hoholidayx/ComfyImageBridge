import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from comfy_workflow_api.contract import normalize
from comfy_workflow_api import batches


class FakeAPI:
    def __init__(self, root):
        self.root = root
        self.data = root/'data'
        self.settings = {'retention': {'max_tasks': 100}}
        self.children = {}
        self.requests = []
        self.fail_at = None

    def compile(self, alias, request):
        normalize(request, alias)

    def submit(self, alias, request):
        if len(self.requests) == self.fail_at:
            raise RuntimeError('submission response uncertain')
        task_id = str(uuid.uuid4())
        self.requests.append(copy.deepcopy(request))
        state = {'task_id': task_id, 'state': 'queued', 'seed_used': request['seed'], 'snapshot': {'sha256': 'test'}}
        self.children[task_id] = state
        return state

    def status(self, task_id):
        return self.children[task_id]

    def fetch(self, task_id):
        child = self.children[task_id]
        return {'snapshot': child['snapshot'], 'outputs': [{'path': 'test.png', 'seed_used': child['seed_used']}]}


class PresetTest(unittest.TestCase):
    def test_defaults_and_disabled_controls(self):
        self.assertEqual(normalize({'prompt': 'x'})['seed'], 0)
        for preset, steps, lora in (('fast', 6, True), ('quality', 25, False)):
            result = normalize({'prompt': 'x', 'preset': preset})
            self.assertEqual((result['steps'], result['use_lora'], result['cfg'], result['negative_prompt']), (steps, lora, 1, ''))
        for override in ({'steps': 7}, {'steps': 25}, {'cfg': 2}, {'negative_prompt': 'bad hands'}, {'preset': 'quality', 'use_lora': True}):
            with self.assertRaises(ValueError):
                normalize({'prompt': 'x', **override})

    def test_character_limit(self):
        self.assertEqual(len(normalize({'prompt': '字'*8000})['prompt']), 8000)
        with self.assertRaises(ValueError):
            normalize({'prompt': '字'*8001})

    def test_quality_steps_follow_comfy_templates_and_reject_custom_overrides(self):
        for alias, steps in (('qwen-image-2.1-t2i',25),('qwen-image-2.1-edit',25)):
            quality=normalize({'prompt':'x','preset':'quality'},alias)
            self.assertEqual((quality['steps'],quality['use_lora']),(steps,False))
            self.assertEqual(normalize(quality,alias),quality)
            self.assertEqual(normalize({'prompt':'x'},alias)['steps'],6)
            wrong=40 if steps==25 else 25
            with self.assertRaisesRegex(ValueError,f'{steps} steps'):
                normalize({'prompt':'x','preset':'quality','steps':wrong},alias)
        self.assertEqual(normalize({'prompt':'x','preset':'quality','images':['reference.png']})['steps'],25)

    def test_count_seeds_partial_failure_and_no_resubmission(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeAPI(Path(tmp))
            batch = batches.submit(api, 'qwen-image-2.1-t2i', {'prompt': 'x'}, 4)
            self.assertEqual([r['seed'] for r in api.requests], [0, 1, 2, 3])
            for index, child in enumerate(batch['children']):
                api.children[child['task_id']]['state'] = 'failed' if index == 2 else 'completed'
            result = batches.fetch(api, batch['task_id'])
            self.assertEqual(result['state'], 'partial')
            self.assertEqual([o['seed_used'] for o in result['outputs']], [0, 1, 3])
            self.assertEqual(len(api.requests), 4)
            for count in (0, 5, True):
                with self.assertRaises(ValueError):
                    batches.submit(api, 'qwen-image-2.1-t2i', {'prompt': 'x'}, count)
            with self.assertRaises(ValueError):
                batches.submit(api, 'qwen-image-2.1-t2i', {'prompt': 'x', 'seed': 2**64-1}, 2)

    def test_uncertain_submission_stops_remaining_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeAPI(Path(tmp))
            api.fail_at = 1
            batch = batches.submit(api, 'qwen-image-2.1-t2i', {'prompt': 'x'}, 4)
            self.assertEqual(len(api.requests), 1)
            self.assertTrue(batch['submission_stopped'])
            self.assertIn('submission_error', batch)

    def test_final_status_error_returns_persisted_parent_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeAPI(Path(tmp))
            def disconnected(task_id):
                raise ConnectionError('status offline after acceptance')
            api.status = disconnected
            with self.assertRaisesRegex(RuntimeError, r'Task [0-9a-f-]{36}:'):
                batches.submit(api, 'qwen-image-2.1-t2i', {'prompt': 'x'}, 2)
            self.assertEqual(len(api.requests), 2)
            self.assertEqual(len(list((api.data/'batches').glob('*/batch.json'))), 1)
