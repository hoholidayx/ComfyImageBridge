"""Durable desktop/CLI completions through Hermes' profile-owned delivery API."""
import asyncio
import logging
from pathlib import Path

from .scope import LOCAL_PLATFORMS, same_scope


LOG = logging.getLogger(__name__)


def prepare_origin(ctx, origin):
    """Freeze a gateway-issued destination before starting any generation work."""
    if origin.get('platform') not in LOCAL_PLATFORMS:
        return origin
    if not origin.get('session_key') or not origin.get('conversation_id'):
        raise ValueError('Background image delivery requires a live, bound Hermes conversation')
    capture = getattr(ctx, 'capture_delivery_target', None)
    if not callable(capture) or not callable(getattr(ctx, 'apublish_completion', None)):
        raise ValueError('Desktop image delivery requires the updated Hermes completion API; '
                         'upgrade the core before submitting a background image task')
    result = capture()
    target = result.get('target') if isinstance(result, dict) and result.get('ok') else None
    if not isinstance(target, dict) or target.get('conversation_id') != origin['conversation_id']:
        raise ValueError('The gateway could not verify this conversation for image delivery')
    return {**origin, 'delivery_target': target}


def _text(value, limit=1800):
    return ' '.join(str(value or '').split())[:limit]


class LocalDelivery:
    def __init__(self, ctx, data_dir, *, store, service):
        self.ctx, self.data_dir, self.store, self.service = ctx, Path(data_dir), store, service
        self.profile = str(getattr(ctx, 'profile_name', None) or 'default')
        self.task = None
        self.recovered = False

    async def _store(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    def filters(self):
        return ({'profile': self.profile, 'platform': platform, 'bot': 'local'}
                for platform in sorted(LOCAL_PLATFORMS))

    async def _payload(self, event):
        frozen = event.get('completion_payload')
        if isinstance(frozen, dict):
            return frozen
        payload = event.get('payload', {})
        text = _text(payload.get('text') or payload.get('message') or 'Comfy 任务状态已更新。')
        artifacts = []
        for image in payload.get('artifacts', []) if event['kind'] in ('completed', 'resend') else []:
            path = Path(image.get('path') or '')
            if (image.get('expired') or not path.is_file()
                    or not path.resolve().is_relative_to(self.data_dir.resolve())):
                raise ValueError('Output is expired or unavailable; generation is not retried')
            if not same_scope(image.get('origin', {}), event['origin']):
                raise ValueError('Output belongs to a different conversation')
            artifacts.append({'path': str(path.resolve()), 'mime_type': 'image/png', 'name': path.name})
            text += '\n'+_text(f"{image.get('title')} | {image['image_id']} | "
                               f"{image.get('width')}×{image.get('height')} | {image.get('preset')}", 300)
        for button in payload.get('buttons', [])[:4]:
            action = button.get('action')
            if action in ('status', 'cancel', 'retry', 'resume', 'resend'):
                identifier = button.get('image_id') if action == 'resend' else event['task_id']
                text += '\n'+_text(f"{button.get('label', action)}：/comfy {action} {identifier}", 250)
        frozen = {'text': text, 'artifacts': artifacts}
        # Reconcile an ambiguous receipt with the exact original request, even
        # after a local output is removed: the host owns accepted cached media.
        await self._store('mark_outbox', event['event_id'], 'sending', completion_payload=frozen)
        return frozen

    async def _send(self, event):
        origin = event['origin']
        job = await self._store('get_job', event['task_id'])
        if job and job.get('delivery_state') == 'cancelled':
            await self._store('mark_outbox', event['event_id'], 'cancelled',
                              error='The bound conversation was deleted; delivery is cancelled')
            return
        target = origin.get('delivery_target')
        if not isinstance(target, dict):
            await self._store('mark_outbox', event['event_id'], 'delivery_failed',
                              error='No verified persistent delivery target; inspect this task manually')
            return
        frozen = await self._payload(event)
        result = await self.ctx.apublish_completion(target, event_id=event['event_id'], **frozen)
        if isinstance(result, dict) and result.get('ok') and result.get('accepted'):
            await self._store('ack_outbox', event['event_id'],
                              message_id=result.get('admission_id') or result.get('message_id'))
            await self._store('update_job', event['task_id'], delivery_state='accepted', delivery_error=None)
            return
        if isinstance(result, dict) and (result.get('cancelled') or result.get('error') == 'target_deleted'):
            await self._store('update_job', event['task_id'], delivery_state='cancelled',
                              delivery_error='The bound conversation was deleted; delivery is cancelled')
            await self._store('mark_outbox', event['event_id'], 'cancelled',
                              error='The bound conversation was deleted; delivery is cancelled')
            return
        if isinstance(result, dict) and result.get('retryable'):
            await self._store('mark_outbox', event['event_id'], 'pending',
                              error=_text(result.get('error') or 'Desktop delivery is temporarily unavailable'))
            await self._store('update_job', event['task_id'], delivery_state='pending',
                              delivery_error=_text(result.get('error')))
            return
        await self._store('mark_outbox', event['event_id'], 'delivery_failed',
                          error=_text(result.get('error') if isinstance(result, dict) else 'Invalid completion receipt'))
        await self._store('update_job', event['task_id'], delivery_state='failed',
                          delivery_error='The framework refused this completion')

    async def run_once(self):
        for scope in self.filters():
            events = await self._store('pending_outbox', origin=scope, limit=100)
            latest = {event['task_id']: event['event_id'] for event in events if event['kind'] == 'progress'}
            for event in events:
                if not await self._store('claim_outbox', event['event_id']):
                    continue
                if event['kind'] == 'progress' and latest[event['task_id']] != event['event_id']:
                    await self._store('mark_outbox', event['event_id'], 'superseded')
                    continue
                try:
                    await self._send(event)
                except asyncio.CancelledError:
                    # Host admission is idempotent for this target/event identity,
                    # including when acceptance happened just before cancellation.
                    await self._store('mark_outbox', event['event_id'], 'pending',
                                      error='Desktop delivery interrupted; the same event will be reconciled')
                    raise
                except (ValueError, PermissionError) as exc:
                    await self._store('mark_outbox', event['event_id'], 'delivery_failed', error=_text(exc))
                except Exception:
                    await self._store('mark_outbox', event['event_id'], 'pending',
                                      error='Completion receipt unavailable; retrying the same event identity')

    async def pump(self):
        while True:
            try:
                if not self.recovered:
                    for scope in self.filters():
                        await self._store('recover_outbox', origin=scope, replay_safe=True)
                        await asyncio.to_thread(self.service.recover_observers, scope)
                    self.recovered = True
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('Comfy desktop completion pump failed; generation is not resubmitted')
            await asyncio.sleep(1)

    def start(self):
        if self.task is not None and not self.task.done():
            return
        coroutine = self.pump()
        try:
            self.task = self.ctx.spawn_task(coroutine, name='comfy-workflows:local-outbox:'+self.profile,
                                           on_gateway_loop=True)
        except RuntimeError:
            # A standalone CLI loads registrations without an event loop. Its
            # query/legacy tools remain available; a later async hook can start
            # the consumer once a gateway session is actually running.
            coroutine.close()
            return False
        return True

    async def on_session(self, **kwargs):
        self.start()
