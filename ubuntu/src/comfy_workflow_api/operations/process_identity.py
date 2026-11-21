"""Read Linux process identity without loading service or power controls."""
import os
from pathlib import Path


def identity(pid):
    path = Path('/proc')/str(pid)
    try:
        fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start_ticks': fields[19], 'uid': path.stat().st_uid,
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'command': (path/'cmdline').read_bytes().decode().rstrip('\0').split('\0')}
    except (OSError, ValueError, IndexError):
        return None


def matches(saved):
    if not saved or saved.get('uid') != os.getuid():
        return False
    current = identity(saved['pid'])
    if not current:
        return False
    # Compatibility for pre-upgrade records. New identities always carry boot_id;
    # the service manager separately verifies boot identity before adoption.
    if 'boot_id' not in saved:
        current = {key: value for key, value in current.items() if key != 'boot_id'}
    return current == saved
