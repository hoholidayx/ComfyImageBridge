"""Finite detached job observer. Restarting it never blindly resubmits a prompt."""
import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import types
import uuid

if not __package__:
    package = types.ModuleType('_comfy_job_worker')
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules[package.__name__] = package
    __package__ = package.__name__

from .jobs import Store, TERMINAL, atomic_json, now
from .transport import Transport

TOTAL_SECONDS = 2100
PREPARE_SECONDS = 780
OBSERVE_SECONDS = 1200
POLL_SECONDS = 2
_CHILDREN = {}


class Deadline(BaseException):
    pass


class Config:
    def __init__(self, values):
        self.values = values

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def identity(pid):
    try:
        proc = Path('/proc') / str(pid)
        if proc.is_dir():
            fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                return None
            return {'pid': pid, 'uid': proc.stat().st_uid, 'start': fields[19],
                    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    'command': (proc/'cmdline').read_bytes().decode().rstrip('\0').split('\0')}
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'lstart=', '-o', 'command='],
                                capture_output=True, text=True, timeout=3)
        if result.returncode or not result.stdout.strip():
            return None
        values = result.stdout.strip().split(None, 6)
        if len(values) != 7 or '<defunct>' in values[6]:
            return None
        boot = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], capture_output=True, text=True, timeout=3)
        if boot.returncode:
            return None
        return {'pid': pid, 'uid': int(values[0]), 'start': ' '.join(values[1:6]),
                'boot_id': boot.stdout.strip(), 'command': values[6]}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def matches(saved):
    return bool(saved and saved.get('uid') == os.getuid() and identity(saved['pid']) == saved)


@contextmanager
def control(store, task_id):
    directory = store.directory(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'worker-control.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def launch(store, task_id):
    with control(store, task_id):
        job = store.require_job(task_id)
        if matches(job.get('worker')):
            return {'started': False, 'worker': job['worker']}
        if job['state'] in TERMINAL:
            return {'started': False, 'state': job['state']}
        command = [sys.executable, str(Path(__file__).resolve()), '--data-dir', str(store.root), '--task-id', task_id]
        directory = store.directory(task_id)
        token = uuid.uuid4().hex
        atomic_json(directory/'launch.json', {'token': token, 'command': command})
        for pid, child in list(_CHILDREN.items()):
            if child.poll() is not None:
                _CHILDREN.pop(pid, None)
        with (directory/'worker.log').open('ab', buffering=0) as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, cwd=directory)
        _CHILDREN[process.pid] = process
        deadline = time.monotonic() + 4
        saved = None
        while time.monotonic() < deadline:
            try:
                ready = json.loads((directory/'worker-ready.json').read_text())
                candidate = ready.get('worker')
                if ready.get('token') == token and candidate and candidate['pid'] == process.pid and matches(candidate):
                    saved = candidate
                    break
            except (OSError, ValueError, KeyError):
                pass
            if process.poll() is not None:
                break
            time.sleep(.02)
        if not saved:
            raise RuntimeError('Cannot verify launched worker identity; inspect worker.log before further action')
        store.update_job(task_id, worker=saved, worker_command=command,
                         worker_log=str(directory/'worker.log'), observer_started_at=now(), observer_stopped_at=None)
        return {'started': True, 'worker': saved}


def manage(action, task_id, data_dir):
    store = Store(data_dir)
    if action not in ('start', 'stop', 'restart', 'status'):
        raise ValueError('Worker action must be start, stop, restart or status')
    job = store.require_job(task_id)
    if action == 'status':
        return {'task_id': task_id, 'state': job['state'], 'running': matches(job.get('worker')),
                'worker': job.get('worker'), 'command': job.get('worker_command'), 'log': job.get('worker_log')}
    if action in ('stop', 'restart'):
        with control(store, task_id):
            job = store.require_job(task_id)
            saved = job.get('worker')
            if saved and not matches(saved) and identity(saved['pid']) is not None:
                raise RuntimeError('Worker PID was reused or identity changed; refusing to signal it')
            # Persist operator intent before the process can die and a reconnect
            # scanner can mistake this deliberate stop for an orphaned observer.
            store.update_job(task_id, observer_stopped_at=now())
            if matches(saved):
                os.kill(saved['pid'], signal.SIGTERM)
                deadline = time.monotonic()+5
                while matches(saved) and time.monotonic() < deadline:
                    time.sleep(.05)
                if matches(saved):
                    # Recheck immediately before escalation; never signal a process group.
                    os.kill(saved['pid'], signal.SIGKILL)
                    deadline = time.monotonic()+2
                    while matches(saved) and time.monotonic() < deadline:
                        time.sleep(.05)
                if matches(saved):
                    raise RuntimeError('Worker did not stop; no replacement launched')
            store.update_job(task_id, observer_stopped_at=now())
            if saved and saved['pid'] in _CHILDREN:
                child = _CHILDREN.pop(saved['pid'])
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _CHILDREN[saved['pid']] = child
        if action == 'stop':
            return manage('status', task_id, data_dir)
    launched = launch(store, task_id)
    return {**manage('status', task_id, data_dir), **launched}


def runtime_class():
    root = Path(__file__).resolve().parent.parent/'ubuntu-runtime'
    package_name = '_comfy_runtime_dependency'
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(root)]
        sys.modules[package_name] = package
    name = package_name+'.runtime'
    spec = importlib.util.spec_from_file_location(name, root/'runtime.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.Runtime


class Runner:
    def __init__(self, store, task_id, *, transport=None, runtime=None, clock=time.monotonic, sleep=time.sleep):
        self.store, self.task_id = store, task_id
        directory = store.directory(task_id)
        self.config = Config(json.loads((directory/'config.json').read_text()))
        self.transport = transport or Transport(self.config, store.root)
        self.runtime = runtime or runtime_class()(self.config, store.root.parent/'ubuntu-runtime')
        self.clock, self.sleep = clock, sleep

    def progress(self, stage, message, **fields):
        self.store.update_job(self.task_id, stage=stage, **fields)
        self.store.enqueue(self.task_id, 'progress', {'message': message}, revision=stage)

    def cancelled_before_submit(self):
        job = self.store.require_job(self.task_id)
        if job.get('cancel_requested') and not job.get('submission_attempted'):
            self.store.update_with_event(self.task_id,
                {'state': 'cancelled', 'stage': 'cancelled', 'retry_allowed': True, 'finished_at': now()},
                'failed', {'message': '任务已在提交前取消。'}, revision='cancelled')
            return True
        return False

    def prepare(self):
        self.progress('preparing', '正在唤醒并检查生图服务。', state='preparing')
        started = self.clock()
        operation_id = self.store.require_job(self.task_id).get('runtime_operation_id')
        result = (self.runtime.run('status', operation_id=operation_id, wait_seconds=30) if operation_id
                  else self.runtime.run('ensure_ready', wait_seconds=30))
        waiting_for_manager = result.get('error', {}).get('type') == 'BlockingIOError'
        if waiting_for_manager:
            self.progress('waiting_for_service_operation', '另一任务正在准备服务；正在等待其完成。', state='preparing')
        while waiting_for_manager or result.get('in_progress') or result.get('state') == 'in_progress':
            if result.get('ready'):
                break
            if self.cancelled_before_submit():
                return False
            if self.clock()-started > PREPARE_SECONDS:
                raise TimeoutError('Service preparation exceeded 780 seconds; inspect its existing operation')
            if not waiting_for_manager:
                operation_id = result.get('operation_id')
                if not operation_id:
                    raise RuntimeError('Service operation returned in_progress without an operation ID')
                self.store.update_job(self.task_id, runtime_operation_id=operation_id)
            self.sleep(POLL_SECONDS)
            # A local flock failure's operation_id is only a receipt UUID, not
            # an operation accepted by Ubuntu. Observe live status without it.
            options = {'wait_seconds': 30}
            if not waiting_for_manager:
                options['operation_id'] = operation_id
            result = self.runtime.run('status', **options)
            if waiting_for_manager and result.get('busy') and not result.get('ready'):
                break  # An unverified active queue is diagnosed, never interrupted.
        self.store.update_job(self.task_id, runtime_result=result)
        if not result.get('ready'):
            if result.get('busy'):
                raise RuntimeError('Existing busy service is not render-verified; no jobs were interrupted or submitted')
            raise RuntimeError('Service is not ready: '+str(result.get('error') or result.get('state') or result.get('message')))
        return not self.cancelled_before_submit()

    def submit(self):
        job = self.store.require_job(self.task_id)
        request = {k: job['request'][k] for k in ('prompt', 'preset', 'seed', 'output_size')}
        blobs = self.transport.images(job['input_paths']) if job['input_paths'] else []
        packet = {'operation': 'submit', 'workflow': 'qwen-image-2.1-edit' if blobs else 'qwen-image-2.1-t2i',
                  'request': request, 'images': blobs, 'count': job['request']['count']}
        # This mark is durable before POST. Loss of the response can never cause automatic resubmission.
        self.store.update_job(self.task_id, state='submitting', stage='submitting', submission_attempted=True, submitted_at=now())
        try:
            response = self.transport.call(packet)
        except Exception as exc:
            self.uncertain(str(exc))
            return False
        if not response.get('success'):
            error = response.get('error', {})
            self.store.update_job(self.task_id, remote_task_id=error.get('task_id'), submission_response=response)
            self.uncertain(str(error.get('message', error)))
            return False
        remote = response['result']
        remote_id = remote.get('task_id')
        if not remote_id:
            self.uncertain('Submission response omitted task_id')
            return False
        self.store.update_job(self.task_id, remote_task_id=remote_id, state=remote.get('state', 'queued'),
                              submission_response=response, prompt_id=remote.get('prompt_id'), submitted_metadata=response.get('metadata'))
        self.transport.record(remote_id, remote)
        self.progress('queued', '任务已提交，正在排队或生成。')
        return True

    def uncertain(self, message):
        self.store.update_with_event(self.task_id,
            {'state': 'submission_uncertain', 'stage': 'submission_uncertain', 'error': message, 'retry_allowed': False},
            'choice', {'message': '提交结果不确定；不会自动重生。请检查原任务。',
                       'buttons': [{'label': '查看状态', 'action': 'status'}, {'label': '恢复观察', 'action': 'resume'}]},
            revision='submission_uncertain')

    def finish(self, result):
        remote_id = self.store.require_job(self.task_id)['remote_task_id']
        self.progress('downloading', '生成完成，正在下载并校验图片。', state='downloading')
        downloaded = self.transport.download({**result, 'task_id': remote_id})
        artifacts = self.store.register_outputs(self.task_id, downloaded.get('outputs', []))
        state = result.get('state', 'completed')
        self.store.update_with_event(self.task_id,
            {'state': state, 'stage': 'completed', 'artifacts': artifacts, 'result': downloaded,
             'finished_at': now(), 'retry_allowed': False},
            'completed', {'artifacts': artifacts, 'text': '图片已生成：'+self.store.require_job(self.task_id)['title']},
            revision='completed')
        # Existing count-based retention may have removed older Transport task directories.
        self.store.reconcile_retention(downloaded.get('cleanup', {}).get('max_tasks', 100))

    def observe(self):
        job = self.store.require_job(self.task_id)
        remote_id = job.get('remote_task_id')
        if not remote_id:
            self.uncertain('Original submission has no known task ID; cannot safely submit again')
            return
        started, failures = self.clock(), 0
        while self.clock()-started <= OBSERVE_SECONDS:
            job = self.store.require_job(self.task_id)
            if job.get('cancel_requested') and not job.get('cancel_dispatched'):
                cancel = self.transport.call({'operation': 'task', 'action': 'cancel', 'task_id': remote_id, 'wait_seconds': 0})
                self.store.update_job(self.task_id, cancel_dispatched=bool(cancel.get('success')), cancellation_response=cancel)
            try:
                response = self.transport.call({'operation': 'task', 'action': 'result', 'task_id': remote_id, 'wait_seconds': 30})
                if not response.get('success'):
                    raise RuntimeError(str(response.get('error')))
                failures = 0
            except Exception as exc:
                failures += 1
                self.store.update_job(self.task_id, observation_error=str(exc), observation_failures=failures)
                if failures >= 3:
                    raise RuntimeError('Three observation failures; original task was not resubmitted') from exc
                self.sleep(POLL_SECONDS)
                continue
            result = response['result']
            state = result.get('state', 'unknown')
            # Remote completion is not local completion until download/registration and
            # the completion outbox have committed. A crash here must remain resumable.
            local_state = 'finalizing' if state in ('completed', 'partial', 'failed', 'cancelled', 'interrupted_by_restart', 'expired') else state
            self.store.update_job(self.task_id, state=local_state, remote_state=state, last_remote_status={k: v for k, v in result.items() if k != 'outputs'}, last_observed_at=now())
            if state in ('completed', 'partial'):
                self.finish(result)
                return
            if state in ('failed', 'cancelled', 'interrupted_by_restart', 'expired'):
                local_state = 'failed' if state == 'expired' else state
                self.store.update_with_event(self.task_id,
                    {'state': local_state, 'stage': local_state, 'finished_at': now(), 'retry_allowed': True, 'error': result.get('error')},
                    'failed', {'message': f'任务结束：{state}；不会自动重生。',
                               'buttons': [{'label': '重新生成', 'action': 'retry'}]}, revision=local_state)
                return
            if result.get('stalled') is True:
                self.store.update_with_event(self.task_id,
                    {'state': 'stalled', 'stage': 'stalled', 'retry_allowed': False},
                    'choice', {'message': '任务疑似停滞，已暂停自动观察；原任务未重提。',
                               'buttons': [{'label': '恢复观察', 'action': 'resume'}, {'label': '取消', 'action': 'cancel'}]}, revision='stalled')
                return
            self.sleep(POLL_SECONDS)
        raise TimeoutError('Observation exceeded 1200 seconds; original task and queue were left intact')

    def run(self):
        job = self.store.require_job(self.task_id)
        if job['state'] in TERMINAL:
            return
        try:
            if self.cancelled_before_submit():
                return
            if not job.get('submission_attempted'):
                if not self.prepare() or not self.submit():
                    return
            elif not job.get('remote_task_id'):
                self.uncertain('Worker resumed after an uncertain submission; no automatic retry')
                return
            self.observe()
        except (Exception, Deadline) as exc:
            job = self.store.require_job(self.task_id)
            if job['state'] in TERMINAL:
                # A receipt mirror failure after the SQLite commit must not downgrade
                # a durable result or replace its already-persisted completion event.
                return
            attempted = job.get('submission_attempted')
            state = 'observation_failed' if attempted else 'failed'
            action = 'resume' if attempted else 'retry'
            label = '恢复观察' if attempted else '重试准备'
            self.store.update_with_event(self.task_id,
                {'state': state, 'error': str(exc), 'stage': state, 'retry_allowed': not attempted, 'observer_finished_at': now()},
                'failed', {'message': str(exc), 'buttons': [{'label': label, 'action': action}]}, revision=state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--action', choices=['start', 'stop', 'restart', 'status'])
    args = parser.parse_args(argv)
    if args.action:
        print(json.dumps(manage(args.action, args.task_id, args.data_dir), ensure_ascii=False))
        return 0
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    def interrupted(_signal, _frame):
        raise Deadline('Worker stopped; remote task was left intact; resume observation to continue')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(TOTAL_SECONDS)
    store = Store(args.data_dir)
    directory = store.directory(args.task_id)
    token = json.loads((directory/'launch.json').read_text())['token']
    # macOS framework Python can exec its real interpreter after Popen. Let the
    # running script attest the final identity, rather than adopting a transient shim.
    atomic_json(directory/'worker-ready.json', {'token': token, 'worker': identity(os.getpid())})
    # The launcher stamps PID/start identity before the child is allowed side effects.
    for _ in range(100):
        saved = store.require_job(args.task_id).get('worker')
        if saved and saved['pid'] == os.getpid() and matches(saved):
            break
        time.sleep(.05)
    else:
        raise RuntimeError('Launcher did not persist matching worker identity')
    Runner(store, args.task_id).run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
