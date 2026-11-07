import base64
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .metadata import client_metadata


class Transport:
    def __init__(self, ctx, data_dir):
        self.ctx = ctx
        self.data_dir = Path(data_dir)

    def call(self, packet):
        root = self.ctx.get_config('remote_root', '')
        host = self.ctx.get_config('ssh_host', '')
        identity = self.ctx.get_config('identity_file', '~/.ssh/id_ed25519')
        user, separator, address = host.partition('@')
        if not separator or not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
            raise ValueError('Configure ssh_host as a trusted user@IPv4 address')
        ipaddress.IPv4Address(address)
        if not Path(root).is_absolute() or '\0' in root:
            raise ValueError('Configure remote_root as an absolute Ubuntu module path; ~ is not expanded remotely')
        if not identity or not Path(identity).expanduser().is_absolute():
            raise ValueError('identity_file must be an absolute local path or begin with ~/')
        code = Path(__file__).with_name('remote_bridge.py').read_text()
        command = ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
                   '-o', 'StrictHostKeyChecking=yes']
        if identity:
            command += ['-i', str(Path(identity).expanduser()), '-o', 'IdentitiesOnly=yes']
        command += [host, 'cd ' + shlex.quote(root) + ' && ' + shlex.quote(root + '/.venv/bin/python') + ' -c ' + shlex.quote(code)]
        process = subprocess.run(command, input=json.dumps({'root': root, 'client_metadata': client_metadata(), **packet}),
            capture_output=True, text=True, timeout=110)
        if not process.stdout.strip():
            raise RuntimeError(f'SSH failed ({process.returncode}); do not blindly retry submissions: {process.stderr[:500]}')
        response = json.loads(process.stdout)
        if not response.get('ok'):
            error = response['error']
            if error.get('task_id'):
                self.record(error['task_id'], {'error': error})
            return {'success': False, 'error': error, 'metadata': response.get('metadata', {})}
        return {'success': True, 'result': response['result'], 'metadata': response.get('metadata', {})}

    def select_edit_seed(self, request, blobs, count):
        """Legacy submissions share the same profile-local provenance constraint."""
        from .jobs import Store
        from . import seed_guard
        store = Store(self.data_dir)
        identities = [seed_guard.fingerprint(base64.b64decode(b, validate=True)) for b in blobs]
        with store.connection() as db:
            policy = seed_guard.select_seed(db, identities, request.get('seed'), count)
        return {**request, 'seed': policy['selected_seed']}, policy

    def freeze_legacy_seed(self, policy, blobs, count):
        """Persist a minimal seed intent before an uncertain legacy remote call."""
        intent_id = str(uuid.uuid4())
        path = self.data_dir/'seed-intents'/(intent_id+'.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {'intent_id': intent_id, 'created_at': datetime.now(timezone.utc).isoformat(),
                 'state': 'submission_started', 'seed_policy': policy, 'count': count,
                 'input_sha256': [hashlib.sha256(base64.b64decode(b, validate=True)).hexdigest() for b in blobs]}
        with path.open('x') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        return intent_id

    def release_legacy_seed(self, intent_id):
        # Only remove after the remote receipt durably records the chosen seed.
        (self.data_dir/'seed-intents'/(intent_id+'.json')).unlink(missing_ok=True)

    def directory(self, task_id):
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError('Invalid task UUID')
        return self.data_dir / 'tasks' / task_id

    def record(self, task_id, value):
        directory = self.directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / '.receipt.tmp'
        prior = json.loads((directory/'receipt.json').read_text()) if (directory/'receipt.json').exists() else {}
        combined = {**prior, **value}
        for key in ('original_request', 'request_used', 'input_images', 'submitted_metadata'):
            if key in prior:
                combined[key] = prior[key]
        temporary.write_text(json.dumps(combined, ensure_ascii=False, indent=2))
        os.replace(temporary, directory / 'receipt.json')

    def save_inputs(self, task_id, blobs):
        directory = self.directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        images = []
        for index, blob in enumerate(blobs, 1):
            data = base64.b64decode(blob, validate=True)
            path = directory / f'input-{index}.png'
            path.write_bytes(data)
            images.append({'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()})
        return images

    def history(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        records = []
        for path in (self.data_dir/'tasks').glob('*/receipt.json'):
            value = json.loads(path.read_text())
            request = value.get('request_used', {})
            records.append({'task_id': path.parent.name, 'created_at': value.get('created_at', ''),
                'state': value.get('state', 'unknown'), 'preset': request.get('preset'),
                'seed_used': value.get('seed_used', request.get('seed')), 'count': value.get('count', 1),
                'output_seeds': [o.get('seed_used') for o in value.get('outputs', [])],
                'prompt_summary': request.get('prompt', '')[:100],
                'paths': [o['path'] for o in value.get('outputs', [])]})
        return sorted(records, key=lambda r: r['created_at'], reverse=True)[:limit]

    def images(self, paths):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
            raise ValueError('Provide 1..10 local PNG paths')
        blobs = []
        total = 0
        for value in paths:
            path = Path(value).expanduser()
            if not path.is_absolute() or path.suffix.lower() != '.png':
                raise ValueError('Use absolute local PNG paths')
            if path.stat().st_size > 20 * 1024**2:
                raise ValueError('Each PNG must be at most 20 MiB')
            data = path.read_bytes()
            if data[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError('Invalid PNG')
            total += len(data)
            if total > 100 * 1024**2:
                raise ValueError('Total input must be at most 100 MiB')
            blobs.append(base64.b64encode(data).decode())
        return blobs

    def download(self, result):
        directory = self.directory(result['task_id'])
        directory.mkdir(parents=True, exist_ok=True)
        for index, output in enumerate(result['outputs'], 1):
            data = base64.b64decode(output.pop('png_base64'), validate=True)
            if hashlib.sha256(data).hexdigest() != output['sha256']:
                raise ValueError('Downloaded PNG checksum mismatch')
            if data[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', data[16:24]) != (output['width'], output['height']):
                raise ValueError('Downloaded PNG dimensions mismatch')
            path = directory / f'{index}.png'
            temporary = path.with_suffix('.download')
            temporary.write_bytes(data)
            os.replace(temporary, path)
            output['remote_path'] = output['path']
            output['path'] = str(path)
        self.record(result['task_id'], result)
        if result.get('workflow') in ('qwen-image-2.1-edit', 'qwen-image-2.1-t2i'):
            from .jobs import Store
            store = Store(self.data_dir)
            with store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                for output in result['outputs']:
                    seed = output.get('seed_used')
                    if (seed is None and len(result['outputs']) == 1
                            and result.get('count', result.get('request_used', {}).get('count', 1)) == 1
                            and len(result.get('children', [])) <= 1):
                        seed = result.get('seed_used')
                    if seed is not None:
                        store.register_provenance(db, output, seed)
        # Match the backend's count cap for local downloaded copies as well.
        cap = result.get('cleanup', {}).get('max_tasks', 100)
        completed = [p for p in (self.data_dir / 'tasks').iterdir() if p.is_dir() and not p.is_symlink() and (p / '1.png').is_file()]
        total = sum(len(list(p.glob('[0-9]*.png'))) for p in completed)
        for path in sorted(completed, key=lambda p: (p / 'receipt.json').stat().st_mtime):
            if total <= cap:
                break
            if path != directory:
                total -= len(list(path.glob('[0-9]*.png')))
                shutil.rmtree(path)
        return {'success': True, 'image': result['outputs'][0]['path'], 'modality': 'image',
                'media': ['MEDIA:' + o['path'] for o in result['outputs']], **result}
