import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comfy_workflow_api import smtp_setup
from comfy_workflow_api.hardware_monitor import smtp_configuration


class SmtpSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'config').mkdir()
        (self.root/'config/settings.toml').write_text(
            '[monitor.smtp]\nsecret_file = "private/smtp.toml"\n')
        self.values = {
            'host': 'smtp.example.test', 'port': 465, 'security': 'ssl',
            'username': 'sender@example.test', 'from_address': 'sender@example.test',
            'to_addresses': ['recipient@example.test'], 'cc_addresses': ['copy@example.test'],
        }

    def test_private_atomic_file_preserves_password_and_cc(self):
        password = 'synthetic-"quoted"-\\-password'
        path = smtp_setup.save_configuration(self.root, self.values, password)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        settings, error = smtp_configuration(self.root)
        self.assertIsNone(error)
        self.assertEqual(settings['password'], password)
        self.assertEqual(settings['cc_addresses'], ['copy@example.test'])
        self.assertEqual(list(path.parent.glob('.smtp-*')), [])

    def test_rejected_update_leaves_existing_credentials_and_no_temporary_file(self):
        path = smtp_setup.save_configuration(self.root, self.values, 'original-synthetic')
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            smtp_setup.save_configuration(self.root, self.values, 'replacement-synthetic')
        with self.assertRaises(ValueError):
            smtp_setup.save_configuration(self.root, {**self.values, 'cc_addresses': ['invalid']},
                                          'replacement-synthetic', replace=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('.smtp-*')), [])

    def test_does_not_follow_secret_file_symlinks(self):
        target = self.root/'unrelated'
        target.write_text('unchanged')
        path = self.root/'private/smtp.toml'
        path.parent.mkdir()
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            smtp_setup.save_configuration(self.root, self.values, 'synthetic', replace=True)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_cli_refuses_noninteractive_password_input(self):
        with patch.object(smtp_setup.sys.stdin, 'isatty', return_value=False), \
                patch.object(smtp_setup.getpass, 'getpass') as prompt, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                smtp_setup.main(['--host', 'smtp.example.test', '--username', 'sender@example.test',
                                 '--to', 'recipient@example.test'])
        prompt.assert_not_called()
        self.assertFalse((self.root/'private/smtp.toml').exists())

    def test_cli_output_and_queued_event_do_not_contain_password(self):
        output = io.StringIO()
        password = 'synthetic-private-app-password'
        with patch.object(smtp_setup.sys.stdin, 'isatty', return_value=True), \
                patch.object(smtp_setup.getpass, 'getpass', return_value=password), \
                patch.object(smtp_setup, 'queue_event', return_value='event-identity') as queue, \
                contextlib.redirect_stdout(output):
            result = smtp_setup.main(['--root', str(self.root), '--host', 'smtp.example.test',
                                      '--username', 'sender@example.test', '--to', 'recipient@example.test',
                                      '--cc', 'copy@example.test', '--test-email'])
        self.assertEqual(result, 0)
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn(password, str(queue.call_args))
        queue.assert_called_once_with(self.root.resolve(), 'test', {'message': 'SMTP configuration test'})

    def test_cli_aborts_if_getpass_cannot_hide_input(self):
        with patch.object(smtp_setup.sys.stdin, 'isatty', return_value=True), \
                patch.object(smtp_setup.getpass, 'getpass', side_effect=smtp_setup.getpass.GetPassWarning), \
                contextlib.redirect_stderr(io.StringIO()):
            result = smtp_setup.main(['--root', str(self.root), '--host', 'smtp.example.test',
                                      '--username', 'sender@example.test', '--to', 'recipient@example.test'])
        self.assertEqual(result, 1)
        self.assertFalse((self.root/'private/smtp.toml').exists())

    def test_racing_first_setup_cannot_overwrite_another_created_file(self):
        path = self.root/'private/smtp.toml'

        def race_create(source, destination):
            Path(destination).write_text('another setup won')
            raise FileExistsError()

        with patch.object(smtp_setup.os, 'link', side_effect=race_create):
            with self.assertRaises(FileExistsError):
                smtp_setup.save_configuration(self.root, self.values, 'synthetic')
        self.assertEqual(path.read_text(), 'another setup won')
        self.assertEqual(list(path.parent.glob('.smtp-*')), [])

    def test_post_publish_sync_failure_reports_that_the_file_was_saved(self):
        real_fsync = smtp_setup.os.fsync
        calls = []

        def fail_directory_sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('synthetic sync failure')
            return real_fsync(fd)

        with patch.object(smtp_setup.os, 'fsync', side_effect=fail_directory_sync):
            with self.assertRaises(smtp_setup.ConfigurationSavedError):
                smtp_setup.save_configuration(self.root, self.values, 'synthetic')
        self.assertIsNotNone(smtp_configuration(self.root)[0])
        self.assertEqual(list((self.root/'private').glob('.smtp-*')), [])

    def test_queue_failure_keeps_valid_config_and_does_not_print_a_traceback(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(smtp_setup.sys.stdin, 'isatty', return_value=True), \
                patch.object(smtp_setup.getpass, 'getpass', return_value='synthetic-secret'), \
                patch.object(smtp_setup, 'queue_event', side_effect=smtp_setup.QueueFull('full')), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = smtp_setup.main(['--root', str(self.root), '--host', 'smtp.example.test',
                                      '--username', 'sender@example.test', '--to', 'recipient@example.test',
                                      '--test-email'])
        self.assertEqual(result, 1)
        self.assertIsNotNone(smtp_configuration(self.root)[0])
        self.assertIn('SMTP is configured', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())
        self.assertNotIn('synthetic-secret', output.getvalue()+error.getvalue())


if __name__ == '__main__':
    unittest.main()
