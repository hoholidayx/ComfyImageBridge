"""Manage the Ubuntu MCP process independently from ComfyUI."""
import argparse
from contextlib import contextmanager
import fcntl
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time

from comfy_workflow_api.settings import load_settings, mcp_listen
from comfy_workflow_api.sleep_guard import identity
from comfy_workflow_api.storage import now, read_json, write_json


PROC = Path('/proc')
NOHUP = '/usr/bin/nohup'
START_SECONDS = 20
STOP_SECONDS = 120


def listeners(port):
    """Read LISTEN socket inodes without depending on elevated permissions."""
    result = []
    for name, family in (('tcp', socket.AF_INET), ('tcp6', socket.AF_INET6)):
        try:
            rows = (PROC/'net'/name).read_text().splitlines()[1:]
        except FileNotFoundError:
            if family == socket.AF_INET6:
                continue
            raise
        for line in rows:
            fields = line.split()
            address, local_port = fields[1].split(':')
            if fields[3] == '0A' and int(local_port, 16) == port:
                raw = b''.join(struct.pack('=I', int(address[i:i+8], 16))
                               for i in range(0, len(address), 8))
                result.append({'address': socket.inet_ntop(family, raw), 'inode': fields[9]})
    return result


def owns_listeners(pid, host, sockets):
    if any(entry['address'] != host for entry in sockets):
        return False
    owned = set()
    try:
        for fd in (PROC/str(pid)/'fd').iterdir():
            try:
                owned.add(str(fd.readlink()))
            except FileNotFoundError:
                continue
    except OSError:
        return False
    return all(f"socket:[{entry['inode']}]" in owned for entry in sockets)


def process_identity(pid):
    current = identity(pid)
    if current:
        return current
    try:
        state = (PROC/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return None
    if state == 'Z':
        return None
    raise RuntimeError('Unable to verify the recorded MCP process identity')


def detached(pid):
    try:
        return os.getsid(pid) == os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


def ignores_hangup(pid):
    try:
        for line in (PROC/str(pid)/'status').read_text().splitlines():
            if line.startswith('SigIgn:'):
                mask = int(line.split()[1], 16)
                return bool(mask & (1 << (signal.SIGHUP - 1)))
    except (OSError, ValueError):
        pass
    return False


def healthy(host, port, pid):
    # Connect straight to the configured listener, independently of the server
    # process, with no proxy or inherited credentials.
    connection = HTTPConnection(host, port, timeout=1)
    try:
        connection.request('GET', '/healthz', headers={'Host': f'{host}:{port}'})
        response = connection.getresponse()
        if response.status != 200:
            return False
        return json.loads(response.read(4096)) == {
            'status': 'ok', 'service': 'comfy-image-mcp', 'pid': pid,
        }
    except (HTTPException, OSError, ValueError):
        return False
    finally:
        connection.close()


class ServiceControl:
    def __init__(self, root):
        self.root = Path(root).resolve()
        settings = load_settings(self.root)
        self.host, self.port = mcp_listen(settings)
        self.directory = Path(settings['data_dir'])/'mcp-service'
        self.state_file = self.directory/'state.json'
        self.log = self.directory/'mcp.log'

    def command(self, host, port):
        return [str(self.root/'.venv/bin/python'), '-m', 'comfy_workflow_api.mcp_server',
                '--root', str(self.root), '--host', host, '--port', str(port)]

    @contextmanager
    def lock(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory/'control.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def saved(self):
        try:
            value = read_json(self.state_file)
        except FileNotFoundError:
            return None
        try:
            process = value['process']
            host, port = mcp_listen({'mcp': {'host': value['host'], 'port': value['port']}})
            valid = (value['phase'] in ('starting', 'running', 'stopped') and
                     type(process['pid']) is int and process['pid'] > 1 and
                     type(process['uid']) is int and process['uid'] == os.getuid() and
                     isinstance(process['start_ticks'], str) and process['start_ticks'].isdigit() and
                     isinstance(process['boot_id'], str) and bool(process['boot_id']) and
                     process['command'] == self.command(host, port))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError('Invalid MCP process record; refusing to manage it')
        return value

    def observe(self, saved, probe=True):
        host, port = (saved['host'], saved['port']) if saved else (self.host, self.port)
        expected = saved['process'] if saved else None
        current = process_identity(expected['pid']) if expected else None
        sockets = listeners(port)
        same_incarnation = current is not None and all(current[k] == expected[k]
            for k in ('pid', 'start_ticks', 'boot_id'))
        owned = same_incarnation and current == expected and detached(expected['pid'])
        owned_port = bool(owned and sockets and owns_listeners(expected['pid'], host, sockets))
        hangup_ignored = bool(owned and ignores_hangup(expected['pid']))
        live = bool(probe and owned_port and hangup_ignored and healthy(host, port, expected['pid']))
        if live:
            status = 'running'
        elif sockets and not owned_port:
            status = 'port_in_use'
        elif same_incarnation and not owned:
            status = 'identity_unverified'
        elif owned:
            status = 'starting' if saved['phase'] == 'starting' else 'unhealthy'
        else:
            status = 'stopped'
        return {'state': status, 'host': host, 'port': port, 'process': expected,
                'owned_process': bool(owned), 'owned_listener': owned_port, 'listeners': sockets,
                'ignores_sighup': hangup_ignored, 'healthy': live, 'log': str(self.log),
                'configured_host': self.host, 'configured_port': self.port}

    def status(self):
        return self.observe(self.saved())

    def start(self):
        previous = self.status()
        if previous['state'] == 'running' and (previous['host'], previous['port']) == (self.host, self.port):
            return previous
        if previous['state'] != 'stopped' or listeners(self.port):
            raise RuntimeError('MCP is already active or its port is occupied; inspect status before starting')
        command = self.command(self.host, self.port)
        with self.log.open('ab', buffering=0) as log:
            process = subprocess.Popen([NOHUP, *command], cwd=self.root, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + START_SECONDS
        saved = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'MCP launcher exited ({process.returncode}); see {self.log}')
            if saved is None:
                current = identity(process.pid)
                if current and current['boot_id'] and current['command'] == command:
                    saved = {'phase': 'starting', 'host': self.host, 'port': self.port,
                             'process': current, 'launch_command': [NOHUP, *command],
                             'started_at': now(), 'log': str(self.log)}
                    write_json(self.state_file, saved)
            if saved is not None:
                observed = self.observe(saved)
                if observed['healthy']:
                    saved.update(phase='running', healthy_at=now())
                    write_json(self.state_file, saved)
                    return observed
                if observed['state'] in ('port_in_use', 'identity_unverified'):
                    raise RuntimeError(f"MCP startup blocked ({observed['state']}); see {self.log}")
            time.sleep(.1)
        raise RuntimeError(f'MCP startup did not confirm PID, listener, SIGHUP and health; see {self.log}')

    def stop(self):
        saved = self.saved()
        observed = self.observe(saved, probe=False)
        if observed['state'] == 'stopped':
            return observed
        if not observed['owned_process'] or observed['state'] == 'port_in_use':
            raise RuntimeError('MCP process or listener ownership is unverified; no signal sent')
        pid = saved['process']['pid']
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return self.observe(saved)
        try:
            # pidfd pins this process even if its numeric PID is later reused.
            current = self.observe(saved, probe=False)
            if not current['owned_process'] or current['state'] == 'port_in_use':
                raise RuntimeError('MCP process or listener changed before signalling; no signal sent')
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except ProcessLookupError:
                pass
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(STOP_SECONDS * 1000):
                raise RuntimeError(f'MCP did not exit after SIGTERM; see {self.log}')
        finally:
            os.close(fd)
        saved.update(phase='stopped', stopped_at=now())
        write_json(self.state_file, saved)
        return self.observe(saved)

    def run(self, action):
        if action == 'status':
            return self.status()
        with self.lock():
            if action in ('stop', 'restart'):
                stopped = self.stop()
                if action == 'stop':
                    return stopped
            return self.start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='status', choices=('start', 'stop', 'restart', 'status'))
    args = parser.parse_args()
    try:
        if sys.platform != 'linux':
            raise RuntimeError('Run this command on the Ubuntu host')
        if args.action != 'status' and not (hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')):
            raise RuntimeError('MCP service management requires Linux pidfd support')
        os.umask(0o077)
        result = ServiceControl(Path(__file__).resolve().parents[1]).run(args.action)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    expected = 'stopped' if args.action == 'stop' else 'running'
    return 0 if result['state'] == expected else 1


if __name__ == '__main__':
    raise SystemExit(main())
