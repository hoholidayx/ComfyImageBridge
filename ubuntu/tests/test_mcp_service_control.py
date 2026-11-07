import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1]/'scripts/mcp-service.py'
SPEC = importlib.util.spec_from_file_location('mcp_service_control_test', SCRIPT)
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'config').mkdir()
        (root/'config/settings.toml').write_text('[mcp]\nhost = "192.168.20.4"\nport = 8787\n')
        self.control = service.ServiceControl(root)
        self.control.directory.mkdir(parents=True)
        self.process = {'pid': 1234, 'uid': os.getuid(), 'start_ticks': '90', 'boot_id': 'boot',
                        'command': self.control.command('192.168.20.4', 8787)}
        self.saved = {'phase': 'running', 'host': '192.168.20.4', 'port': 8787, 'process': self.process}
        self.listener = {'address': '192.168.20.4', 'inode': '900'}

    def test_listeners_keeps_foreign_and_unknown_owners_but_ignores_time_wait(self):
        proc = self.control.root/'proc'
        (proc/'net').mkdir(parents=True)
        address = struct.unpack('=I', socket.inet_aton('192.168.20.4'))[0]

        def row(state, inode, ip=address):
            return f'0: {ip:08X}:2253 00000000:0000 {state} 0 0 0 1000 0 {inode}\n'

        (proc/'net/tcp').write_text('header\n'+row('0A', '900')+row('06', '901')+row('0A', '902', 0))
        directory = proc/'1234/fd'
        directory.mkdir(parents=True)
        (directory/'9').symlink_to('socket:[900]')
        with patch.object(service, 'PROC', proc):
            sockets = service.listeners(8787)
            self.assertEqual(sockets, [self.listener, {'address': '0.0.0.0', 'inode': '902'}])
            self.assertTrue(service.owns_listeners(1234, '192.168.20.4', sockets[:1]))
            self.assertFalse(service.owns_listeners(1234, '192.168.20.4', sockets))

    def test_sighup_disposition_checks_kernel_mask(self):
        proc = self.control.root/'proc'
        (proc/'1234').mkdir(parents=True)
        status = proc/'1234/status'
        with patch.object(service, 'PROC', proc):
            status.write_text('Name:\tpython\nSigIgn:\t0000000000000001\n')
            self.assertTrue(service.ignores_hangup(1234))
            status.write_text('SigIgn:\t0000000000000002\n')
            self.assertFalse(service.ignores_hangup(1234))

    def test_health_requires_listener_process_pid(self):
        with patch.object(service, 'HTTPConnection') as connection:
            http = connection.return_value
            response = http.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({'status': 'ok', 'service': 'comfy-image-mcp', 'pid': 1234}).encode()
            self.assertTrue(service.healthy('192.168.20.4', 8787, 1234))
            http.request.assert_called_once_with('GET', '/healthz', headers={'Host': '192.168.20.4:8787'})
            self.assertFalse(service.healthy('192.168.20.4', 8787, 4321))

    def test_start_is_detached_and_records_exact_command_after_health(self):
        launcher = Mock(pid=1234)
        launcher.poll.return_value = None
        with patch.object(service, 'listeners', return_value=[]), \
             patch.object(service.subprocess, 'Popen', return_value=launcher) as spawn, \
             patch.object(service, 'identity', return_value=self.process), \
             patch.object(self.control, 'status', return_value={'state': 'stopped'}), \
             patch.object(self.control, 'observe', return_value={'state': 'running', 'healthy': True}):
            self.assertEqual(self.control.run('start')['state'], 'running')
        argv, kwargs = spawn.call_args
        self.assertEqual(argv[0], ['/usr/bin/nohup', *self.process['command']])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.STDOUT)
        saved = self.control.saved()
        self.assertEqual(saved['process'], self.process)
        self.assertEqual(saved['phase'], 'running')
        self.assertEqual(saved['log'], str(self.control.log))

    def test_live_status_requires_matching_process_socket_and_hangup_disposition(self):
        service.write_json(self.control.state_file, self.saved)
        with patch.object(service, 'process_identity', return_value=self.process), \
             patch.object(service, 'detached', return_value=True), \
             patch.object(service, 'listeners', return_value=[self.listener]), \
             patch.object(service, 'owns_listeners', return_value=True), \
             patch.object(service, 'ignores_hangup', side_effect=[False, True]), \
             patch.object(service, 'healthy', return_value=True) as probe:
            self.assertEqual(self.control.status()['state'], 'unhealthy')
            probe.assert_not_called()
            self.assertEqual(self.control.status()['state'], 'running')
            probe.assert_called_once_with('192.168.20.4', 8787, 1234)

    def test_foreign_listener_blocks_start_and_stop(self):
        with patch.object(service, 'listeners', return_value=[self.listener]), \
             patch.object(service.subprocess, 'Popen') as spawn, \
             patch.object(service.os, 'pidfd_open', create=True) as open_pid:
            with self.assertRaisesRegex(RuntimeError, 'port is occupied'):
                self.control.run('start')
            with self.assertRaisesRegex(RuntimeError, 'no signal sent'):
                self.control.run('stop')
        spawn.assert_not_called()
        open_pid.assert_not_called()

    def test_pid_reuse_after_open_refuses_signal(self):
        service.write_json(self.control.state_file, self.saved)
        reused = {**self.process, 'start_ticks': '99'}
        with patch.object(service, 'process_identity', side_effect=[self.process, reused]), \
             patch.object(service, 'detached', return_value=True), \
             patch.object(service, 'listeners', return_value=[self.listener]), \
             patch.object(service, 'owns_listeners', return_value=True), \
             patch.object(service, 'ignores_hangup', return_value=True), \
             patch.object(service.os, 'pidfd_open', create=True, return_value=17), \
             patch.object(service.os, 'close') as close, \
             patch.object(service.signal, 'pidfd_send_signal', create=True) as send:
            with self.assertRaisesRegex(RuntimeError, 'changed before signalling'):
                self.control.run('stop')
        send.assert_not_called()
        close.assert_called_once_with(17)

    def test_stop_signals_only_the_verified_pid_and_waits_for_exit(self):
        service.write_json(self.control.state_file, self.saved)
        with patch.object(service, 'process_identity', side_effect=[self.process, self.process, None]), \
             patch.object(service, 'detached', return_value=True), \
             patch.object(service, 'listeners', side_effect=[[self.listener], [self.listener], []]), \
             patch.object(service, 'owns_listeners', return_value=True), \
             patch.object(service, 'ignores_hangup', return_value=True), \
             patch.object(service.os, 'pidfd_open', create=True, return_value=17) as open_pid, \
             patch.object(service.os, 'close'), \
             patch.object(service.signal, 'pidfd_send_signal', create=True) as send, \
             patch.object(service.select, 'poll') as poll:
            poll.return_value.poll.return_value = [(17, service.select.POLLIN)]
            self.assertEqual(self.control.run('stop')['state'], 'stopped')
        open_pid.assert_called_once_with(1234)
        send.assert_called_once_with(17, signal.SIGTERM)
        self.assertEqual(self.control.saved()['phase'], 'stopped')
