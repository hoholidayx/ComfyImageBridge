"""Native Comfy notifications and callbacks; this module never invokes an LLM."""
import asyncio
import json
import logging
import os
import shlex
import weakref
from pathlib import Path

from .scope import LOCAL_PLATFORMS, same_scope

LOG = logging.getLogger(__name__)
_BINDINGS = []
_ACTIONS = frozenset(('status', 'cancel', 'retry', 'resend', 'resume', 'images'))


def _text(value):
    return '' if value is None else str(getattr(value, 'value', value))


def _bot_id(adapter=None, native=None):
    for obj in (getattr(adapter, '_bot', None), getattr(native, 'bot', None)):
        if obj is not None:
            try:
                value = _text(getattr(obj, 'id', None))
            except RuntimeError:  # PTB exposes id only after initialize().
                continue
            if value:
                return value
    return ''


def _profile(ctx):
    return _text(getattr(ctx, 'profile_name', None)) or 'default'


def current_origin(ctx, *, source=None, session_key='', session_id='', platform='', adapter=None):
    """Capture only the calling route, never a gateway-wide last-chat environment."""
    bound_key = ''
    read = lambda name, default='': default
    try:
        from gateway.session_context import bound_session_key, get_session_env
        bound_key = bound_session_key()
        if bound_key:
            read = get_session_env
    except ImportError:  # Standalone CLI has no gateway route.
        pass
    if source is None and bound_key:
        try:
            from gateway.plugin_messages import bound_route
            source = bound_route()
        except ImportError:
            pass
    if source is not None and getattr(source, 'profile_route_rejected', False):
        raise ValueError('Calling route was rejected by the gateway')
    surface = _text(platform or getattr(source, 'platform', None) or read('HERMES_SESSION_PLATFORM'))
    if not surface:
        surface = _text(ctx.session_surface()) if hasattr(ctx, 'session_surface') else 'cli'
    key = session_key or bound_key
    origin = {
        'profile': _text(getattr(source, 'profile', None) or read('HERMES_SESSION_PROFILE')) or _profile(ctx),
        'platform': surface,
        'session_key': key,
        'session_id': session_id or read('HERMES_SESSION_ID'),
    }
    for name, env in (('chat_id', 'CHAT_ID'), ('thread_id', 'THREAD_ID'), ('user_id', 'USER_ID')):
        origin[name] = _text(getattr(source, name, None) if source is not None else read('HERMES_SESSION_' + env))
    if surface in LOCAL_PLATFORMS and not source and not bound_key and not session_key:
        origin.update(bot='local', chat_id='local', thread_id='', user_id='local')
        return origin
    if not origin['chat_id'] or not origin['user_id'] or not (source is not None or bound_key):
        raise ValueError('No trusted calling chat scope; notification destination is not guessed')
    if surface in LOCAL_PLATFORMS:
        if not origin['session_key'] or not origin['session_id']:
            raise ValueError('No trusted local session identity; conversation destination is not guessed')
        source_platform = _text(getattr(source, 'platform', None)) if source is not None else ''
        if source_platform and source_platform not in LOCAL_PLATFORMS:
            raise ValueError('Calling platform does not match the trusted route')
        # Local sessions have no bot. Preserve the gateway's complete conversation
        # identity; a shared local/local fallback would merge independent chats.
        origin['bot'] = 'local'
        # The local source names the logical conversation; SESSION_ID may rotate
        # to a new physical history tip when the same conversation is compressed.
        origin['conversation_id'] = origin['chat_id']
        return origin
    source_adapter = getattr(source, '_transport_adapter_ref', None) if source is not None else None
    if isinstance(source_adapter, weakref.ReferenceType):
        source_adapter = source_adapter()
        if source_adapter is None and adapter is None:
            raise ValueError('Calling bot transport is no longer available')
    if adapter is None and source_adapter is not None:
        adapter = source_adapter
    origin['bot'] = _bot_id(adapter)
    if not origin['bot']:
        candidates = {binding.bot_id for binding in _BINDINGS
                      if binding.profile == origin['profile'] and surface == 'telegram' and binding.bot_id}
        if len(candidates) == 1:
            origin['bot'] = candidates.pop()
    if not origin['bot']:
        raise ValueError('No verified bot binding for this chat; refusing cross-bot delivery')
    return origin


def capture_origin(ctx):
    return current_origin(ctx)


def _metadata(origin):
    return {'thread_id': origin.get('thread_id') or None, '_interim_send': True}


def _clean(value, limit=500):
    return ' '.join(_text(value).split())[:limit]


def _status_text(value):
    if not isinstance(value, dict):
        return _clean(value)
    if value.get('success') is False:
        return 'Comfy：' + _clean(value.get('error', value.get('message', '操作失败')))
    job = value.get('job', value)
    fields = {key: job[key] for key in ('task_id', 'title', 'state', 'stage', 'operation_id', 'error') if key in job}
    return json.dumps(fields or {'success': True}, ensure_ascii=False)


class Delivery:
    def __init__(self, ctx, data_dir, *, service=None, store=None):
        if service is None or store is None:
            from .jobs import JobService, Store
            store = store or Store(data_dir)
            service = service or JobService(ctx, data_dir)
        self.ctx, self.data_dir, self.store, self.service = ctx, Path(data_dir), store, service
        self.profile = _profile(ctx)
        self.adapter = self.native = None
        self.task = None

    @property
    def bot_id(self):
        return _bot_id(self.adapter, self.native)

    def owns(self, origin):
        return (origin.get('platform') == 'telegram' and origin.get('profile') == self.profile
                and bool(self.bot_id) and _text(origin.get('bot')) == self.bot_id)

    async def _store(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    async def _action(self, action, target, origin):
        if action == 'images':
            values = await asyncio.to_thread(self.store.artifacts, origin=origin, query=target, limit=10)
            return '\n'.join(f"{item['image_id']} | {_clean(item.get('title'), 120)}"
                             + (' | 已过期，不可作参考' if item.get('expired') else '') for item in values) or '暂无图片记录。'
        value = await asyncio.to_thread(getattr(self.service, action), target, origin=origin)
        return _status_text(value)

    async def command(self, raw_args, *, source=None, session_key='', session_id='', platform='', **kwargs):
        try:
            origin = current_origin(self.ctx, source=source, session_key=session_key, session_id=session_id,
                                    platform=platform)
            action, _, remainder = raw_args.strip().partition(' ')
            if action.lower() == 'run':
                # Preserve JSON quoting exactly; no shell, eval or model rewrite is involved.
                request = json.loads(remainder)
                if not isinstance(request, dict):
                    raise ValueError('run requires a JSON object')
                value = await asyncio.to_thread(self.service.create, request, origin)
                return _status_text(value)
            args = shlex.split(raw_args)
            if not args:
                return ('/comfy status|cancel|retry|resume <task_id>；/comfy resend <image_id>；'
                        '/comfy images [关键词]；/comfy run <JSON>')
            action = args[0].lower()
            if action not in _ACTIONS:
                raise ValueError('Unsupported action; use status, cancel, retry, resume, resend, images or run')
            target = ' '.join(args[1:])
            if action == 'status' and not target:
                values = await asyncio.to_thread(self.service.list, origin=origin, limit=10)
                if isinstance(values, dict):
                    values = values.get('jobs', values.get('tasks', []))
                return '\n'.join(_status_text(value) for value in values) or '暂无任务。'
            if action != 'images' and not target:
                raise ValueError('This action requires an exact task_id or image_id')
            return await self._action(action, target, origin)
        except Exception as exc:
            return 'Comfy：' + _clean(exc)

    async def callback(self, update, context):
        """Consume one scoped, expiring token; never interpret button text as commands."""
        query = update.callback_query
        data = _text(query.data)
        if not data.startswith('comfy:'):
            return
        try:
            message, user = query.message, query.from_user
            if message is None or user is None:
                raise ValueError('Button has no trusted chat/user')
            source = self.adapter.build_source(
                chat_id=str(message.chat.id), user_id=str(user.id),
                thread_id=_text(getattr(message, 'message_thread_id', None)) or None,
                chat_type='dm' if getattr(message.chat, 'type', '') == 'private' else 'group')
            origin = current_origin(self.ctx, source=source, platform='telegram', adapter=self.adapter)
            record = await self._store('consume_action', data[6:], origin)
            if not record:
                raise ValueError('Button expired, already consumed, or belongs to another scope')
            action = record.get('action')
            if action not in _ACTIONS:
                raise ValueError('Unsupported button action')
            await query.answer()
            if hasattr(query, 'edit_message_reply_markup'):
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    LOG.info('Comfy button markup could not be cleared')
            target = record.get('image_id') if action == 'resend' else record.get('task_id', '')
            text = await self._action(action, target, origin)
            # This is a native callback response, not a new model turn.
            await self.adapter.send(origin['chat_id'], text, metadata=_metadata(origin))
        except Exception as exc:
            try:
                await query.answer(_clean(exc, 180), show_alert=True)
            except Exception:
                LOG.info('Comfy callback acknowledgement failed')
        from telegram.ext import ApplicationHandlerStop
        raise ApplicationHandlerStop

    async def _buttons(self, event, message_id):
        buttons = event.get('payload', {}).get('buttons', [])
        if not buttons or not message_id:
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = []
        for button in buttons[:4]:
            if button.get('action') not in _ACTIONS:
                continue
            token = await self._store('create_action', event['task_id'], button['action'],
                                      origin=event['origin'], image_id=button.get('image_id'))
            callback_data = 'comfy:' + token
            if len(callback_data.encode()) > 64:
                raise ValueError('Action token exceeds Telegram callback limit')
            keyboard.append([InlineKeyboardButton(_clean(button.get('label', button['action']), 40),
                                                  callback_data=callback_data)])
        if keyboard:
            await self.native.bot.edit_message_reply_markup(
                chat_id=event['origin']['chat_id'], message_id=message_id,
                reply_markup=InlineKeyboardMarkup(keyboard))

    async def _send(self, event):
        origin, payload = event['origin'], event.get('payload', {})
        text = _clean(payload.get('text') or payload.get('message') or 'Comfy 任务状态已更新。', 1800)
        job = await self._store('get_job', event['task_id'])
        message_id = job.get('notification_message_id') if job else None
        images = payload.get('artifacts', []) if event['kind'] in ('completed', 'resend') else []
        delivered = list(event.get('delivered_outputs', []))
        if not images:
            result = (await self.adapter.edit_message(origin['chat_id'], message_id, text, finalize=True,
                                                       metadata=_metadata(origin)) if message_id
                      else await self.adapter.send(origin['chat_id'], text, metadata=_metadata(origin)))
            if not result.success:
                raise RuntimeError(_clean(getattr(result, 'error', 'delivery failed')))
            message_id = _text(result.message_id) or message_id
            await self._store('update_job', event['task_id'], notification_message_id=message_id)
        else:
            if message_id:
                try:
                    await self.adapter.edit_message(origin['chat_id'], message_id, text, finalize=True,
                                                    metadata=_metadata(origin))
                except Exception:
                    LOG.info('Comfy completion status could not be edited; image delivery continues')
            for image in images:
                if image.get('expired') or not image.get('path') or not Path(image['path']).is_file():
                    raise ValueError('Output is expired or unavailable; generation is not retried')
                if not same_scope(image.get('origin', {}), origin):
                    raise ValueError('Output belongs to a different chat scope')
                if not Path(image['path']).resolve().is_relative_to(self.data_dir.resolve()):
                    raise ValueError('Output path is outside this plugin data directory')
                if image['image_id'] in delivered:
                    continue
                caption = f"{_clean(image.get('title'), 120)}\n{image['image_id']} | {image.get('width')}×{image.get('height')} | {image.get('preset')}"
                result = await self.adapter.send_image_file(origin['chat_id'], image['path'], caption=caption,
                                                            metadata=_metadata(origin))
                if not result.success:
                    raise RuntimeError(_clean(getattr(result, 'error', 'image delivery failed')))
                message_id = _text(result.message_id)
                delivered.append(image['image_id'])
                await self._store('mark_outbox', event['event_id'], 'sending',
                                  delivered_outputs=delivered, message_id=message_id)
        await self._store('ack_outbox', event['event_id'], message_id=message_id)
        try:
            await self._buttons(event, message_id)
        except Exception as exc:
            # A failed markup update must never resend an already delivered photo/text.
            await self._store('mark_outbox', event['event_id'], 'delivered', button_error=_clean(exc))

    async def run_once(self):
        events = await self._store('pending_outbox', limit=100,
                                   origin={'profile': self.profile, 'platform': 'telegram', 'bot': self.bot_id})
        events = [event for event in events if self.owns(event.get('origin', {}))]
        latest = {}
        for event in events:
            if event['kind'] == 'progress':
                latest[event['task_id']] = event['event_id']
        for event in events:
            if not await self._store('claim_outbox', event['event_id']):
                continue
            if event['kind'] == 'progress' and latest[event['task_id']] != event['event_id']:
                await self._store('mark_outbox', event['event_id'], 'superseded')
                continue
            try:
                await self._send(event)
            except asyncio.CancelledError:
                await self._store('mark_outbox', event['event_id'], 'delivery_uncertain',
                                  error='Gateway delivery was interrupted; use explicit resend')
                raise
            except Exception as exc:
                await self._store('mark_outbox', event['event_id'], 'delivery_uncertain', error=_clean(exc))

    async def pump(self):
        recovered = False
        while True:
            try:
                if self.bot_id:
                    if not recovered:
                        origin_filter = {'profile': self.profile, 'platform': 'telegram', 'bot': self.bot_id}
                        await self._store('recover_outbox', origin=origin_filter)
                        restore = getattr(self.service, 'recover_observers', None)
                        if callable(restore):
                            # Only an existing known remote task can be re-observed. The service
                            # owns the once-only claim and explicit-stop guard; no submit here.
                            await asyncio.to_thread(restore, origin_filter)
                        from .jobs import atomic_json, now
                        await asyncio.to_thread(atomic_json, self.data_dir / 'delivery-binding.json', {
                            **origin_filter, 'gateway_pid': os.getpid(), 'observed_at': now()})
                        recovered = True
                    await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('Comfy outbox pump failed; no generation was resubmitted')
            await asyncio.sleep(1)

    def factory(self, native, adapter):
        self.native, self.adapter = native, adapter
        from telegram.ext import CallbackQueryHandler
        native.add_handler(CallbackQueryHandler(self.callback, pattern=r'^comfy:'))
        if self not in _BINDINGS:
            _BINDINGS.append(self)
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = self.ctx.spawn_task(self.pump(), name='comfy-workflows:outbox:' + self.profile)


def install(ctx, data_dir, *, service=None, store=None):
    delivery = Delivery(ctx, data_dir, service=service, store=store)
    if callable(getattr(ctx, 'apublish_completion', None)):
        from .local_delivery import LocalDelivery
        delivery.local = LocalDelivery(ctx, data_dir, service=delivery.service, store=delivery.store)
        delivery.local.start()
        ctx.register_hook('pre_llm_call', delivery.local.on_session)
    ctx.register_platform_handler('telegram', delivery.factory)
    ctx.register_command('comfy', delivery.command, description='Comfy 任务、取消、显式重试和图片索引',
                         args_hint='status|cancel|retry|resume|resend|images|run [ID/关键词/JSON]')
    if hasattr(ctx, 'on_unload'):
        ctx.on_unload(lambda: _BINDINGS.remove(delivery) if delivery in _BINDINGS else None)
    return delivery
