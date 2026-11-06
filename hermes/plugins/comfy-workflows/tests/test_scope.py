"""Messaging routes stay reusable; local jobs retain their session identity."""
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('tested_comfy_scope', ROOT / 'scope.py')
scope = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scope
SPEC.loader.exec_module(scope)

TELEGRAM = {
    'profile': 'default', 'bot': '11', 'platform': 'telegram', 'chat_id': '100',
    'thread_id': '2', 'user_id': '7', 'session_key': 'telegram-route', 'session_id': 's1',
}
LOCAL = {
    'profile': 'default', 'bot': 'local', 'platform': 'local', 'chat_id': 'local-chat',
    'thread_id': '', 'user_id': 'local-user', 'session_key': 'local-route',
    'conversation_id': 'conversation-1', 'session_id': 'physical-tip',
}


class ScopeTest(unittest.TestCase):
    def test_telegram_scope_ignores_session_rotation(self):
        rotated = {**TELEGRAM, 'session_key': 'another-route', 'conversation_id': 'another-conversation', 'session_id': 's2'}
        self.assertEqual(set(scope.scope_fields(TELEGRAM, rotated)), set(scope.BASE_FIELDS))
        self.assertTrue(scope.same_scope(TELEGRAM, rotated))
        self.assertTrue(scope.scope_matches(TELEGRAM, rotated))
        scope.require_scope(TELEGRAM, rotated)

    def test_each_local_platform_includes_both_session_fields(self):
        for platform in ('cli', 'local', 'tui', 'desktop'):
            with self.subTest(platform=platform):
                origin = {**LOCAL, 'platform': platform}
                self.assertEqual(set(scope.scope_fields(origin)),
                                 set(scope.BASE_FIELDS) | {'session_key', 'conversation_id'})
                self.assertTrue(scope.same_scope(origin, dict(origin)))

    def test_local_sessions_cannot_access_each_other_under_same_chat_and_user(self):
        for field in ('session_key', 'conversation_id'):
            other = {**LOCAL, field: 'another-session'}
            with self.subTest(field=field):
                self.assertFalse(scope.same_scope(LOCAL, other))
                self.assertFalse(scope.scope_matches(LOCAL, other))
                with self.assertRaises(PermissionError):
                    scope.require_scope(LOCAL, other)

    def test_local_compression_keeps_logical_conversation_scope(self):
        compressed = {**LOCAL, 'session_id': 'new-physical-tip'}
        self.assertTrue(scope.same_scope(LOCAL, compressed))
        self.assertTrue(scope.scope_matches(LOCAL, compressed))
        scope.require_scope(LOCAL, compressed)

    def test_legacy_local_session_id_is_conservative_canonical_fallback(self):
        legacy = {key: value for key, value in LOCAL.items() if key != 'conversation_id'}
        current = {**LOCAL, 'conversation_id': legacy['session_id'], 'session_id': 'new-physical-tip'}
        for old in (legacy, {**legacy, 'conversation_id': ''}):
            self.assertTrue(scope.same_scope(old, current))
            self.assertTrue(scope.scope_matches(old, current))
            scope.require_scope(old, current)
        self.assertFalse(scope.same_scope(legacy, LOCAL))
        self.assertTrue(scope.scope_matches(LOCAL, {'session_id': LOCAL['conversation_id']}))
        self.assertFalse(scope.scope_matches(LOCAL, {'session_id': LOCAL['session_id']}))

    def test_local_background_filter_does_not_require_individual_session(self):
        binding = {'profile': 'default', 'platform': 'local', 'bot': 'local'}
        self.assertTrue(scope.scope_matches(LOCAL, binding))
        for field in ('profile', 'platform', 'bot'):
            with self.subTest(field=field):
                self.assertFalse(scope.scope_matches(LOCAL, {**binding, field: 'another'}))

    def test_explicit_local_session_filter_is_checked_even_without_platform_in_caller(self):
        for field in ('session_key', 'conversation_id'):
            with self.subTest(field=field):
                self.assertTrue(scope.scope_matches(LOCAL, {field: LOCAL[field]}))
                self.assertFalse(scope.scope_matches(LOCAL, {field: 'another-session'}))

    def test_either_origin_can_activate_local_session_matching(self):
        for left, right in ((LOCAL, TELEGRAM), (TELEGRAM, LOCAL)):
            with self.subTest(left=left['platform'], right=right['platform']):
                self.assertEqual(set(scope.scope_fields(left, right)),
                                 set(scope.BASE_FIELDS) | {'session_key', 'conversation_id'})
        # Partial matching normally ignores a Telegram caller's session metadata.
        # A local record must still enforce those fields when supplied explicitly.
        caller = {'platform': 'telegram', 'session_id': 's2'}
        self.assertFalse(scope.scope_matches(LOCAL, caller))

    def test_standalone_local_empty_session_fields_remain_compatible(self):
        standalone = {key: value for key, value in LOCAL.items()
                      if key not in ('session_key', 'conversation_id', 'session_id')}
        explicit_empty = {**standalone, 'session_key': '', 'conversation_id': '', 'session_id': ''}
        self.assertTrue(scope.same_scope(standalone, explicit_empty))
        self.assertTrue(scope.scope_matches(standalone, explicit_empty))
        scope.require_scope(standalone, explicit_empty)

    def test_none_caller_keeps_internal_store_compatibility(self):
        self.assertTrue(scope.scope_matches(LOCAL, None))
        scope.require_scope(LOCAL, None)

    def test_strict_scope_checks_every_base_field(self):
        for field in scope.BASE_FIELDS:
            with self.subTest(field=field):
                other = {**TELEGRAM, field: 'another'}
                self.assertFalse(scope.same_scope(TELEGRAM, other))
                with self.assertRaises(PermissionError):
                    scope.require_scope(TELEGRAM, other)


if __name__ == '__main__':
    unittest.main()
