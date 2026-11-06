"""Frozen at plugin import so an old process cannot report a new disk copy's hash."""
import hashlib
from pathlib import Path

PLUGIN_VERSION = '0.6.1'
CONTRACT_VERSION = 7
ROOT = Path(__file__).resolve().parent
digest = hashlib.sha256()
for path in sorted(ROOT.glob('*.py')) + [ROOT/'plugin.yaml']:
    digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
LOADED_HASH = digest.hexdigest()


def client_metadata():
    return {'plugin_version': PLUGIN_VERSION, 'plugin_hash': LOADED_HASH, 'contract_version': CONTRACT_VERSION}
