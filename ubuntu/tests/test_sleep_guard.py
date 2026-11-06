import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from comfy_workflow_api import sleep_guard as guard
from comfy_workflow_api.storage import write_json


class SleepGuardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.api = SimpleNamespace(data=Path(self.temporary.name), settings={'sleep_guard': {'enabled': True}})

    def tearDown(self):
        self.temporary.cleanup()

    def test_identity_rejects_pid_reuse_and_changed_command(self):
        saved = {'pid': 123, 'uid': os.getuid(), 'start_ticks': '456', 'command': ['our-worker']}
        with patch.object(guard, 'identity', return_value=saved):
            self.assertTrue(guard.matches(saved))
        for changed in ({**saved, 'start_ticks': '789'}, {**saved, 'command': ['other-process']}, None):
            with patch.object(guard, 'identity', return_value=changed):
                self.assertFalse(guard.matches(saved))

    def test_lease_is_removed_even_when_submission_raises(self):
        owner = {'pid': 123}
        with patch.object(guard, 'identity', return_value=owner), patch.object(guard, 'start'):
            with self.assertRaisesRegex(RuntimeError, 'submit failed'):
                with guard.hold(self.api):
                    self.assertEqual(len(list((guard.home(self.api)/'leases').glob('*.json'))), 1)
                    raise RuntimeError('submit failed')
        self.assertEqual(list((guard.home(self.api)/'leases').glob('*.json')), [])

    def test_stale_lease_does_not_keep_machine_awake(self):
        for key in ('live', 'dead'):
            write_json(guard.home(self.api)/'leases'/f'{key}.json', {'owner': {'key': key, 'pid': 99999991 if key == 'live' else 99999992}})
        with patch.object(guard, 'matches', side_effect=lambda owner: owner['key'] == 'live'):
            self.assertEqual(guard.live_leases(self.api), [{'owner': {'key': 'live', 'pid': 99999991}}])
        self.assertFalse((guard.home(self.api)/'leases/dead.json').exists())

    def test_refuses_to_signal_stale_pid_or_active_work(self):
        with patch.object(guard.os, 'kill') as kill:
            with patch.object(guard, 'status', return_value={'running': False, 'phase': 'watching'}):
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    guard.stop(self.api, force=True)
            self.api.client = SimpleNamespace(request=lambda path: {'queue_pending': [[0, 'job']]})
            with patch.object(guard, 'status', return_value={'running': True}), patch.object(guard, 'live_leases', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, 'active work'):
                    guard.stop(self.api)
            kill.assert_not_called()

    def test_orphan_supervisor_still_reports_actual_gnome_lock(self):
        write_json(guard.home(self.api)/'state.json', {'token':'orphan-token',
                   'supervisor':{'pid':123}, 'worker':{'pid':456}})
        with patch.object(guard,'matches',side_effect=lambda item:bool(item and item.get('pid') == 123)), patch.object(guard,'inhibited',return_value=True) as probe:
            result = guard.status(self.api)
        self.assertFalse(result['running'])
        self.assertTrue(result['inhibited'])
        probe.assert_called_once_with('orphan-token')

    def test_gnome_query_failure_is_unknown_not_released(self):
        write_json(guard.home(self.api)/'state.json', {'token':'recorded-token'})
        with patch.object(guard.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='D-Bus unavailable')):
            result = guard.status(self.api)
        self.assertIsNone(result['inhibited'])
        self.assertIn('D-Bus unavailable',result['health_error'])

    def test_disabled_guard_does_not_launch_anything(self):
        self.api.settings['sleep_guard']['enabled'] = False
        with patch.object(guard, 'start') as start:
            with guard.hold(self.api):
                guard.ticket(self.api, 'not-created')
            start.assert_not_called()
        self.assertFalse((self.api.data/'sleep-guard').exists())
