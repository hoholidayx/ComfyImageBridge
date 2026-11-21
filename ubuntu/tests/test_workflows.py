import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from comfy_workflow_api import WorkflowAPI, APIError
from comfy_workflow_api.compiler import compile_graph, topology
from comfy_workflow_api._edit import parameters
from comfy_workflow_api.storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    base_url = "http://127.0.0.1:8188"

    def __init__(self):
        self.posts = 0
        self.prompt_id = None
        self.lost = False
        self.reject = False
        self.complete = False

    def upload(self, path, task_id, index):
        return f'comfy-workflow-api/{task_id}/{index}.png'

    def request(self, path, method="GET", body=None, **kwargs):
        if path == "/prompt":
            self.posts += 1
            if self.reject:
                raise APIError(400, "invalid graph")
            self.prompt_id = body["prompt_id"]
            if self.lost:
                raise TimeoutError("Response lost after acceptance")
            return {"prompt_id": self.prompt_id, "node_errors": {}}
        if path.startswith("/history/"):
            return {self.prompt_id: {"status": {"completed": True, "status_str": "success"},
                "outputs": {"save": {"images": [{"filename": "test.png", "subfolder": "", "type": "output"}]}}}} if self.complete else {}
        if path == "/queue":
            return {"queue_running": [], "queue_pending": [[0, self.prompt_id]] if self.prompt_id else []}
        if path.startswith("/view?"):
            return (ROOT / "examples/robot.png").read_bytes()
        raise AssertionError(path)


class WorkflowsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("config", "workflows"):
            shutil.copytree(ROOT / name, self.root / name)
        settings = self.root/'config/settings.toml'
        settings.write_text(settings.read_text().replace('enabled = true', 'enabled = false'))
        self.client = FakeClient()
        self.api = WorkflowAPI(self.root, client=self.client)

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, name):
        return read_json(ROOT / "examples" / name)

    def test_edit_images_and_size_modes(self):
        request = self.request("edit-single.json")
        for count in (1, 2, 10):
            for mode in ({"mode": "follow_image"}, {"mode": "explicit", "width": 384, "height": 320},
                         {"mode": "aspect_ratio", "aspect_ratio": "9:16", "megapixels": 1}):
                request.update(images=["robot.png"]*count, output_size=mode)
                result = self.api.compile("qwen-image-2.1-edit", request, ROOT / "examples")
                graph = result["prompt"]
                encoder = next(n for n in graph.values() if n["class_type"] == "TextEncodeQwenImage21")
                keys = [k for k in encoder["inputs"] if k.startswith("images.")]
                self.assertEqual(keys, [f"images.image_{i}" for i in range(1, count+1)])
                for i in range(1, count+1):
                    self.assertEqual(encoder["inputs"][f"images.image_{i}"], [f"input_{i}", 0])
                self.assertEqual(graph["468"]["inputs"]["switch"], mode["mode"] != "follow_image")
        for count in (0, 11):
            request["images"] = ["robot.png"] * count
            with self.assertRaises(ValueError):
                self.api.compile("qwen-image-2.1-edit", request, ROOT / "examples")

    def test_t2i_uses_edit_graph_and_injects_parameters(self):
        request = self.request("t2i.json")
        request.update(prompt="NEW PROMPT", seed=123, preset='quality', steps=40)
        graph = self.api.compile("qwen-image-2.1-t2i", request)["prompt"]
        self.assertFalse(any(n["class_type"] in ("LoadImage", "TextGenerate") for n in graph.values()))
        self.assertEqual(graph["474"]["inputs"]["prompt"], "NEW PROMPT")
        self.assertEqual(graph["458"]["inputs"]["seed"], 123)
        self.assertEqual(graph["458"]["inputs"]["steps"], 40)
        self.assertFalse(graph['485']['inputs']['switch'])

    def test_shared_loaders_and_stable_lora_branch(self):
        request = self.request('t2i.json')
        request.pop('steps', None)
        fast = self.api.compile('qwen-image-2.1-t2i', request)['prompt']
        plain = self.api.compile('qwen-image-2.1-t2i', {**request, 'preset': 'quality'})['prompt']
        edit = self.api.compile('qwen-image-2.1-edit', self.request('edit-single.json'), ROOT/'examples')['prompt']
        for node in ('451', '453', '454', '479', '469', '495'):
            self.assertEqual(fast[node], plain[node])
            self.assertEqual(fast[node], edit[node])
        self.assertEqual(fast['458']['inputs']['model'], ['485', 0])
        self.assertEqual(fast['485']['inputs']['on_true'], ['469', 0])
        self.assertEqual(fast['469']['inputs']['model'], ['479', 0])
        self.assertEqual(fast['485']['inputs']['on_false'], ['495', 0])

    def test_uni_presets_only_change_steps_and_lora_for_generation_and_edit(self):
        modes = [{'mode': 'explicit', 'width': 576, 'height': 1024},
                 {'mode': 'aspect_ratio', 'aspect_ratio': '9:16', 'megapixels': .2}]
        for alias, example in [('qwen-image-2.1-t2i', 't2i.json'), ('qwen-image-2.1-edit', 'edit-single.json')]:
            for mode in modes + ([{'mode': 'follow_image'}] if alias.endswith('edit') else []):
                with self.subTest(alias=alias, mode=mode):
                    request = {**self.request(example), 'seed': 42, 'output_size': mode}
                    fast, quality = [self.api.compile(alias, {**request, 'preset': preset}, ROOT/'examples')['prompt']
                                     for preset in ('fast', 'quality')]
                    for graph, steps, use_lora in [(fast, 6, True), (quality, 40, False)]:
                        self.assertEqual(graph['458']['class_type'], 'KSampler')
                        inputs = graph['458']['inputs']
                        self.assertEqual((inputs['seed'], inputs['cfg'], inputs['sampler_name'], inputs['scheduler'],
                                          inputs['denoise']), (42, 1, 'euler', 'simple', 1))
                        self.assertEqual(inputs.pop('steps'), steps)
                        self.assertIs(graph['485']['inputs'].pop('switch'), use_lora)
                    self.assertEqual(fast, quality)

    def test_fast_receipt_records_native_sampler_and_recovers_request(self):
        task = self.api.submit('qwen-image-2.1-t2i', self.request('t2i.json'))
        directory = self.api._directory(task['task_id'])
        config = read_json(directory/'sampling.json')
        self.assertEqual((config['sampler_class'], config['scheduler'], config['steps'], config['use_lora']),
                         ('KSampler', 'simple', 6, True))
        state = read_json(directory/'task.json')
        state.pop('request_used')
        write_json(directory/'task.json', state)
        self.client.complete = True
        result = self.api.fetch(task['task_id'])
        self.assertEqual(result['request_used']['steps'], 6)
        self.assertEqual(result['sampling_config'], config)

    def test_fast_requires_the_verified_six_step_lora(self):
        snapshot, _ = self.api._snapshot('qwen-image-2.1-t2i')
        subgraph = snapshot['definitions']['subgraphs'][0]
        next(node for node in subgraph['nodes'] if node['id'] == 479)['widgets_values_named']['lora_name'] = 'other.safetensors'
        with self.assertRaisesRegex(ValueError, 'Unsupported fast LoRA'):
            compile_graph('qwen-image-2.1-t2i', snapshot, self.request('t2i.json'))

    def test_sync_keeps_one_uni_graph_for_both_aliases(self):
        snapshot, entry = self.api._snapshot('qwen-image-2.1-edit')
        parent, _, defaults = parameters(snapshot)
        self.assertEqual((defaults['steps'], defaults['use_lora']), (40, False))
        previous = self.root / entry['path']
        legacy = self.root / 'workflows/snapshots/qwen-image-2.1-edit' / (entry['sha256'] + '.json')
        write_json(legacy, snapshot)
        parent['title'] = 'Updated uni'
        calls = []
        def workflow(name):
            calls.append(name)
            return snapshot
        self.client.workflow = workflow
        for _ in range(2):
            manifest = self.api.sync_workflows()
            self.assertEqual(len({item['path'] for item in manifest.values()}), 1)
            for alias in manifest:
                self.assertEqual(self.api._snapshot(alias)[0], snapshot)
        self.assertEqual(calls, ['qwen-image-2.1-unified.json'] * 2)
        self.assertFalse(previous.exists())
        self.assertFalse(legacy.exists())
        self.assertEqual(self.api.sync_workflows(), manifest)
        current = self.root / manifest['qwen-image-2.1-edit']['path']
        self.assertEqual(list((self.root/'workflows/snapshots').rglob('*.json')), [current])

    def test_quality_edit_batch_preserves_40_in_children_and_receipts(self):
        request={**self.request('edit-single.json'),'preset':'quality','seed':0}
        batch=self.api.submit_batch('qwen-image-2.1-edit',request,count=2,request_dir=ROOT/'examples')
        self.assertEqual(batch['request_used']['steps'],40)
        for index, child in enumerate(batch['children']):
            directory=self.api._directory(child['task_id'])
            state=read_json(directory/'task.json')
            effective=read_json(directory/'effective-request.json')
            graph=read_json(directory/'prompt.json')
            self.assertEqual(state['request_used']['steps'],40)
            self.assertEqual((effective['steps'],effective['seed'],effective['use_lora']),(40,index,False))
            self.assertEqual(graph['458']['inputs']['steps'],40)
            self.assertFalse(graph['485']['inputs']['switch'])

    def test_bad_request_does_not_create_task(self):
        request = self.request("t2i.json")
        request["steps"] = -1
        with self.assertRaises(ValueError):
            self.api.submit("qwen-image-2.1-t2i", request)
        self.assertEqual(self.client.posts, 0)
        self.assertFalse((self.root / "data/runs").exists())

    def test_lost_response_is_recovered_without_resubmission(self):
        self.client.lost = True
        with self.assertRaises(RuntimeError):
            self.api.submit("qwen-image-2.1-t2i", self.request("t2i.json"))
        directory = next((self.root / "data/runs").iterdir())
        state = read_json(directory / "task.json")
        self.assertEqual(state["state"], "submission_uncertain")
        resumed = WorkflowAPI(self.root, client=self.client)
        self.assertEqual(resumed.status(state["task_id"])["state"], "queued")
        with self.assertRaises(TimeoutError):
            resumed.wait(state["task_id"], timeout=0)
        self.assertEqual(resumed.status(state["task_id"])["state"], "queued")
        self.client.complete = True
        resumed.wait(state["task_id"], timeout=0)
        result = resumed.fetch(state["task_id"])
        self.assertEqual(result["outputs"][0]["width"], 320)
        self.assertEqual(self.client.posts, 1)

    def test_explicit_rejection_is_failed(self):
        self.client.reject = True
        with self.assertRaises(RuntimeError):
            self.api.submit("qwen-image-2.1-t2i", self.request("t2i.json"))
        state = read_json(next((self.root / "data/runs").iterdir()) / "task.json")
        self.assertEqual(state["state"], "failed")
        self.assertEqual(self.client.posts, 1)

    def test_corrupt_snapshot_rejected(self):
        manifest = read_json(self.root / "workflows/manifest.json")
        path = self.root / manifest["qwen-image-2.1-t2i"]["path"]
        snapshot = read_json(path)
        snapshot["nodes"][0]["type"] = "UnknownNode"
        write_json(path, snapshot)
        with self.assertRaises(ValueError):
            self.api.compile("qwen-image-2.1-t2i", self.request("t2i.json"))

    def test_compile_does_not_change_snapshot(self):
        snapshot, _ = self.api._snapshot("qwen-image-2.1-t2i")
        original = copy.deepcopy(snapshot)
        compile_graph("qwen-image-2.1-t2i", snapshot, self.request("t2i.json"))
        self.assertEqual(snapshot, original)
        changed = copy.deepcopy(snapshot)
        changed["nodes"][0]["mode"] = 4
        self.assertNotEqual(topology(changed), topology(snapshot))

    def test_dynamic_socket_order_does_not_change_topology(self):
        snapshot, _ = self.api._snapshot("qwen-image-2.1-edit")
        changed = copy.deepcopy(snapshot)
        sg = changed["definitions"]["subgraphs"][0]
        node = next(n for n in sg["nodes"] if n["type"] == "TextEncodeQwenImage21")
        old_inputs = node["inputs"][:]
        node["inputs"] = list(reversed(old_inputs))
        for link in sg["links"]:
            if link["target_id"] == node["id"]:
                link["target_slot"] = len(old_inputs) - 1 - link["target_slot"]
        self.assertEqual(topology(changed), topology(snapshot))

    def test_unknown_sync_does_not_replace_manifest(self):
        before = (self.root / "workflows/manifest.json").read_bytes()
        snapshot, _ = self.api._snapshot("qwen-image-2.1-t2i")
        snapshot["nodes"][0]["type"] = "UnknownNode"
        self.client.workflow = lambda _: snapshot
        with self.assertRaises(ValueError):
            self.api.sync_workflows()
        self.assertEqual((self.root / "workflows/manifest.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
