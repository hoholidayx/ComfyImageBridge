import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'tested_comfy_cards'
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.context_cards', ROOT / 'context_cards.py')
cards = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cards
spec.loader.exec_module(cards)

ORIGIN = {'profile': 'default', 'platform': 'telegram', 'bot': '11', 'chat_id': '100',
          'thread_id': '2', 'user_id': '7', 'session_key': 'trusted-key', 'session_id': 's1'}


class Store:
    def __init__(self, values):
        self.values = values

    def artifacts(self, origin=None, limit=20):
        return self.values[:limit]


def artifact(index, **kwargs):
    return {'image_id': 'image-' + str(index), 'task_id': 'task', 'title': '机器人作品' + str(index),
            'width': 512, 'height': 512, 'preset': 'fast', 'reference_ids': [], 'expired': False,
            'created_at': f'2026-11-06T12:00:{index:02d}Z', 'origin': ORIGIN, **kwargs}


class ContextCardsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = types.SimpleNamespace()
        self.patcher = patch.object(cards, 'capture_origin', return_value=dict(ORIGIN))
        self.patcher.start()
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def collector(self, *values):
        return cards.ContextCards(self.ctx, self.temp.name, store=Store(list(values)))

    def test_new_session_gets_only_latest_four_and_no_later_backfill(self):
        collector = self.collector(*(artifact(i) for i in range(10)))
        result = collector.before_turn(session_id='s1', conversation_history=[])
        self.assertEqual(len(cards.MARKER.findall(result['context'])), 4)
        self.assertNotIn('[comfy-image:image-5]', result['context'])
        history = [{'role': 'user', 'content': '普通聊天', 'api_content': '普通聊天\n' + result['context']}]
        self.assertIsNone(collector.before_turn(session_id='s1', conversation_history=history))

    def test_failed_turn_does_not_acknowledge_returned_cards(self):
        collector = self.collector(artifact(1))
        first = collector.before_turn(session_id='s1', conversation_history=[])
        second = collector.before_turn(session_id='s1', conversation_history=[])
        self.assertEqual(first, second)
        history = [{'role': 'user', 'api_content': first['context']}]
        self.assertIsNone(collector.before_turn(session_id='s1', conversation_history=history))

    def test_old_tool_result_is_not_durable_acknowledgement(self):
        collector = self.collector(artifact(1))
        history = [{'role': 'tool', 'content': '[comfy-image:image-1]'}]
        self.assertIsNotNone(collector.before_turn(session_id='s1', conversation_history=history))

    def test_other_chat_scope_and_child_turns_receive_no_cards(self):
        collector = self.collector(artifact(1, origin={**ORIGIN, 'chat_id': '999'}))
        self.assertIsNone(collector.before_turn(session_id='s1', conversation_history=[]))
        self.assertIsNone(self.collector(artifact(1)).before_turn(session_id='s1', parent_session_id='parent'))
        self.assertIsNone(self.collector(artifact(1)).before_turn(session_id='different'))

    def test_expired_cards_are_titles_only_and_never_claim_visual_verification(self):
        result = self.collector(artifact(1, expired=True)).before_turn(session_id='s1')
        self.assertIn('未经视觉核验', result['context'])
        self.assertIn('不可作参考图', result['context'])
        self.assertNotIn('/path/to', result['context'])

    def test_local_cards_follow_logical_conversation_through_compression(self):
        local = {**ORIGIN, 'platform': 'local', 'bot': 'local', 'session_key': 'local:route',
                 'conversation_id': 'logical-a', 'session_id': 'old-tip'}
        current = {**local, 'session_id': 'new-tip'}
        collector = self.collector(artifact(1, origin=local),
                                   artifact(2, origin={**local, 'conversation_id': 'logical-b'}))
        with patch.object(cards, 'capture_origin', return_value=current):
            result = collector.before_turn(session_id='new-tip')
        self.assertIn('[comfy-image:image-1]', result['context'])
        self.assertNotIn('[comfy-image:image-2]', result['context'])

    def test_total_injected_characters_are_bounded(self):
        collector = self.collector(*(artifact(i, title='很长的标题' * 100,
                                             reference_ids=['x' * 500] * 10) for i in range(4)))
        result = collector.before_turn(session_id='s1')
        self.assertLessEqual(len(result['context']), 1600)

    def test_multimodal_text_sidecar_card_is_deduplicated(self):
        history = [{'role': 'user', 'content': [{'type': 'text', 'text': '[comfy-image:image-1]'}]}]
        self.assertIsNone(self.collector(artifact(1)).before_turn(session_id='s1', conversation_history=history))

    def test_install_registers_only_pre_llm_hook(self):
        hooks = {}
        ctx = types.SimpleNamespace(register_hook=lambda name, callback: hooks.update({name: callback}))
        cards.install(ctx, self.temp.name, store=Store([]))
        self.assertEqual(list(hooks), ['pre_llm_call'])


if __name__ == '__main__':
    unittest.main()
