import asyncio
import importlib.util
import sys
import tempfile
import time
import types
import unittest
import uuid
import weakref
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'tested_comfy_delivery'
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.delivery', ROOT / 'delivery.py')
delivery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = delivery
spec.loader.exec_module(delivery)


def origin(**overrides):
    return {'profile': 'default', 'platform': 'telegram', 'bot': '11', 'chat_id': '100',
            'thread_id': '2', 'user_id': '7', 'session_key': 'trusted-key', 'session_id': 's1', **overrides}


class FakeStore:
    def __init__(self):
        self.events = []
        self.values = {}
        self.images = []
        self.actions = {}

    def pending_outbox(self, limit=50, origin=None):
        return [dict(value) for value in self.events if value['state'] == 'pending'
                and all(value['origin'].get(k) == v for k, v in (origin or {}).items())][:limit]

    def claim_outbox(self, event_id):
        event = next(value for value in self.events if value['event_id'] == event_id)
        if event['state'] != 'pending':
            return False
        event['state'] = 'sending'
        return True

    def mark_outbox(self, event_id, state, **fields):
        event = next(value for value in self.events if value['event_id'] == event_id)
        event.update(state=state, **fields)
        return event

    def ack_outbox(self, event_id, message_id=None):
        return self.mark_outbox(event_id, 'delivered', message_id=message_id)

    def recover_outbox(self, origin=None):
        count = 0
        for event in self.events:
            if event['state'] == 'sending' and all(event['origin'].get(k) == v for k, v in (origin or {}).items()):
                event['state'] = 'delivery_uncertain'
                count += 1
        return count

    def get_job(self, task_id):
        return self.values.get(task_id, {})

    def update_job(self, task_id, **fields):
        self.values.setdefault(task_id, {}).update(**fields)

    def artifacts(self, origin=None, query='', limit=20):
        return [value for value in self.images if delivery.same_scope(value['origin'], origin)
                and (not query or query in value.get('title', '') or query == value['image_id'])][:limit]

    def create_action(self, task_id, action, origin=None, image_id=None):
        token = str(len(self.actions) + 1)
        self.actions[token] = {'task_id': task_id, 'action': action, 'origin': origin,
                               'image_id': image_id, 'expires_at': time.time() + 86400, 'consumed': False}
        return token

    def consume_action(self, token, origin):
        value = self.actions.get(token)
        if (not value or value['consumed'] or value['expires_at'] < time.time()
                or not delivery.same_scope(value['origin'], origin)):
            return None
        value['consumed'] = True
        return value


class FakeService:
    def __init__(self):
        self.calls = []

    def status(self, task_id, origin=None):
        self.calls.append(('status', task_id, origin))
        return {'task_id': task_id, 'state': 'running'}

    def cancel(self, task_id, origin=None):
        self.calls.append(('cancel', task_id, origin))
        return {'task_id': task_id, 'state': 'cancel_requested'}

    def retry(self, task_id, origin=None):
        self.calls.append(('retry', task_id, origin))
        if task_id == 'uncertain':
            raise ValueError('submission_uncertain cannot be retried')
        return {'task_id': 'explicit-retry', 'state': 'pending'}

    def resend(self, image_id, origin=None):
        self.calls.append(('resend', image_id, origin))
        return {'task_id': 'old-task', 'state': 'completed'}

    def list(self, origin=None, limit=20):
        return [{'task_id': 'known-task', 'state': 'completed'}]

    def create(self, args, origin):
        self.calls.append(('create', args, origin))
        return {'task_id': 'new-task', 'state': 'accepted'}

    def resume(self, task_id, origin=None):
        self.calls.append(('resume', task_id, origin))
        return {'task_id': task_id, 'state': 'observing'}


class FakeContext:
    profile_name = 'default'

    def __init__(self):
        self.factories, self.commands, self.tasks = {}, {}, []

    def register_platform_handler(self, name, factory):
        self.factories[name] = factory

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def on_unload(self, callback):
        pass

    def spawn_task(self, coro, **kwargs):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def session_surface(self):
        return 'telegram'


class FakeAdapter:
    def __init__(self):
        self._bot = types.SimpleNamespace(id=11)
        self.sent = []
        self.fail_image = False

    async def send(self, chat_id, content, **kwargs):
        self.sent.append(('send', chat_id, content, kwargs))
        return types.SimpleNamespace(success=True, message_id=str(len(self.sent)))

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.sent.append(('edit', chat_id, content, kwargs))
        return types.SimpleNamespace(success=True, message_id=message_id)

    async def send_image_file(self, chat_id, image_path, **kwargs):
        self.sent.append(('image', chat_id, image_path, kwargs))
        if self.fail_image:
            raise TimeoutError('response lost after upload')
        return types.SimpleNamespace(success=True, message_id=str(len(self.sent)))

    def build_source(self, **kwargs):
        return types.SimpleNamespace(platform='telegram', profile='default', **kwargs)


class HandlerStop(Exception):
    pass


class Query:
    def __init__(self, token, *, user_id=7, chat_id=100, thread_id=2):
        self.data = 'comfy:' + token
        self.from_user = types.SimpleNamespace(id=user_id)
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id, type='private'),
                                             message_thread_id=thread_id)
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_reply_markup(self, **kwargs):
        pass


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store, self.service, self.ctx, self.adapter = FakeStore(), FakeService(), FakeContext(), FakeAdapter()
        self.sender = delivery.Delivery(self.ctx, self.temp.name, store=self.store, service=self.service)
        self.sender.adapter = self.adapter
        self.sender.native = types.SimpleNamespace(bot=self.adapter._bot)
        self.sdk = types.ModuleType('telegram.ext')
        self.sdk.ApplicationHandlerStop = HandlerStop
        self.sdk.CallbackQueryHandler = lambda handler, pattern: types.SimpleNamespace(callback=handler, pattern=pattern)
        self.patcher = patch.dict(sys.modules, {'telegram.ext': self.sdk})
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()
        delivery._BINDINGS.clear()

    def event(self, event_id, kind='progress', scope=None, **payload):
        value = {'event_id': event_id, 'task_id': 'task', 'kind': kind, 'revision': event_id,
                 'state': 'pending', 'origin': scope or origin(), 'payload': payload}
        self.store.events.append(value)
        return value

    def image(self, image_id='image-1', **overrides):
        path = Path(self.temp.name) / (image_id + '.png')
        path.write_bytes(b'fake output; no image interpretation')
        return {'image_id': image_id, 'task_id': 'task', 'title': '几何机器人', 'path': str(path),
                'width': 512, 'height': 512, 'preset': 'fast', 'origin': origin(), **overrides}

    async def test_progress_coalesces_and_updates_one_native_message(self):
        old = self.event('1', text='旧进度')
        latest = self.event('2', text='新进度')
        await self.sender.run_once()
        self.assertEqual(old['state'], 'superseded')
        self.assertEqual(latest['state'], 'delivered')
        self.event('3', text='已完成')
        await self.sender.run_once()
        self.assertEqual([value[0] for value in self.adapter.sent], ['send', 'edit'])
        self.assertEqual(self.adapter.sent[0][-1]['metadata']['thread_id'], '2')
        self.assertEqual(self.service.calls, [])

    async def test_mismatched_bot_or_profile_never_claims_or_sends(self):
        a = self.event('1', scope=origin(bot='another'))
        b = self.event('2', scope=origin(profile='another'))
        await self.sender.run_once()
        self.assertEqual([a['state'], b['state']], ['pending', 'pending'])
        self.assertEqual(self.adapter.sent, [])

    async def test_completion_uploads_once_and_never_resubmits_generation(self):
        event = self.event('1', kind='completed', artifacts=[self.image()])
        await self.sender.run_once()
        await self.sender.run_once()
        self.assertEqual(event['state'], 'delivered')
        self.assertEqual(event['delivered_outputs'], ['image-1'])
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.service.calls, [])

    async def test_ambiguous_upload_is_not_retried(self):
        self.adapter.fail_image = True
        event = self.event('1', kind='completed', artifacts=[self.image()])
        await self.sender.run_once()
        await self.sender.run_once()
        self.assertEqual(event['state'], 'delivery_uncertain')
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.service.calls, [])

    async def test_cross_scope_artifact_is_not_uploaded(self):
        event = self.event('1', kind='completed', artifacts=[self.image(origin=origin(user_id='8'))])
        await self.sender.run_once()
        self.assertEqual(event['state'], 'delivery_uncertain')
        self.assertEqual(self.adapter.sent, [])

    async def test_callback_is_single_consume_and_checks_user_chat_and_topic(self):
        token = self.store.create_action('task', 'cancel', origin=origin())
        for mismatch in ({'user_id': 8}, {'chat_id': 101}, {'thread_id': 3}):
            with self.assertRaises(HandlerStop):
                await self.sender.callback(types.SimpleNamespace(callback_query=Query(token, **mismatch)), None)
        self.assertEqual(self.service.calls, [])
        with self.assertRaises(HandlerStop):
            await self.sender.callback(types.SimpleNamespace(callback_query=Query(token)), None)
        with self.assertRaises(HandlerStop):
            await self.sender.callback(types.SimpleNamespace(callback_query=Query(token)), None)
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.service.calls[0][0], 'cancel')

    async def test_expired_callback_does_not_run_action(self):
        token = self.store.create_action('task', 'retry', origin=origin())
        self.store.actions[token]['expires_at'] = time.time() - 1
        with self.assertRaises(HandlerStop):
            await self.sender.callback(types.SimpleNamespace(callback_query=Query(token)), None)
        self.assertEqual(self.service.calls, [])

    async def test_native_retry_and_resend_are_explicit_and_uncertain_stays_rejected(self):
        with patch.object(delivery, 'current_origin', return_value=origin()):
            text = await self.sender.command('retry uncertain')
            self.assertIn('cannot be retried', text)
            await self.sender.command('resend image-1')
        self.assertEqual([value[0] for value in self.service.calls], ['retry', 'resend'])

    async def test_run_json_uses_same_service_without_shell_or_llm(self):
        with patch.object(delivery, 'current_origin', return_value=origin()):
            text = await self.sender.command('run {"prompt":"中文 \\"quoted\\" `literal` $(literal)","size":"512x512"}')
        self.assertIn('accepted', text)
        self.assertEqual(self.service.calls[0][0], 'create')
        self.assertEqual(self.service.calls[0][1]['prompt'], '中文 "quoted" `literal` $(literal)')

    async def test_resume_only_calls_observer_resume(self):
        with patch.object(delivery, 'current_origin', return_value=origin()):
            await self.sender.command('resume task')
        self.assertEqual([value[0] for value in self.service.calls], ['resume'])

    async def test_install_registers_scoped_handler_and_supervised_task(self):
        sender = delivery.install(self.ctx, self.temp.name, store=self.store, service=self.service)
        native = types.SimpleNamespace(bot=self.adapter._bot, add_handler=lambda handler: self.assertEqual(handler.pattern, '^comfy:'))
        self.ctx.factories['telegram'](native, self.adapter)
        self.assertIn('comfy', self.ctx.commands)
        self.assertEqual(len(self.ctx.tasks), 1)
        sender.task.cancel()
        await asyncio.gather(sender.task, return_exceptions=True)

    async def test_origin_without_route_refuses_ambient_chat(self):
        with patch.dict(sys.modules, {'gateway.session_context': types.SimpleNamespace(
                bound_session_key=lambda: '', get_session_env=lambda key, default='': 'foreign-chat')}):
            with self.assertRaises(ValueError):
                delivery.capture_origin(self.ctx)

    async def test_live_receiving_bot_weakref_is_authoritative(self):
        other_adapter = FakeAdapter()
        other_adapter._bot.id = 99
        source = types.SimpleNamespace(platform='telegram', profile='default', chat_id='100', user_id='7',
                                       thread_id='2', _transport_adapter_ref=weakref.ref(other_adapter))
        self.assertEqual(delivery.current_origin(self.ctx, source=source)['bot'], '99')

    async def test_gateway_desktop_route_preserves_session_identity_without_a_bot(self):
        source = types.SimpleNamespace(platform='local', profile='default', chat_id='local-chat-a',
                                       user_id='local-user', thread_id='')
        values = {'HERMES_SESSION_ID': 'durable-a', 'HERMES_SESSION_PLATFORM': 'local'}
        with patch.dict(sys.modules, {
                'gateway.session_context': types.SimpleNamespace(
                    bound_session_key=lambda: 'local:route-a', get_session_env=lambda key, default='': values.get(key, default)),
                'gateway.plugin_messages': types.SimpleNamespace(bound_route=lambda: source)}):
            captured = delivery.capture_origin(self.ctx)
        self.assertEqual(captured, {
            'profile': 'default', 'platform': 'local', 'bot': 'local', 'chat_id': 'local-chat-a',
            'thread_id': '', 'user_id': 'local-user', 'session_key': 'local:route-a', 'session_id': 'durable-a',
            'conversation_id': 'local-chat-a'})

    async def test_local_route_never_replaces_missing_identity_with_shared_local_values(self):
        for changed in ({'chat_id': ''}, {'user_id': ''}, {'session_key': ''}, {'session_id': ''}):
            values = {'chat_id': 'local-chat-a', 'user_id': 'local-user',
                      'session_key': 'local:route-a', 'session_id': 'durable-a', **changed}
            source = types.SimpleNamespace(platform='local', profile='default',
                                           chat_id=values['chat_id'], user_id=values['user_id'])
            with self.subTest(missing=changed), self.assertRaises(ValueError):
                delivery.current_origin(self.ctx, source=source,
                                        session_key=values['session_key'], session_id=values['session_id'])

    async def test_local_route_cannot_override_gateway_rejection_or_messaging_platform(self):
        source = types.SimpleNamespace(platform='local', profile_route_rejected=True)
        with self.assertRaisesRegex(ValueError, 'rejected'):
            delivery.current_origin(self.ctx, source=source, session_key='a', session_id='a')
        source = types.SimpleNamespace(platform='telegram', profile='default', chat_id='100', user_id='7')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            delivery.current_origin(self.ctx, source=source, platform='desktop', session_key='a', session_id='a')

    async def test_missing_telegram_bot_is_still_rejected(self):
        source = types.SimpleNamespace(platform='telegram', profile='default', chat_id='100', user_id='7')
        with self.assertRaisesRegex(ValueError, 'No verified bot'):
            delivery.current_origin(self.ctx, source=source)

    def real_store(self):
        from importlib import import_module
        module = import_module(PACKAGE + '.jobs')
        store = module.Store(self.temp.name)
        task_id = str(uuid.uuid4())
        store.insert_job({'task_id': task_id, 'title': '几何机器人', 'state': 'completed',
                          'created_at': module.now(), 'origin': origin(), 'request': {
                              'title_source': 'request_intent', 'preset': 'fast', 'seed': 0}})
        return store, task_id

    async def test_real_store_restart_keeps_delivery_receipt_without_duplicate_upload(self):
        store, task_id = self.real_store()
        image = self.image()
        artifacts = store.register_outputs(task_id, [image])
        event = store.enqueue(task_id, 'completed', {'artifacts': artifacts, 'text': '已完成'})
        sender = delivery.Delivery(self.ctx, self.temp.name, store=store, service=self.service)
        sender.adapter, sender.native = self.adapter, self.sender.native
        await sender.run_once()
        from importlib import import_module
        reopened = import_module(PACKAGE + '.jobs').Store(self.temp.name)
        replacement = delivery.Delivery(self.ctx, self.temp.name, store=reopened, service=self.service)
        replacement.adapter, replacement.native = self.adapter, self.sender.native
        await replacement.run_once()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(reopened.pending_outbox(), [])
        with reopened.connection() as db:
            state = db.execute('SELECT state FROM outbox WHERE event_id=?', (event['event_id'],)).fetchone()[0]
        self.assertEqual(state, 'delivered')

    async def test_real_store_interrupted_send_requires_explicit_resend(self):
        store, task_id = self.real_store()
        event = store.enqueue(task_id, 'progress', {'text': '生成中'})
        store.claim_outbox(event['event_id'])
        self.assertEqual(store.recover_outbox(origin={'profile': 'default', 'platform': 'telegram', 'bot': '11'}), 1)
        sender = delivery.Delivery(self.ctx, self.temp.name, store=store, service=self.service)
        sender.adapter, sender.native = self.adapter, self.sender.native
        await sender.run_once()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.service.calls, [])

    async def test_failed_button_markup_does_not_resend_delivered_photo(self):
        telegram = types.ModuleType('telegram')
        telegram.InlineKeyboardButton = lambda label, callback_data: (label, callback_data)
        telegram.InlineKeyboardMarkup = lambda keyboard: keyboard

        async def fail_markup(**kwargs):
            raise TimeoutError('markup response uncertain')

        self.sender.native.bot.edit_message_reply_markup = fail_markup
        event = self.event('1', kind='completed', artifacts=[self.image()],
                           buttons=[{'label': '重发', 'action': 'resend', 'image_id': 'image-1'}])
        with patch.dict(sys.modules, {'telegram': telegram}):
            await self.sender.run_once()
            await self.sender.run_once()
        self.assertEqual(event['state'], 'delivered')
        self.assertIn('button_error', event)
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_pump_restores_known_observers_once_and_writes_only_public_binding(self):
        restored = []
        self.service.recover_observers = lambda scope: restored.append(scope)
        pending = self.event('1', text='准备中')
        self.store.claim_outbox(pending['event_id'])
        task = asyncio.create_task(self.sender.pump())
        binding_path = Path(self.temp.name) / 'delivery-binding.json'
        try:
            for unused in range(100):
                if binding_path.is_file():
                    break
                await asyncio.sleep(.002)
            self.assertTrue(binding_path.is_file())
            import json
            binding = json.loads(binding_path.read_text())
            self.assertEqual(set(binding), {'profile', 'platform', 'bot', 'gateway_pid', 'observed_at'})
            self.assertEqual(binding['bot'], '11')
            self.assertEqual(restored, [{'profile': 'default', 'platform': 'telegram', 'bot': '11'}])
            self.assertEqual(pending['state'], 'delivery_uncertain')
            self.assertEqual(self.adapter.sent, [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
