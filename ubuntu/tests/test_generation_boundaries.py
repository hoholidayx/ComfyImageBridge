"""Regression checks for code shared across generation and Ubuntu operations."""
from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
import uuid

from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api.backend import GenerationRuntime
from comfy_workflow_api.image_jobs import ImageJobs
from comfy_workflow_api.operations.service_readiness import workflow_hash
from test_image_jobs import FakeComfyUI, ROBOT, ROOT


class GenerationBoundaryTests(unittest.TestCase):
    def test_generation_and_operations_can_be_loaded_independently(self):
        checks = [
            (('api', 'generation', 'image_jobs'), ('operations', 'sleep_guard', 'sleep_cycle', 'hardware_monitor')),
            (('operations.render_probe', 'operations.runtime', 'operations.maintenance',
              'sleep_cycle', 'sleep_guard', 'hardware_monitor'), ('api', 'generation', 'image_jobs', 'mcp_server')),
            (('operations.service_readiness',), ('operations.service_manager', 'operations.render_probe', 'sleep_guard')),
        ]
        for imports, forbidden in checks:
            with self.subTest(imports=imports):
                script = (
                    "import importlib, sys\n"
                    f"for name in {imports!r}: importlib.import_module('comfy_workflow_api.' + name)\n"
                    f"found = [n for n in {forbidden!r} if 'comfy_workflow_api.' + n in sys.modules]\n"
                    "assert not found, found"
                )
                run = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_generation_uses_supplied_runtime_for_text_and_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('config', 'workflows'):
                shutil.copytree(ROOT/name, root/name)
            client = FakeComfyUI()
            runtime = Mock(spec=GenerationRuntime)
            runtime.submitting.return_value = nullcontext('test-instance')
            runtime.fetching.return_value = nullcontext()
            runtime.retired.return_value = None
            api = WorkflowAPI(root, client=client, runtime_factory=lambda backend: runtime)
            api.settings['poll_interval'] = 0
            api.settings['retention']['auto_cleanup'] = False
            jobs = ImageJobs(api)
            for images in ((), (ROBOT,)):
                job = str(uuid.uuid4())
                jobs.submit(job, 'A geometric robot', images=images)
                jobs.tick()
                client.complete()
                jobs.tick()
                self.assertEqual(jobs.result(job)[1], ROBOT)
            self.assertEqual(runtime.require_ready.call_count, 2)
            self.assertEqual(runtime.submitting.call_count, 2)
            self.assertEqual(runtime.fetching.call_count, 2)
            self.assertEqual(len(client.posts), 2)
            self.assertEqual(client.uploads, [ROBOT])
            self.assertFalse((api.data/'comfyui-service').exists())
            self.assertFalse((api.data/'sleep-guard').exists())

    def test_ready_evidence_tracks_render_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('src', 'scripts', 'workflows'):
                shutil.copytree(ROOT/name, root/name, ignore=shutil.ignore_patterns('__pycache__'))
            launcher = root/'run_comfyui.sh'
            launcher.touch()
            original = workflow_hash(root, launcher)
            package = root/'src/comfy_workflow_api'
            for path, relevant in (
                (package/'hardware_monitor.py', False), (package/'operations/log_retention.py', False),
                (package/'version.py', False), (package/'compiler.py', True),
                (package/'operations/render_probe.py', True), (root/'workflows/manifest.json', True),
                (launcher, True),
            ):
                with self.subTest(path=path.name):
                    content = path.read_bytes()
                    path.write_bytes(content + b'\n')
                    self.assertEqual(workflow_hash(root, launcher) != original, relevant)
                    path.write_bytes(content)
