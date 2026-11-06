"""Checkout configuration shared by the SDK and lifecycle scripts."""
import os
from pathlib import Path
import shutil
import tomllib


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
