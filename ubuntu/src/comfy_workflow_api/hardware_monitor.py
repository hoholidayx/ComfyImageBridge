"""Bounded hardware observation and durable, asynchronous SMTP notifications.

GPU access lives in hardware_probe's single-flight helper.  This module never
waits for SMTP from a sampling or sleep-management call.  SQLite commits kernel
cursors, incident changes and outgoing notifications together.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import smtplib
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
import tomllib
import uuid

from .settings import load_settings, module_root
from .storage import now, read_json, write_json


DEFAULTS = {
    'sample_seconds': 15, 'probe_timeout_seconds': 5, 'failure_samples': 3,
    'recovery_samples': 3, 'reminder_seconds': 1800,
    'temp_warning_margin': 10, 'temp_critical_margin': 5,
    'outbox_limit': 1000,
}
RETRY_SECONDS = (60, 300, 900, 1800, 3600)
UNIT = 'myhermes2comfy-monitor.service'
SECRET_KEYS = re.compile(r'password|secret|token|credential|authorization', re.I)


def configuration(root):
    settings = load_settings(root)
    config = {**DEFAULTS, **settings.get('monitor', {})}
    for key in DEFAULTS:
        value = config[key]
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f'monitor.{key} must be positive')
    for key in ('failure_samples', 'recovery_samples', 'outbox_limit'):
        if int(config[key]) != config[key]:
            raise ValueError(f'monitor.{key} must be an integer')
        config[key] = int(config[key])
    if config['temp_warning_margin'] < config['temp_critical_margin']:
        raise ValueError('monitor temperature warning margin must cover critical margin')
    return config, Path(settings['data_dir'])/'hardware-monitor'


def _clean(value, secrets=()):
    if isinstance(value, dict):
        return {str(k): '[redacted]' if SECRET_KEYS.search(str(k)) else _clean(v, secrets)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[redacted]')
        return value[:12000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _clean(str(value), secrets)


def _valid_cc_address(value):
    """Accept one bare mailbox, never a display name or an address-list header."""
    if (not isinstance(value, str) or not value or len(value) > 254
            or not value.isascii() or any(character.isspace() or ord(character) < 32
                                           or ord(character) == 127 for character in value)):
        return False
    try:
        address = Address(addr_spec=value)
    except (ValueError, TypeError, HeaderParseError, IndexError):
        # Python 3.12.3 can raise IndexError for a missing domain after @.
        return False
    if (not address.username or len(address.username) > 64 or not address.domain
            or len(address.domain) > 253 or address.addr_spec != value):
        return False
    return all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
               for label in address.domain.split('.'))


def smtp_configuration(root, config=None):
    """Return private settings to the sender only; errors never contain values."""
    config = config or configuration(root)[0]
    reference = config.get('smtp', {})
    filename = reference.get('secret_file') if isinstance(reference, dict) else None
    if not filename:
        return None, 'SMTP secret_file is not configured'
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = Path(root)/path
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
            return None, 'SMTP secret file must be a regular private file (0600)'
        if info.st_uid not in (os.getuid(), 0):
            return None, 'SMTP secret file has a different owner'
        content = tomllib.loads(path.read_text())
        value = content.get('smtp', content)
        if not isinstance(value, dict):
            raise ValueError()
        result = {
            'host': value.get('host'), 'port': value.get('port', 587),
            'security': value.get('security', 'starttls'),
            'username': value.get('username', ''), 'password': value.get('password', ''),
            'from_address': value.get('from_address'), 'to_addresses': value.get('to_addresses'),
            'cc_addresses': value.get('cc_addresses', []),
        }
        if result['security'] not in ('starttls', 'ssl'):
            return None, 'SMTP security must be starttls or ssl'
        if type(result['port']) is not int or not 1 <= result['port'] <= 65535:
            raise ValueError()
        for key in ('host', 'from_address'):
            if not isinstance(result[key], str) or not result[key] or any(c in result[key] for c in '\r\n'):
                raise ValueError()
        if not isinstance(result['to_addresses'], list) or not result['to_addresses']:
            raise ValueError()
        if any(not isinstance(item, str) or not item or any(c in item for c in '\r\n')
               for item in result['to_addresses']):
            raise ValueError()
        if not isinstance(result['cc_addresses'], list) or any(
                not _valid_cc_address(item) for item in result['cc_addresses']):
            raise ValueError()
        if not all(isinstance(result[key], str) for key in ('username', 'password')):
            raise ValueError()
        return result, None
    except (OSError, ValueError, TypeError):
        return None, 'SMTP secret file is missing, unreadable or invalid'


class QueueFull(RuntimeError):
    pass


class EventStore:
    def __init__(self, root):
        self.root = Path(root)
        self.config, self.folder = configuration(root)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder/'monitor.sqlite3'
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents (
                key TEXT PRIMARY KEY, category TEXT NOT NULL, severity TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                recoveries INTEGER NOT NULL DEFAULT 0, first_at REAL NOT NULL,
                last_fault REAL NOT NULL, last_notice REAL NOT NULL DEFAULT 0,
                details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL,
                sent_at REAL, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS kernel_seen (cursor TEXT PRIMARY KEY, seen REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(created,id) WHERE sent_at IS NULL;
        ''')
        self.connection.commit()

    def close(self):
        self.connection.close()

    def state(self, key, default=None):
        row = self.connection.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_state(self, key, value):
        self.connection.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                                (key, json.dumps(_clean(value), ensure_ascii=False)))

    def _enqueue(self, kind, details, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        pending = self.connection.execute('SELECT count(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0]
        if pending >= self.config['outbox_limit']:
            self.set_state('queue_error', 'Notification queue is full; a notification could not be queued')
            print('hardware-monitor: notification queue is full', file=sys.stderr, flush=True)
            raise QueueFull('Notification queue is full; inspect local incidents and journal')
        self.set_state('queue_error', None)
        event_id = str(uuid.uuid4())
        payload = {'event_id': event_id, 'kind': kind, 'host': socket.gethostname(),
                   'occurred_at': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                   'details': _clean(details)}
        self.connection.execute('INSERT INTO outbox(id,created,payload,next_try) VALUES (?,?,?,?)',
                                (event_id, timestamp, json.dumps(payload, ensure_ascii=False), timestamp))
        return event_id

    def enqueue(self, kind, details, timestamp=None):
        with self.connection:
            try:
                result = self._enqueue(kind, details, timestamp)
            except QueueFull:
                # Retain the visible overflow marker even though no event fits.
                self.connection.commit()
                raise
        return result

    def due(self, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        row = self.connection.execute('SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY created,id LIMIT 1').fetchone()
        return dict(row) if row and row['next_try'] <= timestamp else None

    def outcome(self, event_id, success, error=None, timestamp=None):
        timestamp = time.time() if timestamp is None else timestamp
        with self.connection:
            row = self.connection.execute('SELECT attempts,sent_at FROM outbox WHERE id=?', (event_id,)).fetchone()
            if not row or row['sent_at'] is not None:
                return
            if success:
                self.connection.execute('UPDATE outbox SET sent_at=?,last_error=NULL WHERE id=?', (timestamp, event_id))
                self.set_state('last_delivery', timestamp)
                self.set_state('last_delivery_error', None)
            else:
                attempts = row['attempts']+1
                retry = RETRY_SECONDS[min(attempts-1, len(RETRY_SECONDS)-1)]
                safe = str(error or 'SMTP delivery failed')[:200]
                self.connection.execute('UPDATE outbox SET attempts=?,next_try=?,last_error=? WHERE id=?',
                                        (attempts, timestamp+retry, safe, event_id))
                self.set_state('last_delivery_error', safe)

    def incidents(self):
        return [dict(row) | {'details': json.loads(row['details'])}
                for row in self.connection.execute('SELECT * FROM incidents WHERE active=1 ORDER BY key')]

    def observe(self, cpu, gpu, kernel, timestamp=None):
        """Commit consumed kernel events, incident transitions and mail atomically."""
        timestamp = time.time() if timestamp is None else timestamp
        anomalies = _anomalies(cpu, gpu, self.config)
        paused = bool(gpu.get('paused') or gpu.get('status') == 'paused')
        with self.connection:
            latest_kernel = self.state('last_kernel_fault', 0)
            for event in kernel.get('events', []):
                cursor = event.get('cursor')
                if not cursor:
                    continue
                inserted = self.connection.execute('INSERT OR IGNORE INTO kernel_seen VALUES (?,?)', (cursor, timestamp)).rowcount
                if not inserted:
                    continue
                latest_kernel = timestamp
                message = event.get('message', '')
                match = re.search(r'(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', message)
                device = match.group(0) if match else 'system'
                category = 'cpu' if re.search(r'\bMCE\b|Hardware Error|Machine check', message, re.I) else 'gpu'
                key = f'{category}:{device}:kernel'
                existing = anomalies.setdefault(key, {'category': category, 'severity': 'critical',
                                                      'immediate': True, 'events': []})
                existing['events'].append(_clean(event))
                existing['events'] = existing['events'][-8:]
            self.set_state('last_kernel_fault', latest_kernel)
            if kernel.get('cursor'):
                self.set_state('kernel_cursor', kernel['cursor'])
                self.set_state('kernel_boot_id', kernel.get('boot_id'))
            rows = {row['key']: dict(row) for row in self.connection.execute('SELECT * FROM incidents')}
            for key, detail in anomalies.items():
                row = rows.get(key)
                failures = (row['failures'] if row else 0)+1
                active = bool(row and row['active'])
                activate = detail.get('immediate') or failures >= self.config['failure_samples']
                notify = activate and (bool(detail.get('events')) or not active or not row['last_notice'] or
                                       row['severity'] != detail['severity'] or
                                       timestamp-row['last_notice'] >= self.config['reminder_seconds'])
                last_notice = row['last_notice'] if row else 0
                if notify:
                    try:
                        self._enqueue('fault' if detail.get('events') or not active or not last_notice else 'reminder',
                                      {'incident': key, **detail}, timestamp)
                        last_notice = timestamp
                    except QueueFull:
                        # Keep the incident and cursor. Retry notification at the next observation.
                        pass
                self.connection.execute('INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (key, detail['category'], detail['severity'], int(active or activate), failures, 0,
                     row['first_at'] if row else timestamp, timestamp, last_notice,
                     json.dumps(_clean(detail), ensure_ascii=False)))
            for key, row in rows.items():
                if key in anomalies:
                    continue
                if row['category'] == 'gpu' and paused:
                    continue
                if row['active'] and not row['last_notice']:
                    # A one-shot kernel event may already have advanced the
                    # cursor while the outbox was full. Keep its original
                    # details durable and retry the fault before recovery.
                    try:
                        self._enqueue('fault', {'incident': key, **json.loads(row['details'])}, timestamp)
                    except QueueFull:
                        continue
                    self.connection.execute('UPDATE incidents SET last_notice=? WHERE key=?',
                                            (timestamp, key))
                if ((row['category'] == 'gpu' and gpu.get('status') != 'ok') or
                        (row['category'] == 'cpu' and not _cpu_valid(cpu))):
                    # Unknown/error samples interrupt consecutive recovery; a
                    # planned sleep pause above leaves the counter untouched.
                    self.connection.execute('UPDATE incidents SET recoveries=0 WHERE key=?', (key,))
                    if (row['active'] and row['last_notice'] and
                            timestamp-row['last_notice'] >= self.config['reminder_seconds']):
                        # Preserve the original incident's severity while its
                        # category is unreadable and recovery is unconfirmed.
                        try:
                            self._enqueue('reminder', {'incident': key, **json.loads(row['details'])}, timestamp)
                        except QueueFull:
                            pass
                        else:
                            self.connection.execute('UPDATE incidents SET last_notice=? WHERE key=?',
                                                    (timestamp, key))
                    continue
                recoveries = row['recoveries']+1
                recovered = recoveries >= self.config['recovery_samples'] and timestamp-latest_kernel >= 45
                active = bool(row['active'])
                if active and recovered:
                    try:
                        self._enqueue('recovery', {'incident': key,
                            'meaning': 'Hardware observations recovered; rendering readiness is separate',
                            'observation': gpu if row['category'] == 'gpu' else cpu}, timestamp)
                    except QueueFull:
                        # Leave active so recovery notification is retried when the queue has room.
                        continue
                    active = False
                self.connection.execute('UPDATE incidents SET active=?,failures=0,recoveries=? WHERE key=?',
                                        (int(active), recoveries, key))
            self.connection.execute('DELETE FROM kernel_seen WHERE seen<?', (timestamp-7*86400,))
            self.connection.execute('DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at<?', (timestamp-30*86400,))
        return self.incidents()


def _cpu_valid(cpu):
    return cpu.get('status') not in ('unknown', 'critical', 'error') and not cpu.get('error')


def _thermal(temperatures, config):
    breaches = []
    for sensor in temperatures or []:
        value, limit = sensor.get('celsius'), sensor.get('critical_celsius')
        if not isinstance(value, (int, float)) or not isinstance(limit, (int, float)):
            continue
        if value >= limit-config['temp_warning_margin']:
            severe = value >= limit-config['temp_critical_margin']
            breaches.append({'label': sensor.get('label'), 'celsius': value,
                             'critical_celsius': limit, 'severity': 'critical' if severe else 'warning'})
    return breaches


def _anomalies(cpu, gpu, config):
    result = {}
    if not _cpu_valid(cpu):
        result['cpu:telemetry'] = {'category': 'cpu', 'severity': 'warning', 'observation': _clean(cpu)}
    temperatures = _thermal(cpu.get('temperatures'), config)
    if temperatures:
        critical = any(item['severity'] == 'critical' for item in temperatures)
        result['cpu:thermal'] = {'category': 'cpu', 'severity': 'critical' if critical else 'warning',
                                 'immediate': critical, 'temperatures': temperatures}
    if gpu.get('paused') or gpu.get('status') == 'paused':
        return result
    if gpu.get('status') != 'ok':
        result['gpu:telemetry'] = {'category': 'gpu', 'severity': 'critical' if gpu.get('status') == 'critical' else 'warning',
                                   'immediate': gpu.get('status') == 'critical', 'observation': _clean(gpu)}
    if gpu.get('rocm_error') and gpu.get('status') == 'ok':
        result['gpu:rocm_smi'] = {'category': 'gpu', 'severity': 'warning',
            'meaning': 'ROCm monitoring source is unavailable; independent GPU observations remain readable',
            'observation': _clean(gpu)}
    for device in gpu.get('gpus', []):
        temperatures = _thermal(device.get('temperatures'), config)
        if temperatures:
            critical = any(item['severity'] == 'critical' for item in temperatures)
            result[f"gpu:{device.get('pci', 'unknown')}:thermal"] = {
                'category': 'gpu', 'severity': 'critical' if critical else 'warning',
                'immediate': critical, 'pci': device.get('pci'), 'temperatures': temperatures}
    return result


def queue_event(root, kind, details):
    """Persist a notification; safe to call while SMTP or networking is offline."""
    private, _ = smtp_configuration(root)
    secrets = [private.get('password', '')] if private else []
    store = EventStore(root)
    try:
        return store.enqueue(str(kind), _clean(details, secrets))
    finally:
        store.close()


def _read_database(folder):
    path = folder/'monitor.sqlite3'
    if not path.is_file():
        return {'pending': 0, 'last_error': None, 'last_delivery_at': None, 'incidents': []}
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.2) as db:
            db.row_factory = sqlite3.Row
            pending = db.execute('SELECT count(*),min(created) FROM outbox WHERE sent_at IS NULL').fetchone()
            state = {row['key']: json.loads(row['value']) for row in db.execute('SELECT * FROM state')}
            incidents = [dict(row) | {'details': json.loads(row['details'])}
                         for row in db.execute('SELECT * FROM incidents WHERE active=1 ORDER BY key')]
            return {'pending': pending[0], 'oldest_pending_at': pending[1],
                    'last_error': state.get('queue_error') or state.get('last_delivery_error'),
                    'last_delivery_at': state.get('last_delivery'), 'incidents': incidents}
    except (sqlite3.Error, OSError, ValueError):
        return {'pending': None, 'last_error': 'Notification database is unavailable', 'incidents': []}


def _identity(pid):
    from .sleep_guard import identity
    return identity(pid)


def _matches(saved):
    return bool(saved and saved.get('uid') == os.getuid() and _identity(saved.get('pid')) == saved)


def status(root):
    """Read cached observations only: never mkdir, sample hardware or send mail."""
    config, folder = configuration(root)
    try:
        snapshot = read_json(folder/'health.json')
    except (OSError, ValueError):
        snapshot = {}
    private, error = smtp_configuration(root, config)
    delivery = _read_database(folder)
    heartbeat = snapshot.get('heartbeat_unix', 0)
    running = _matches(snapshot.get('process')) and not snapshot.get('stopped_at')
    stale = not running or time.time()-heartbeat > max(45, config['sample_seconds']*3)
    return {**snapshot, 'running': bool(running), 'stale': stale,
            'status': 'unknown' if stale else snapshot.get('status', 'unknown'),
            'notifications_configured': bool(private),
            'notifications': {**delivery, 'configured': bool(private), 'configuration_error': error},
            'incidents': delivery['incidents'], 'log': f'journalctl -u {UNIT}',
            'data_dir': str(folder)}


def check(root):
    from .hardware_probe import CpuSampler, gpu_probe
    config, _ = configuration(root)
    return {'observed_at': now(), 'cpu': CpuSampler().sample(),
            'gpu': gpu_probe(root, timeout_seconds=config['probe_timeout_seconds'])}


def _smtp_error(exc):
    code = getattr(exc, 'smtp_code', None)
    return type(exc).__name__+(f' (SMTP {code})' if isinstance(code, int) else '')


def _deliver(root, event_id):
    """Subprocess-only sender. Never print credentials or SMTP server responses."""
    store = EventStore(root)
    try:
        row = store.connection.execute('SELECT * FROM outbox WHERE id=?', (event_id,)).fetchone()
        if not row or row['sent_at'] is not None:
            return 0
        settings, error = smtp_configuration(root, store.config)
        if not settings:
            store.outcome(event_id, False, error)
            return 1
        payload = json.loads(row['payload'])
        message = EmailMessage()
        message['From'] = settings['from_address']
        message['To'] = ', '.join(settings['to_addresses'])
        if settings['cc_addresses']:
            message['Cc'] = ', '.join(settings['cc_addresses'])
        message['Subject'] = f"[ComfyUI {payload['kind']}] {payload['host']}"
        message['Message-ID'] = f'<{event_id}@myhermes2comfy.local>'
        message['Date'] = datetime.fromtimestamp(row['created'], timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
        message.set_content(json.dumps({**payload, 'delivery_attempt_at': now()}, ensure_ascii=False, indent=2))
        context = ssl.create_default_context()
        try:
            factory = smtplib.SMTP_SSL if settings['security'] == 'ssl' else smtplib.SMTP
            kwargs = {'host': settings['host'], 'port': settings['port'], 'timeout': 5}
            if settings['security'] == 'ssl':
                kwargs['context'] = context
            with factory(**kwargs) as smtp:
                if settings['security'] == 'starttls':
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if settings['username']:
                    smtp.login(settings['username'], settings['password'])
                recipients = list(dict.fromkeys(settings['to_addresses'] + settings['cc_addresses']))
                refused = smtp.send_message(message, from_addr=settings['from_address'], to_addrs=recipients)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
            store.outcome(event_id, True)
            return 0
        except Exception as exc:
            store.outcome(event_id, False, _smtp_error(exc))
            return 1
    finally:
        store.close()


class Sender:
    """A small supervisor thread imposes the SMTP process's total deadline."""
    def __init__(self, root):
        self.root = Path(root)
        self.config, self.folder = configuration(root)
        self.stopping = threading.Event()
        self.thread = None
        self.process = None
        self.saved = None
        self.event_id = None
        self.attempts = 0
        self.deadline = None
        self.next_scan = 0

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='smtp-supervisor', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.pump()
            except Exception as exc:
                print(f'hardware-monitor: SMTP supervisor {_smtp_error(exc)}', file=sys.stderr, flush=True)
            self.stopping.wait(.2)
        if self.process and self.process.poll() is None and _matches(self.saved):
            os.kill(self.saved['pid'], signal.SIGKILL)
        # Never wait() here: kernel waits must not delay monitor shutdown.

    def pump(self):
        if self.process:
            if self.process.poll() is None:
                if time.monotonic() >= self.deadline:
                    if _matches(self.saved):
                        os.kill(self.saved['pid'], signal.SIGKILL)
                    store = EventStore(self.root)
                    try:
                        store.outcome(self.event_id, False, 'SMTP total deadline exceeded')
                    finally:
                        store.close()
                    self.deadline = float('inf')
                return
            store = EventStore(self.root)
            try:
                row = store.connection.execute('SELECT attempts,sent_at FROM outbox WHERE id=?', (self.event_id,)).fetchone()
                if row and row['sent_at'] is None and row['attempts'] == self.attempts:
                    store.outcome(self.event_id, False, 'SMTP sender exited without a delivery receipt')
            finally:
                store.close()
            self.process = None
            self.saved = None
        if time.monotonic() < self.next_scan:
            return
        self.next_scan = time.monotonic()+1
        private, _ = smtp_configuration(self.root, self.config)
        if not private:
            return
        store = EventStore(self.root)
        try:
            due = store.due()
        finally:
            store.close()
        if not due:
            return
        lock = (self.folder/'sender.lock').open('a')
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._observe_orphan()
                return
            self.event_id = due['id']
            self.attempts = due['attempts']
            command = [sys.executable, '-m', 'comfy_workflow_api.hardware_monitor', '--root', str(self.root),
                       '_send', '--event-id', self.event_id]
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock.fileno(),))
            self.saved = _identity(self.process.pid)
            self.deadline = time.monotonic()+15
            write_json(self.folder/'sender.json', {'process': self.saved, 'event_id': self.event_id,
                       'attempts': self.attempts, 'deadline_monotonic': self.deadline, 'timed_out': False})
        finally:
            # The child holds the same lock description across monitor restarts.
            lock.close()

    def _observe_orphan(self):
        try:
            saved = read_json(self.folder/'sender.json')
        except (OSError, ValueError):
            return
        if (saved.get('timed_out') or time.monotonic() < saved.get('deadline_monotonic', float('inf'))
                or not _matches(saved.get('process'))):
            return
        os.kill(saved['process']['pid'], signal.SIGKILL)
        store = EventStore(self.root)
        try:
            row = store.connection.execute('SELECT attempts,sent_at FROM outbox WHERE id=?', (saved.get('event_id'),)).fetchone()
            if row and row['sent_at'] is None and row['attempts'] == saved.get('attempts'):
                store.outcome(saved['event_id'], False, 'SMTP total deadline exceeded after monitor restart')
        finally:
            store.close()
        write_json(self.folder/'sender.json', {**saved, 'timed_out': True})


@contextmanager
def _singleton(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'daemon.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run(root):
    from .hardware_probe import CpuSampler, gpu_probe, read_kernel_events
    root = Path(root).resolve()
    config, folder = configuration(root)
    stopping = threading.Event()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    with _singleton(folder):
        store = EventStore(root)
        sender = Sender(root)
        cpu_sampler = CpuSampler()
        process = _identity(os.getpid())
        started = now()
        sender.start()
        snapshot = {}
        try:
            while not stopping.is_set():
                tick = time.monotonic()
                try:
                    cpu = cpu_sampler.sample()
                except Exception as exc:
                    cpu = {'status': 'unknown', 'error': type(exc).__name__}
                try:
                    gpu = gpu_probe(root, timeout_seconds=config['probe_timeout_seconds'])
                except Exception as exc:
                    gpu = {'status': 'unknown', 'observed_at': now(), 'error': type(exc).__name__, 'gpus': []}
                try:
                    kernel = read_kernel_events(cursor=store.state('kernel_cursor'), boot_id=store.state('kernel_boot_id'))
                except Exception as exc:
                    kernel = {'events': [], 'error': type(exc).__name__}
                incidents = store.observe(cpu, gpu, kernel)
                level = 'critical' if any(item['severity'] == 'critical' for item in incidents) else 'warning' if incidents else 'unknown' if gpu.get('status') != 'ok' else 'ok'
                snapshot = {'observed_at': now(), 'heartbeat_unix': time.time(), 'status': level,
                    'cpu': cpu, 'gpu': gpu, 'kernel_error': kernel.get('error'), 'incidents': incidents,
                    'process': process, 'started_at': started, 'command': [sys.executable, *sys.argv],
                    'unit': UNIT, 'invocation_id': os.environ.get('INVOCATION_ID'), 'sighup': 'ignored',
                    'log': f'journalctl -u {UNIT}'}
                write_json(folder/'health.json', snapshot)
                stopping.wait(max(0, config['sample_seconds']-(time.monotonic()-tick)))
        finally:
            sender.stop()
            store.close()
            if snapshot:
                write_json(folder/'health.json', {**snapshot, 'stopped_at': now()})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    parser.add_argument('action', choices=('run', 'status', 'check', 'test-email', '_send'))
    parser.add_argument('--event-id')
    args = parser.parse_args()
    root = module_root(args.root)
    try:
        if args.action == 'run':
            return run(root)
        if args.action == '_send':
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            def expired(*_):
                raise TimeoutError('SMTP total deadline exceeded')
            signal.signal(signal.SIGALRM, expired)
            signal.alarm(15)
            return _deliver(root, str(uuid.UUID(args.event_id)))
        if args.action == 'status':
            result = status(root)
        elif args.action == 'check':
            result = check(root)
        else:
            event_id = queue_event(root, 'test', {'message': 'Hardware-monitor SMTP test notification'})
            result = {'queued': True, 'event_id': event_id, 'notifications_configured': bool(smtp_configuration(root)[0]),
                      'message': 'The running monitor sends this event asynchronously; status reports delivery or configuration errors'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'success': False, 'error': _clean(str(exc)), 'type': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
