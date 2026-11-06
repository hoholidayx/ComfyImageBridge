"""Opt-in GNOME integration test. Uses an isolated fake queue; never starts ComfyUI."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api import sleep_guard as guard
from comfy_workflow_api.storage import now, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def verify():
    report = {'started_at': now(), 'checks': []}
    queue = {'pending': [], 'fail': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if queue['fail']:
                self.send_error(503)
                return
            self.respond({'queue_running': [], 'queue_pending': queue['pending']})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            queue['pending'].append([0, payload['prompt_id']])
            self.respond({'prompt_id': payload['prompt_id'], 'node_errors': {}})

        def respond(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='comfy-sleep-guard-') as tmp:
        root = Path(tmp)
        shutil.copytree(ROOT/'config', root/'config')
        (root/'workflows').symlink_to(ROOT/'workflows')
        settings = root/'config/settings.toml'
        settings.write_text(settings.read_text().replace('poll_seconds = 2', 'poll_seconds = 0.2')
                            .replace('idle_grace_seconds = 10', 'idle_grace_seconds = 2'))
        base_url = f'http://127.0.0.1:{server.server_port}'
        api = WorkflowAPI(root, base_url)

        def child(code, **kwargs):
            return subprocess.run([sys.executable, '-c',
                'from comfy_workflow_api import WorkflowAPI; from comfy_workflow_api import sleep_guard as g; import json; '
                f'api=WorkflowAPI({str(root)!r}, {base_url!r}); '+code],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25, check=True, **kwargs)

        def await_state(predicate, timeout=12):
            deadline = time.monotonic()+timeout
            while time.monotonic() < deadline:
                current = guard.status(api)
                if predicate(current):
                    return current
                time.sleep(.2)
            raise AssertionError('guard state timed out: '+json.dumps(current))

        try:
            # Each call runs in a short-lived separate process, as Hermes does over SSH.
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: child("print(json.dumps(api.submit('qwen-image-2.1-t2i', {'prompt':'geometric verification', 'output_size':{'mode':'explicit','width':512,'height':512}})))"), range(3)))
            submissions = [json.loads(result.stdout) for result in results]
            current = await_state(lambda s: s['running'] and s['inhibited'] and s.get('leases') == 0 and s.get('queued_or_running'))
            assert len(queue['pending']) == 3
            report['checks'].append('three concurrent SDK submissions; submitter processes exited; one guard retains real GNOME inhibitor')
            report['processes'] = {key: current[key] for key in ('supervisor', 'worker')}
            assert os.getsid(current['supervisor']['pid']) == current['supervisor']['pid']
            assert os.getsid(current['worker']['pid']) == current['supervisor']['pid']
            for key in ('supervisor', 'worker'):
                os.kill(current[key]['pid'], signal.SIGHUP)
            time.sleep(2.5)
            assert guard.status(api)['inhibited']
            report['checks'].append('detached session and process group; both processes survive SIGHUP; queued work exceeds idle grace')
            try:
                guard.stop(api)
                raise AssertionError('busy guard was stopped')
            except RuntimeError as exc:
                assert 'active work' in str(exc)
            report['checks'].append('normal stop refuses active generation')
            queue['fail'] = True
            await_state(lambda s: bool(s.get('queue_error')))
            time.sleep(2.5)
            assert guard.status(api)['inhibited']
            report['checks'].append('queue connection failure after submission conservatively retains lock')
            queue['fail'] = False
            queue['pending'] = []
            await_state(lambda s: not s['running'] and s.get('phase') == 'stopped')
            assert not guard.inhibited(current['token'])
            report['checks'].append('empty queue releases inhibitor automatically and prunes finished tickets')
            started = json.loads(child('print(json.dumps(g.start(api)))').stdout)
            stopped = guard.stop(api)
            assert not stopped['running'] and not guard.inhibited(started['token'])
            report['checks'].append('new process can start/status/stop owned guard without sudo')
            with guard.hold(api):
                time.sleep(2.5)
                assert guard.status(api)['inhibited']
            await_state(lambda s: not s['running'])
            report['checks'].append('upload/download request lease protects an empty queue; release is automatic')
            try:
                child("api.submit('qwen-image-2.1-t2i', {'prompt':'must not be submitted'})",
                      env={**os.environ, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/comfy-guard-no-such-bus'})
                raise AssertionError('submission proceeded without a desktop inhibitor')
            except subprocess.CalledProcessError as exc:
                assert 'Sleep guard launcher failed' in exc.stderr or 'Sleep inhibitor exited' in exc.stderr
            assert not queue['pending']
            assert not guard.live_leases(api)
            report['checks'].append('unavailable desktop D-Bus fails before generation POST; no leaked lease')
            report['tasks'] = [task['task_id'] for task in submissions]
            report['passed'] = True
        finally:
            current = guard.status(api)
            if current['running']:
                guard.stop(api, force=True)
            log = guard.home(api)/'guard.log'
            report['guard_log'] = log.read_text() if log.exists() else ''
            report['finished_at'] = now()
            destination = ROOT/'data/verification/sleep-guard.json'
            write_json(destination, report)
            print(json.dumps({'report': str(destination), **report}, ensure_ascii=False, indent=2))
            server.shutdown()


if __name__ == '__main__':
    verify()
