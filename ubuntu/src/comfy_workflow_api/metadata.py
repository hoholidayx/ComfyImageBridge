import hashlib
from pathlib import Path
from .version import VERSION


def backend_metadata():
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for path in sorted(package.rglob('*.py')):
        digest.update(path.relative_to(package).as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    return {'backend_version': VERSION, 'backend_hash': digest.hexdigest()}
