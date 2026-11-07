"""Profile-local Qwen image provenance and edit seed selection.

The index retains SHA-256 identities of exact file bytes and their producer seeds.
"""
import hashlib
import json
from pathlib import Path
import re
import secrets
import time


FAMILY = 'qwen-image-2.1'
WORKFLOWS = frozenset((FAMILY, FAMILY + '-edit', FAMILY + '-t2i'))
UINT64 = 2**64
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS qwen_image_sources(
            workflow TEXT NOT NULL, file_sha256 TEXT NOT NULL,
            seed TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(workflow,file_sha256,seed)
        );
        CREATE TABLE IF NOT EXISTS qwen_seed_guard_meta(
            name TEXT PRIMARY KEY, value TEXT NOT NULL
        );
    ''')


def fingerprint(data):
    """Hash the exact bytes being copied or uploaded."""
    return {'file_sha256': hashlib.sha256(data).hexdigest()}


def _valid_seed(seed):
    return type(seed) is int and 0 <= seed < UINT64


def register(db, identity, seed, workflow=FAMILY):
    """Idempotently associate an actual producer seed; caller owns transaction."""
    if workflow not in WORKFLOWS:
        return False
    if not _valid_seed(seed):
        raise ValueError('Image provenance seed must be an unsigned 64-bit integer')
    file_sha = identity.get('file_sha256')
    if not isinstance(file_sha, str) or not SHA256.fullmatch(file_sha):
        raise ValueError('Image provenance requires a valid file SHA256')
    db.execute('''INSERT OR IGNORE INTO qwen_image_sources
                  (workflow,file_sha256,seed,created_at) VALUES(?,?,?,?)''',
               (FAMILY, file_sha, str(seed), time.time()))
    return True


def known_seeds(db, identities):
    """Profile-wide seed-only lookup; never expose another chat's metadata."""
    forbidden, known = set(), 0
    for identity in identities:
        rows = db.execute('SELECT seed FROM qwen_image_sources WHERE workflow=? AND file_sha256=?',
                          (FAMILY, identity['file_sha256'])).fetchall()
        values = {int(row[0]) for row in rows}
        if any(not 0 <= value < UINT64 for value in values):
            raise ValueError('Invalid persisted image provenance seed')
        known += bool(values)
        forbidden.update(values)
    return forbidden, known, len(identities)-known


def select_seed(db, identities, requested_seed, count=1, candidate_seed=None):
    """Pick a safe contiguous seed range, retaining a frozen candidate if safe."""
    if requested_seed is not None and not _valid_seed(requested_seed):
        raise ValueError('seed must be an unsigned 64-bit integer')
    if type(count) is not int or not 1 <= count <= 4:
        raise ValueError('count must be 1..4')
    for seed in (requested_seed, candidate_seed):
        if seed is not None and (not _valid_seed(seed) or seed + count > UINT64):
            raise ValueError('seed+count-1 must fit unsigned 64-bit range')
    forbidden, known, unknown = known_seeds(db, identities)

    def safe(value):
        return not any(value <= seed < value+count for seed in forbidden)

    candidate = candidate_seed if candidate_seed is not None else requested_seed
    conflict = candidate is not None and not safe(candidate)
    if candidate is None or conflict:
        for _ in range(32):
            candidate = secrets.randbelow(UINT64-count+1)
            if safe(candidate):
                break
        else:
            raise RuntimeError('Unable to select a nonconflicting edit seed; no task submitted')
    reason = ('reference_seed_conflict' if conflict else
              'edit_random' if requested_seed is None else 'explicit')
    return {'requested_seed': requested_seed, 'selected_seed': candidate, 'reason': reason,
            'known_reference_count': known, 'unknown_reference_count': unknown}


def identity_for_output(output):
    """Return a previously verified download hash without reading its path."""
    expected = output.get('sha256')
    if isinstance(expected, str) and SHA256.fullmatch(expected):
        return {'file_sha256': expected}
    return None


def migrate(db, root):
    """One-time, transactional backfill of existing client-owned provenance."""
    key = 'backfill_v1'
    if db.execute('SELECT value FROM qwen_seed_guard_meta WHERE name=?', (key,)).fetchone():
        return {'already_complete': True, 'registered': 0, 'skipped': 0}
    registered = skipped = 0

    def add(output, workflow=FAMILY):
        nonlocal registered, skipped
        seed = output.get('seed_used')
        identity = identity_for_output(output)
        if workflow not in WORKFLOWS or not _valid_seed(seed) or identity is None:
            skipped += 1
            return
        register(db, identity, seed, workflow)
        registered += 1

    for row in db.execute('SELECT body FROM artifacts').fetchall():
        try:
            output = json.loads(row[0])
            add(output, output.get('workflow', FAMILY))
        except (ValueError, TypeError, AttributeError):
            skipped += 1
    for path in sorted((Path(root)/'tasks').glob('*/receipt.json')):
        try:
            receipt = json.loads(path.read_text())
            workflow = receipt.get('workflow', FAMILY)
            for output in receipt.get('outputs', []):
                add(output, workflow)
        except (OSError, ValueError, TypeError, AttributeError):
            skipped += 1
    db.execute('INSERT OR IGNORE INTO qwen_seed_guard_meta VALUES(?,?)',
               (key, json.dumps({'registered': registered, 'skipped': skipped})))
    return {'already_complete': False, 'registered': registered, 'skipped': skipped}
