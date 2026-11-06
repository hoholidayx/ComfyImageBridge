"""Bounded, dependency-free hardware observations; unreadable values stay unknown.

The caller never opens GPU attributes. A separate session owns every GPU read,
and an inherited flock prevents another probe even if a driver call cannot die.
"""
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

_MAX_OUTPUT = 256 * 1024
_PENDING = []
_KERNEL_PENDING = None
_PCI = re.compile(r'(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]')
_SEVERE = re.compile(
    r'(?:amdgpu|drm).*(?:failed to resume|resume.*(?:failed|error)|ring\s+\S+\s+timeout|'
    r'GPU reset.*(?:failed|failure)|GPU recovery.*failed|GPU fault|VM.*fault)|'
    r'Restore process.*failed during resume|PM:.*failed to resume|'
    r'(?:mce:.*(?:hardware error|machine check)|Machine check events logged|'
    r'\[Hardware Error\].*(?:CPU|Machine Check))', re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return None


def _number(value):
    try:
        number = float(str(value).strip().removesuffix('%').strip())
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _integer(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 else None


def _boot_id():
    return _text('/proc/sys/kernel/random/boot_id')


def _identity(pid):
    try:
        path = Path('/proc') / str(pid)
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': int(pid), 'start_ticks': fields[19], 'pgid': int(fields[2]),
                'uid': path.stat().st_uid, 'boot_id': _boot_id()}
    except (OSError, ValueError, IndexError):
        return None


def _write_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def _reap():
    _PENDING[:] = [process for process in _PENDING if process.poll() is None]


def _bounded_command(command, timeout_seconds, pass_fds=(), identity_path=None):
    """Use files, not pipes or Popen context cleanup, so timeout never waits."""
    _reap()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=err, pass_fds=pass_fds)
        if identity_path:
            state = _read_json(identity_path)
            state['command_process'] = _identity(process.pid)
            state['command'] = list(command)
            _write_json(identity_path, state)
        deadline = time.monotonic() + max(0, timeout_seconds)
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(min(0.025, max(0, deadline - time.monotonic())))
        timed_out = process.poll() is None
        if timed_out:
            process.kill()
            if process.poll() is None:
                _PENDING.append(process)
        out.seek(0)
        err.seek(0)
        return {'returncode': process.returncode, 'timed_out': timed_out,
                'stdout': out.read(_MAX_OUTPUT).decode(errors='replace'),
                'stderr': err.read(8192).decode(errors='replace'), 'process': process}


def _temperatures(hwmon):
    values = []
    for path in sorted(Path(hwmon).glob('temp*_input')):
        number = _number(_text(path))
        if number is None:
            continue
        celsius = number / 1000
        if not -50 <= celsius <= 200:
            continue
        prefix = path.name.removesuffix('_input')
        critical = _number(_text(path.with_name(prefix + '_crit')))
        values.append({'label': _text(path.with_name(prefix + '_label')) or prefix,
                       'celsius': celsius,
                       'critical_celsius': critical / 1000 if critical is not None else None})
    return values


class CpuSampler:
    def __init__(self):
        self._previous = None

    def sample(self):
        result = {'status': 'unknown', 'observed_at': _now(), 'utilization_percent': None,
                  'logical_cpus': os.cpu_count(), 'load_average': None, 'temperatures': [],
                  'memory': {'total_bytes': None, 'available_bytes': None, 'used_percent': None}}
        try:
            result['load_average'] = list(os.getloadavg())
        except OSError:
            pass
        stat = _text('/proc/stat')
        if stat:
            try:
                counters = [int(value) for value in stat.splitlines()[0].split()[1:9]]
                current = (sum(counters), counters[3] + counters[4])
                if self._previous:
                    total = current[0] - self._previous[0]
                    idle = current[1] - self._previous[1]
                    if total > 0 and 0 <= idle <= total:
                        result['utilization_percent'] = 100 * (total - idle) / total
                self._previous = current
            except (ValueError, IndexError):
                pass
        memory = _text('/proc/meminfo')
        if memory:
            rows = dict(re.findall(r'^(\w+):\s+(\d+)\s+kB', memory, re.MULTILINE))
            total = int(rows['MemTotal']) * 1024 if 'MemTotal' in rows else None
            available = int(rows['MemAvailable']) * 1024 if 'MemAvailable' in rows else None
            result['memory'] = {'total_bytes': total, 'available_bytes': available,
                                'used_percent': 100 * (total - available) / total
                                if total and available is not None else None}
        for hwmon in sorted(Path('/sys/class/hwmon').glob('hwmon*')):
            if _text(hwmon / 'name') in ('k10temp', 'coretemp', 'zenpower', 'k8temp'):
                result['temperatures'].extend(_temperatures(hwmon))
        if result['utilization_percent'] is not None:
            result['status'] = 'ok'
        if any(item['critical_celsius'] is not None and item['celsius'] >= item['critical_celsius']
               for item in result['temperatures']):
            result['status'] = 'critical'
        return result


def _normalize_pci(value):
    found = _PCI.search(str(value))
    if not found:
        return None
    value = found.group().lower()
    return value if value.count(':') == 2 else '0000:' + value


def _rocm_records(payload):
    """Use only supported core readings; Memory Activity is deliberately ignored."""
    result = {}
    if not isinstance(payload, dict):
        return result
    for record in payload.values():
        if not isinstance(record, dict):
            continue
        pci = next((_normalize_pci(value) for key, value in record.items()
                    if 'bus' in key.lower() and _normalize_pci(value)), None)
        if not pci:
            continue
        data = {'temperatures': [], 'utilization_percent': None,
                'vram_used_bytes': None, 'vram_total_bytes': None}
        for key, value in record.items():
            lower = key.lower()
            number = _number(value)
            if 'temperature' in lower and number is not None and -50 <= number <= 200:
                data['temperatures'].append({'label': key, 'celsius': number, 'critical_celsius': None})
            elif 'gpu use' in lower and number is not None and 0 <= number <= 100:
                data['utilization_percent'] = number
            elif 'vram' in lower and 'used' in lower:
                # ROCm names this 'VRAM Total Used Memory (B)'. Match used
                # first so it cannot overwrite the total-capacity reading.
                data['vram_used_bytes'] = _integer(value)
            elif 'vram' in lower and 'total' in lower:
                data['vram_total_bytes'] = _integer(value)
        result[pci] = data
    return result


def _collect_gpu(lock_fd, identity_path, deadline):
    gpus = []
    for device in sorted(Path('/sys/bus/pci/devices').glob('*')):
        if _text(device / 'vendor') != '0x1002' or not (_text(device / 'class') or '').startswith('0x03'):
            continue
        try:
            driver = (device / 'driver').resolve(strict=True).name
        except OSError:
            driver = None
        gpu = {'pci': device.name.lower(), 'driver': driver, 'temperatures': [],
               'utilization_percent': None, 'vram_used_bytes': None, 'vram_total_bytes': None,
               'kfd_present': Path('/dev/kfd').exists(), 'sources': [],
               'runtime_status': _text(device / 'power/runtime_status'),
               'power_watts': None, 'power_sensor': None}
        for hwmon in sorted((device / 'hwmon').glob('hwmon*')):
            gpu['temperatures'].extend(_temperatures(hwmon))
            if gpu['power_watts'] is None:
                for sensor in ('power1_average', 'power1_input'):
                    power = _number(_text(hwmon / sensor))
                    if power is not None and power >= 0:
                        gpu['power_watts'] = power / 1_000_000
                        gpu['power_sensor'] = sensor
                        break
        use = _number(_text(device / 'gpu_busy_percent'))
        gpu['utilization_percent'] = use if use is not None and 0 <= use <= 100 else None
        gpu['vram_used_bytes'] = _integer(_text(device / 'mem_info_vram_used'))
        gpu['vram_total_bytes'] = _integer(_text(device / 'mem_info_vram_total'))
        gpu['sources'].append('sysfs')
        gpus.append(gpu)
    executable = shutil.which('rocm-smi')
    rocm_error = None
    if executable and time.monotonic() < deadline:
        try:
            query = _bounded_command([executable, '--showbus', '--showtemp', '--showuse',
                                      '--showmeminfo', 'vram', '--json'],
                                     min(2, max(0, deadline - time.monotonic())),
                                     pass_fds=(lock_fd,), identity_path=identity_path)
            if query['timed_out']:
                rocm_error = 'rocm-smi timed out'
            elif query['returncode']:
                rocm_error = 'rocm-smi failed: ' + query['stderr'][:1024]
            else:
                records = _rocm_records(json.loads(query['stdout']))
                if not records:
                    rocm_error = 'rocm-smi returned no PCI-addressed core readings'
                elif any(not record['temperatures'] and all(record[key] is None for key in
                         ('utilization_percent', 'vram_used_bytes', 'vram_total_bytes'))
                         for record in records.values()):
                    rocm_error = 'rocm-smi core readings are all unavailable (N/A)'
                for gpu in gpus:
                    record = records.get(gpu['pci'])
                    if record:
                        for key in ('utilization_percent', 'vram_used_bytes', 'vram_total_bytes'):
                            if gpu[key] is None:
                                gpu[key] = record[key]
                        if not gpu['temperatures']:
                            gpu['temperatures'] = record['temperatures']
                        gpu['sources'].append('rocm-smi')
        except (OSError, ValueError) as exc:
            rocm_error = 'rocm-smi unavailable: ' + str(exc)
    status = 'ok' if gpus and all(gpu['driver'] == 'amdgpu' and gpu['kfd_present'] and gpu['temperatures'] and
                                 gpu['utilization_percent'] is not None and
                                 gpu['vram_used_bytes'] is not None and
                                 gpu['vram_total_bytes'] is not None for gpu in gpus) else 'unknown'
    if any(item['critical_celsius'] is not None and item['celsius'] >= item['critical_celsius']
           for gpu in gpus for item in gpu['temperatures']):
        status = 'critical'
    result = {'status': status, 'observed_at': _now(), 'gpus': gpus}
    if rocm_error:
        result['rocm_error'] = rocm_error
        if 'timed out' in rocm_error:
            result.update(status='unknown', probe_stuck=True, error=rocm_error)
    if not gpus:
        result['error'] = 'No AMD display GPU found on PCI'
    elif any(not gpu['kfd_present'] for gpu in gpus):
        result['error'] = 'ROCm compute device /dev/kfd is unavailable'
    elif any(gpu['driver'] != 'amdgpu' for gpu in gpus):
        result['error'] = 'An AMD GPU is not bound to the amdgpu driver'
    elif status == 'unknown' and 'error' not in result:
        result['error'] = 'GPU core temperature, utilization or VRAM readings are unavailable'
    return result


def _sleep_gate(data_dir, allow_during_restore):
    current_boot = _boot_id()
    if not current_boot:
        return 'current boot ID is unavailable'
    try:
        path = data_dir / 'sleep-cycle/state.json'
        if not path.exists():
            return None
        state = json.loads(path.read_text())
        if (not isinstance(state, dict) or not state.get('cycle_token')
                or not isinstance(state.get('boot_id'), str) or not state['boot_id']):
            return 'unreadable sleep-cycle state'
    except (OSError, ValueError):
        return 'unreadable sleep-cycle state'
    phase = state.get('phase', state.get('state'))
    if phase not in ('preparing', 'sleeping', 'restoring', 'restored', 'restore_failed', 'complete', 'aborted'):
        return 'unknown sleep-cycle phase'
    if state['boot_id'] != current_boot:
        return None
    if phase in ('preparing', 'sleeping') or (phase == 'restoring' and not allow_during_restore):
        return phase
    return None


def gpu_probe(root, timeout_seconds=5, allow_during_restore=False):
    from .settings import load_settings
    observed = _now()
    data = Path(load_settings(Path(root))['data_dir'])
    phase = _sleep_gate(data, allow_during_restore)
    if phase:
        return {'status': 'unknown', 'observed_at': observed, 'gpus': [], 'paused': True,
                'error': 'GPU probe paused: ' + phase}
    home = data / 'hardware-probe'
    home.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(home / 'gpu.lock', os.O_CREAT | os.O_RDWR, 0o600)
    process = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                previous = _read_json(home / 'gpu-process.json')
            except (OSError, ValueError):
                previous = {}
            return {'status': 'unknown', 'observed_at': observed, 'gpus': [],
                    'probe_stuck': True, 'error': 'Previous GPU probe is still running',
                    'probe_process': previous}
        # Recheck after locking: a sleep request may have arrived during setup.
        phase = _sleep_gate(data, allow_during_restore)
        if phase:
            return {'status': 'unknown', 'observed_at': observed, 'gpus': [], 'paused': True,
                    'error': 'GPU probe paused: ' + phase}
        budget = max(0.05, min(5.0, float(timeout_seconds)))
        deadline = time.monotonic() + budget
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--gpu-helper',
                                        str(lock_fd), str(home / 'gpu-process.json'), str(deadline),
                                        str(int(bool(allow_during_restore)))],
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       start_new_session=True, pass_fds=(lock_fd,))
            os.close(lock_fd)
            lock_fd = None
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            if process.poll() is None:
                identity = _identity(process.pid)
                if identity and identity['pgid'] == process.pid and identity['uid'] == os.getuid():
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                if process.poll() is None:
                    _PENDING.append(process)
                return {'status': 'unknown', 'observed_at': observed, 'gpus': [],
                        'probe_stuck': True, 'error': 'GPU probe exceeded its time budget'}
            out.seek(0)
            err.seek(0)
            if process.returncode:
                return {'status': 'unknown', 'observed_at': observed, 'gpus': [],
                        'error': 'GPU helper failed: ' + err.read(8192).decode(errors='replace')}
            result = json.loads(out.read(_MAX_OUTPUT))
            if not isinstance(result, dict):
                raise ValueError('GPU helper returned a non-object')
            return result
    except (OSError, ValueError) as exc:
        return {'status': 'unknown', 'observed_at': observed, 'gpus': [], 'error': str(exc)}
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        _reap()


def read_kernel_events(cursor=None, boot_id=None):
    """Return only new current-boot critical messages, plus the advance cursor."""
    global _KERNEL_PENDING
    current_boot = _boot_id()
    result = {'events': [], 'cursor': cursor if boot_id == current_boot else None,
              'boot_id': current_boot}
    if not current_boot:
        result['error'] = 'Kernel boot ID is unavailable'
        return result
    if _KERNEL_PENDING is not None and _KERNEL_PENDING.poll() is None:
        result['error'] = 'Previous journal query is still running'
        return result
    command = ['journalctl', '-k', '--boot=0', '--no-pager', '--output=json', '--show-cursor']
    if cursor and boot_id == current_boot:
        command.append('--after-cursor=' + cursor)
    else:
        # Establish a baseline on first observation rather than replay old faults.
        command.append('--lines=0')
    try:
        deadline = time.monotonic() + 2
        query = _bounded_command(command, 2)
        _KERNEL_PENDING = query['process'] if query['timed_out'] else None
        failure = query.get('stderr', '').lower()
        expired_cursor = (cursor and boot_id == current_boot and not query['timed_out']
                          and query['returncode'] and 'cursor' in failure
                          and any(word in failure for word in ('failed', 'invalid', 'not found', 'unavailable')))
        if expired_cursor and time.monotonic() < deadline:
            baseline = [value for value in command if not value.startswith('--after-cursor=')]
            baseline.append('--lines=0')
            query = _bounded_command(baseline, max(0, deadline - time.monotonic()))
            _KERNEL_PENDING = query['process'] if query['timed_out'] else None
            result['error'] = ('Kernel journal cursor is unavailable; reset to the current-boot '
                               'baseline without replaying old events; the missing interval cannot be recovered')
        if query['timed_out'] or query['returncode']:
            result['error'] = 'Kernel journal query timed out' if query['timed_out'] else query['stderr'][:1024]
            return result
        for line in query['stdout'].splitlines():
            if line.startswith('-- cursor: '):
                result['cursor'] = line.removeprefix('-- cursor: ').strip()
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            result['cursor'] = record.get('__CURSOR', result['cursor'])
            if str(record.get('_BOOT_ID', '')).replace('-', '') != current_boot.replace('-', ''):
                continue
            message = record.get('MESSAGE', '')
            if not isinstance(message, str) or not _SEVERE.search(message):
                continue
            timestamp = record.get('__REALTIME_TIMESTAMP')
            at = datetime.fromtimestamp(int(timestamp) / 1_000_000, timezone.utc).isoformat() if timestamp else None
            result['events'].append({'cursor': record.get('__CURSOR'), 'message': message,
                                     'at': at, 'boot_id': current_boot})
    except (OSError, ValueError, OverflowError) as exc:
        result['error'] = str(exc)
    return result


def _helper_main():
    lock_fd = int(sys.argv[2])
    path = Path(sys.argv[3])
    deadline = float(sys.argv[4])
    # Keep the inherited open file description locked until all driver callers end.
    os.fstat(lock_fd)
    phase = _sleep_gate(path.parent.parent, sys.argv[5] == '1')
    if phase or time.monotonic() >= deadline:
        print(json.dumps({'status': 'unknown', 'observed_at': _now(), 'gpus': [],
                          'paused': bool(phase), 'error': 'GPU helper paused: ' + phase
                          if phase else 'GPU helper started after its deadline'}), flush=True)
        return
    _write_json(path, {'helper': _identity(os.getpid()), 'pgid': os.getpgrp(),
                       'boot_id': _boot_id(), 'command': sys.argv, 'observed_at': _now()})
    print(json.dumps(_collect_gpu(lock_fd, path, deadline)), flush=True)


if __name__ == '__main__' and len(sys.argv) == 6 and sys.argv[1] == '--gpu-helper':
    _helper_main()
