import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comfy_workflow_api.settings import load_settings, module_root, service_paths
from comfy_workflow_api.retention import _safe_root
from comfy_workflow_api._edit import parameters


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root/'config').mkdir()

    def test_paths_follow_configured_comfy_and_data_roots(self):
        (self.root/'config/settings.toml').write_text('data_dir="records"\n[service]\ncomfy_dir="ComfyUI"\nrun_script="start-custom.sh"\nuv_bin="bin/uv"\n[retention]\n')
        settings = load_settings(self.root)
        comfy, run, uv = service_paths(self.root, settings)
        self.assertEqual(comfy, self.root/'ComfyUI')
        self.assertEqual(run, comfy/'start-custom.sh')
        self.assertEqual(uv, self.root/'bin/uv')
        self.assertEqual(Path(settings['data_dir']), self.root/'records')
        self.assertEqual(Path(settings['retention']['comfy_output_dir']), comfy/'output/comfy-workflow-api')

    def test_module_root_honors_explicit_root_and_environment(self):
        with patch.dict(os.environ, {'COMFY_WORKFLOW_HOME': str(self.root)}):
            self.assertEqual(module_root(), self.root)
            self.assertEqual(module_root(self.root/'other'), self.root/'other')

    def test_configured_retention_symlink_is_preserved_and_rejected(self):
        target = self.root/'actual/output/comfy-workflow-api'
        target.mkdir(parents=True)
        link = self.root/'output'
        link.symlink_to(target.parent, target_is_directory=True)
        (self.root/'config/settings.toml').write_text('[retention]\ncomfy_output_dir="output/comfy-workflow-api"\n')
        settings = load_settings(self.root)
        self.assertEqual(Path(settings['retention']['comfy_output_dir']), link/'comfy-workflow-api')
        with self.assertRaisesRegex(ValueError, 'real output'):
            _safe_root(settings['retention']['comfy_output_dir'], 'output')

    def test_distributed_snapshot_defaults_use_only_public_examples(self):
        distribution = Path(__file__).resolve().parents[1]
        expected = json.loads((distribution/'examples/t2i.json').read_text())['prompt']
        manifest = json.loads((distribution/'workflows/manifest.json').read_text())
        for record in manifest.values():
            workflow = json.loads((distribution/record['path']).read_text())
            self.assertEqual(parameters(workflow)[2]['prompt'], expected)
            for graph in [workflow]+workflow['definitions']['subgraphs']:
                for node in graph.get('nodes', []):
                    if node['type'] == 'LoadImage':
                        self.assertEqual(node['widgets_values'][0], 'robot.png')
                        self.assertEqual(node['widgets_values_named']['image'], 'robot.png')
                    if node['type'] == 'TextEncodeQwenImage21':
                        self.assertEqual(node['widgets_values_named']['prompt'], expected)
                        self.assertEqual(node['widgets_values'][0], expected)
