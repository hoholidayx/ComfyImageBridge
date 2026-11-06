import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from comfy_workflow_api import hardware_monitor as monitor
from comfy_workflow_api.storage import read_json, write_json


CPU = {'status': 'ok', 'utilization_percent': 100, 'temperatures': []}
GPU = {'status': 'ok', 'gpus': [{'pci': '0000:03:00.0', 'utilization_percent': 100,
        'temperatures': [{'label': 'junction', 'celsius': 60, 'critical_celsius': 110}],
        'optional': {'Memory Activity': 'N/A'}}]}
UNKNOWN = {'status': 'unknown', 'gpus': [], 'error': 'GPU query timed out'}
KERNEL = {'events': [], 'cursor': 'cursor-current', 'boot_id': 'boot-current'}


class HardwareMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root/'config').mkdir()
        self.configure()
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temporary.cleanup()

    def configure(self, extra='', smtp=False):
        content = 'data_dir = "data"\n[monitor]\n'+extra
        if smtp:
            content += '\n[monitor.smtp]\nsecret_file = "smtp.toml"\n'
        (self.root/'config/settings.toml').write_text(content)

    def store(self):
        store = monitor.EventStore(self.root)
        self.stores.append(store)
        return store

    def rows(self, store):
        return [dict(row) | {'payload': json.loads(row['payload'])}
                for row in store.connection.execute('SELECT * FROM outbox ORDER BY created,id')]

    def smtp(self, password='a-private-password'):
        self.configure(smtp=True)
        (self.root/'smtp.toml').write_text('''[smtp]
host = "smtp.example.test"
port = 587
security = "starttls"
username = "example-user"
password = "'''+password+'''"
from_address = "sender@example.test"
to_addresses = ["recipient@example.test"]
''')
        (self.root/'smtp.toml').chmod(0o600)

    def test_normal_na_and_high_utilization_do_not_alert(self):
        store = self.store()
        for i in range(8):
            self.assertEqual(store.observe(CPU, GPU, KERNEL, 1000+i*15), [])
        self.assertEqual(self.rows(store), [])

    def test_three_failures_and_recovery_persist_across_restart(self):
        store = self.store()
        for timestamp in (1000, 1015):
            self.assertEqual(store.observe(CPU, UNKNOWN, KERNEL, timestamp), [])
        store.observe(CPU, UNKNOWN, KERNEL, 1030)
        self.assertEqual(len(store.incidents()), 1)
        self.assertEqual(len(self.rows(store)), 1)
        restarted = self.store()
        restarted.observe(CPU, UNKNOWN, KERNEL, 1045)
        self.assertEqual(len(self.rows(restarted)), 1)
        for timestamp in (1060, 1075, 1090):
            restarted.observe(CPU, GPU, KERNEL, timestamp)
        self.assertEqual(restarted.incidents(), [])
        self.assertEqual([row['payload']['kind'] for row in self.rows(restarted)], ['fault', 'recovery'])
        restarted.observe(CPU, GPU, KERNEL, 1105)
        self.assertEqual(len(self.rows(restarted)), 2)

    def test_failed_rocm_source_is_distinct_from_gpu_health(self):
        store = self.store()
        source_failure = {**GPU, 'rocm_error': 'ROCm core readings unavailable'}
        for timestamp in (1000, 1015, 1030):
            store.observe(CPU, source_failure, KERNEL, timestamp)
        incidents = store.incidents()
        self.assertEqual([item['key'] for item in incidents], ['gpu:rocm_smi'])
        self.assertEqual(incidents[0]['severity'], 'warning')
        self.assertEqual(self.rows(store)[0]['payload']['details']['observation']['status'], 'ok')

    def test_pause_neither_counts_failures_nor_claims_recovery(self):
        store = self.store()
        for timestamp in (1000, 1015):
            store.observe(CPU, UNKNOWN, KERNEL, timestamp)
        for i in range(5):
            store.observe(CPU, {'status': 'unknown', 'paused': True}, KERNEL, 1030+i*15)
        self.assertEqual(self.rows(store), [])
        store.observe(CPU, UNKNOWN, KERNEL, 1120)
        self.assertEqual(len(store.incidents()), 1)
        for i in range(5):
            store.observe(CPU, {'status': 'unknown', 'paused': True}, KERNEL, 1140+i*15)
        self.assertEqual(len(store.incidents()), 1)
        self.assertEqual(len(self.rows(store)), 1)

    def test_temperature_critical_immediate_warning_debounced(self):
        store = self.store()
        warning_cpu = {'status': 'ok', 'temperatures': [{'label': 'package', 'celsius': 91, 'critical_celsius': 100}]}
        store.observe(warning_cpu, GPU, KERNEL, 1000)
        self.assertEqual(self.rows(store), [])
        critical = {'status': 'ok', 'gpus': [{'pci': '0000:03:00.0',
                    'temperatures': [{'label': 'junction', 'celsius': 106, 'critical_celsius': 110}]}]}
        store.observe(CPU, critical, KERNEL, 1015)
        self.assertEqual(len(self.rows(store)), 1)
        self.assertEqual(store.incidents()[0]['severity'], 'critical')

    def test_kernel_deduplicates_cursor_and_waits_before_recovery(self):
        store = self.store()
        kernel = {'events': [{'cursor': 'fatal-one', 'message': 'amdgpu 0000:03:00.0: PM: failed to resume async: error -14',
                             'at': '2026-11-06T00:00:00Z', 'boot_id': 'boot-current'}],
                  'cursor': 'fatal-one', 'boot_id': 'boot-current'}
        store.observe(CPU, GPU, kernel, 1000)
        self.assertEqual(len(self.rows(store)), 1)
        store.observe(CPU, GPU, kernel, 1015)
        store.observe(CPU, GPU, kernel, 1030)
        self.assertEqual(len(store.incidents()), 1)
        self.assertEqual(len(self.rows(store)), 1)
        store.observe(CPU, GPU, KERNEL, 1045)
        self.assertEqual(store.incidents(), [])
        self.assertEqual(len(self.rows(store)), 2)
        self.assertEqual(store.state('kernel_cursor'), 'cursor-current')

    def test_each_new_kernel_fault_notifies_immediately_during_an_active_incident(self):
        store = self.store()
        for index, timestamp in enumerate((1000, 1015)):
            cursor = f'fatal-{index}'
            kernel = {'events': [{'cursor': cursor,
                                 'message': 'amdgpu 0000:03:00.0: GPU reset failed'}],
                      'cursor': cursor, 'boot_id': 'boot-current'}
            store.observe(CPU, GPU, kernel, timestamp)
            # Re-reading a consumed cursor still must not enqueue another mail.
            store.observe(CPU, GPU, kernel, timestamp + 1)
        rows = self.rows(store)
        self.assertEqual([row['payload']['kind'] for row in rows], ['fault', 'fault'])
        self.assertEqual([row['payload']['details']['events'][0]['cursor'] for row in rows],
                         ['fatal-0', 'fatal-1'])
        self.assertEqual(len(store.incidents()), 1)

    def test_queue_full_kernel_fault_is_retried_after_restart_before_recovery(self):
        self.configure('outbox_limit = 1\n')
        store = self.store()
        first = store.enqueue('test', {}, 1000)
        kernel = {'events': [{'cursor': 'fatal-one', 'message': 'amdgpu GPU reset failed'}],
                  'cursor': 'fatal-one', 'boot_id': 'boot-current'}
        with patch('sys.stderr'):
            store.observe(CPU, GPU, kernel, 1015)
            store.observe(CPU, GPU, KERNEL, 1020)
        self.assertEqual(store.state('kernel_cursor'), KERNEL['cursor'])
        self.assertEqual(len(self.rows(store)), 1)
        store.outcome(first, True, timestamp=1025)
        restarted = self.store()
        restarted.observe(CPU, GPU, KERNEL, 1030)
        rows = self.rows(restarted)
        self.assertEqual([row['payload']['kind'] for row in rows], ['test', 'fault'])
        self.assertEqual(rows[1]['payload']['details']['events'][0]['cursor'], 'fatal-one')
        restarted.outcome(rows[1]['id'], True, timestamp=1035)
        restarted.observe(CPU, GPU, KERNEL, 1045)
        restarted.observe(CPU, GPU, KERNEL, 1060)
        self.assertEqual(restarted.incidents(), [])
        self.assertEqual([row['payload']['kind'] for row in self.rows(restarted)],
                         ['test', 'fault', 'recovery'])

    def test_unknown_sample_interrupts_consecutive_thermal_recovery(self):
        store = self.store()
        hot = {**GPU, 'gpus': [{'pci': '0000:03:00.0', 'temperatures': [
            {'label': 'junction', 'celsius': 106, 'critical_celsius': 110}]}]}
        store.observe(CPU, hot, KERNEL, 1000)
        for timestamp in (1015, 1030):
            store.observe(CPU, GPU, KERNEL, timestamp)
        store.observe(CPU, UNKNOWN, KERNEL, 1045)
        store.observe(CPU, GPU, KERNEL, 1060)
        self.assertEqual([row['payload']['kind'] for row in self.rows(store)], ['fault'])
        self.assertEqual([item['key'] for item in store.incidents()], ['gpu:0000:03:00.0:thermal'])
        for timestamp in (1075, 1090):
            store.observe(CPU, GPU, KERNEL, timestamp)
        self.assertEqual(store.incidents(), [])
        self.assertEqual([row['payload']['kind'] for row in self.rows(store)], ['fault', 'recovery'])

    def test_kernel_cursor_and_first_notification_share_transaction(self):
        store = self.store()
        kernel = {'events': [{'cursor': 'fatal-one', 'message': 'amdgpu GPU reset failed'}],
                  'cursor': 'fatal-one', 'boot_id': 'boot-current'}
        with patch.object(store, '_enqueue', side_effect=RuntimeError('injected transaction failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                store.observe(CPU, GPU, kernel, 1000)
        self.assertIsNone(store.state('kernel_cursor'))
        self.assertEqual(store.connection.execute('SELECT count(*) FROM kernel_seen').fetchone()[0], 0)
        store.observe(CPU, GPU, kernel, 1015)
        self.assertEqual(len(self.rows(store)), 1)

    def test_persistent_reminder_uses_thirty_minute_interval(self):
        store = self.store()
        for timestamp in (1000, 1015, 1030, 1045, 2800):
            store.observe(CPU, UNKNOWN, KERNEL, timestamp)
        self.assertEqual(len(self.rows(store)), 1)
        restarted = self.store()
        restarted.observe(CPU, UNKNOWN, KERNEL, 2830)
        self.assertEqual([row['payload']['kind'] for row in self.rows(restarted)], ['fault', 'reminder'])

    def test_kernel_critical_reminder_persists_while_gpu_observations_are_unknown(self):
        store = self.store()
        kernel = {'events': [{'cursor': 'fatal-one',
                             'message': 'amdgpu 0000:03:00.0: GPU reset failed'}],
                  'cursor': 'fatal-one', 'boot_id': 'boot-current'}
        store.observe(CPU, GPU, kernel, 1000)
        for timestamp in (1015, 1030, 1045, 2799):
            store.observe(CPU, UNKNOWN, KERNEL, timestamp)
        key = 'gpu:0000:03:00.0:kernel'
        rows = [row for row in self.rows(store) if row['payload']['details'].get('incident') == key]
        self.assertEqual([row['payload']['kind'] for row in rows], ['fault'])
        restarted = self.store()
        restarted.observe(CPU, UNKNOWN, KERNEL, 2800)
        rows = [row for row in self.rows(restarted) if row['payload']['details'].get('incident') == key]
        self.assertEqual([row['payload']['kind'] for row in rows], ['fault', 'reminder'])
        self.assertEqual(rows[-1]['payload']['details']['severity'], 'critical')
        self.assertEqual(rows[-1]['payload']['details']['events'][0]['cursor'], 'fatal-one')

    def test_paused_gpu_freezes_recovery_without_reminding_unresolved_kernel_fault(self):
        store = self.store()
        kernel = {'events': [{'cursor': 'fatal-one',
                             'message': 'amdgpu 0000:03:00.0: GPU reset failed'}],
                  'cursor': 'fatal-one', 'boot_id': 'boot-current'}
        store.observe(CPU, GPU, kernel, 1000)
        for timestamp in (1015, 1030):
            store.observe(CPU, GPU, KERNEL, timestamp)
        paused = {'status': 'unknown', 'paused': True}
        for timestamp in (2800, 4600):
            store.observe(CPU, paused, KERNEL, timestamp)
        self.assertEqual([row['payload']['kind'] for row in self.rows(store)], ['fault'])
        self.assertEqual(store.incidents()[0]['recoveries'], 2)
        # Healthy observations finish recovery without a stale fault reminder,
        # even after the reminder interval elapsed during the planned pause.
        store.observe(CPU, GPU, KERNEL, 4615)
        self.assertEqual(store.incidents(), [])
        self.assertEqual([row['payload']['kind'] for row in self.rows(store)], ['fault', 'recovery'])

    def test_queue_full_preserves_incident_and_retries_notice_when_space_returns(self):
        self.configure('outbox_limit = 1\n')
        store = self.store()
        first = store.enqueue('test', {}, 1000)
        with patch('sys.stderr'):
            for timestamp in (1015, 1030, 1045):
                store.observe(CPU, UNKNOWN, KERNEL, timestamp)
        self.assertEqual(len(store.incidents()), 1)
        self.assertIn('full', store.state('queue_error'))
        self.assertEqual(len(self.rows(store)), 1)
        store.outcome(first, True, timestamp=1050)
        store.observe(CPU, UNKNOWN, KERNEL, 1060)
        self.assertEqual(len(self.rows(store)), 2)
        self.assertEqual(self.rows(store)[1]['payload']['kind'], 'fault')
        self.assertIsNone(store.state('queue_error'))

    def test_failed_delivery_uses_backoff_and_preserves_event_order(self):
        store = self.store()
        first = store.enqueue('fault', {}, 1000)
        store.enqueue('recovery', {}, 1001)
        store.outcome(first, False, 'ConnectionError', timestamp=1002)
        self.assertIsNone(store.due(1061))
        self.assertEqual(store.due(1062)['id'], first)
        store.outcome(first, False, 'ConnectionError', timestamp=1062)
        self.assertEqual(store.due(1362)['id'], first)
        store.outcome(first, True, timestamp=1362)
        self.assertEqual(store.due(1362)['created'], 1001)

    def test_private_smtp_config_and_notification_payload_redaction(self):
        password = 'a-private-password'
        self.smtp(password)
        event_id = monitor.queue_event(self.root, 'fault', {'password': password, 'message': f'failed with {password}'})
        store = self.store()
        row = self.rows(store)[0]
        self.assertEqual(row['id'], event_id)
        self.assertNotIn(password, json.dumps(row))
        self.assertIn('[redacted]', json.dumps(row))
        client = SimpleNamespace(__enter__=None)
        with patch.object(monitor.smtplib, 'SMTP') as factory:
            smtp = factory.return_value.__enter__.return_value
            smtp.login.side_effect = RuntimeError(f'provider echoed {password}')
            self.assertEqual(monitor._deliver(self.root, event_id), 1)
        self.assertNotIn(password, json.dumps(self.rows(store)))
        self.assertEqual(self.rows(store)[0]['last_error'], 'RuntimeError')
        (self.root/'smtp.toml').chmod(0o644)
        self.assertFalse(monitor.smtp_configuration(self.root)[0])

    def test_successful_smtp_validates_tls_and_has_stable_message_id(self):
        self.smtp()
        event_id = monitor.queue_event(self.root, 'test', {'message': 'synthetic test'})
        with patch.object(monitor.smtplib, 'SMTP') as factory:
            smtp = factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            self.assertEqual(monitor._deliver(self.root, event_id), 0)
        smtp.starttls.assert_called_once()
        self.assertTrue(factory.call_args.kwargs['timeout'] == 5)
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message['Message-ID'], f'<{event_id}@myhermes2comfy.local>')
        store = self.store()
        self.assertIsNotNone(self.rows(store)[0]['sent_at'])

    def test_status_is_read_only_and_shows_unconfigured_email(self):
        data = self.root/'data'
        self.assertFalse(data.exists())
        result = monitor.status(self.root)
        self.assertFalse(result['notifications_configured'])
        self.assertFalse(result['running'])
        self.assertTrue(result['stale'])
        self.assertFalse(data.exists())

    def test_sender_hard_deadline_updates_outbox_only_once(self):
        store = self.store()
        event_id = store.enqueue('test', {}, 1000)
        sender = monitor.Sender(self.root)
        sender.process = SimpleNamespace(poll=lambda: None)
        sender.saved = {'pid': 123}
        sender.event_id, sender.attempts, sender.deadline = event_id, 0, 10
        with patch.object(monitor, '_matches', return_value=True), patch.object(monitor.time, 'monotonic', return_value=20), patch.object(monitor.os, 'kill') as kill:
            sender.pump()
            sender.pump()
        kill.assert_called_once_with(123, signal.SIGKILL)
        self.assertEqual(self.rows(store)[0]['attempts'], 1)

    def test_sender_restart_observes_existing_identity_before_kill(self):
        store = self.store()
        event_id = store.enqueue('test', {}, 1000)
        sender = monitor.Sender(self.root)
        write_json(sender.folder/'sender.json', {'process': {'pid': 123}, 'event_id': event_id,
                   'attempts': 0, 'deadline_monotonic': 0, 'timed_out': False})
        with patch.object(monitor, '_matches', return_value=False), patch.object(monitor.os, 'kill') as kill:
            sender._observe_orphan()
            kill.assert_not_called()
        with patch.object(monitor, '_matches', return_value=True), patch.object(monitor.os, 'kill') as kill:
            sender._observe_orphan()
            sender._observe_orphan()
            kill.assert_called_once_with(123, signal.SIGKILL)
        self.assertTrue(read_json(sender.folder/'sender.json')['timed_out'])
        self.assertEqual(self.rows(store)[0]['attempts'], 1)

    def test_unexpected_sender_exit_is_backed_off(self):
        store = self.store()
        event_id = store.enqueue('test', {}, 1000)
        sender = monitor.Sender(self.root)
        sender.process = SimpleNamespace(poll=lambda: 1)
        sender.event_id, sender.attempts = event_id, 0
        sender.pump()
        self.assertEqual(self.rows(store)[0]['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
