"""Durable, profile-local generation jobs, artifact references and delivery outbox.

This database belongs to the plugin, never to Hermes' conversation database.  All
remote side effects are performed by the finite worker, not by these read APIs.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import struct
import time
import uuid


TERMINAL = frozenset({'completed', 'partial', 'failed', 'cancelled', 'interrupted_by_restart'})
RATIOS = ('1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9')
SCOPE_FIELDS = ('profile', 'bot', 'platform', 'chat_id', 'thread_id', 'user_id')
CONFIG_DEFAULTS = {
    'ssh_host': '',
    'identity_file': '~/.ssh/id_ed25519',
    'remote_root': '',
    'lan_cidr': '', 'mac_address': '',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def scope_matches(record, caller):
    """Partial matching is useful to a profile's adapter pump; callers supply full scope."""
    if caller is None:
        return True
    return all(str(record.get(key) or '') == str(value or '')
               for key, value in caller.items() if key in SCOPE_FIELDS)


def require_scope(record, caller):
    if caller is not None and any(str(record.get(key) or '') != str(caller.get(key) or '') for key in SCOPE_FIELDS):
        raise PermissionError('Task or image belongs to another conversation/user')


def trusted_origin(origin):
    if not isinstance(origin, dict) or any(not origin.get(k) for k in ('profile', 'platform', 'chat_id', 'user_id')):
        raise ValueError('Trusted conversation origin is required')
    return origin


def normalize(args):
    if not isinstance(args, dict) or set(args) - {'prompt', 'preset', 'images', 'size', 'title', 'seed', 'count'}:
        raise ValueError('Only prompt, preset, images, size, title, seed and count are supported')
    prompt = args.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        raise ValueError('prompt must contain 1..8000 characters; never truncated')
    preset = args.get('preset', 'fast')
    if preset not in ('fast', 'quality'):
        raise ValueError('preset must be fast or quality; CFG/steps/LoRA overrides are disabled')
    images = args.get('images') or []
    if not isinstance(images, list) or len(images) > 10 or any(not isinstance(p, str) for p in images):
        raise ValueError('images must be an ordered list of 0..10 local PNG paths or image IDs')
    seed, count = args.get('seed', 0), args.get('count', 1)
    if type(seed) is not int or seed < 0 or type(count) is not int or not 1 <= count <= 4 or seed + count - 1 >= 2**64:
        raise ValueError('count must be 1..4 and seeds unsigned 64-bit; default seed is FIXED 0')
    title = args.get('title')
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 80):
        raise ValueError('title must contain 1..80 characters')
    size = args.get('size', 'auto')
    if size == 'auto':
        output_size = {'mode': 'follow_image'} if images else {'mode': 'explicit', 'width': 1024, 'height': 1024}
    elif size in RATIOS:
        output_size = {'mode': 'aspect_ratio', 'aspect_ratio': size, 'megapixels': 1, 'multiple': 32}
    elif isinstance(size, str) and re.fullmatch(r'\d{2,4}[xX×]\d{2,4}', size):
        width, height = map(int, re.split('[xX×]', size))
        if any(not 64 <= v <= 2048 or v % 32 for v in (width, height)):
            raise ValueError('Explicit width/height must be 64..2048 and multiples of 32')
        output_size = {'mode': 'explicit', 'width': width, 'height': height}
    else:
        raise ValueError('size must be auto, a supported aspect ratio, or WIDTHxHEIGHT')
    return {'prompt': prompt, 'preset': preset, 'images': images, 'size': size,
            'output_size': output_size, 'title': title.strip() if title else ' '.join(prompt.split())[:60],
            'title_source': 'request_intent' if title else 'prompt_excerpt', 'seed': seed, 'count': count}


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'jobs.sqlite3'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs(task_id TEXT PRIMARY KEY, created_at TEXT, state TEXT, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY, task_id TEXT, state TEXT, created_at TEXT, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(token TEXT PRIMARY KEY, task_id TEXT, expires_at REAL, consumed_at REAL, body TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def directory(self, task_id):
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError('Invalid task UUID')
        return self.root / 'jobs' / task_id

    def insert_job(self, job):
        with self.connection() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?)',
                       (job['task_id'], job['created_at'], job['state'], json.dumps(job, ensure_ascii=False)))
        atomic_json(self.directory(job['task_id']) / 'receipt.json', job)
        return job

    def get_job(self, task_id):
        self.directory(task_id)
        with self.connection() as db:
            row = db.execute('SELECT body FROM jobs WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def require_job(self, task_id):
        job = self.get_job(task_id)
        if job is None:
            raise ValueError('Unknown local task ID')
        return job

    def update_job(self, task_id, **fields):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown local task ID')
            job = {**json.loads(row[0]), **fields, 'updated_at': now()}
            db.execute('UPDATE jobs SET state=?,body=? WHERE task_id=?',
                       (job['state'], json.dumps(job, ensure_ascii=False), task_id))
        atomic_json(self.directory(task_id) / 'receipt.json', job)
        return job

    def jobs(self, origin=None, query='', limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        with self.connection() as db:
            rows = db.execute('SELECT body FROM jobs ORDER BY created_at DESC').fetchall()
        results = [json.loads(row[0]) for row in rows]
        return [j for j in results if scope_matches(j.get('origin', {}), origin)
                and (not query or query.casefold() in (j.get('title', '') + ' ' + j['task_id']).casefold())][:limit]

    def get_artifact(self, image_id):
        if not isinstance(image_id, str) or not re.fullmatch(r'img_\d{6,}', image_id):
            raise ValueError('Invalid image ID')
        with self.connection() as db:
            row = db.execute('SELECT body FROM artifacts WHERE id=?', (int(image_id[4:]),)).fetchone()
        if row is None:
            raise ValueError('Unknown image ID')
        value = json.loads(row[0])
        if not value.get('expired') and not Path(value['path']).is_file():
            value = self.update_artifact(image_id, expired=True, expired_at=now())
        return value

    def update_artifact(self, image_id, **fields):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM artifacts WHERE id=?', (int(image_id[4:]),)).fetchone()
            if row is None:
                raise ValueError('Unknown image ID')
            value = {**json.loads(row[0]), **fields}
            db.execute('UPDATE artifacts SET body=? WHERE id=?', (json.dumps(value, ensure_ascii=False), int(image_id[4:])))
        return value

    def artifacts(self, origin=None, query='', limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        with self.connection() as db:
            rows = db.execute('SELECT body FROM artifacts ORDER BY id DESC').fetchall()
        results = [json.loads(row[0]) for row in rows]
        selected = [v for v in results if scope_matches(v.get('origin', {}), origin)
                    and (not query or query.casefold() in (v.get('title', '') + ' ' + v['image_id']).casefold())][:limit]
        return [self.get_artifact(v['image_id']) for v in selected]

    def register_outputs(self, task_id, outputs):
        job = self.require_job(task_id)
        registered = []
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = {json.loads(row[0]).get('output_index'): json.loads(row[0]) for row in
                     db.execute('SELECT body FROM artifacts WHERE task_id=?', (task_id,))}
            for index, output in enumerate(outputs):
                if index in prior:
                    registered.append(prior[index])
                    continue
                value = {key: output[key] for key in ('path', 'width', 'height', 'sha256', 'remote_path', 'seed_used') if key in output}
                value.update(task_id=task_id, output_index=index, title=job['title'], title_source=job['request']['title_source'],
                             origin=job['origin'], preset=job['request']['preset'],
                             seed_used=output.get('seed_used', job['request']['seed'] + index),
                             reference_ids=job.get('reference_ids', []), created_at=now(), expired=False)
                cursor = db.execute('INSERT INTO artifacts(task_id,body) VALUES(?,?)', (task_id, '{}'))
                value['image_id'] = f'img_{cursor.lastrowid:06d}'
                db.execute('UPDATE artifacts SET body=? WHERE id=?', (json.dumps(value, ensure_ascii=False), cursor.lastrowid))
                registered.append(value)
        return registered

    def reconcile_retention(self, max_jobs=100):
        """Keep tombstone titles, but bound copied inputs of finished local jobs too."""
        if type(max_jobs) is not int or max_jobs < 1:
            raise ValueError('retention max_jobs must be positive')
        with self.connection() as db:
            images = [json.loads(row[0]) for row in db.execute('SELECT body FROM artifacts')]
            jobs = [json.loads(row[0]) for row in db.execute('SELECT body FROM jobs ORDER BY created_at DESC')]
        for artifact in images:
            if not artifact.get('expired') and not Path(artifact['path']).is_file():
                self.update_artifact(artifact['image_id'], expired=True, expired_at=now())
        finished = [job for job in jobs if job['state'] in TERMINAL]
        for job in finished[max_jobs:]:
            inputs = self.directory(job['task_id'])/'inputs'
            if inputs.is_dir():
                shutil.rmtree(inputs)
                self.update_job(job['task_id'], inputs_expired=True,
                                retry_allowed=False if job.get('input_paths') else job.get('retry_allowed', False))

    def enqueue(self, task_id, kind, payload, revision=None):
        job = self.require_job(task_id)
        revision = str(revision if revision is not None else uuid.uuid4().hex)
        event_id = hashlib.sha256(f'{task_id}:{kind}:{revision}'.encode()).hexdigest()[:32]
        event = {'event_id': event_id, 'task_id': task_id, 'kind': kind, 'revision': revision,
                 'state': 'pending', 'origin': job['origin'], 'created_at': now(), 'payload': payload}
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)',
                       (event_id, task_id, 'pending', event['created_at'], json.dumps(event, ensure_ascii=False)))
        return event

    def update_with_event(self, task_id, fields, kind, payload, revision):
        """Commit terminal state and its notification together, including after a crash."""
        event_id = hashlib.sha256(f'{task_id}:{kind}:{revision}'.encode()).hexdigest()[:32]
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM jobs WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown local task ID')
            job = {**json.loads(row[0]), **fields, 'updated_at': now()}
            event = {'event_id': event_id, 'task_id': task_id, 'kind': kind, 'revision': str(revision),
                     'state': 'pending', 'origin': job['origin'], 'created_at': now(), 'payload': payload}
            db.execute('UPDATE jobs SET state=?,body=? WHERE task_id=?', (job['state'], json.dumps(job, ensure_ascii=False), task_id))
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)',
                       (event_id, task_id, 'pending', event['created_at'], json.dumps(event, ensure_ascii=False)))
        atomic_json(self.directory(task_id)/'receipt.json', job)
        return job

    def pending_outbox(self, limit=50):
        with self.connection() as db:
            rows = db.execute("SELECT body FROM outbox WHERE state='pending' ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mark_outbox(self, event_id, state, **fields):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM outbox WHERE event_id=?', (event_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown outbox event')
            value = {**json.loads(row[0]), **fields, 'state': state, 'updated_at': now()}
            db.execute('UPDATE outbox SET state=?,body=? WHERE event_id=?',
                       (state, json.dumps(value, ensure_ascii=False), event_id))
        return value

    def claim_outbox(self, event_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT body FROM outbox WHERE event_id=? AND state='pending'", (event_id,)).fetchone()
            if row is None:
                return False
            value = {**json.loads(row[0]), 'state': 'sending', 'attempted_at': now()}
            db.execute("UPDATE outbox SET state='sending',body=? WHERE event_id=?", (json.dumps(value, ensure_ascii=False), event_id))
        return True

    def ack_outbox(self, event_id, message_id=None):
        return self.mark_outbox(event_id, 'delivered', message_id=message_id, delivered_at=now())

    def recover_outbox(self, origin=None):
        with self.connection() as db:
            rows = db.execute("SELECT event_id,body FROM outbox WHERE state='sending'").fetchall()
        count = 0
        for event_id, body in rows:
            if scope_matches(json.loads(body).get('origin', {}), origin):
                self.mark_outbox(event_id, 'delivery_uncertain', error='Gateway stopped during send; explicit resend is required')
                count += 1
        return count

    def create_action(self, task_id, action, origin=None, image_id=None):
        job = self.require_job(task_id)
        require_scope(job['origin'], origin)
        if action not in ('status', 'cancel', 'retry', 'resend', 'resume'):
            raise ValueError('Unsupported callback action')
        token = secrets.token_urlsafe(18)
        value = {'token': token, 'task_id': task_id, 'action': action, 'origin': job['origin'],
                 'image_id': image_id, 'created_at': now(), 'expires_at': time.time() + 86400}
        with self.connection() as db:
            db.execute('INSERT INTO actions VALUES(?,?,?,?,?)', (token, task_id, value['expires_at'], None, json.dumps(value, ensure_ascii=False)))
        return token

    def consume_action(self, token, origin):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body,consumed_at,expires_at FROM actions WHERE token=?', (token,)).fetchone()
            if row is None:
                raise ValueError('Unknown callback token')
            value = json.loads(row[0])
            require_scope(value['origin'], origin)
            if row[1] is not None:
                raise ValueError('Action already consumed; query the existing task')
            if row[2] < time.time():
                raise ValueError('Action expired; query current task status')
            db.execute('UPDATE actions SET consumed_at=? WHERE token=?', (time.time(), token))
        return value


class JobService:
    def __init__(self, ctx, data_dir):
        self.ctx = ctx
        self.store = Store(data_dir)

    def create(self, args, origin, *, retry_of=None, reference_ids=None):
        request = normalize(args)
        trusted_origin(origin)
        task_id = str(uuid.uuid4())
        directory = self.store.directory(task_id)
        directory.mkdir(parents=True, exist_ok=False)
        paths, references, total = [], [], 0
        try:
            for index, reference in enumerate(request['images'], 1):
                if reference.startswith('img_'):
                    artifact = self.store.get_artifact(reference)
                    require_scope(artifact['origin'], origin)
                    if artifact['expired']:
                        raise ValueError(f'{reference} has expired; provide an available reference image')
                    path = Path(artifact['path'])
                    references.append(reference)
                else:
                    path = Path(reference).expanduser()
                    if not path.is_absolute():
                        raise ValueError('Use an image ID or absolute local PNG path')
                if path.suffix.lower() != '.png' or path.stat().st_size > 20 * 1024**2:
                    raise ValueError('Each input must be a local PNG of at most 20 MiB')
                data = path.read_bytes()
                if len(data) > 20 * 1024**2 or len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR' or not all(struct.unpack('>II', data[16:24])):
                    raise ValueError('Invalid input PNG header')
                if index == 1 and request['size'] == 'auto':
                    width, height = struct.unpack('>II', data[16:24])
                    if any(not 64 <= value <= 2048 or value % 32 for value in (width, height)):
                        raise ValueError('auto requires the original first image width/height to be 64..2048 and multiples of 32; choose an explicit size or aspect ratio')
                    # The saved edit template's follow_image observes its resized
                    # reference, so freeze original dimensions before preprocessing.
                    request['output_size'] = {'mode': 'explicit', 'width': width, 'height': height}
                total += len(data)
                if total > 100 * 1024**2:
                    raise ValueError('Combined inputs exceed 100 MiB')
                destination = directory / 'inputs' / f'{index}.png'
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                paths.append(str(destination))
            config = {key: self.ctx.get_config(key, default) for key, default in CONFIG_DEFAULTS.items()}
            atomic_json(directory / 'request.json', args)
            atomic_json(directory / 'config.json', config)
            job = {'task_id': task_id, 'created_at': now(), 'updated_at': now(), 'state': 'accepted',
                   'stage': 'accepted', 'title': request['title'], 'origin': dict(origin), 'request': request,
                   'input_paths': paths, 'reference_ids': references, 'submission_attempted': False,
                   'remote_task_id': None, 'cancel_requested': False, 'artifacts': []}
            if retry_of:
                job.update(retry_of=retry_of, reference_ids=reference_ids or [])
            self.store.insert_job(job)
        except Exception:
            shutil.rmtree(directory)
            raise
        from .worker import launch
        try:
            launch(self.store, task_id)
        except Exception as exc:
            self.store.update_job(task_id, state='failed', stage='worker_start', error=str(exc), retry_allowed=True)
            self.store.enqueue(task_id, 'failed', {'message': f'后台任务启动失败：{exc}', 'buttons': [{'label': '重试', 'action': 'retry'}]}, revision='worker_start')
            return {**self.status(task_id, origin), 'success': False}
        return self.status(task_id, origin)

    def status(self, task_id, origin=None):
        trusted_origin(origin)
        job = self.store.require_job(task_id)
        require_scope(job['origin'], origin)
        from .worker import matches
        artifacts = [self.store.get_artifact(a['image_id']) for a in job.get('artifacts', [])]
        completed_at = (job.get('finished_at') or job.get('observer_finished_at')) if job['state'] in TERMINAL else None
        elapsed_until = datetime.fromisoformat(completed_at).timestamp() if completed_at else time.time()
        result = {'success': True, 'task_id': task_id, 'state': job['state'], 'stage': job.get('stage'),
                'title': job['title'], 'preset': job['request']['preset'], 'seed_used': job['request']['seed'],
                'count': job['request']['count'], 'artifacts': artifacts, 'worker_running': matches(job.get('worker')),
                'elapsed_seconds': round(max(0, elapsed_until-datetime.fromisoformat(job['created_at']).timestamp()), 3),
                'remote_task_id': job.get('remote_task_id'), 'error': job.get('error'),
                'cancel_requested': job.get('cancel_requested', False), 'receipt_path': str(self.store.directory(task_id)/'receipt.json')}
        result.update({key: value for key, value in job.get('last_remote_status', {}).items()
                       if key in ('queued_seconds', 'running_seconds', 'last_progress_at', 'stalled', 'stall_reason',
                                  'cancel_requested_at', 'cancellation_confirmed')})
        return result

    def list(self, query='', limit=20, origin=None):
        trusted_origin(origin)
        return {'success': True, 'tasks': [{k: j.get(k) for k in ('task_id', 'title', 'state', 'created_at', 'artifacts')}
                                         for j in self.store.jobs(origin=origin, query=query, limit=limit)],
                'artifacts': self.store.artifacts(origin=origin, query=query, limit=limit)}

    def cancel(self, task_id, origin=None):
        trusted_origin(origin)
        job = self.store.require_job(task_id)
        require_scope(job['origin'], origin)
        if job['state'] not in TERMINAL:
            self.store.update_job(task_id, cancel_requested=True)
            self.store.enqueue(task_id, 'progress', {'message': '已请求取消；将核实任务是否真正停止。'}, revision='cancel_requested')
            from .worker import launch, matches
            if not matches(job.get('worker')):
                launch(self.store, task_id)
        return self.status(task_id, origin)

    def retry(self, task_id, origin=None):
        trusted_origin(origin)
        job = self.store.require_job(task_id)
        require_scope(job['origin'], origin)
        if job['state'] not in ('failed', 'cancelled', 'interrupted_by_restart') or not job.get('retry_allowed'):
            raise ValueError('Retry is unsafe or unnecessary; inspect/resume the original task instead')
        request = {key: job['request'][key] for key in ('prompt', 'preset', 'size', 'title', 'seed', 'count')}
        request['images'] = job['input_paths']
        return self.create(request, job['origin'], retry_of=task_id, reference_ids=job.get('reference_ids', []))

    def resend(self, image_id, origin=None):
        trusted_origin(origin)
        artifact = self.store.get_artifact(image_id)
        require_scope(artifact['origin'], origin)
        if artifact['expired']:
            raise ValueError('Image expired; it cannot be resent')
        event = self.store.enqueue(artifact['task_id'], 'resend', {'artifacts': [artifact], 'text': artifact['title']})
        return {'success': True, 'task_id': artifact['task_id'], 'image_id': image_id, 'event_id': event['event_id'], 'state': 'pending_delivery'}

    def resume(self, task_id, origin=None):
        trusted_origin(origin)
        job = self.store.require_job(task_id)
        require_scope(job['origin'], origin)
        from .worker import launch
        launch(self.store, task_id)
        return self.status(task_id, origin)

    def apply_action(self, token, origin):
        trusted_origin(origin)
        action = self.store.consume_action(token, origin)
        if action['action'] == 'resend':
            return self.resend(action['image_id'], origin)
        return getattr(self, action['action'])(action['task_id'], origin=origin)

    def recover_observers(self, origin_filter):
        """Reconnect may resume known tasks once, never recreate their submissions."""
        if not isinstance(origin_filter, dict) or any(not origin_filter.get(k) for k in ('profile', 'platform', 'bot')):
            raise ValueError('Observer recovery requires a trusted profile/platform/bot binding')
        from .worker import launch, matches
        with self.store.connection() as db:
            rows = db.execute('SELECT task_id,body FROM jobs ORDER BY created_at').fetchall()
        recovered = []
        for task_id, body in rows:
            candidate = json.loads(body)
            if (not scope_matches(candidate.get('origin', {}), origin_filter)
                    or candidate['state'] in TERMINAL | {'stalled', 'submission_uncertain'}
                    or not candidate.get('remote_task_id') or candidate.get('observer_stopped_at')
                    or candidate.get('observer_recovery_attempted_at') or matches(candidate.get('worker'))):
                continue
            # Claim in SQLite before launch: concurrent adapter reconnects cannot
            # repeatedly revive an unhealthy observer or recreate its submission.
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT body FROM jobs WHERE task_id=?', (task_id,)).fetchone()
                job = json.loads(row[0])
                if (job['state'] in TERMINAL | {'stalled', 'submission_uncertain'}
                        or not job.get('remote_task_id') or job.get('observer_stopped_at')
                        or job.get('observer_recovery_attempted_at') or matches(job.get('worker'))):
                    continue
                job.update(observer_recovery_attempted_at=now(), observer_recovery_count=1,
                           submission_attempted=True, updated_at=now())
                db.execute('UPDATE jobs SET body=? WHERE task_id=?', (json.dumps(job, ensure_ascii=False), task_id))
            try:
                if launch(self.store, task_id).get('started'):
                    recovered.append(task_id)
            except Exception as exc:
                self.store.update_job(task_id, observer_recovery_error=str(exc))
                self.store.enqueue(task_id, 'failed', {'message': '恢复任务观察失败；原生成未重提。',
                    'buttons': [{'label': '恢复观察', 'action': 'resume'}]}, revision='observer_recovery_failed')
        return recovered
