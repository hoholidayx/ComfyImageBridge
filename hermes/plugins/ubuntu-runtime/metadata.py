import hashlib
from pathlib import Path

VERSION = '0.3.1'
ROOT = Path(__file__).resolve().parent


def tree_hash(root):
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.glob('*.py')) + [root/'plugin.yaml']:
        digest.update(path.name.encode()+b'\0'+path.read_bytes()+b'\0')
    return digest.hexdigest()


LOADED_HASH = tree_hash(ROOT)


def metadata():
    return {'plugin_version': VERSION, 'plugin_hash': LOADED_HASH}
