import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from comfy_workflow_api import WorkflowAPI, APIError
from comfy_workflow_api.compiler import compile_graph, topology
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
        request.update(prompt="NEW PROMPT", seed=123, preset='quality', steps=25)
        graph = self.api.compile("qwen-image-2.1-t2i", request)["prompt"]
        self.assertFalse(any(n["class_type"] in ("LoadImage", "TextGenerate") for n in graph.values()))
        self.assertEqual(graph["474"]["inputs"]["prompt"], "NEW PROMPT")
        self.assertEqual(graph["458"]["inputs"]["seed"], 123)
        self.assertEqual(graph["458"]["inputs"]["steps"], 25)
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
        self.assertEqual(fast['458']['class_type'], 'SamplerCustom')
        self.assertEqual(fast['pruna_sigmas']['inputs']['nodes'], '1.0, 0.925, 0.85, 0.625, 0.375, 0.125')
        self.assertEqual(plain['458']['inputs']['steps'], 25)
        self.assertFalse(plain['485']['inputs']['switch'])

    def test_presets_compile_correctly_for_both_workflows(self):
        for alias, example, quality_steps in (('qwen-image-2.1-t2i','t2i.json',25),
                                             ('qwen-image-2.1-edit','edit-single.json',25)):
            for preset, steps, lora in (('fast',6,True),('quality',quality_steps,False)):
                request={**self.request(example),'preset':preset}
                graph=self.api.compile(alias,request,ROOT/'examples')['prompt']
                if preset=='quality':
                    self.assertEqual(graph['458']['inputs']['steps'],steps)
                else:
                    self.assertEqual(len(graph['pruna_sigmas']['inputs']['nodes'].split(',')),steps)
                self.assertEqual(graph['485']['inputs']['switch'],lora)
                self.assertEqual(graph['458']['inputs']['cfg'],1)
                if preset=='quality':
                    expected=read_json(ROOT/'workflows/reference/comfy-quality.json')['workflows'][alias]['sampling']
                    actual={key:graph['458']['inputs'][key] for key in expected}
                    self.assertEqual(actual,expected)

    def test_fast_schedule_uses_actual_output_latent_and_original_model_branch(self):
        for alias,example in [('qwen-image-2.1-t2i','t2i.json'),('qwen-image-2.1-edit','edit-single.json')]:
            modes=[{'mode':'explicit','width':576,'height':1024},
                   {'mode':'aspect_ratio','aspect_ratio':'9:16','megapixels':.2,'multiple':32}]
            if alias.endswith('edit'):
                modes.append({'mode':'follow_image'})
            for mode in modes:
                req={**self.request(example),'preset':'fast','seed':42,'output_size':mode}
                graph=self.api.compile(alias,req,ROOT/'examples')['prompt']
                sample=graph['458']['inputs']
                self.assertEqual(sample['noise_seed'],42)
                self.assertEqual(sample['model'],['485',0])
                self.assertEqual(graph['pruna_sigmas']['inputs']['latent'],sample['latent_image'])
                self.assertEqual(graph['pruna_sampler']['inputs']['sampler_name'],'euler')
                self.assertEqual(sample['sigmas'],['pruna_sigmas',0])
                self.assertTrue(graph['485']['inputs']['switch'])

    def test_fast_receipt_records_schedule_and_legacy_fallback_works(self):
        task=self.api.submit('qwen-image-2.1-t2i',self.request('t2i.json'))
        directory=self.api._directory(task['task_id'])
        config=read_json(directory/'sampling.json')
        self.assertEqual(config['scheduler'],'pruna-v0.2-custom')
        self.assertEqual(config['raw_sigmas'],[1.0,.925,.85,.625,.375,.125])
        state=read_json(directory/'task.json')
        state.pop('request_used')
        write_json(directory/'task.json',state)
        self.client.complete=True
        result=self.api.fetch(task['task_id'])
        self.assertEqual(result['request_used']['steps'],6)
        self.assertEqual(result['sampling_config'],config)

    def test_unknown_fast_lora_does_not_silently_use_pruna_sigmas(self):
        snapshot,_=self.api._snapshot('qwen-image-2.1-t2i')
        for subgraph in snapshot['definitions']['subgraphs']:
            for node in subgraph['nodes']:
                if node['id']==479:
                    node['widgets_values_named']['lora_name']='other-adapter.safetensors'
        with self.assertRaisesRegex(ValueError,'Unsupported fast LoRA'):
            compile_graph('qwen-image-2.1-t2i',snapshot,self.request('t2i.json'))

    def test_quality_edit_batch_preserves_25_in_children_and_receipts(self):
        request={**self.request('edit-single.json'),'preset':'quality','seed':0}
        batch=self.api.submit_batch('qwen-image-2.1-edit',request,count=2,request_dir=ROOT/'examples')
        self.assertEqual(batch['request_used']['steps'],25)
        for index, child in enumerate(batch['children']):
            directory=self.api._directory(child['task_id'])
            state=read_json(directory/'task.json')
            effective=read_json(directory/'effective-request.json')
            graph=read_json(directory/'prompt.json')
            self.assertEqual(state['request_used']['steps'],25)
            self.assertEqual((effective['steps'],effective['seed'],effective['use_lora']),(25,index,False))
            self.assertEqual(graph['458']['inputs']['steps'],25)
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
