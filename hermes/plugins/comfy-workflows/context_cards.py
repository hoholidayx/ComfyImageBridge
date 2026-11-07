"""Bounded artifact intent cards, admitted on the next real user turn only."""
import re
from .delivery import capture_origin, same_scope

MARKER = re.compile(r'\[comfy-image:([A-Za-z0-9_.-]{1,100})\]')
MAX_CARDS = 4
MAX_CHARS = 1600


def _clean(value, length=160):
    return ' '.join(str(value or '').split()).replace('[comfy-image:', '[image:')[:length]


def _seen(history):
    """Only stamped user/assistant API text acknowledges a previously admitted card."""
    found = set()
    for message in history or []:
        if message.get('role') not in ('user', 'assistant'):
            continue
        content = message.get('api_content') or message.get('content', '')
        if isinstance(content, str):
            found.update(MARKER.findall(content))
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get('text'), str):
                    found.update(MARKER.findall(block['text']))
    return found


def card(image):
    image_id = str(image.get('image_id', ''))
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', image_id):
        return ''
    state = 'expired：原图已过期，不可作参考图' if image.get('expired') else 'available：可用 image_id 引用'
    refs = ','.join(_clean(value, 100) for value in image.get('reference_ids', [])[:10]) or '无'
    return (f"[comfy-image:{image_id}] 标题/生成意图：{_clean(image.get('title'))}；"
            f"{image.get('width')}×{image.get('height')}；{_clean(image.get('preset'), 20)}；seed {image.get('seed_used')}；"
            f"参考图：{refs}；{state}。")


class ContextCards:
    def __init__(self, ctx, data_dir, *, store=None):
        if store is None:
            from .jobs import Store
            store = Store(data_dir)
        self.ctx, self.store = ctx, store

    def before_turn(self, *, session_id='', conversation_history=None, parent_session_id='', **kwargs):
        if parent_session_id or kwargs.get('platform') in ('cron', 'kanban', 'subagent'):
            return None
        try:
            origin = capture_origin(self.ctx)
        except (ValueError, ImportError):
            return None
        if session_id and origin.get('session_id') and origin['session_id'] != session_id:
            return None
        # The durable receipt/index is authoritative. Returning context is NOT an acknowledgement:
        # failed/aborted turns may never write the API sidecar. A later history read proves admission.
        seen = _seen(conversation_history)
        values = self.store.artifacts(origin=origin, limit=100)
        values = [value for value in values if same_scope(value.get('origin', {}), origin)]
        values.sort(key=lambda value: (str(value.get('created_at', '')), str(value.get('image_id', ''))), reverse=True)
        # New sessions receive the newest four, never a four-per-turn crawl of all old images.
        values = values[:MAX_CARDS]
        lines = []
        header = 'Comfy 产物索引（标题描述生成意图，未经视觉核验；需要确认画面时再读取图片）：\n'
        length = len(header)
        for image in values:
            if image.get('image_id') in seen:
                continue
            text = card(image)
            if not text:
                continue
            if length + len(text) + 1 > MAX_CHARS:
                continue
            lines.append(text)
            length += len(text) + 1
            if len(lines) >= MAX_CARDS:
                break
        return {'context': header + '\n'.join(reversed(lines))} if lines else None


def install(ctx, data_dir, *, store=None):
    cards = ContextCards(ctx, data_dir, store=store)
    ctx.register_hook('pre_llm_call', cards.before_turn)
    return cards
