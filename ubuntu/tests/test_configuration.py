import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comfy_workflow_api.operations import mcp_listener
from comfy_workflow_api.settings import load_settings, mcp_listen, module_root, service_paths
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

    def test_lan_uses_one_rfc1918_address_on_a_connected_physical_link(self):
        def link(name, ip, flags=('UP', 'LOWER_UP')):
            return {'ifname': name, 'flags': flags, 'addr_info': [{'family': 'inet', 'scope': 'global', 'local': ip}]}

        for name in ('eth0', 'eth1', 'eth2'):
            (self.root/name/'device').mkdir(parents=True)
        links = [link('eth0', '192.168.10.4'), link('docker0', '172.17.0.1'),
                 link('eth1', '198.51.100.4'), link('eth2', '10.0.0.9', ['UP'])]
        with patch.object(mcp_listener, '_NET_INTERFACES', self.root), patch.object(
                mcp_listener.subprocess, 'check_output', return_value=json.dumps(links)) as query:
            self.assertEqual(mcp_listener.mcp_bind_host('lan'), '192.168.10.4')
            for unresolved in (links[1:], links + [link('eth2', '10.0.0.9')], links + [link('eth2', '192.168.10.4')]):
                query.return_value = json.dumps(unresolved)
                with self.assertRaisesRegex(ValueError, 'Set mcp.host explicitly'):
                    mcp_listener.mcp_bind_host('lan')
            query.side_effect = FileNotFoundError
            with self.assertRaisesRegex(ValueError, 'Unable to read LAN addresses'):
                mcp_listener.mcp_bind_host('lan')
            query.reset_mock()
            for host in ('127.0.0.1', '192.168.10.4'):
                self.assertEqual(mcp_listener.mcp_bind_host(host), host)
            query.assert_not_called()
        self.assertEqual(mcp_listen({}), ('127.0.0.1', 8787))
        self.assertEqual(mcp_listen({'mcp': {'host': 'lan'}}), ('lan', 8787))
        for host in ('0.0.0.0', '198.51.100.4'):
            with self.assertRaises(ValueError):
                mcp_listen({'mcp': {'host': host}})

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
