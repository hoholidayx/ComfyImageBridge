import hashlib
from pathlib import Path
from .contract import VERSION


def backend_metadata():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return {'backend_version': VERSION, 'backend_hash': digest.hexdigest()}
