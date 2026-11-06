from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tested_ubuntu_runtime', PLUGIN/'__init__.py', submodule_search_locations=[str(PLUGIN)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from tested_ubuntu_runtime.runtime import Runtime


class Context:
    def __init__(self, **config):
        self.config = {"ssh_host":"tester@192.0.2.10", "remote_root":"/srv/comfy-workflow-api", "lan_cidr":"192.0.2.0/24", "mac_address":"02:00:00:00:00:01", **config}

    def get_config(self, key, default=None):
        return self.config.get(key, default)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = Runtime(Context(), self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_offline_readonly_status_never_wakes_or_ssh(self):
        with patch.object(self.runtime, 'reachable', return_value=False), patch.object(self.runtime, 'wake') as wake, patch.object(self.runtime, 'remote') as remote:
            result = self.runtime.run('status')
        self.assertTrue(result['success'])
        self.assertEqual(result['power_state'], 'offline_unknown')
        self.assertFalse(result['wake_packets_sent'])
        wake.assert_not_called()
        remote.assert_not_called()
        self.assertTrue(Path(result['receipt_path']).is_file())

    def test_offline_stop_and_restart_never_wake(self):
        with patch.object(self.runtime, 'reachable', return_value=False), patch.object(self.runtime, 'wake') as wake, patch.object(self.runtime, 'remote') as remote:
            for action in ('stop', 'restart'):
                self.assertFalse(self.runtime.run(action)['success'])
        wake.assert_not_called()
        remote.assert_not_called()

    def test_online_ensure_reuses_without_wake(self):
        with patch.object(self.runtime, 'reachable', return_value=True), patch.object(self.runtime, 'wake') as wake, patch.object(self.runtime, 'remote', return_value={'success': True, 'reused_existing': True}) as remote:
            result = self.runtime.run('ensure_ready')
        wake.assert_not_called()
        remote.assert_called_once_with('ensure_ready')
        self.assertTrue(result['reused_existing'])

    def test_online_ssh_management_works_without_wol_configuration(self):
        runtime = Runtime(Context(lan_cidr='', mac_address=''), self.root)
        with patch.object(runtime, 'reachable', return_value=True), patch.object(runtime, 'wake') as wake, patch.object(runtime, 'remote', return_value={'success': True, 'ready': True}) as remote:
            self.assertTrue(runtime.run('ensure_ready')['ready'])
            self.assertTrue(runtime.run('status')['success'])
        self.assertEqual(remote.call_count, 2)
        wake.assert_not_called()

    def test_offline_ensure_without_wol_fails_before_sending_packets(self):
        runtime = Runtime(Context(lan_cidr='', mac_address=''), self.root)
        with patch.object(runtime, 'reachable', return_value=False), patch('tested_ubuntu_runtime.runtime.socket.socket') as factory, patch.object(runtime, 'remote') as remote:
            result = runtime.run('ensure_ready')
        self.assertFalse(result['success'])
        self.assertIn('Wake-on-LAN is not configured', result['error']['message'])
        self.assertFalse(result['wake_packets_sent'])
        factory.assert_not_called()
        remote.assert_not_called()

    def test_sleeping_ensure_orders_wake_then_service(self):
        events = []
        with patch.object(self.runtime, 'reachable', return_value=False), patch.object(self.runtime, 'wait_for_ssh', side_effect=lambda: events.append('wake') or 7), patch.object(self.runtime, 'remote', side_effect=lambda action: events.append(action) or {'success': True}):
            result = self.runtime.run('ensure_ready')
        self.assertEqual(events, ['wake', 'ensure_ready'])
        self.assertEqual(result['wake_seconds'], 7)

    def test_failed_wake_never_starts_service(self):
        with patch.object(self.runtime, 'reachable', return_value=False), patch.object(self.runtime, 'wait_for_ssh', side_effect=TimeoutError('offline')), patch.object(self.runtime, 'remote') as remote:
            result = self.runtime.run('ensure_ready')
        remote.assert_not_called()
        self.assertFalse(result['success'])
        self.assertFalse(result['error']['retry_automatically'])

    def test_ssh_payload_is_stdin_and_timeout_is_not_retried(self):
        odd = "/home/user/quote ' ; $(touch /tmp/never-execute)"
        runtime = Runtime(Context(remote_root=odd), self.root)
        runtime.record = {'operation_id': 'test'}
        response = subprocess.CompletedProcess([], 0, '{"success":true}', '')
        with patch('tested_ubuntu_runtime.runtime.subprocess.run', return_value=response) as mocked:
            runtime.remote('ensure_ready')
        argv, kwargs = mocked.call_args
        self.assertEqual(json.loads(kwargs['input'])['root'], odd)
        self.assertFalse(kwargs.get('shell', False))
        self.assertIn('StrictHostKeyChecking=yes', argv[0])
        with patch('tested_ubuntu_runtime.runtime.subprocess.run', side_effect=subprocess.TimeoutExpired('ssh', 230)) as mocked:
            with self.assertRaisesRegex(RuntimeError, 'completion is unknown'):
                runtime.remote('restart')
        self.assertEqual(mocked.call_count, 1)

    def test_untrusted_hosts_and_bad_mac_are_rejected(self):
        for config in ({'ssh_host': '-oProxyCommand=bad'}, {'ssh_host': 'root@host;id'},
                       {'mac_address': 'bad'}, {'remote_root': 'relative'},
                       {'ssh_host': ''}, {'remote_root': ''}, {'remote_root': '~/workspace/backend'},
                       {'mac_address': ''}, {'lan_cidr': ''}):
            with self.assertRaises(ValueError):
                Runtime(Context(**config), self.root)

    def test_wol_exact_packet_and_actual_lan_binding(self):
        self.runtime.run = None
        self.runtime.record = {'operation_id': 'test'}
        self.runtime.receipt = self.root/'receipt.json'
        route = subprocess.CompletedProcess([], 0, 'interface: en1\n', '')
        address = subprocess.CompletedProcess([], 0, '192.0.2.11\n', '')
        with patch('tested_ubuntu_runtime.runtime.subprocess.run', side_effect=[route,address]), patch('tested_ubuntu_runtime.runtime.socket.socket') as factory, patch('tested_ubuntu_runtime.runtime.time.sleep'):
            self.runtime.wake()
        sock = factory.return_value.__enter__.return_value
        sock.bind.assert_called_once_with(('192.0.2.11',0))
        self.assertEqual(sock.sendto.call_count,3)
        packet, destination = sock.sendto.call_args.args
        self.assertEqual(packet, b'\xff'*6+bytes.fromhex('020000000001')*16)
        self.assertEqual(destination, ('192.0.2.255',9))

    def test_vpn_route_and_wrong_sender_never_send_packets(self):
        for results in ([subprocess.CompletedProcess([],0,'interface: utun4','')],
                        [subprocess.CompletedProcess([],0,'interface: en1',''),subprocess.CompletedProcess([],0,'10.0.0.1','')]):
            with patch('tested_ubuntu_runtime.runtime.subprocess.run',side_effect=results), patch('tested_ubuntu_runtime.runtime.socket.socket') as sock:
                with self.assertRaises(RuntimeError):
                    self.runtime.wake()
                sock.assert_not_called()

    def test_tool_arguments_cannot_supply_a_command(self):
        result = json.loads(package.handle(Context(), 'comfy_service', {'action':'ensure_ready','command':'bad'}))
        self.assertFalse(result['success'])
        self.assertIn('Unknown arguments',result['error']['message'])


if __name__ == '__main__':
    unittest.main()

class AutomaticPolicyTest(unittest.TestCase):
    def test_no_confirmation_command_is_registered(self):
        from unittest.mock import Mock
        ctx=Mock()
        package.register(ctx)
        ctx.register_command.assert_not_called()
        self.assertEqual(ctx.register_tool.call_count,2)

    def test_policy_is_not_a_model_permission_flag(self):
        for args in ({'action':'recover','force':True},{'action':'recover','approval':{'confirmed_at':'fake'}}):
            result=json.loads(package.handle(Context(),'comfy_service',args))
            self.assertFalse(result['success'])
            self.assertIn('Unknown arguments',result['error']['message'])
