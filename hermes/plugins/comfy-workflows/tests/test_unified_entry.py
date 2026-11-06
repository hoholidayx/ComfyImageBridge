"""Public tool dispatch: scope, background acceptance, and legacy compatibility."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('unified_entry', ROOT / '__init__.py',
                                            submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)


class UnifiedEntryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        plugins = ModuleType('plugins')
        plugins.__path__ = []
        storage = ModuleType('plugins.plugin_storage')
        storage.plugin_data_dir = lambda name: Path(self.temp.name) / name
        self.patch = patch.dict(sys.modules, {'plugins': plugins, 'plugins.plugin_storage': storage})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ctx = SimpleNamespace(has_plugin=lambda name: True)
        self.origin = {'profile': 'test', 'platform': 'telegram', 'bot': '123',
                       'chat_id': '100', 'user_id': '100', 'session_key': 'test:100'}

    def test_generation_returns_only_background_acceptance_and_version(self):
        service = Mock()
        service.create.return_value = {'success': True, 'task_id': 'test-job', 'state': 'accepted', 'title': 'robot'}
        args = {'prompt': 'private synthetic prompt', 'title': 'robot', 'preset': 'quality'}
        with patch('unified_entry.jobs.JobService', return_value=service), \
             patch('unified_entry.delivery.capture_origin', return_value=self.origin):
            result = json.loads(package.handle(self.ctx, 'comfy_image', args))
        service.create.assert_called_once_with(args, self.origin)
        self.assertEqual(result['task_id'], 'test-job')
        self.assertEqual(result['metadata']['contract_version'], 7)
        self.assertNotIn(args['prompt'], json.dumps(result))
        self.assertLess(len(json.dumps(result)), 1000)

    def test_list_does_not_require_task_id_and_is_scoped(self):
        service = Mock()
        service.list.return_value = {'success': True, 'tasks': [{'task_id': 'one', 'title': 'robot'}]}
        with patch('unified_entry.jobs.JobService', return_value=service), \
             patch('unified_entry.delivery.capture_origin', return_value=self.origin):
            result = json.loads(package.handle(self.ctx, 'comfy_task', {'action': 'list', 'query': 'robot'}))
        service.list.assert_called_once_with(query='robot', limit=20, origin=self.origin)
        self.assertTrue(result['success'])

    def test_real_desktop_origin_flows_through_public_tool_dispatch(self):
        source = SimpleNamespace(platform='local', profile='test', chat_id='desktop-chat',
                                 user_id='desktop-user', thread_id='')
        values = {'HERMES_SESSION_ID': 'durable-desktop'}
        route = SimpleNamespace(bound_session_key=lambda: 'local:desktop-route',
                                get_session_env=lambda key, default='': values.get(key, default))
        service = Mock()
        service.list.return_value = {'success': True, 'tasks': []}
        with patch('unified_entry.jobs.JobService', return_value=service), \
                patch.dict(sys.modules, {'gateway.session_context': route,
                                        'gateway.plugin_messages': SimpleNamespace(bound_route=lambda: source)}):
            result = json.loads(package.handle(self.ctx, 'comfy_task', {'action': 'list'}))
        self.assertTrue(result['success'])
        captured = service.list.call_args.kwargs['origin']
        self.assertEqual(captured['bot'], 'local')
        self.assertEqual(captured['chat_id'], 'desktop-chat')
        self.assertEqual(captured['session_key'], 'local:desktop-route')
        self.assertEqual(captured['session_id'], 'durable-desktop')

    def test_missing_runtime_does_not_create_generation(self):
        service = Mock()
        self.ctx.has_plugin = lambda name: False
        with patch('unified_entry.jobs.JobService', return_value=service), \
             patch('unified_entry.delivery.capture_origin', return_value=self.origin):
            result = json.loads(package.handle(self.ctx, 'comfy_image', {'prompt': 'robot'}))
        self.assertFalse(result['success'])
        service.create.assert_not_called()

    def test_model_cannot_override_host_or_shell(self):
        result = json.loads(package.handle(self.ctx, 'comfy_image', {'prompt': 'robot', 'ssh_host': 'elsewhere'}))
        self.assertFalse(result['success'])
        self.assertFalse(result['error']['retry_submission'])

    def test_legacy_task_id_still_routes_to_existing_client(self):
        service = Mock()
        service.store.get_job.return_value = None
        with patch('unified_entry.jobs.JobService', return_value=service), \
             patch('unified_entry.delivery.capture_origin', return_value=self.origin), \
             patch.object(package, '_handle_legacy', return_value='{"success":true}') as legacy:
            self.assertTrue(json.loads(package.handle(self.ctx, 'comfy_task', {'task_id': 'legacy'}))['success'])
        legacy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
