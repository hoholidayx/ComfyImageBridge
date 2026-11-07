"""Durable single-image jobs shared by service adapters."""

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import uuid

from .api import OutputUnavailable
from .client import APIError, png_size
from .contract import MAX_PROMPT_LENGTH, PRESETS
from .lifecycle import BackendUnavailable
from .retention import cleanup_lock
from .service_readiness import ready_instance
from .sleep_cycle import SleepPending, assert_allowed
from .storage import read_json


MAX_IMAGES = 10
MAX_IMAGE_BYTES = 20 * 1024**2
MAX_IMAGE_TOTAL_BYTES = 32 * 1024**2
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 32_000_000
UINT64 = 2**64
RATIOS = frozenset(('1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9'))
SHA256 = re.compile(r'[0-9a-f]{64}')
ACTIVE = ('pending', 'submitting', 'queued', 'running', 'submission_uncertain')
LOG = logging.getLogger(__name__)


class ImageJobError(RuntimeError):
    """A stable service error whose text is safe to return to the caller."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _task_id(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise ImageJobError('invalid_request', 'Use a canonical UUID for the request or task ID')


def _output_size(size, edit):
    if size == 'auto':
        return {'mode': 'follow_image'} if edit else {'mode': 'explicit', 'width': 1024, 'height': 1024}
    if isinstance(size, str):
        if size in RATIOS:
            return {'mode': 'aspect_ratio', 'aspect_ratio': size}
        match = re.fullmatch(r'([0-9]{2,4})x([0-9]{2,4})', size)
        if match:
            width, height = map(int, match.groups())
            if all(64 <= axis <= 2048 and axis % 32 == 0 for axis in (width, height)):
                return {'mode': 'explicit', 'width': width, 'height': height}
    raise ImageJobError('invalid_request', 'Size must be auto, a supported ratio, or aligned width x height')


def _image_hashes(images):
    if len(images) > MAX_IMAGES:
        raise ImageJobError('invalid_request', 'Use at most ten ordered reference images')
    total = 0
    hashes = []
    for data in images:
        if not isinstance(data, bytes) or len(data) > MAX_IMAGE_BYTES:
            raise ImageJobError('invalid_request', 'Each reference must be PNG bytes of at most 20 MiB')
        total += len(data)
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise ImageJobError('invalid_request', 'References exceed 32 MiB in total')
        try:
            width, height = png_size(data)
        except ValueError as exc:
            raise ImageJobError('invalid_request', 'Each reference must be a PNG') from exc
        if not (0 < width <= MAX_IMAGE_SIDE and 0 < height <= MAX_IMAGE_SIDE
                and width * height <= MAX_IMAGE_PIXELS):
            raise ImageJobError('invalid_request', 'Reference image dimensions exceed supported limits')
        hashes.append(hashlib.sha256(data).hexdigest())
    return hashes


def _request(prompt, preset, size, seed, images):
    workflow = 'qwen-image-2.1-edit' if images else 'qwen-image-2.1-t2i'
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_LENGTH:
        raise ImageJobError('invalid_request', f'Prompt must contain 1–{MAX_PROMPT_LENGTH} characters')
    if not isinstance(preset, str) or preset not in PRESETS[workflow]:
        raise ImageJobError('invalid_request', 'Preset must be fast or quality')
    if seed is not None and (not isinstance(seed, str) or not re.fullmatch(r'[0-9]{1,20}', seed)
                             or int(seed) >= UINT64):
        raise ImageJobError('invalid_request', 'Seed must be a decimal unsigned 64-bit integer string')
    return {'workflow': workflow, 'prompt': prompt, 'preset': preset,
            'output_size': _output_size(size, bool(images)),
            'seed': str(int(seed)) if seed is not None else None, 'image_hashes': _image_hashes(images)}


class ImageJobs:
    def __init__(self, api, readiness=None):
        self.api = api
        self.readiness = readiness
        self.home = api.data / 'image-jobs'
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.home / 'jobs.sqlite3'
        self.max_pending = api.settings.get('mcp', {}).get('max_pending_tasks', 8)
        self.max_results = api.settings['retention']['max_tasks']
        if type(self.max_pending) is not int or self.max_pending < 1:
            raise ValueError('mcp.max_pending_tasks must be a positive integer')
        if type(self.max_results) is not int or self.max_results < 1:
            raise ValueError('retention.max_tasks must be a positive integer')
        self.poll_interval = api.settings['poll_interval']
        self.reconcile_seconds = api.settings['wait_timeout']
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT,
                    created REAL NOT NULL, state TEXT NOT NULL, error_code TEXT,
                    backend_id TEXT, prompt_id TEXT, chosen_seed TEXT,
                    next_check REAL DEFAULT 0, uncertain_since REAL, output_wait_since REAL,
                    ended REAL, result TEXT, png BLOB
                );
                CREATE INDEX IF NOT EXISTS jobs_work ON jobs(state, next_check, created);
                CREATE INDEX IF NOT EXISTS jobs_retention ON jobs(state, ended);
                CREATE TABLE IF NOT EXISTS references_png (
                    task_id TEXT NOT NULL, position INTEGER NOT NULL, png BLOB NOT NULL,
                    PRIMARY KEY (task_id, position)
                );
                CREATE TABLE IF NOT EXISTS image_seeds (
                    sha256 TEXT NOT NULL, seed TEXT NOT NULL, PRIMARY KEY (sha256, seed)
                );
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY);
            ''')
        self._backfill()

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def _backfill(self):
        with self._db() as db:
            if db.execute("SELECT 1 FROM settings WHERE name='seed_backfill_v1'").fetchone():
                return
        # Only SHA256 and actual seed from the SDK's verified output receipts are imported.
        # Preserve lock ordering with worker fetch: output cleanup, then image jobs DB.
        with cleanup_lock(self.api.data), self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM settings WHERE name='seed_backfill_v1'").fetchone():
                return
            for receipt in (self.api.data / 'runs').glob('*/result.json'):
                try:
                    value = read_json(receipt)
                    if value.get('workflow') not in PRESETS:
                        continue
                    for output in value.get('outputs', []):
                        sha256, seed = output.get('sha256'), output.get('seed_used', value.get('seed_used'))
                        if (isinstance(sha256, str) and SHA256.fullmatch(sha256)
                                and type(seed) is int and 0 <= seed < UINT64):
                            db.execute('INSERT OR IGNORE INTO image_seeds VALUES (?, ?)', (sha256, str(seed)))
                except (OSError, ValueError, AttributeError, TypeError):
                    continue
            db.execute("INSERT INTO settings VALUES ('seed_backfill_v1')")

    @staticmethod
    def _public(row):
        result = {'task_id': row['task_id'], 'state': row['state']}
        if row['error_code'] is not None:
            result['error_code'] = row['error_code']
        return result

    @staticmethod
    def _existing(row, fingerprint):
        if row['fingerprint'] != fingerprint:
            raise ImageJobError('request_conflict', 'Request ID was used with different image parameters')
        return ImageJobs._public(row)

    def _find(self, db, task_id):
        return db.execute('SELECT task_id, fingerprint, state, error_code FROM jobs WHERE task_id=?',
                          (task_id,)).fetchone()

    def _check_ready(self):
        try:
            assert_allowed(self.api)
            ready = self.readiness() if self.readiness is not None else ready_instance(self.api)
        except SleepPending:
            ready = False
        if not ready:
            raise ImageJobError('backend_unavailable', 'ComfyUI is unavailable for generation')

    def _admission_ready(self, instance_id):
        if self.readiness is not None:
            return bool(self.readiness())
        return instance_id is not None and ready_instance(self.api) == instance_id

    def submit(self, request_id, prompt, preset='fast', size='auto', seed=None, images=()):
        task_id = _task_id(request_id)
        images = tuple(images)
        request = json.dumps(_request(prompt, preset, size, seed, images), sort_keys=True)
        fingerprint = hashlib.sha256(request.encode()).hexdigest()
        with self._db() as db:
            row = self._find(db, task_id)
        if row is not None:
            return self._existing(row, fingerprint)
        try:
            self._check_ready()
        except ImageJobError:
            with self._db() as db:
                row = self._find(db, task_id)
            if row is not None:
                return self._existing(row, fingerprint)
            raise
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._find(db, task_id)
            if row is not None:
                return self._existing(row, fingerprint)
            pending = db.execute('SELECT count(*) FROM jobs WHERE state IN (?, ?, ?, ?, ?) AND next_check IS NOT NULL',
                                 ACTIVE).fetchone()[0]
            if pending >= self.max_pending:
                raise ImageJobError('queue_full', 'The image queue is full')
            db.execute("INSERT INTO jobs (task_id, fingerprint, request, created, state) VALUES (?, ?, ?, ?, 'pending')",
                       (task_id, fingerprint, request, time.time()))
            db.executemany('INSERT INTO references_png VALUES (?, ?, ?)',
                           ((task_id, index, image) for index, image in enumerate(images)))
        return {'task_id': task_id, 'state': 'pending'}

    def status(self, task_id):
        with self._db() as db:
            row = self._find(db, _task_id(task_id))
        if row is None:
            raise ImageJobError('task_not_found', 'Image task was not found')
        return self._public(row)

    def result(self, task_id):
        with self._db() as db:
            row = db.execute('SELECT state, result, png FROM jobs WHERE task_id=?', (_task_id(task_id),)).fetchone()
        if row is None:
            raise ImageJobError('task_not_found', 'Image task was not found')
        if row['state'] == 'expired':
            raise ImageJobError('result_expired', 'Image result has expired')
        if row['state'] != 'completed':
            raise ImageJobError('result_not_ready', 'Image result is unavailable for this task state')
        return json.loads(row['result']), row['png']

    def _chosen_seed(self, db, request):
        seed = int(request['seed']) if request['seed'] is not None else None
        if not request['image_hashes']:
            return seed if seed is not None else 0
        forbidden = set()
        for sha256 in request['image_hashes']:
            forbidden.update(int(row[0]) for row in db.execute(
                'SELECT seed FROM image_seeds WHERE sha256=?', (sha256,)))
        if seed is not None and seed not in forbidden:
            return seed
        while True:
            seed = secrets.randbits(64)
            if seed not in forbidden:
                return seed

    def _before_prompt(self, task_id, backend_id, prompt_id):
        # The saved SDK output must survive cleanup even if the process dies
        # after fetching it and before committing the MCP result transaction.
        self.api.pin_result(backend_id, True)
        with self._db() as db:
            db.execute('UPDATE jobs SET prompt_id=? WHERE task_id=?', (prompt_id, task_id))

    def _release_pin(self, backend_id):
        if backend_id is not None:
            try:
                self.api.pin_result(backend_id, False)
            except OSError:
                LOG.error('Unable to release result retention pin')

    def _submit_pending(self, job):
        task_id = job['task_id']
        request = json.loads(job['request'])
        backend_id = str(uuid.uuid4())
        with self._db() as db:
            seed = int(job['chosen_seed']) if job['chosen_seed'] is not None else self._chosen_seed(db, request)
            db.execute("UPDATE jobs SET state='submitting', backend_id=?, chosen_seed=? WHERE task_id=?",
                       (backend_id, str(seed), task_id))
        workflow = request['workflow']
        effective = {key: request[key] for key in ('prompt', 'preset', 'output_size')}
        effective['seed'] = seed
        try:
            with tempfile.TemporaryDirectory(dir=self.home, prefix='submit-') as scratch:
                if request['image_hashes']:
                    effective['images'] = []
                    with self._db() as db:
                        for reference in db.execute('SELECT position, png FROM references_png WHERE task_id=? ORDER BY position',
                                                    (task_id,)):
                            path = Path(scratch) / f"{reference['position'] + 1}.png"
                            path.write_bytes(reference['png'])
                            effective['images'].append(str(path))
                    if len(effective['images']) != len(request['image_hashes']):
                        raise ValueError('Saved references are incomplete')
                self.api.submit(workflow, effective, task_id=backend_id, readiness=self._admission_ready,
                                before_prompt=lambda prompt_id: self._before_prompt(task_id, backend_id, prompt_id))
        except (SleepPending, BackendUnavailable):
            self._finish(task_id, 'failed', 'backend_unavailable')
            return
        except (ValueError, OSError, RuntimeError):
            # The durable marker differentiates an attempted POST from preparation.
            with self._db() as db:
                dispatched = db.execute('SELECT prompt_id FROM jobs WHERE task_id=?', (task_id,)).fetchone()[0]
            if dispatched:
                self._record_state(task_id, 'submission_uncertain', 'submission_uncertain')
            else:
                self.api.abandon_unsubmitted(backend_id)
                self._finish(task_id, 'failed', 'generation_failed')
            return
        self._record_state(task_id, 'queued')

    def _record_state(self, task_id, state, code=None):
        now = time.time()
        with self._db() as db:
            job = db.execute('SELECT backend_id, uncertain_since FROM jobs WHERE task_id=?', (task_id,)).fetchone()
            if state == 'submission_uncertain':
                since = job['uncertain_since']
                since = since if since is not None else now
                next_check = now + self.poll_interval if now - since < self.reconcile_seconds else None
            else:
                since, next_check = None, now + self.poll_interval
            db.execute('UPDATE jobs SET state=?, error_code=?, uncertain_since=?, next_check=?, request=NULL WHERE task_id=?',
                       (state, code, since, next_check, task_id))
            db.execute('DELETE FROM references_png WHERE task_id=?', (task_id,))
        if next_check is None:
            self._release_pin(job['backend_id'])

    def _finish(self, task_id, state, code=None, result=None, png=None):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            backend_id = db.execute('SELECT backend_id FROM jobs WHERE task_id=?', (task_id,)).fetchone()[0]
            if result is not None:
                db.execute('INSERT OR IGNORE INTO image_seeds VALUES (?, ?)', (result['sha256'], result['seed_used']))
            db.execute('UPDATE jobs SET state=?, error_code=?, ended=?, request=NULL, result=?, png=? WHERE task_id=?',
                       (state, code, time.time(), json.dumps(result) if result is not None else None, png, task_id))
            db.execute('DELETE FROM references_png WHERE task_id=?', (task_id,))
            db.execute("""UPDATE jobs SET state='expired', error_code='result_expired', result=NULL, png=NULL
                          WHERE task_id IN (
                            SELECT task_id FROM jobs WHERE state IN ('completed', 'failed')
                            ORDER BY ended DESC, task_id DESC LIMIT -1 OFFSET ?)""", (self.max_results,))
        self._release_pin(backend_id)

    def _collect(self, job):
        task_id = job['task_id']
        with self.api.fetched(job['backend_id'], reuse_saved=True) as receipt:
            if len(receipt.get('outputs', [])) != 1:
                raise ValueError('Expected one PNG output')
            image = receipt['outputs'][0]
            png = Path(image['path']).read_bytes()
            width, height = png_size(png)
            sha256 = hashlib.sha256(png).hexdigest()
            if sha256 != image['sha256'] or (width, height) != (image['width'], image['height']):
                raise ValueError('Output verification failed')
            result = {'task_id': task_id, 'width': width, 'height': height, 'sha256': sha256,
                      'seed_used': str(image['seed_used'])}
            self._finish(task_id, 'completed', result=result, png=png)

    def _retry_collect(self, task_id):
        now = time.time()
        with self._db() as db:
            since = db.execute('SELECT output_wait_since FROM jobs WHERE task_id=?', (task_id,)).fetchone()[0]
            since = since if since is not None else now
            db.execute('UPDATE jobs SET output_wait_since=? WHERE task_id=?', (since, task_id))
        if now - since >= self.reconcile_seconds:
            self._finish(task_id, 'failed', 'output_unavailable')
        else:
            self._record_state(task_id, 'running')

    def _observe(self, job):
        task_id = job['task_id']
        if job['state'] == 'submitting' and job['prompt_id'] is None:
            self.api.abandon_unsubmitted(job['backend_id'])
            self._release_pin(job['backend_id'])
            with self._db() as db:
                db.execute("UPDATE jobs SET state='pending', backend_id=NULL WHERE task_id=?", (task_id,))
            return
        try:
            state = self.api.status(job['backend_id'])['state']
        except FileNotFoundError:
            self._record_state(task_id, 'submission_uncertain', 'submission_uncertain')
            return
        except (ValueError, OSError, RuntimeError, AttributeError, KeyError, TypeError):
            # Bound reconciliation after losing contact with an already dispatched job.
            code = job['error_code'] or 'backend_status_unavailable'
            self._record_state(task_id, 'submission_uncertain', code)
            return
        if state == 'completed':
            try:
                self._collect(job)
            except (ValueError, KeyError, TypeError):
                self._finish(task_id, 'failed', 'output_invalid')
            except (OutputUnavailable, FileNotFoundError):
                self._finish(task_id, 'failed', 'output_unavailable')
            except APIError as exc:
                if exc.status == 404:
                    self._finish(task_id, 'failed', 'output_unavailable')
                else:
                    self._retry_collect(task_id)
            except (OSError, RuntimeError):
                self._retry_collect(task_id)
        elif state in ('failed', 'cancelled', 'interrupted_by_restart'):
            self._finish(task_id, 'failed', state if state != 'failed' else 'generation_failed')
        elif state in ('queued', 'running'):
            self._record_state(task_id, state)
        else:
            self._record_state(task_id, 'submission_uncertain', 'submission_uncertain')

    def tick(self):
        with self._db() as db:
            job = db.execute('SELECT task_id, state, request, backend_id, prompt_id, chosen_seed, error_code FROM jobs '
                             'WHERE state IN (?, ?, ?, ?, ?) AND next_check<=? ORDER BY next_check, created LIMIT 1',
                             (*ACTIVE, time.time())).fetchone()
        if job is None:
            return False
        if job['state'] == 'pending':
            self._submit_pending(job)
        else:
            self._observe(job)
        return True

    def run(self, stop):
        with (self.home / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self._db() as db:
                finished = db.execute("SELECT backend_id FROM jobs WHERE backend_id IS NOT NULL AND "
                                      "(state IN ('completed', 'failed', 'expired') OR next_check IS NULL)").fetchall()
            for job in finished:
                self._release_pin(job['backend_id'])
            # The worker may have been killed during input staging, before its temporary
            # directory was closed. Its durable input copies are still in SQLite.
            for folder in self.home.glob('submit-*'):
                if folder.is_dir() and not folder.is_symlink():
                    for path in folder.glob('*.png'):
                        path.unlink()
                    folder.rmdir()
            while not stop.is_set():
                try:
                    self.tick()
                except (OSError, sqlite3.Error):
                    LOG.error('Unable to access image job storage; the worker will retry')
                stop.wait(self.poll_interval)
