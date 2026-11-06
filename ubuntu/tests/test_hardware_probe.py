import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from comfy_workflow_api import hardware_probe as probe


class HardwareProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/settings.toml').write_text('data_dir="data"\n')
        self.data = self.root / 'data'
        self.data.mkdir()
        boot = patch.object(probe, '_boot_id', return_value='boot')
        boot.start()
        self.addCleanup(boot.stop)

    def state(self, phase):
        directory = self.data / 'sleep-cycle'
        directory.mkdir(exist_ok=True)
        (directory / 'state.json').write_text(json.dumps({'phase': phase, 'cycle_token': 'cycle', 'boot_id': 'boot'}))

    def test_sleep_gate_runs_before_gpu_or_process_access(self):
        for phase in ('preparing', 'sleeping', 'restoring'):
            self.state(phase)
            with patch.object(probe.subprocess, 'Popen') as spawn:
                result = probe.gpu_probe(self.root)
            self.assertTrue(result['paused'])
            self.assertEqual(result['status'], 'unknown')
            spawn.assert_not_called()
        self.state('sleeping')
        self.assertTrue(probe.gpu_probe(self.root, allow_during_restore=True)['paused'])
        self.state('restoring')
        self.assertIsNone(probe._sleep_gate(self.data, True))

    def test_malformed_sleep_state_pauses_instead_of_touching_gpu(self):
        self.state('sleeping')
        path = self.data / 'sleep-cycle/state.json'
        for value in ('not json', '[]'):
            path.write_text(value)
            with patch.object(probe.subprocess, 'Popen') as spawn:
                self.assertTrue(probe.gpu_probe(self.root)['paused'])
            spawn.assert_not_called()

    def test_previous_boot_sleep_cycle_does_not_permanently_pause_gpu(self):
        self.state('sleeping')
        path = self.data / 'sleep-cycle/state.json'
        value = json.loads(path.read_text())
        value['boot_id'] = 'previous-boot'
        path.write_text(json.dumps(value))
        original = path.read_bytes()
        self.assertIsNone(probe._sleep_gate(self.data, False))
        self.assertEqual(path.read_bytes(), original)

    def test_unreadable_boot_id_stays_unknown_without_starting_a_probe(self):
        with patch.object(probe, '_boot_id', return_value=None), \
             patch.object(probe.subprocess, 'Popen') as spawn:
            result = probe.gpu_probe(self.root)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['paused'])
        self.assertIn('boot ID', result['error'])
        spawn.assert_not_called()

    def test_incomplete_or_unknown_cycle_record_fails_closed(self):
        self.state('sleeping')
        path = self.data / 'sleep-cycle/state.json'
        for state in ({'phase': 'sleeping'}, {'phase': 'unexpected', 'cycle_token': 'cycle', 'boot_id': 'boot'}):
            path.write_text(json.dumps(state))
            with patch.object(probe.subprocess, 'Popen') as spawn:
                self.assertTrue(probe.gpu_probe(self.root)['paused'])
            spawn.assert_not_called()

    def test_existing_probe_lock_prevents_another_driver_process(self):
        home = self.data / 'hardware-probe'
        home.mkdir()
        with (home / 'gpu.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(probe.subprocess, 'Popen') as spawn:
                first = probe.gpu_probe(self.root)
                second = probe.gpu_probe(self.root)
        self.assertEqual(first['status'], 'unknown')
        self.assertTrue(first['probe_stuck'])
        self.assertTrue(second['probe_stuck'])
        spawn.assert_not_called()

    def test_probe_lock_survives_parent_close_when_child_inherits_descriptor(self):
        path = self.root / 'inherited.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, pass_fds=(fd,))
        os.close(fd)
        try:
            with path.open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            child.kill()
            child.wait(timeout=2)
        with path.open('a') as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_bounded_command_does_not_wait_for_an_unkillable_process(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        with patch.object(probe.subprocess, 'Popen', return_value=process):
            before = time.monotonic()
            result = probe._bounded_command(['synthetic-probe'], 0.01)
            elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.5)
        self.assertTrue(result['timed_out'])
        process.kill.assert_called_once()
        process.wait.assert_not_called()
        process.communicate.assert_not_called()
        process.poll.return_value = -9
        probe._reap()

    def test_gpu_timeout_has_no_unbounded_cleanup(self):
        process = Mock(pid=123456789, returncode=None)
        process.poll.return_value = None
        with patch.object(probe.subprocess, 'Popen', return_value=process), \
             patch.object(probe, '_identity', return_value=None):
            before = time.monotonic()
            result = probe.gpu_probe(self.root, timeout_seconds=0.05)
            elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.5)
        self.assertTrue(result['probe_stuck'])
        process.kill.assert_called_once()
        process.wait.assert_not_called()
        process.communicate.assert_not_called()
        process.poll.return_value = -9
        probe._reap()

    def test_cpu_first_sample_is_unknown_then_utilization_is_delta(self):
        sampler = probe.CpuSampler()
        stats = iter(['cpu  100 0 20 800 80 0 0 0\n', 'cpu  120 0 30 850 100 0 0 0\n'])
        def read(path):
            if str(path) == '/proc/stat':
                return next(stats)
            if str(path) == '/proc/meminfo':
                return 'MemTotal: 1000 kB\nMemAvailable: 250 kB\n'
            return None
        with patch.object(probe, '_text', side_effect=read):
            first, second = sampler.sample(), sampler.sample()
        self.assertIsNone(first['utilization_percent'])
        self.assertEqual(second['utilization_percent'], 30)
        self.assertEqual(second['memory']['used_percent'], 75)
        self.assertEqual(second['status'], 'ok')

    def test_optional_rocm_na_is_ignored_and_missing_values_stay_none(self):
        records = probe._rocm_records({'card0': {'PCI Bus': '03:00.0',
            'Temperature (Sensor edge) (C)': '45.5', 'GPU use (%)': '0',
            'VRAM Total Memory (B)': '25769803776', 'VRAM Total Used Memory (B)': '4096',
            'Memory Activity': 'N/A'}, 'card1': {'PCI Bus': '0000:04:00.0', 'GPU use (%)': 'N/A'}})
        self.assertEqual(records['0000:03:00.0']['utilization_percent'], 0)
        self.assertEqual(records['0000:03:00.0']['temperatures'][0]['celsius'], 45.5)
        self.assertEqual(records['0000:03:00.0']['vram_total_bytes'], 25769803776)
        self.assertEqual(records['0000:03:00.0']['vram_used_bytes'], 4096)
        self.assertIsNone(records['0000:04:00.0']['utilization_percent'])
        self.assertIsNone(records['0000:04:00.0']['vram_used_bytes'])

    def gpu_fixture(self, temperature='45000'):
        devices = self.root / 'pci'
        device = devices / '0000:03:00.0'
        sensor = device / 'hwmon/hwmon2'
        sensor.mkdir(parents=True)
        for filename, value in {'vendor': '0x1002', 'class': '0x030000', 'gpu_busy_percent': '0',
                                'mem_info_vram_used': '4096', 'mem_info_vram_total': '25769803776'}.items():
            (device / filename).write_text(value)
        driver = self.root / 'amdgpu'
        driver.mkdir()
        (device / 'driver').symlink_to(driver)
        for filename, value in {'temp1_input': temperature, 'temp1_crit': '110000',
                                'temp1_label': 'junction'}.items():
            (sensor / filename).write_text(value)
        kfd = self.root / 'kfd'
        kfd.touch()
        def path(value):
            if str(value) == '/sys/bus/pci/devices':
                return devices
            if str(value) == '/dev/kfd':
                return kfd
            return Path(value)
        return path

    def test_sysfs_pci_identity_and_temperature_crit(self):
        factory = self.gpu_fixture('111000')
        with patch.object(probe, 'Path', side_effect=factory), patch.object(probe.shutil, 'which', return_value=None):
            result = probe._collect_gpu(1, self.root / 'unused', time.monotonic() + 5)
        self.assertEqual(result['status'], 'critical')
        self.assertEqual(result['gpus'][0]['pci'], '0000:03:00.0')
        self.assertEqual(result['gpus'][0]['temperatures'][0],
                         {'label': 'junction', 'celsius': 111, 'critical_celsius': 110})

    def test_standard_rocm_keys_supply_core_readings_when_sysfs_is_unavailable(self):
        factory = self.gpu_fixture()
        device = self.root / 'pci/0000:03:00.0'
        for name in ('gpu_busy_percent', 'mem_info_vram_used', 'mem_info_vram_total',
                     'hwmon/hwmon2/temp1_input'):
            (device / name).unlink()
        payload = {'card17': {'PCI Bus': '03:00.0', 'GPU use (%)': '10',
                             'Temperature (Sensor edge) (C)': '47.5',
                             'VRAM Total Memory (B)': '25769803776',
                             'VRAM Total Used Memory (B)': '4096', 'Memory Activity': 'N/A'}}
        with patch.object(probe, 'Path', side_effect=factory), \
             patch.object(probe.shutil, 'which', return_value='/synthetic/rocm-smi'), \
             patch.object(probe, '_bounded_command', return_value={
                 'timed_out': False, 'returncode': 0, 'stdout': json.dumps(payload)}):
            result = probe._collect_gpu(1, self.root / 'unused', time.monotonic() + 5)
        self.assertEqual(result['status'], 'ok')
        gpu = result['gpus'][0]
        self.assertEqual(gpu['pci'], '0000:03:00.0')
        self.assertEqual(gpu['utilization_percent'], 10)
        self.assertEqual(gpu['vram_total_bytes'], 25769803776)
        self.assertEqual(gpu['vram_used_bytes'], 4096)
        self.assertEqual(gpu['temperatures'][0]['celsius'], 47.5)
        self.assertEqual(gpu['sources'], ['sysfs', 'rocm-smi'])
        self.assertNotIn('rocm_error', result)

    def test_power_and_runtime_observations_stay_inside_the_gpu_helper(self):
        factory = self.gpu_fixture()
        device = self.root / 'pci/0000:03:00.0'
        (device / 'power').mkdir()
        (device / 'power/runtime_status').write_text('active')
        (device / 'hwmon/hwmon2/power1_average').write_text('46500000')
        with patch.object(probe, 'Path', side_effect=factory), \
             patch.object(probe.shutil, 'which', return_value=None):
            gpu = probe._collect_gpu(1, self.root / 'unused', time.monotonic() + 5)['gpus'][0]
        self.assertEqual(gpu['runtime_status'], 'active')
        self.assertEqual(gpu['power_watts'], 46.5)
        self.assertEqual(gpu['power_sensor'], 'power1_average')

    def test_rocm_timeout_is_not_healthy_even_if_sysfs_works(self):
        factory = self.gpu_fixture()
        with patch.object(probe, 'Path', side_effect=factory), \
             patch.object(probe.shutil, 'which', return_value='/synthetic/rocm-smi'), \
             patch.object(probe, '_bounded_command', return_value={'timed_out': True}):
            result = probe._collect_gpu(1, self.root / 'unused', time.monotonic() + 5)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['probe_stuck'])

    def test_missing_kfd_is_unknown_even_with_normal_gpu_telemetry(self):
        factory = self.gpu_fixture()
        (self.root / 'kfd').unlink()
        with patch.object(probe, 'Path', side_effect=factory), patch.object(probe.shutil, 'which', return_value=None):
            result = probe._collect_gpu(1, self.root / 'unused', time.monotonic() + 5)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('/dev/kfd', result['error'])

    def test_rocm_all_core_na_is_reported_separately_from_healthy_sysfs(self):
        factory = self.gpu_fixture()
        payload = {'card0': {'PCI Bus': '0000:03:00.0', 'GPU use (%)': 'N/A',
                            'Temperature (Sensor edge) (C)': 'N/A',
                            'VRAM Total Memory (B)': 'N/A', 'VRAM Total Used Memory (B)': 'N/A',
                            'Memory Activity': 'N/A'}}
        with patch.object(probe, 'Path', side_effect=factory), \
             patch.object(probe.shutil, 'which', return_value='/synthetic/rocm-smi'), \
             patch.object(probe, '_bounded_command', return_value={
                 'timed_out': False, 'returncode': 0, 'stdout': json.dumps(payload)}):
            result = probe._collect_gpu(1, self.root / 'unused', time.monotonic() + 5)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('N/A', result['rocm_error'])

    def test_kernel_current_boot_cursor_and_real_resume_faults(self):
        messages = ['amdgpu 0000:03:00.0: PM: failed to resume async: error -14',
                    'Restore process 123 failed during resume',
                    'amdgpu: ring gfx_0.0.0 timeout, signaled seq=10',
                    'amdgpu: GPU reset failed', 'mce: [Hardware Error]: Machine check events logged',
                    'amdgpu: ring initialized successfully']
        records = [{'__CURSOR': f'c{index}', '_BOOT_ID': 'current', 'MESSAGE': message,
                    '__REALTIME_TIMESTAMP': '1000000'} for index, message in enumerate(messages)]
        records.append({'__CURSOR': 'other', '_BOOT_ID': 'old', 'MESSAGE': messages[0]})
        output = '\n'.join(json.dumps(record) for record in records) + '\n-- cursor: end\n'
        with patch.object(probe, '_boot_id', return_value='current'), \
             patch.object(probe, '_bounded_command', return_value={
                 'timed_out': False, 'returncode': 0, 'stdout': output, 'stderr': '', 'process': Mock()}) as run:
            result = probe.read_kernel_events('before', 'current')
        self.assertEqual(len(result['events']), 5)
        self.assertEqual(result['cursor'], 'end')
        self.assertIn('--after-cursor=before', run.call_args.args[0])
        self.assertIn('-k', run.call_args.args[0])
        self.assertNotIn('--kernel', run.call_args.args[0])
        self.assertTrue(all(event['boot_id'] == 'current' for event in result['events']))

    def test_kernel_first_observation_baselines_instead_of_replaying_previous_boot(self):
        with patch.object(probe, '_boot_id', return_value='new'), \
             patch.object(probe, '_bounded_command', return_value={
                 'timed_out': False, 'returncode': 0, 'stdout': '-- cursor: now\n',
                 'stderr': '', 'process': Mock()}) as run:
            result = probe.read_kernel_events('old-cursor', 'old')
        self.assertEqual(result['cursor'], 'now')
        self.assertIn('--lines=0', run.call_args.args[0])
        self.assertNotIn('--after-cursor=old-cursor', run.call_args.args[0])

    def test_journal_boot_ids_without_hyphens_match_kernel_boot_id(self):
        boot = '00112233-4455-6677-8899-aabbccddeeff'
        record = {'__CURSOR': 'event', '_BOOT_ID': boot.replace('-', ''),
                  'MESSAGE': 'amdgpu: PM: failed to resume async: error -14'}
        with patch.object(probe, '_boot_id', return_value=boot), \
             patch.object(probe, '_bounded_command', return_value={
                 'timed_out': False, 'returncode': 0, 'stdout': json.dumps(record),
                 'stderr': '', 'process': Mock()}):
            result = probe.read_kernel_events('before', boot)
        self.assertEqual(len(result['events']), 1)

    def test_helper_rechecks_sleep_phase_after_process_creation(self):
        self.state('preparing')
        home = self.data / 'hardware-probe'
        home.mkdir()
        with (home / 'gpu.lock').open('a') as lock, \
             patch.object(probe.sys, 'argv', ['probe', '--gpu-helper', str(lock.fileno()),
                                             str(home / 'gpu-process.json'),
                                             str(time.monotonic() + 5), '0']), \
             patch.object(probe, '_collect_gpu') as collect, patch('builtins.print') as output:
            probe._helper_main()
        collect.assert_not_called()
        self.assertTrue(json.loads(output.call_args.args[0])['paused'])

    def test_expired_kernel_cursor_resets_baseline_without_replaying_faults(self):
        answers = [
            {'timed_out': False, 'returncode': 1, 'stdout': '',
             'stderr': 'Failed to seek to cursor: Invalid argument', 'process': Mock()},
            {'timed_out': False, 'returncode': 0, 'stdout': '-- cursor: fresh-baseline\n',
             'stderr': '', 'process': Mock()},
        ]
        with patch.object(probe, '_boot_id', return_value='current'), \
             patch.object(probe, '_bounded_command', side_effect=answers) as run:
            result = probe.read_kernel_events('vacuumed-cursor', 'current')
        self.assertEqual(result['cursor'], 'fresh-baseline')
        self.assertEqual(result['events'], [])
        self.assertIn('missing interval', result['error'])
        first, second = [call.args[0] for call in run.call_args_list]
        self.assertIn('--after-cursor=vacuumed-cursor', first)
        self.assertIn('--lines=0', second)
        self.assertFalse(any(value.startswith('--after-cursor=') for value in second))
        self.assertLessEqual(run.call_args_list[1].args[1], 2)

    def test_kernel_permission_error_does_not_reset_or_hide_cursor(self):
        answer = {'timed_out': False, 'returncode': 1, 'stdout': '',
                  'stderr': 'Failed to open journal: Permission denied', 'process': Mock()}
        with patch.object(probe, '_boot_id', return_value='current'), \
             patch.object(probe, '_bounded_command', return_value=answer) as run:
            result = probe.read_kernel_events('before', 'current')
        self.assertEqual(result['cursor'], 'before')
        self.assertIn('Permission denied', result['error'])
        self.assertEqual(run.call_count, 1)

    def test_kernel_stuck_query_does_not_start_another(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(probe, '_KERNEL_PENDING', process), \
             patch.object(probe, '_boot_id', return_value='current'), \
             patch.object(probe, '_bounded_command') as run:
            result = probe.read_kernel_events('before', 'current')
        self.assertIn('still running', result['error'])
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
