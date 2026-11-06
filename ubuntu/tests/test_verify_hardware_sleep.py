"""The acceptance runner's offline checks never suspend a machine."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location('verify_hardware_sleep', Path(__file__).parents[1]/'scripts/verify-hardware-sleep.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class SleepVerificationTest(unittest.TestCase):
    def off(self, token='old'):
        return {'owned_process_running': False, 'listeners_present': False,
                'sleep_cycle': {'active': False, 'phase': 'complete', 'cycle_token': token,
                                'was_running': False}, 'inhibitor': {'inhibited': False, 'leases': 0}}

    def runner(self):
        controller = SimpleNamespace(systemctl=Mock(), stop_empty_owner=Mock(), owner_members=Mock(return_value=[]))
        runner = verify.Verification(controller, {})
        runner.progress = Mock()
        runner.gpu_check = Mock(return_value={'gpu': {'status': 'ok'}})
        return runner

    def test_default_invocation_only_prints_plan(self):
        with patch('sys.argv', ['verify-hardware-sleep.py']), patch.object(verify, 'load_controller') as load, redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(), 0)
        load.assert_not_called()

    def test_busy_unknown_and_foreign_instances_are_refused(self):
        for value in ({'owned_process_running': True, 'queue_known': False},
                      {'owned_process_running': True, 'queue_known': True, 'queue_running': 1},
                      {'listeners_present': True, 'owned_listener': False},
                      {'process': {'pid': 10}, 'processes_exited': False}):
            with self.assertRaises(RuntimeError):
                verify.assert_idle_owned(value)

    def test_sdk_is_always_run_as_configured_user(self):
        controller = SimpleNamespace(command=Mock(return_value=SimpleNamespace(returncode=0, stdout='{}', stderr='')))
        config = {'user': 'example', 'home': '/home/example', 'root': '/home/example/sdk',
                  'path': '/usr/bin', 'python': '/home/example/sdk/.venv/bin/python'}
        verify.sdk(controller, config, 'write', {'success': False})
        argv = controller.command.call_args.args[0]
        self.assertEqual(argv[:4], ['/usr/sbin/runuser', '-u', 'example', '--'])
        self.assertIn(verify.USER_CODE, argv)
        self.assertNotIn('shell', controller.command.call_args.kwargs)

    def test_cycle_requires_real_sleep_and_a_new_completed_token(self):
        runner = self.runner()
        runner.wait_inhibitor_release = Mock(return_value=self.off())
        runner.current = Mock(return_value=self.off('new'))
        with patch.object(verify, 'elapsed_clock', side_effect=[0, 1, 2]), \
             patch.object(verify, 'sleep_offset', side_effect=[0, 15]):
            result = runner.one_cycle(1, False)
        self.assertEqual(result['sleep_cycle']['cycle_token'], 'new')
        runner.controller.systemctl.assert_called_once_with('suspend', timeout=175)
        self.assertTrue(runner.receipt['cycles'][0]['success'])
        runner.gpu_check.assert_called_once()

    def test_fresh_gpu_check_retries_monitor_lock_contention(self):
        runner = verify.Verification(SimpleNamespace(), {})
        samples = [{'gpu': {'status': 'unknown', 'probe_stuck': True}},
                   {'gpu': {'status': 'ok'}}]
        with patch.object(verify, 'sdk', side_effect=samples) as sdk, \
             patch.object(verify, 'elapsed_clock', return_value=0), \
             patch.object(verify.time, 'sleep'):
            self.assertEqual(runner.gpu_check()['gpu']['status'], 'ok')
        self.assertEqual(sdk.call_count, 2)

    def test_fresh_gpu_check_never_accepts_rocm_na_even_with_sysfs_ok(self):
        runner = verify.Verification(SimpleNamespace(), {})
        clock = iter([0, 1, 2, 3, 16])
        with patch.object(verify, 'sdk', return_value={'gpu': {'status': 'ok', 'rocm_error': 'N/A'}}), \
             patch.object(verify, 'elapsed_clock', side_effect=lambda: next(clock)), \
             patch.object(verify.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'GPU health check failed'):
                runner.gpu_check()

    def test_failed_recovery_does_not_request_another_sleep(self):
        runner = self.runner()
        runner.wait_inhibitor_release = Mock(return_value=self.off())
        failed = self.off('new')
        failed['sleep_cycle'].update(phase='restore_failed', active=True, error='GPU unavailable')
        runner.current = Mock(return_value=failed)
        with patch.object(verify, 'elapsed_clock', side_effect=[0, 1, 2]), \
             patch.object(verify, 'sleep_offset', side_effect=[0, 15]):
            with self.assertRaisesRegex(RuntimeError, 'GPU unavailable'):
                runner.one_cycle(1, False)
        self.assertEqual(runner.controller.systemctl.call_count, 1)

    def test_second_cycle_requires_new_instance_and_pid(self):
        runner = self.runner()
        before = {**self.off(), 'owned_process_running': True, 'ready': True, 'queue_known': True,
                  'service_instance_id': 'old-instance', 'process': {'pid': 10}}
        after = {**before, 'sleep_cycle': {'active': False, 'phase': 'complete', 'cycle_token': 'new', 'was_running': True}}
        runner.wait_inhibitor_release = Mock(return_value=before)
        runner.current = Mock(return_value=after)
        with patch.object(verify, 'elapsed_clock', side_effect=[0, 1, 2]), \
             patch.object(verify, 'sleep_offset', side_effect=[0, 15]):
            with self.assertRaisesRegex(RuntimeError, 'new service instance and PID'):
                runner.one_cycle(2, True)

    def test_original_off_state_requires_normal_stop_and_empty_owner(self):
        runner = self.runner()
        runner.original = self.off()
        runner.stop = Mock(return_value=self.off())
        runner.restore_original()
        runner.stop.assert_called_once()
        runner.controller.stop_empty_owner.assert_called_once()
        self.assertTrue(runner.receipt['restoration']['success'])

    def test_restore_failure_cannot_be_reported_as_passed(self):
        runner = self.runner()
        runner.current = Mock(side_effect=[self.off(), RuntimeError('gate still active')])
        runner.controller.install = Mock(side_effect=RuntimeError('install failed'))
        self.assertEqual(runner.run(), 1)
        self.assertFalse(runner.receipt['success'])
        self.assertIn('restoration_error', runner.receipt)


if __name__ == '__main__':
    unittest.main()
