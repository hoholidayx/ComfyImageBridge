"""Validate dependency ordering and process ownership without live systemd."""
from contextlib import contextmanager, ExitStack
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('hardware_service', Path(__file__).parents[1]/'scripts/hardware-service.py')
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


class HardwareServiceTests(unittest.TestCase):
    def config(self):
        return {'root': '/home/example/comfy sdk', 'python': '/home/example/comfy sdk/.venv/bin/python',
                'user': 'example', 'home': '/home/example', 'uid': 1000,
                'path': '/home/example/.local/bin:/usr/bin:/bin'}

    @contextmanager
    def installation(self, existing=False, fail_action=None):
        """Use real temporary files, with only the systemd boundary simulated."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            config_path, controller = root/'service.json', root/'control.py'
            unit_dir = root/'systemd'
            unit_dir.mkdir()
            paths = [config_path, controller, *(unit_dir/name for name in service.UNITS)]
            previous = {**self.config(), 'path': '/previous/path:/usr/bin:/bin'}
            if existing:
                config_path.write_text(json.dumps(previous))
                controller.write_text('# previous controller\n')
                for name in service.UNITS:
                    (unit_dir/name).write_text(service.MARKER + '# previous ' + name + '\n')
                controller.chmod(0o700)
            before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
                      for path in paths}
            state = {'running': existing, 'failed_once': False,
                     'enabled': {service.SLEEP: 'enabled' if existing else 'not-found',
                                 service.MONITOR: 'disabled' if existing else 'not-found'}}
            calls = []
            def systemctl(*args, **kwargs):
                calls.append(args)
                if args[0] == fail_action and not state['failed_once']:
                    state['failed_once'] = True
                    raise RuntimeError('injected ' + fail_action + ' failure')
                output = ''
                if args[0] == 'is-enabled':
                    output = state['enabled'][args[1]] + '\n'
                elif args[0] == 'show':
                    output = ('active' if state['running'] else 'inactive') + '\n'
                elif args[0] in ('enable', 'disable'):
                    for name in args[1:]:
                        if name in state['enabled']:
                            state['enabled'][name] = ('enabled-runtime' if '--runtime' in args else 'enabled') if args[0] == 'enable' else 'disabled'
                    if '--now' in args and service.MONITOR in args:
                        state['running'] = args[0] == 'enable'
                elif args[0] in ('start', 'restart', 'stop'):
                    self.assertEqual(args[1], service.MONITOR)
                    state['running'] = args[0] != 'stop'
                return SimpleNamespace(stdout=output, stderr='', returncode=0)
            for name, value in (('CONFIG', config_path), ('CONTROLLER', controller), ('UNIT_DIR', unit_dir)):
                stack.enter_context(patch.object(service, name, value))
            stack.enter_context(patch.object(service, 'require_root'))
            stack.enter_context(patch.object(service, 'load_config', return_value=previous))
            stack.enter_context(patch.object(service, 'cycle', return_value={'active': False}))
            stack.enter_context(patch.object(service, 'command'))
            stack.enter_context(patch.object(service, 'systemctl', side_effect=systemctl))
            yield SimpleNamespace(paths=paths, before=before, previous=previous, state=state, calls=calls)

    def assert_files_restored(self, installation):
        for path, previous in installation.before.items():
            if previous is None:
                self.assertFalse(path.exists(), path)
            else:
                self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), previous)

    def test_install_dry_run_does_not_touch_files_or_services(self):
        with patch.object(service, 'require_root') as root, \
             patch.object(service, 'validate_units') as validate, \
             patch.object(service, 'systemctl') as systemctl, \
             patch.object(service, 'atomic_write') as write:
            self.assertTrue(service.install(self.config(), dry_run=True)['success'])
        for operation in (root, validate, systemctl, write):
            operation.assert_not_called()

    def test_install_validation_failure_preserves_existing_deployment(self):
        with self.installation(existing=True) as installed, \
             patch.object(service, 'validate_units', side_effect=RuntimeError('invalid unit')):
            with self.assertRaisesRegex(RuntimeError, 'invalid unit'):
                service.install(self.config())
            self.assert_files_restored(installed)
            self.assertEqual(installed.calls, [])

    def test_pending_sleep_gate_blocks_install_and_uninstall_without_mutation(self):
        for operation in (service.install, service.uninstall):
            with self.subTest(operation=operation.__name__), self.installation(existing=True) as installed, \
                 patch.object(service, 'cycle', return_value={'active': True, 'phase': 'restore_failed'}), \
                 patch.object(service, 'stop_empty_owner') as owner:
                with self.assertRaises(RuntimeError):
                    operation(self.config())
                self.assert_files_restored(installed)
                self.assertEqual(installed.calls, [])
                owner.assert_not_called()

    def test_upgrade_restarts_monitor_before_accepting_health(self):
        with self.installation(existing=True) as installed, \
             patch.object(service, 'verify_monitor', return_value={'main_pid': 42}) as verify:
            self.assertTrue(service.install(self.config())['success'])
            self.assertIn(('restart', service.MONITOR), installed.calls)
            self.assertNotIn(('enable', '--now', service.MONITOR), installed.calls)
            verify.assert_called_once_with(self.config())
            self.assertEqual(service.CONFIG.read_text(), json.dumps(self.config(), indent=2) + '\n')

    def test_failed_upgrade_restores_files_modes_enablement_and_running_monitor(self):
        with self.installation(existing=True) as installed, \
             patch.object(service, 'verify_monitor', side_effect=[RuntimeError('no fresh health'), {}]) as verify:
            with self.assertRaisesRegex(RuntimeError, 'previous service files and enablement restored'):
                service.install(self.config())
            self.assert_files_restored(installed)
            self.assertEqual(installed.state['enabled'], {service.SLEEP: 'enabled', service.MONITOR: 'disabled'})
            self.assertTrue(installed.state['running'])
            self.assertEqual(verify.call_args_list[-1].args, (installed.previous,))
            self.assertIn(('stop', service.MONITOR), installed.calls)
            self.assertIn(('start', service.MONITOR), installed.calls)

    def test_failed_first_install_removes_files_and_stops_replacement(self):
        with self.installation() as installed, \
             patch.object(service, 'verify_monitor', side_effect=RuntimeError('no fresh health')):
            with self.assertRaisesRegex(RuntimeError, 'previous service files and enablement restored'):
                service.install(self.config())
            self.assert_files_restored(installed)
            self.assertEqual(installed.state['enabled'], {service.SLEEP: 'disabled', service.MONITOR: 'disabled'})
            self.assertFalse(installed.state['running'])
            self.assertNotIn(('start', service.MONITOR), installed.calls)

    def test_reload_failure_restores_files_without_restarting_previous_process(self):
        with self.installation(existing=True, fail_action='daemon-reload') as installed:
            with self.assertRaisesRegex(RuntimeError, 'previous service files and enablement restored'):
                service.install(self.config())
            self.assert_files_restored(installed)
            self.assertTrue(installed.state['running'])
            self.assertFalse(any(call[0] in ('start', 'stop', 'restart', 'disable', 'enable') for call in installed.calls))

    def test_partial_write_failure_restores_previous_files(self):
        real_write = service.atomic_write
        with self.installation(existing=True) as installed:
            failed = False
            def write(path, content, mode):
                nonlocal failed
                if Path(path).name == service.SLEEP and not failed:
                    failed = True
                    raise OSError('injected write failure')
                return real_write(path, content, mode)
            with patch.object(service, 'atomic_write', side_effect=write):
                with self.assertRaisesRegex(RuntimeError, 'previous service files and enablement restored'):
                    service.install(self.config())
            self.assert_files_restored(installed)

    def test_rollback_errors_are_reported_as_needing_attention(self):
        with self.installation(existing=True), \
             patch.object(service, 'verify_monitor', side_effect=RuntimeError('unhealthy source')):
            with self.assertRaisesRegex(RuntimeError, 'rollback needs attention.*verify previous monitor'):
                service.install(self.config())

    def test_runtime_enablement_is_restored_after_upgrade_failure(self):
        with self.installation(existing=True) as installed, \
             patch.object(service, 'verify_monitor', side_effect=[RuntimeError('bad health'), {}]):
            installed.state['enabled'][service.SLEEP] = 'enabled-runtime'
            with self.assertRaises(RuntimeError):
                service.install(self.config())
            self.assertEqual(installed.state['enabled'][service.SLEEP], 'enabled-runtime')
            self.assertIn(('enable', '--runtime', service.SLEEP), installed.calls)

    def test_uninstall_reload_failure_restores_deployment_and_is_retryable(self):
        with self.installation(existing=True, fail_action='daemon-reload') as installed, \
             patch.object(service, 'owner_members', return_value=[]), \
             patch.object(service, 'stop_empty_owner') as stop_owner, \
             patch.object(service, 'verify_monitor', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'Uninstall failed; previous service files and enablement restored'):
                service.uninstall(installed.previous)
            self.assert_files_restored(installed)
            self.assertEqual(installed.state['enabled'], {service.SLEEP: 'enabled', service.MONITOR: 'disabled'})
            self.assertTrue(installed.state['running'])
            self.assertTrue(service.uninstall(installed.previous)['success'])
            for path in installed.paths:
                self.assertFalse(path.exists())
            self.assertFalse(installed.state['running'])
            self.assertEqual(stop_owner.call_count, 2)
            self.assertFalse(any(call[0] == 'start' and call[1] == service.OWNER for call in installed.calls))

    def test_uninstall_partial_delete_failure_restores_files(self):
        real_unlink = Path.unlink
        with self.installation(existing=True) as installed, \
             patch.object(service, 'owner_members', return_value=[]), \
             patch.object(service, 'stop_empty_owner'), \
             patch.object(service, 'verify_monitor', return_value={}):
            failed = False
            def unlink(path, *args, **kwargs):
                nonlocal failed
                if path == service.CONTROLLER and not failed:
                    failed = True
                    raise OSError('injected unlink failure')
                return real_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', unlink):
                with self.assertRaisesRegex(RuntimeError, 'Uninstall failed; previous service files and enablement restored'):
                    service.uninstall(installed.previous)
            self.assert_files_restored(installed)
            self.assertTrue(installed.state['running'])

    def test_uninstall_checks_empty_owner_before_disabling_anything(self):
        with self.installation(existing=True) as installed, \
             patch.object(service, 'owner_members', return_value=[]), \
             patch.object(service, 'stop_empty_owner', side_effect=RuntimeError('owner is changing')):
            with self.assertRaisesRegex(RuntimeError, 'owner is changing'):
                service.uninstall(installed.previous)
            self.assert_files_restored(installed)
            self.assertFalse(any(call[0] in ('enable', 'disable', 'stop', 'start') for call in installed.calls))

    def test_uninstall_refuses_foreign_unit_before_service_changes(self):
        with self.installation(existing=True) as installed, \
             patch.object(service, 'owner_members', return_value=[]):
            (service.UNIT_DIR/service.MONITOR).write_text('# foreign service\n')
            with self.assertRaisesRegex(RuntimeError, 'not managed by this installer'):
                service.uninstall(installed.previous)
            self.assertEqual(installed.calls, [])

    def test_sleep_failure_propagates_and_post_only_dispatches(self):
        units = service.render_units(self.config())
        sleep = units[service.SLEEP]
        self.assertIn('Before=sleep.target', sleep)
        self.assertIn('RequiredBy=sleep.target', sleep)
        self.assertIn('StopWhenUnneeded=yes', sleep)
        self.assertIn('ExecStopPost=', sleep)
        self.assertNotIn('system-sleep/', sleep)
        with patch.object(service, 'cycle', return_value={'active': True, 'phase': 'sleeping', 'cycle_token': 'test'}), \
             patch.object(service, 'dispatch_resume') as dispatch:
            service.coordinator(self.config(), 'coordinator-post')
        dispatch.assert_called_once()

    def test_owner_cgroup_survives_start_program_and_is_independent(self):
        units = service.render_units(self.config())
        owner = units[service.OWNER]
        self.assertIn('User=example', owner)
        self.assertIn('Type=oneshot', owner)
        self.assertIn('RemainAfterExit=yes', owner)
        self.assertIn('TimeoutStartSec=600', owner)
        self.assertIn('WorkingDirectory=/home/example/comfy sdk\n', owner)
        self.assertNotIn('WantedBy=multi-user.target', owner)
        monitor = units[service.MONITOR]
        self.assertNotIn('Conflicts=sleep.target', monitor)
        self.assertNotIn('PartOf=', monitor)
        self.assertNotIn('network-online.target', monitor)

    def test_refuses_to_stop_nonempty_or_starting_owner(self):
        with patch.object(service, 'owner_state', return_value='active'), \
             patch.object(service, 'owner_members', return_value=[123]), \
             patch.object(service, 'systemctl') as systemctl:
            with self.assertRaises(RuntimeError):
                service.stop_empty_owner()
            systemctl.assert_not_called()
        with patch.object(service, 'owner_state', return_value='activating'):
            with self.assertRaises(RuntimeError):
                service.stop_empty_owner()

    def test_finalize_only_after_owner_is_fully_active(self):
        calls = []
        def cycle(config, action, **kwargs):
            calls.append(action)
            return {'active': True, 'success': True, 'phase': 'sleeping', 'cycle_token': 'test', 'was_running': True}
        with patch.object(service, 'cycle', side_effect=cycle), \
             patch.object(service, 'owner_state', return_value='inactive'), \
             patch.object(service, 'owner_ready', return_value=False), \
             patch.object(service, 'systemctl'):
            with self.assertRaises(RuntimeError):
                service.coordinator(self.config(), 'coordinator-resume')
        self.assertNotIn('finalize', calls)
        self.assertIn('fail', calls)

    def test_restored_receipt_does_not_bypass_owner_activation(self):
        calls = []
        def cycle(config, action, **kwargs):
            calls.append(action)
            return {'active': True, 'success': True, 'phase': 'restored', 'cycle_token': 'test', 'was_running': True}
        with patch.object(service, 'cycle', side_effect=cycle), \
             patch.object(service, 'owner_ready', return_value=False):
            with self.assertRaises(RuntimeError):
                service.coordinator(self.config(), 'coordinator-resume')
        self.assertNotIn('finalize', calls)

    def test_old_boot_cycle_never_dispatches_resume(self):
        old = {'active': False, 'stale_boot': True, 'phase': 'sleeping', 'cycle_token': 'old', 'was_running': True}
        with patch.object(service, 'cycle', return_value=old), \
             patch.object(service, 'dispatch_resume') as dispatch, \
             patch.object(service, 'systemctl') as ctl:
            service.coordinator(self.config(), 'coordinator-post')
            result = service.coordinator(self.config(), 'coordinator-resume')
        dispatch.assert_not_called()
        ctl.assert_not_called()
        self.assertFalse(result['success'])

    def test_quote_systemd_expansions_and_control_characters(self):
        self.assertEqual(service.quoted('/home/a %b'), '"/home/a %%b"')
        with self.assertRaises(ValueError):
            service.quoted('bad\nExecStart=/bin/false')

    def test_start_requires_fresh_health_from_systemd_main_pid(self):
        unit = SimpleNamespace(stdout='ActiveState=active\nMainPID=42\n')
        health = {'running': True, 'stale': False, 'process': {'pid': 42}}
        with patch.object(service, 'systemctl', return_value=unit), \
             patch.object(service, 'user_command', return_value=health):
            self.assertEqual(service.verify_monitor(self.config())['main_pid'], 42)
        with patch.object(service, 'systemctl', return_value=unit), \
             patch.object(service, 'user_command', return_value={**health, 'process': {'pid': 99}}):
            with self.assertRaises(RuntimeError):
                service.verify_monitor(self.config(), timeout_seconds=.01)

    def test_start_failure_is_not_reported_as_success(self):
        with patch.object(service, 'systemctl', return_value=SimpleNamespace(stdout='ActiveState=failed\nMainPID=0\n')):
            with self.assertRaises(RuntimeError):
                service.verify_monitor(self.config(), timeout_seconds=.01)


if __name__ == '__main__':
    unittest.main()
