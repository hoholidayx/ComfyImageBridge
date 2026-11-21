"""Read the effective logind and GNOME idle-sleep policies without activating services."""
import re
import subprocess
import time

PROBE_SECONDS = 1.0
LOGIND = 'org.freedesktop.login1'
UPOWER = 'org.freedesktop.UPower'
SESSION = 'org.gnome.SessionManager'
POWER_SCHEMA = 'org.gnome.settings-daemon.plugins.power'
SLEEP_ACTIONS = {'sleep', 'suspend', 'hibernate', 'hybrid-sleep', 'suspend-then-hibernate'}
GUARDS = {'logind', 'gnome'}


def _bus(scope, name, path, interface, method, *args):
    return ['/usr/bin/busctl', scope, '--auto-start=no', '--allow-interactive-authorization=no',
            'call', name, path, interface, method, *args]


def _property(scope, name, path, interface, prop):
    return _bus(scope, name, path, 'org.freedesktop.DBus.Properties', 'Get', 'ss', interface, prop)


def _logind_enabled(read):
    def prop(name):
        return read(_property('--system', LOGIND, '/org/freedesktop/login1', LOGIND + '.Manager', name))

    action = re.fullmatch(r'v s "([a-z-]+)"', prop('IdleAction') or '')
    if not action:
        return None
    if action[1] not in SLEEP_ACTIONS:
        return False if action[1] in {'ignore', 'lock', 'poweroff', 'halt', 'reboot', 'kexec', 'soft-reboot'} else None
    delay = re.fullmatch(r'v t ([0-9]{1,20})', prop('IdleActionUSec') or '')
    return 0 < int(delay[1]) < 2**64 - 1 if delay else None


def _gnome_enabled(read):
    owner = read(_bus('--user', 'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                      'NameHasOwner', 's', 'org.gnome.SettingsDaemon.Power'))
    if owner != 'b true':
        return False if owner == 'b false' else None
    active = read(_property('--user', SESSION, '/org/gnome/SessionManager', SESSION, 'SessionIsActive'))
    if active != 'v b true':
        return False if active == 'v b false' else None
    battery = read(_property('--system', UPOWER, '/org/freedesktop/UPower', UPOWER, 'OnBattery'))
    source = {'v b true': 'battery', 'v b false': 'ac'}.get(battery)
    if not source:
        return None
    key = f'sleep-inactive-{source}-'
    action = read(['/usr/bin/gsettings', 'get', POWER_SCHEMA, key + 'type'])
    if action in {"'nothing'", "'blank'", "'interactive'", "'logout'", "'shutdown'"}:
        return False
    if action not in {"'suspend'", "'hibernate'"}:
        return None
    delay = read(['/usr/bin/gsettings', 'get', POWER_SCHEMA, key + 'timeout'])
    return int(delay) > 0 if re.fullmatch(r'-?[0-9]{1,10}', delay or '') else None


def detect(env):
    deadline = time.monotonic() + PROBE_SECONDS

    def read(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=min(remaining, 0.4))
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            return None
        return result.stdout.strip() if result.returncode == 0 and not result.stderr.strip() else None

    system = _logind_enabled(read)
    if system is True:
        return 'logind'
    desktop = _gnome_enabled(read)
    if desktop is True:
        return 'gnome'
    return 'disabled' if system is False and desktop is False else 'unknown'
