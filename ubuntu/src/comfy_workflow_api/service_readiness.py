"""Read-only readiness evidence for the locally managed ComfyUI instance."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

from .client import Client
from .settings import service_paths
from .sleep_guard import identity, matches
from .storage import now, read_json


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def process_identity(pid):
    item = identity(pid)
    if item:
        item['boot_id'] = boot_id()
    return item


def process_matches(item):
    return bool(item and (not item.get('boot_id') or item['boot_id'] == boot_id()) and matches({k:v for k,v in item.items() if k != 'boot_id'}))


def group(pid):
    try:
        fields = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[2]), int(fields[3])
    except (OSError, ValueError):
        return None


def listeners(port):
    """Keep every LISTEN socket, including ones with unreadable process ownership."""
    sockets = {}
    for name in ('tcp', 'tcp6'):
        try:
            lines = (Path('/proc/net')/name).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for line in lines:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == port:
                sockets[fields[9]] = {'inode': fields[9], 'family': name,
                                      'address': fields[1], 'state': 'LISTEN', 'owners': []}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            found = set()
            for fd in (proc/'fd').iterdir():
                target = os.readlink(fd)
                if target.startswith('socket:['):
                    inode = target[8:-1]
                    if inode in sockets:
                        found.add(inode)
            owner = process_identity(int(proc.name)) if found else None
            for inode in found:
                if owner:
                    sockets[inode]['owners'].append(owner)
        except (OSError, ProcessLookupError):
            continue
    return list(sockets.values())


def resume_evidence():
    # BOOTTIME-MONOTONIC increases on suspend even when the kernel journal is private.
    # Round milliseconds to avoid measurement jitter; kernel boot identity scopes it.
    evidence = {'boot_id': boot_id(), 'resume_id': None, 'source': 'unknown', 'observed_at': now()}
    if hasattr(time, 'CLOCK_BOOTTIME'):
        offset = time.clock_gettime(time.CLOCK_BOOTTIME)-time.clock_gettime(time.CLOCK_MONOTONIC)
        evidence.update(suspend_offset_seconds=round(offset, 2),
                        resume_id=f"{evidence['boot_id']}:{round(offset, 2)}", source='boottime_minus_monotonic')
    try:
        result = subprocess.run(['journalctl', '-b', '-k', '-g', 'PM: suspend exit', '-n', '1',
                                 '-o', 'json', '--no-pager'], capture_output=True, text=True, timeout=2)
        entries = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        if result.returncode == 0 and entries:
            item = entries[-1]
            evidence.update(last_resume_at=item.get('__REALTIME_TIMESTAMP'), journal_cursor=item.get('__CURSOR'))
            if item.get('__CURSOR'):

                if evidence['resume_id'] is None:
                    evidence.update(resume_id=f"{evidence['boot_id']}:{item['__CURSOR']}", source='kernel_journal')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return evidence


def same_resume(left, right):
    if left is None or right is None:
        return False
    if left == right:
        return True
    # Clock subtraction is not atomic. Tolerate measurement jitter without
    # masking a practical suspend interval, and keep it scoped to one boot.
    try:
        boot_a, value_a = left.split(':', 1)
        boot_b, value_b = right.split(':', 1)
        return boot_a == boot_b and abs(float(value_a)-float(value_b)) <= .05
    except (AttributeError, ValueError):
        return False


def instance_id(state):
    if state.get('service_instance_id'):
        return state['service_instance_id']
    process = state.get('process') or {}
    if not process.get('pid'):
        return None
    raw = [process.get('boot_id') or boot_id(), process.get('pid'), process.get('start_ticks'), process.get('uid')]
    return 'legacy-'+hashlib.sha256(json.dumps(raw).encode()).hexdigest()[:24]


def workflow_hash(root, run_script):
    paths = [root/'workflows/manifest.json', root/'scripts/comfyui-service.py', run_script]
    paths += sorted((root/'src/comfy_workflow_api').glob('*.py'))
    value = hashlib.sha256()
    for path in paths:
        value.update(str(path.name).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def known_members(state):
    items = [state.get('process')] + state.get('managed_processes', [])
    return {item['pid']: item for item in items if item and process_matches(item)}


def owned_owner(owner, state, live):
    root = state.get('process') or {}
    pgid = state.get('process_group', root.get('pid'))
    return bool(owner and owner.get('uid') == os.getuid() and process_matches(owner) and
                group(owner['pid']) == (pgid, pgid) and
                (owner['pid'] in live or (live and int(owner.get('start_ticks', 0)) >= int(root.get('start_ticks', 0)))))


def health(client):
    observed = now()
    try:
        system = client.request('/system_stats')
        queue = client.request('/queue')
        queue_known = isinstance(queue, dict) and all(isinstance(queue.get(k), list) for k in ('queue_running','queue_pending'))
        return {'http_healthy': isinstance(system, dict) and 'system' in system and queue_known,
                'system': system, 'queue': queue, 'queue_known': queue_known,
                'http_observed_at': observed}
    except Exception as exc:
        return {'http_healthy': False, 'queue_known': False, 'error': str(exc), 'http_observed_at': observed}


def render_verification(state, data, service_instance_id, resume_id, current_hash):
    verification = state.get('verification') or {}
    try:
        same_workflow = verification.get('workflow_hash') == current_hash()
    except OSError:
        same_workflow = False
    verified = bool(verification.get('success') and verification.get('service_instance_id') == service_instance_id
                    and same_resume(verification.get('resume_id'), resume_id) and same_workflow)
    invalidation = None
    invalidated = False
    invalidation_path = data/'invalidations'/f"{service_instance_id}.json"
    if invalidation_path.exists():
        try:
            invalidation = read_json(invalidation_path)
            if invalidation.get('service_instance_id') == service_instance_id:
                invalidated = datetime.fromisoformat(invalidation['observed_at']).timestamp() >= datetime.fromisoformat(verification.get('verified_at', '')).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            invalidated = True  # Unknown invalidation freshness cannot certify rendering.
    return verified and not invalidated, invalidation if invalidated else None


def ready_instance(api):
    """Return the verified instance ID, or None when evidence is incomplete."""
    configured = api.settings['base_url'].rstrip('/')
    target = urlsplit(configured)
    if (target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost', '::1')
            or api.client.base_url != configured):
        return None
    data = api.data/'comfyui-service'
    try:
        state = read_json(data/'state.json')
        instance = state.get('service_instance_id')
        if not instance or state.get('phase') == 'stopping':
            return None
        _, run_script, _ = service_paths(api.root, api.settings)
        verified, _ = render_verification(state, data, instance, resume_evidence().get('resume_id'),
                                          lambda: workflow_hash(api.root, run_script))
        if not verified:
            return None
        live = known_members(state)
        sockets = listeners(target.port or 80)
        if not (live and sockets and all(item['owners'] and all(
                owned_owner(owner, state, live) for owner in item['owners']) for item in sockets)):
            return None
        return instance if health(Client(configured, timeout=2))['http_healthy'] else None
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
