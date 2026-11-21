import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from comfy_workflow_api import sleep_guard as guard
from comfy_workflow_api.operations import process_identity
from comfy_workflow_api.storage import write_json


class SleepGuardTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.api = SimpleNamespace(data=Path(self.temporary.name), settings={'sleep_guard': {'enabled': True}})

    def tearDown(self):
        self.temporary.cleanup()

    def test_identity_rejects_pid_reuse_and_changed_command(self):
        saved = {'pid': 123, 'uid': os.getuid(), 'start_ticks': '456', 'command': ['our-worker']}
        with patch.object(process_identity, 'identity', return_value=saved):
            self.assertTrue(guard.matches(saved))
        for changed in ({**saved, 'start_ticks': '789'}, {**saved, 'command': ['other-process']}, None):
            with patch.object(process_identity, 'identity', return_value=changed):
                self.assertFalse(guard.matches(saved))

    def test_lease_is_removed_even_when_submission_raises(self):
        owner = {'pid': 123}
        with patch.object(guard, 'policy_for', return_value='gnome'), patch.object(guard, 'identity', return_value=owner), patch.object(guard, 'start'):
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
        probe.assert_called_once_with('orphan-token', 'gnome')

    def test_gnome_query_failure_is_unknown_not_released(self):
        write_json(guard.home(self.api)/'state.json', {'token':'recorded-token'})
        with patch.object(guard.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='D-Bus unavailable')):
            result = guard.status(self.api)
        self.assertIsNone(result['inhibited'])
        self.assertIn('D-Bus unavailable',result['health_error'])

    def test_disabled_guard_does_not_launch_anything(self):
        self.api.settings['sleep_guard']['enabled'] = False
        with patch.object(guard, 'start') as start, patch.object(guard.sleep_policy, 'detect') as probe:
            with guard.hold(self.api):
                guard.ticket(self.api, 'not-created')
            start.assert_not_called()
            probe.assert_not_called()
        self.assertFalse((self.api.data/'sleep-guard').exists())

    def test_logind_verifies_both_inhibitors_for_this_token_and_user(self):
        who = 'comfy-workflow-api-unique'
        for what, owner, mode, uid, expected in [('sleep:idle', who, 'block', os.getuid(), True),
                ('sleep', who, 'block', os.getuid(), False), ('idle:sleep', who, 'delay', os.getuid(), False),
                ('idle:sleep', who+'2', 'block', os.getuid(), False), ('idle:sleep', who, 'block', -1, False)]:
            row = [what, owner, 'test', mode, uid, 42]
            reply = json.dumps({'type':'a(ssssuu)', 'data':[[row]]})
            with self.subTest(row=row), patch.object(guard.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=0, stdout=reply)):
                self.assertIs(guard.inhibited('unique', 'logind'), expected)

    def test_selected_inhibitor_launches_the_detached_worker_and_records_its_driver(self):
        self.api.root = self.api.data.parent
        self.api.client = SimpleNamespace(base_url='http://127.0.0.1:8188')
        for driver, tool, flags in [('gnome', '/usr/bin/gnome-session-inhibit', 'suspend:idle'),
                                    ('logind', '/usr/bin/systemd-inhibit', '--what=idle:sleep')]:
            with self.subTest(driver=driver), patch.object(guard, 'policy_for', return_value=driver), \
                    patch.object(guard.subprocess, 'Popen') as launch, patch.object(guard, 'identity') as identity, \
                    patch.object(guard, 'status') as status:
                identity.side_effect = lambda pid: {'pid': 123, 'command': launch.call_args.args[0][1:]}
                status.side_effect = lambda api, policy: {**guard.state(api), 'running': True, 'ready': True, 'inhibited': True}
                current = guard.start(self.api)
                args = launch.call_args.args[0]
                self.assertEqual(args[:2], ['/usr/bin/nohup', tool])
                self.assertIn(flags, args)
                self.assertIn('comfy_workflow_api._sleep_guard_worker', args)
                self.assertEqual(current['driver'], driver)
                self.assertTrue(launch.call_args.kwargs['start_new_session'])
                self.assertEqual(launch.call_args.kwargs['stdin'], guard.subprocess.DEVNULL)
                (guard.home(self.api)/'state.json').unlink()

    def test_ticket_tracks_the_granted_lease_even_if_policy_changes(self):
        with patch.object(guard, 'policy_for', return_value='logind') as policy, patch.object(guard, 'start') as start:
            with guard.hold(self.api, task_id='job'):
                policy.return_value = 'disabled'
                guard.ticket(self.api, 'job')
                guard.ticket(self.api, 'unrelated')
            start.assert_called_once_with(self.api, policy='logind')
            policy.assert_called_once()
        ticket = self.api.data/'sleep-guard/tickets/job.json'
        self.assertTrue(ticket.is_file())
        self.assertFalse(ticket.with_name('unrelated.json').exists())
        self.api.settings['sleep_guard']['enabled'] = False
        guard.ticket(self.api, 'job', remove=True)
        self.assertFalse(ticket.exists())
