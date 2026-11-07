"""Checkout configuration shared by the SDK and lifecycle scripts."""
from ipaddress import IPv4Address, IPv4Network
import os
from pathlib import Path
import shutil
import tomllib


_PRIVATE_NETWORKS = tuple(map(IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))


def mcp_listen(settings):
    config = settings.get('mcp', {})
    if not isinstance(config, dict):
        raise ValueError('mcp must be a configuration table')
    host, port = config.get('host', '127.0.0.1'), config.get('port', 8787)
    try:
        address = IPv4Address(host) if isinstance(host, str) else None
    except ValueError:
        address = None
    if address is None or not (address.is_loopback or any(address in network for network in _PRIVATE_NETWORKS)):
        raise ValueError('mcp.host must be an explicit loopback or private LAN IPv4 address')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('mcp.port must be an integer from 1 to 65535')
    return str(address), port


def module_root(root=None):
    return Path(root or os.environ.get('COMFY_WORKFLOW_HOME') or
                Path(__file__).resolve().parents[2]).expanduser().resolve()


def resolve_path(value, base):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(base)/path).resolve()


def service_paths(root, settings):
    service = settings.get('service', {})
    comfy = resolve_path(service.get('comfy_dir', '~/workspace/ComfyUI'), root)
    run = resolve_path(service.get('run_script', 'run_comfyui.sh'), comfy)
    uv = service.get('uv_bin') or os.environ.get('COMFY_UV_BIN') or shutil.which('uv') or '~/.local/bin/uv'
    return comfy, run, resolve_path(uv, root)


def load_settings(root):
    settings = tomllib.loads((Path(root)/'config/settings.toml').read_text())
    comfy, _, _ = service_paths(root, settings)
    settings['data_dir'] = str(resolve_path(settings.get('data_dir', 'data'), root))
    retention = settings.setdefault('retention', {})
    for kind in ('output', 'input'):
        key = f'comfy_{kind}_dir'
        path = Path(retention.get(key) or comfy/kind/'comfy-workflow-api').expanduser()
        # Preserve symlinks for retention._safe_root to reject before deleting.
        retention[key] = str((path if path.is_absolute() else Path(root)/path).absolute())
    return settings
