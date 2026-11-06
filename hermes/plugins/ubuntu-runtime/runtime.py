"""Bounded local-LAN/SSH orchestration. User tool arguments never become commands."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
import uuid
from .metadata import metadata


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temporary.open('w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Runtime:
    def __init__(self, ctx, data_dir):
        self.data = Path(data_dir)
        self.destination = ctx.get_config('ssh_host', '')
        user, separator, host = self.destination.partition('@')
        if not separator or not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
            raise ValueError('ssh_host must be a trusted user@IPv4 address')
        self.host = str(ipaddress.IPv4Address(host))
        self.root = ctx.get_config('remote_root', '')
        self.identity = str(Path(ctx.get_config('identity_file', '~/.ssh/id_ed25519')).expanduser())
        if not Path(self.root).is_absolute() or '\0' in self.root or not Path(self.identity).is_absolute():
            raise ValueError('Configure absolute remote_root and local identity_file paths; ~ is not expanded remotely')
        network = ctx.get_config('lan_cidr', '')
        self.mac = ctx.get_config('mac_address', '')
        if bool(network) != bool(self.mac):
            raise ValueError('Configure lan_cidr and mac_address together, or leave both empty for SSH only')
        self.network = ipaddress.IPv4Network(network) if network else None
        if self.mac and not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', self.mac):
            raise ValueError('Invalid Wake-on-LAN MAC')
        self.events = []

    def reachable(self):
        try:
            with socket.create_connection((self.host, 22), timeout=1.5):
                return True
        except OSError:
            return False

    def event(self, stage, **details):
        self.events.append({'at': now(), 'stage': stage, **details})
        self.record.update(stage=stage, events=self.events)
        atomic_json(self.receipt, self.record)

    @contextmanager
    def serialized(self, action):
        if action == 'status':
            yield
            return
        self.data.mkdir(parents=True, exist_ok=True)
        with (self.data/'management.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def wake(self):
        if not self.network or not self.mac:
            raise ValueError('Wake-on-LAN is not configured: set both mac_address and lan_cidr; no wake packets sent')
        if ipaddress.IPv4Address(self.host) not in self.network:
            raise ValueError('Target is outside configured Wake-on-LAN subnet')
        # Wake-on-LAN currently supports a macOS sender. Follow its actual route,
        # rather than assuming en1 or sending a broadcast through a VPN.
        route = subprocess.run(['/sbin/route', '-n', 'get', self.host],
                               capture_output=True, text=True, timeout=5, check=True)
        match = re.search(r'^\s*interface:\s*(\S+)', route.stdout, re.MULTILINE)
        if not match or not re.fullmatch(r'en\d+', match[1]):
            raise RuntimeError('No direct Mac LAN interface; Wake-on-LAN requires the same LAN')
        interface = match[1]
        sender = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface],
                                capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        if ipaddress.IPv4Address(sender) not in self.network:
            raise RuntimeError('Sender is outside the configured LAN; no wake packets sent')
        packet = b'\xff'*6 + bytes.fromhex(self.mac.replace(':', ''))*16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((sender, 0))
            for _ in range(3):
                sock.sendto(packet, (str(self.network.broadcast_address), 9))
                time.sleep(.2)
        self.event('wake_packets_sent', interface=interface, sender=sender,
                   broadcast=str(self.network.broadcast_address), packets=3)

    def wait_for_ssh(self):
        started = time.monotonic()
        self.wake()
        while time.monotonic()-started < 180:
            if self.reachable():
                elapsed = round(time.monotonic()-started, 3)
                self.event('ssh_restored', wake_seconds=elapsed)
                return elapsed
            time.sleep(1)
        raise TimeoutError('SSH did not return within 180 seconds; no service operation attempted')

    def remote(self, action):
        code = Path(__file__).with_name('remote_bridge.py').read_text()
        command = ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                   '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
                   '-o', 'StrictHostKeyChecking=yes', '-i', self.identity, '-o', 'IdentitiesOnly=yes', self.destination,
                   'cd '+shlex.quote(self.root)+' && '+shlex.quote(self.root+'/.venv/bin/python')+' -c '+shlex.quote(code)]
        packet = {'root': self.root, 'action': action, 'operation_id': self.record['operation_id'],
                  'client_metadata': metadata(), **getattr(self, 'remote_options', {})}
        try:
            process = subprocess.run(command, input=json.dumps(packet), capture_output=True,
                                     text=True, timeout=45 if action != 'status' else 40)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('SSH operation timed out; completion is unknown. Query status before further service changes') from exc
        if not process.stdout.strip():
            raise RuntimeError(f'SSH failed ({process.returncode}); query status before retry: {process.stderr[:600]}')
        return json.loads(process.stdout)

    def run(self, action, *, operation_id=None, wait_seconds=30):
        if action not in ('status', 'ensure_ready', 'stop', 'restart', 'recover'):
            raise ValueError('Unsupported service action')
        if operation_id is not None and action != 'status':
            raise ValueError('operation_id is only accepted with status')
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise ValueError('wait_seconds must be 0..30')
        self.remote_options = {'wait_seconds': wait_seconds}
        if operation_id:
            self.remote_options['query_operation_id'] = str(uuid.UUID(operation_id))
        self.events = []
        operation_id = str(uuid.uuid4())
        self.receipt = self.data/'operations'/operation_id/'receipt.json'
        self.record = {'operation_id': operation_id, 'action': action, 'host': self.destination,
                       'created_at': now(), 'metadata': metadata()}
        started = time.monotonic()
        try:
            with self.serialized(action):
                online = self.reachable()
                self.event('network_checked', ssh_reachable=online)
                if not online:
                    if action != 'ensure_ready':
                        response = {'success': action == 'status', 'power_state': 'offline_unknown',
                                    'ssh_reachable': False, 'wake_packets_sent': False,
                                    'message': 'SSH unreachable; may be sleeping, powered off or unreachable. No wake attempted.'}
                    else:
                        self.record['wake_seconds'] = self.wait_for_ssh()
                        self.event('service_preparation')
                        response = self.remote(action)
                else:
                    self.event('service_operation' if action != 'status' else 'readonly_status')
                    response = self.remote(action)
                self.record['response'] = response
        except Exception as exc:
            response = {'success': False, 'error': {'type': type(exc).__name__, 'message': str(exc),
                        'retry_automatically': False}, 'failed_stage': self.record.get('stage', 'initializing')}
        remote_operation_id = response.get('operation_id', operation_id)
        response.update(operation_id=remote_operation_id, action=action, local_receipt_path=str(self.receipt),
                        elapsed_seconds=round(time.monotonic()-started, 3), metadata=response.get('metadata', metadata()),
                        wake_packets_sent=any(e['stage']=='wake_packets_sent' for e in self.events))
        if 'wake_seconds' in self.record:
            response['wake_seconds'] = self.record['wake_seconds']
        if response.get('receipt_path'):
            response['remote_receipt_path'] = response['receipt_path']
        response['receipt_path'] = str(self.receipt)
        response['observed_at'] = now()
        if response.get('power_state') == 'offline_unknown':
            response['wake_capability'] = {'mac_configured':bool(self.mac),'same_lan_required':True,'last_successful_wake':self.last_wake()}
        if self.record.get('wake_seconds') is not None:
            atomic_json(self.data/'last-wake.json', {'observed_at':now(),'wake_seconds':self.record['wake_seconds'],'mac':self.mac})
        self.record.update(response=response, finished_at=now())
        atomic_json(self.receipt, self.record)
        return response

    def last_wake(self):
        try:
            return json.loads((self.data/'last-wake.json').read_text())
        except (OSError, ValueError):
            return None
