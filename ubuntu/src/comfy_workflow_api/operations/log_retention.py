"""Rotate Bridge-owned append-only logs while preserving inherited descriptors."""
import logging
import os
from pathlib import Path
import stat
import tempfile
from ..storage import is_job_id


def rotate_logs(api, dry_run):
    retention = api.settings['retention']
    size, keep = retention['log_max_bytes'], retention['log_backups']
    logs = [api.data / name for name in ('cleanup.log', 'mcp-service/mcp.log', 'comfyui-service/comfyui.log',
                                         'comfyui-service/preflight.log', 'sleep-guard/guard.log')]
    logs.extend(path for path in (api.data / 'runs').glob('*/task.log') if is_job_id(path.parent.name))
    report = {'rotated_logs': [], 'expired_log_archives': [], 'log_errors': []}
    for path in logs:
        try:
            _maintain_log(path, api.data, size, keep, dry_run, report)
        except (OSError, RuntimeError) as exc:
            report['log_errors'].append({'path': str(path.relative_to(api.data)), 'error': str(exc)})
            logging.getLogger(__name__).warning('Unable to rotate %s: %s', path.name, exc)
    return report


def _maintain_log(path, root, size, keep, dry_run, report):
    if path.resolve() != path or not path.is_file():
        return
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return
    for archive in path.parent.glob(path.name + '.[0-9]*'):
        suffix = archive.name.removeprefix(path.name + '.')
        if suffix.isdecimal() and int(suffix) > keep and not archive.is_symlink() and archive.is_file():
            report['expired_log_archives'].append(str(archive.relative_to(root)))
            if not dry_run:
                archive.unlink()
    if info.st_size < size:
        return
    report['rotated_logs'].append(str(path.relative_to(root)))
    if dry_run:
        return
    # The supervised processes inherit an O_APPEND file descriptor for their whole lifetime.
    with path.open('r+b', buffering=0) as current, tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            current.seek(-size, os.SEEK_END)
            remaining = size
            while remaining:
                chunk = current.read(min(remaining, 1024**2))
                if not chunk:
                    raise OSError(f'Log changed during rotation: {path.name}')
                target.write(chunk)
                remaining -= len(chunk)
            target.flush()
            os.fsync(target.fileno())
            for index in range(keep, 1, -1):
                previous = path.with_name(f'{path.name}.{index-1}')
                if previous.is_file() and not previous.is_symlink():
                    previous.replace(path.with_name(f'{path.name}.{index}'))
            temporary.replace(path.with_name(path.name + '.1'))
            current.truncate(0)
        finally:
            temporary.unlink(missing_ok=True)
