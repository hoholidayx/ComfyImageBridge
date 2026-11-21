"""Native Ubuntu inhibitor commands and verification of acquired locks."""
import json
import os
import subprocess

from .sleep_policy import LOGIND


def environment():
    return {**os.environ, 'DBUS_SESSION_BUS_ADDRESS': os.environ.get('DBUS_SESSION_BUS_ADDRESS') or f'unix:path=/run/user/{os.getuid()}/bus'}


def inhibited(token, driver='gnome'):
    if driver == 'logind':
        result = subprocess.run(['/usr/bin/busctl', '--system', '--auto-start=no', '--json=short', 'call',
                                 LOGIND, '/org/freedesktop/login1', LOGIND + '.Manager',
                                 'ListInhibitors'], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError('logind inhibitor list is unavailable')
        try:
            reply = json.loads(result.stdout)
            if reply['type'] != 'a(ssssuu)':
                raise ValueError
            rows, = reply['data']
            return any(who == f'comfy-workflow-api-{token}' and mode == 'block' and uid == os.getuid()
                       and {'idle', 'sleep'} <= set(what.split(':')) for what, who, _, mode, uid, _ in rows)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError('Invalid logind inhibitor list') from exc
    if driver != 'gnome':
        raise RuntimeError('Unknown idle-sleep inhibitor')
    result = subprocess.run(['/usr/bin/gnome-session-inhibit', '--list'], env=environment(),
                            text=True, capture_output=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f'GNOME inhibitor query failed ({result.returncode}): {result.stderr.strip()[:500]}')
    return f'comfy-workflow-api-{token}:' in result.stdout


def command(driver, token):
    who = f'comfy-workflow-api-{token}'
    reason = 'ComfyUI API requests or queued/running generation tasks'
    if driver == 'logind':
        return ['/usr/bin/systemd-inhibit', '--what=idle:sleep', '--mode=block', '--who='+who, '--why='+reason, '--']
    if driver == 'gnome':
        return ['/usr/bin/gnome-session-inhibit', '--app-id', who, '--reason', reason, '--inhibit', 'suspend:idle']
    raise ValueError('Unknown idle-sleep inhibitor')
