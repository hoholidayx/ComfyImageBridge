import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tested_comfy', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from tested_comfy.transport import Transport


class Context:
    def get_config(self, name, default=None):
        return {"ssh_host": "tester@192.0.2.10", "remote_root": "/srv/comfy-workflow-api"}.get(name, default)


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = Transport(Context(), Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_user_content_is_stdin_only(self):
        prompt = 'quotes " ; $(touch /tmp/never-execute) `echo test`\n中文'
        response = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'result': {'task_id': str(uuid.uuid4())}}), '')
        with patch('tested_comfy.transport.subprocess.run', return_value=response) as mocked:
            self.transport.call({'operation': 'submit', 'request': {'prompt': prompt}})
        args, kwargs = mocked.call_args
        self.assertNotIn(prompt, args[0][-1])
        self.assertEqual(json.loads(kwargs['input'])['request']['prompt'], prompt)
        self.assertFalse(kwargs.get('shell', False))
        self.assertIn('StrictHostKeyChecking=yes', args[0])

    def test_missing_or_untrusted_connection_config_never_runs_ssh(self):
        class Config:
            def __init__(self, **values):
                self.values = values

            def get_config(self, key, default=None):
                return self.values.get(key, default)

        for values in ({}, {'ssh_host': '-oProxyCommand=bad'},
                       {'ssh_host': 'tester@192.0.2.10', 'remote_root': '~/workspace/backend'},
                       {'ssh_host': 'tester@192.0.2.10', 'remote_root': '/srv/backend', 'identity_file': ''}):
            with self.subTest(config=values), patch('tested_comfy.transport.subprocess.run') as mocked:
                with self.assertRaises(ValueError):
                    Transport(Config(**values), self.temp.name).call({'operation': 'info'})
                mocked.assert_not_called()

    def test_input_order_and_local_paths(self):
        signature = b'\x89PNG\r\n\x1a\n'
        paths = []
        for index in (1, 2):
            path = Path(self.temp.name) / f'{index}.png'
            path.write_bytes(signature + bytes([index]))
            paths.append(str(path))
        encoded = self.transport.images(paths)
        self.assertEqual([base64.b64decode(x)[-1] for x in encoded], [1, 2])
        for invalid in ([], ['https://example.com/a.png'], paths*6):
            with self.assertRaises(ValueError):
                self.transport.images(invalid)

    def test_checksum_failure_does_not_save_output(self):
        task_id = str(uuid.uuid4())
        result = {'task_id': task_id, 'outputs': [{'png_base64': base64.b64encode(b'wrong').decode(), 'sha256': 'bad'}]}
        with self.assertRaises(ValueError):
            self.transport.download(result)
        self.assertFalse((self.transport.directory(task_id) / '1.png').exists())

    def test_sdk_submission_error_retains_task_id(self):
        task_id = str(uuid.uuid4())
        error = {'task_id': task_id, 'message': 'submission uncertain', 'retry_submission': False}
        response = subprocess.CompletedProcess([], 1, json.dumps({'ok': False, 'error': error}), '')
        with patch('tested_comfy.transport.subprocess.run', return_value=response):
            result = self.transport.call({'operation': 'submit'})
        self.assertFalse(result['success'])
        self.assertTrue((self.transport.directory(task_id) / 'receipt.json').exists())

    def test_receipt_keeps_reproduction_parameters_and_history_is_offline(self):
        task_id = str(uuid.uuid4())
        request = {'prompt': 'keep this', 'preset': 'fast', 'seed': 0, 'steps': 6, 'cfg': 1}
        self.transport.record(task_id, {'task_id': task_id, 'request_used': request, 'original_request': {'prompt': 'keep this'}, 'state': 'queued'})
        self.transport.record(task_id, {'state': 'completed', 'request_used': {'prompt': 'overwrite'}})
        receipt = json.loads((self.transport.directory(task_id)/'receipt.json').read_text())
        self.assertEqual(receipt['request_used'], request)
        self.assertEqual(self.transport.history()[0]['prompt_summary'], 'keep this')

    def test_preflight_rejects_bad_prompts_and_seed_overflow(self):
        for args in ({'prompt': 'x'*8001}, {'prompt': 'x', 'seed': 2**64-1, 'count': 2}, {'prompt': 'x', 'count': 5}):
            with self.assertRaises(ValueError):
                package.validate_generation(args)


if __name__ == '__main__':
    unittest.main()
