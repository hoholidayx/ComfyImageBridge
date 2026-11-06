"""Desktop completions keep their original target through retries and restarts."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'tested_comfy_local'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
from importlib import import_module
local = import_module(PACKAGE+'.local_delivery')
jobs = import_module(PACKAGE+'.jobs')


def origin(**changes):
    return {'profile': 'default', 'platform': 'local', 'bot': 'local',
            'chat_id': 'conversation-a', 'user_id': 'local-user', 'thread_id': '',
            'session_key': 'local:route-a', 'session_id': 'physical-tip-a',
            'conversation_id': 'conversation-a',
            'delivery_target': {'token': 'synthetic-ticket-a', 'conversation_id': 'conversation-a'}, **changes}


class Context:
    profile_name = 'default'

    def __init__(self):
        self.calls, self.accepted, self.tasks, self.hooks = [], {}, [], {}
        self.task_options = []
        self.lose_receipt_once = False
        self.reply = None
        self.capture = origin()['delivery_target']

    def capture_delivery_target(self):
        return {'ok': True, 'target': dict(self.capture)}

    async def apublish_completion(self, target, **kwargs):
        self.calls.append((dict(target), dict(kwargs)))
        if self.reply:
            return self.reply
        key = (target['token'], kwargs['event_id'])
        self.accepted.setdefault(key, {'target': dict(target), **kwargs})
        if self.lose_receipt_once:
            self.lose_receipt_once = False
            raise TimeoutError('Acceptance receipt was lost')
        return {'ok': True, 'accepted': True, 'admission_id': 'admission-'+kwargs['event_id']}

    def get_config(self, key, default=None):
        return default

    def spawn_task(self, coroutine, **kwargs):
        self.task_options.append(kwargs)
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_platform_handler(self, name, handler):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def on_unload(self, *args):
        pass


class LocalDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store, self.ctx = jobs.Store(self.root), Context()
        self.restored = []
        self.service = types.SimpleNamespace(recover_observers=lambda scope: self.restored.append(scope))
        self.sender = local.LocalDelivery(self.ctx, self.root, store=self.store, service=self.service)

    def event(self, *, scope=None, kind='completed', image=True):
        scope = scope or origin()
        task_id = str(uuid.uuid4())
        self.store.insert_job({'task_id': task_id, 'state': 'completed', 'origin': scope,
                               'created_at': jobs.now(), 'request': {'preset': 'fast', 'seed': 0}})
        payload = {'message': '几何机器人已完成'}
        if image:
            path = self.root/'tasks'/task_id/'1.png'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'synthetic image fixture')
            payload['artifacts'] = [{'image_id': 'image-'+task_id, 'origin': scope, 'path': str(path),
                                     'title': '几何机器人', 'width': 64, 'height': 64, 'preset': 'fast'}]
        return self.store.enqueue(task_id, kind, payload)

    def state(self, event):
        with self.store.connection() as db:
            return db.execute('SELECT state FROM outbox WHERE event_id=?', (event['event_id'],)).fetchone()[0]

    async def test_completion_uses_frozen_original_target_and_is_not_sent_twice(self):
        event = self.event()
        self.ctx.capture = {'token': 'another-current-window', 'conversation_id': 'conversation-b'}
        await self.sender.run_once()
        await self.sender.run_once()
        self.assertEqual(self.state(event), 'delivered')
        self.assertEqual(len(self.ctx.calls), 1)
        target, submitted = self.ctx.calls[0]
        self.assertEqual(target, origin()['delivery_target'])
        self.assertEqual(submitted['event_id'], event['event_id'])
        self.assertEqual(len(submitted['artifacts']), 1)

    async def test_lost_acceptance_receipt_retries_only_same_idempotent_delivery(self):
        event = self.event()
        self.ctx.lose_receipt_once = True
        await self.sender.run_once()
        self.assertEqual(self.state(event), 'pending')
        Path(event['payload']['artifacts'][0]['path']).unlink()
        replacement = local.LocalDelivery(self.ctx, self.root, store=jobs.Store(self.root), service=self.service)
        await replacement.run_once()
        self.assertEqual(self.state(event), 'delivered')
        self.assertEqual(len(self.ctx.calls), 2)
        self.assertEqual(len(self.ctx.accepted), 1)
        self.assertEqual(self.ctx.calls[0], self.ctx.calls[1])

    async def test_temporarily_unavailable_target_stays_pending_and_never_moves_windows(self):
        event = self.event()
        self.ctx.reply = {'ok': False, 'retryable': True, 'error': 'gateway_unavailable'}
        await self.sender.run_once()
        self.assertEqual(self.state(event), 'pending')
        self.ctx.reply = None
        await self.sender.run_once()
        self.assertEqual(self.state(event), 'delivered')
        self.assertTrue(all(call[0] == origin()['delivery_target'] for call in self.ctx.calls))

    async def test_deleted_conversation_cancels_delivery_and_later_events(self):
        event = self.event()
        self.ctx.reply = {'ok': False, 'retryable': False, 'cancelled': True, 'error': 'target_deleted'}
        await self.sender.run_once()
        self.assertEqual(self.state(event), 'cancelled')
        self.assertEqual(self.store.get_job(event['task_id'])['delivery_state'], 'cancelled')
        later = self.store.enqueue(event['task_id'], 'progress', {'message': '已完成'}, revision='later')
        await self.sender.run_once()
        self.assertEqual(self.state(later), 'cancelled')
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(self.ctx.accepted, {})

    async def test_ok_without_durable_acceptance_never_acknowledges_delivery(self):
        event = self.event()
        self.ctx.reply = {'ok': True, 'delivered': False}
        await self.sender.run_once()
        self.assertNotEqual(self.state(event), 'delivered')

    async def test_other_profile_bot_and_telegram_events_are_not_claimed(self):
        events = [self.event(scope=origin(profile='other')),
                  self.event(scope=origin(bot='other')),
                  self.event(scope=origin(platform='telegram'))]
        await self.sender.run_once()
        self.assertEqual([self.state(event) for event in events], ['pending']*3)
        self.assertEqual(self.ctx.calls, [])

    async def test_targetless_legacy_events_do_not_block_later_bound_delivery(self):
        for unused in range(101):
            self.event(scope={k: v for k, v in origin().items() if k != 'delivery_target'}, image=False)
        wanted = self.event()
        await self.sender.run_once()
        await self.sender.run_once()
        self.assertEqual(self.state(wanted), 'delivered')
        self.assertEqual(len(self.ctx.accepted), 1)

    async def test_foreign_output_is_not_handed_to_core(self):
        event = self.event()
        payload = {**event['payload'], 'artifacts': [{**event['payload']['artifacts'][0],
                                                   'origin': origin(conversation_id='another')} ]}
        with self.store.connection() as db:
            db.execute('UPDATE outbox SET body=? WHERE event_id=?',
                       (json.dumps({**event, 'payload': payload}), event['event_id']))
        await self.sender.run_once()
        self.assertEqual(self.state(event), 'delivery_failed')
        self.assertEqual(self.ctx.calls, [])

    async def test_local_consumer_starts_without_a_telegram_factory(self):
        delivery = import_module(PACKAGE+'.delivery')
        sender = delivery.install(self.ctx, self.root, store=self.store, service=self.service)
        try:
            self.assertIsInstance(sender.local, local.LocalDelivery)
            self.assertEqual(len(self.ctx.tasks), 1)
            self.assertTrue(self.ctx.task_options[0]['on_gateway_loop'])
            self.assertIn('pre_llm_call', self.ctx.hooks)
        finally:
            for task in self.ctx.tasks:
                task.cancel()
            await asyncio.gather(*self.ctx.tasks, return_exceptions=True)

    async def test_creation_captures_core_target_before_launching_worker(self):
        caller = {k: v for k, v in origin().items() if k != 'delivery_target'}
        service = jobs.JobService(self.ctx, self.root)
        with patch(PACKAGE+'.worker.launch'):
            result = service.create({'prompt': 'a geometric robot'}, caller)
        saved = self.store.get_job(result['task_id'])
        self.assertEqual(saved['origin']['delivery_target'], self.ctx.capture)

    async def test_missing_core_api_fails_before_task_or_worker_creation(self):
        ctx = types.SimpleNamespace(get_config=lambda key, default=None: default)
        service = jobs.JobService(ctx, self.root)
        with patch(PACKAGE+'.worker.launch') as launch:
            with self.assertRaisesRegex(ValueError, 'updated Hermes'):
                service.create({'prompt': 'a geometric robot'}, origin())
        launch.assert_not_called()
        self.assertEqual(self.store.jobs(), [])


if __name__ == '__main__':
    unittest.main()
