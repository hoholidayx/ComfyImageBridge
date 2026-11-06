import hashlib
import json
import re
from pathlib import Path
from .contract import CONTRACT_VERSION, VERSION


def tree_hash(root, manifest=None):
    root = Path(root)
    paths = sorted(root.glob('*.py'))
    if manifest and (root / manifest).is_file():
        paths.append(root / manifest)
    h = hashlib.sha256()
    for path in paths:
        h.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return h.hexdigest()


def client_contract(root, plugin='comfy-workflows'):
    """Read the release manifest without copying client implementation here."""
    try:
        manifest = json.loads((Path(root)/'config/client-contract.json').read_text())
        if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('contract_version') != CONTRACT_VERSION:
            raise ValueError('Unsupported client contract manifest version')
        item = manifest['plugins'][plugin]
        if not isinstance(item, dict) or not isinstance(item.get('version'), str) or not re.fullmatch(r'[0-9a-f]{64}', item.get('hash', '')):
            raise ValueError(f'Invalid client release entry: {plugin}')
        return {**item, 'error': None}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'version': None, 'hash': None, 'error': f'Client release manifest unavailable: {exc}'}


def metadata(root):
    expected = client_contract(root)
    return {'backend_version': VERSION, 'contract_version': CONTRACT_VERSION,
            'backend_hash': tree_hash(Path(__file__).parent),
            'expected_plugin_version': expected['version'],
            'expected_plugin_hash': expected['hash'],
            'client_contract_error': expected['error']}
