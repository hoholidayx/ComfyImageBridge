"""Shared route checks, with durable session isolation for local clients."""

LOCAL_PLATFORMS = ('cli', 'local', 'tui', 'desktop')
BASE_FIELDS = ('profile', 'bot', 'platform', 'chat_id', 'thread_id', 'user_id')
SESSION_FIELDS = ('session_key', 'conversation_id')


def _text(value):
    return str(getattr(value, 'value', value) or '')


def _value(origin, key):
    if key == 'conversation_id':
        # Physical session tips rotate during compression. Older receipts only
        # recorded session_id, which remains their conservative identity fallback.
        return _text(origin.get('conversation_id') or origin.get('session_id'))
    return _text(origin.get(key))


def scope_fields(*origins):
    """Messaging keeps route scope; either local side also requires its session."""
    local = any(_text((origin or {}).get('platform')) in LOCAL_PLATFORMS for origin in origins)
    return BASE_FIELDS + SESSION_FIELDS if local else BASE_FIELDS


def scope_matches(record, caller):
    """Match supplied scope fields only, including partial adapter binding filters."""
    if caller is None:
        return True
    fields = scope_fields(record, caller)
    return all(_value(record, key) == _value(caller, key) for key in fields
               if key in caller or (key == 'conversation_id' and 'session_id' in caller))


def same_scope(left, right):
    return all(_value(left, key) == _value(right, key) for key in scope_fields(left, right))


def require_scope(record, caller):
    if caller is not None and not same_scope(record, caller):
        raise PermissionError('Task or image belongs to another conversation/user')
