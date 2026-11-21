import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from comfy_workflow_api.operations import sleep_policy


class IdleSleepPolicyTests(unittest.TestCase):
    def probe(self, **values):
        settings = {
            'IdleAction': 'v s "ignore"', 'IdleActionUSec': 'v t 600000000',
            'org.gnome.SettingsDaemon.Power': 'b true', 'SessionIsActive': 'v b true',
            'OnBattery': 'v b false', 'sleep-inactive-ac-type': "'suspend'", 'sleep-inactive-ac-timeout': '600',
            'sleep-inactive-battery-type': "'nothing'", 'sleep-inactive-battery-timeout': '300',
        } | values

        def command(args, **options):
            self.assertGreater(options['timeout'], 0)
            if args[0].endswith('busctl'):
                self.assertIn('--auto-start=no', args)
            result = settings[args[-1]]
            if isinstance(result, Exception):
                raise result
            return subprocess.CompletedProcess(args, 0, result, '')

        with patch.object(subprocess, 'run', side_effect=command):
            return sleep_policy.detect(os.environ)

    def test_gnome_uses_current_source_running_daemon_and_active_session(self):
        for values, expected in [
            ({}, 'gnome'), ({'OnBattery': 'v b true'}, 'disabled'),
            ({'OnBattery': 'v b true', 'sleep-inactive-battery-type': "'hibernate'"}, 'gnome'),
            ({'SessionIsActive': 'v b false'}, 'disabled'),
            ({'org.gnome.SettingsDaemon.Power': 'b false'}, 'disabled'),
            ({'sleep-inactive-ac-type': "'blank'"}, 'disabled'), ({'sleep-inactive-ac-timeout': '0'}, 'disabled'),
            ({'sleep-inactive-ac-timeout': 'malformed'}, 'unknown'), ({'OnBattery': 'bad'}, 'unknown'),
            ({'SessionIsActive': PermissionError()}, 'unknown'),
            ({'OnBattery': subprocess.TimeoutExpired('busctl', .4)}, 'unknown'),
            ({'sleep-inactive-ac-type': FileNotFoundError()}, 'unknown'),
        ]:
            with self.subTest(values=values):
                self.assertEqual(self.probe(**values), expected)

    def test_logind_uses_effective_sleep_action_and_finite_idle_timeout(self):
        for action, delay, expected in [
            ('suspend-then-hibernate', '300000000', 'logind'), ('sleep', '1000000', 'logind'),
            ('lock', '300000000', 'disabled'), ('suspend', '0', 'disabled'),
            ('hibernate', str(2**64 - 1), 'disabled'), ('suspend', 'bad', 'unknown'), ('unexpected', '20', 'unknown'),
        ]:
            with self.subTest(action=action, delay=delay):
                self.assertEqual(self.probe(IdleAction=f'v s "{action}"', IdleActionUSec='v t '+delay,
                                            **{'org.gnome.SettingsDaemon.Power':'b false'}), expected)

    def test_probes_share_one_deadline_and_keep_positive_evidence_from_either_source(self):
        self.assertEqual(self.probe(IdleAction=PermissionError()), 'gnome')
        self.assertEqual(self.probe(IdleAction='v s "suspend"', SessionIsActive=PermissionError()), 'logind')
        with patch.object(sleep_policy.time, 'monotonic', side_effect=[0, .8, 1.1]), patch.object(
            subprocess, 'run', return_value=Mock(returncode=0, stdout='v s "ignore"', stderr='')
        ) as command:
            self.assertEqual(sleep_policy.detect({}), 'unknown')
            self.assertEqual(command.call_count, 1)
            self.assertAlmostEqual(command.call_args.kwargs['timeout'], .2)
