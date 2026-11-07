"""Offline tests use synthetic PNGs only; no image/GPU dependencies."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('standalone_seed_guard_tests', ROOT/'seed_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def chunk(name, value):
    return (struct.pack('>I', len(value)) + name + value
            + struct.pack('>I', zlib.crc32(name+value) & 0xffffffff))


def png(rgb=bytes((10, 20, 30)), *, metadata=None):
    header = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    info = chunk(b'tEXt', metadata) if metadata else b''
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + info
            + chunk(b'IDAT', zlib.compress(b'\0' + rgb)) + chunk(b'IEND', b''))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE artifacts(id INTEGER PRIMARY KEY,task_id TEXT,body TEXT)')
        guard.initialize(self.db)
        self.image = guard.fingerprint(png())
        self.other = guard.fingerprint(png(bytes(3)))

    def tearDown(self): self.db.close()

    def test_many_to_many_and_uint64_text_storage(self):
        for seed in [0, 2**63, 2**64-1]:
            guard.register(self.db, self.image, seed)
            guard.register(self.db, self.image, seed)
        values = self.db.execute('SELECT seed,typeof(seed) FROM qwen_image_sources').fetchall()
        self.assertEqual({int(v[0]) for v in values}, {0,2**63,2**64-1})
        self.assertTrue(all(v[1]=='text' for v in values))
        self.assertFalse(guard.register(self.db,self.image,2,workflow='another-model'))
        for invalid in [True,-1,2**64,'1',None]:
            with self.subTest(seed=invalid):
                with self.assertRaises(ValueError):guard.register(self.db,self.image,invalid)

    def test_copied_and_renamed_bytes_keep_seed_association(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / 'original.png'
            original.write_bytes(png())
            renamed = original.rename(root / 'renamed.png')
            copied = root / 'copy.png'
            copied.write_bytes(renamed.read_bytes())
            identity = guard.fingerprint(copied.read_bytes())
        self.assertEqual(identity, self.image)
        self.assertEqual(identity, {'file_sha256': hashlib.sha256(png()).hexdigest()})
        guard.register(self.db, self.image, 23)
        with patch.object(guard.secrets, 'randbelow', return_value=24):
            result = guard.select_seed(self.db, [identity], 23)
        self.assertEqual(result, {'requested_seed': 23, 'selected_seed': 24,
            'reason': 'reference_seed_conflict', 'known_reference_count': 1,
            'unknown_reference_count': 0})

    def test_metadata_change_counts_as_unknown_and_preserves_explicit_seed(self):
        guard.register(self.db, self.image, 23)
        changed = guard.fingerprint(png(metadata=b'fixture\0synthetic'))
        self.assertNotEqual(self.image, changed)
        with patch.object(guard.secrets, 'randbelow', side_effect=AssertionError('must retain seed')):
            result = guard.select_seed(self.db, [changed], 23)
        self.assertEqual(result, {'requested_seed': 23, 'selected_seed': 23,
            'reason': 'explicit', 'known_reference_count': 0, 'unknown_reference_count': 1})

    def test_random_default_unknown_explicit_and_preserved_candidate(self):
        with patch.object(guard.secrets,'randbelow',return_value=77) as random:
            policy=guard.select_seed(self.db,[self.image],None)
            self.assertEqual((policy['selected_seed'],policy['reason'],policy['unknown_reference_count']),
                             (77,'edit_random',1))
            random.assert_called_once_with(2**64)
        with patch.object(guard.secrets,'randbelow',side_effect=AssertionError('must retain seed')):
            explicit=guard.select_seed(self.db,[self.image],13)
            candidate=guard.select_seed(self.db,[self.image],None,candidate_seed=77)
        self.assertEqual(explicit['selected_seed'],13)
        self.assertEqual(explicit['reason'],'explicit')
        self.assertEqual(candidate['selected_seed'],77)
        self.assertEqual(candidate['reason'],'edit_random')

    def test_all_references_and_middle_of_batch_interval(self):
        guard.register(self.db,self.image,12)
        guard.register(self.db,self.other,99)
        with patch.object(guard.secrets,'randbelow',side_effect=[98,11,20]):
            result=guard.select_seed(self.db,[self.image,self.other],10,count=4)
        self.assertEqual(result['selected_seed'],20)
        self.assertEqual(result['known_reference_count'],2)
        self.assertEqual(result['reason'],'reference_seed_conflict')

    def test_preselected_candidate_newly_conflicting_and_bounded_random_failure(self):
        guard.register(self.db,self.image,77)
        with patch.object(guard.secrets,'randbelow',return_value=78):
            policy=guard.select_seed(self.db,[self.image],None,candidate_seed=77)
        self.assertEqual((policy['selected_seed'],policy['reason']),(78,'reference_seed_conflict'))
        with patch.object(guard.secrets,'randbelow',return_value=77) as draw:
            with self.assertRaises(RuntimeError):guard.select_seed(self.db,[self.image],77)
            self.assertEqual(draw.call_count,32)

    def test_count_range_and_invalid_seed_inputs(self):
        for seed,count in [(2**64-1,2),(-1,1),(True,1),(None,0),(None,5)]:
            with self.subTest(seed=seed,count=count):
                with self.assertRaises(ValueError):guard.select_seed(self.db,[self.image],seed,count)
        value=guard.select_seed(self.db,[self.image],2**64-1)
        self.assertEqual(value['selected_seed'],2**64-1)
        with self.assertRaises(ValueError):guard.select_seed(self.db,[self.image],None,2,candidate_seed=2**64-1)
        with patch.object(guard.secrets,'randbelow',return_value=2**64-4) as draw:
            value=guard.select_seed(self.db,[self.image],None,4)
        draw.assert_called_once_with(2**64-3)
        self.assertEqual(value['selected_seed']+3,2**64-1)

    def test_database_errors_fail_instead_of_becoming_unknown(self):
        self.db.execute('DROP TABLE qwen_image_sources')
        with self.assertRaises(sqlite3.OperationalError):guard.select_seed(self.db,[self.image],1)

    def test_deleted_artifact_and_file_do_not_remove_provenance(self):
        guard.register(self.db,self.image,7)
        self.db.execute('DELETE FROM artifacts')
        with patch.object(guard.secrets,'randbelow',return_value=8):
            policy=guard.select_seed(self.db,[self.image],7)
        self.assertEqual(policy['selected_seed'],8)

    def test_migration_uses_stored_hashes_for_expired_or_replaced_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / 'fixture.png'
            image.write_bytes(png())
            replaced = root / 'replaced.png'
            replaced.write_bytes(png(bytes(3)))
            for n,output in enumerate([
                {'path':str(image),'sha256':self.image['file_sha256'],'seed_used':4},
                {'path':str(root/'gone.png'),'sha256':self.image['file_sha256'],'seed_used':5},
                {'path':str(replaced),'sha256':self.image['file_sha256'],'seed_used':6},
                {'path':str(replaced),'seed_used':7},
            ]):
                self.db.execute('INSERT INTO artifacts VALUES(?,?,?)',(n,'fixture-task',json.dumps(output)))
            task=root/'tasks/fixture-task';task.mkdir(parents=True)
            (task/'receipt.json').write_text(json.dumps({'workflow':'qwen-image-2.1-edit',
                'outputs':[{'path':str(image),'sha256':self.image['file_sha256'],'seed_used':2**64-1}]}))
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('must use stored hashes')):
                result = guard.migrate(self.db, root)
            self.assertFalse(result['already_complete'])
            self.assertEqual(result['registered'],4)
            self.assertEqual(result['skipped'],1)
            self.assertEqual(guard.known_seeds(self.db,[self.image])[0],{4,5,6,2**64-1})
            self.assertEqual(guard.known_seeds(self.db,[self.other])[0],set())
            self.assertTrue(guard.migrate(self.db,root)['already_complete'])
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM qwen_image_sources').fetchone()[0],4)

    def test_registration_works_with_existing_nullable_column(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('''CREATE TABLE qwen_image_sources(
                workflow TEXT NOT NULL, file_sha256 TEXT NOT NULL, pixel_sha256 TEXT,
                seed TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(workflow,file_sha256,seed))''')
            db.execute('INSERT INTO qwen_image_sources VALUES(?,?,?,?,?)',
                       (guard.FAMILY, self.image['file_sha256'], 'unused', '11', 0))
            guard.initialize(db)
            guard.register(db, self.other, 17)
            self.assertEqual(guard.known_seeds(db, [self.image])[0], {11})
            self.assertEqual(guard.known_seeds(db, [self.other])[0], {17})


if __name__=='__main__': unittest.main()
