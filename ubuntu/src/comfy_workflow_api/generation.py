from datetime import datetime, timezone
from contextlib import contextmanager
import copy
import hashlib
import json
import os
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path
from .client import APIError, png_size
from .backend import BackendContext, GenerationRuntime, OutputUnavailable
from .compiler import compile_graph, digest
from .storage import cleanup_lock, now, read_json, write_json
from .retention import cleanup_after_fetch, result_pin
from .contract import normalize
from .metadata import backend_metadata
from .sampling import configuration as sampling_configuration
from . import snapshots
from .task_diagnostics import TERMINAL, decorate


class WorkflowRuns(BackendContext):
    def __init__(self, root, base_url, client, runtime_factory):
        super().__init__(root, base_url, client)
        self.runtime: GenerationRuntime = runtime_factory(self)

    def health(self):
        return {"system": self.client.request("/system_stats"), "queue": self.client.request("/queue")}

    def list_workflows(self):
        return snapshots.list_workflows(self.root)

    def sync_workflows(self):
        return snapshots.sync_workflows(self)

    def _snapshot(self, alias):
        return snapshots.read_snapshot(self.root, self.data, alias)

    def _request(self, request, request_dir=None):
        request = copy.deepcopy(request)
        if "images" in request:
            if not isinstance(request["images"], list):
                raise ValueError("images must be a list")
            request["images"] = [str((Path(request_dir or Path.cwd()) / p).resolve()) for p in request["images"]]
            for path in request["images"]:
                if Path(path).suffix.lower() != ".png":
                    raise ValueError("Only local PNG files are supported")
                png_size(Path(path).read_bytes())
        return request

    def compile(self, workflow, request, request_dir=None):
        snapshot, manifest = self._snapshot(workflow)
        request = self._request(request, request_dir)
        return {"workflow": workflow, "snapshot": manifest, "submitted": False,
                "prompt": compile_graph(workflow, snapshot, request)}

    def _directory(self, task_id):
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError("Task ID must be a canonical UUID")
        return self.data / "runs" / task_id

    def _save(self, directory, state, message):
        state["updated_at"] = now()
        write_json(directory / "task.json", state)
        with (directory / "task.log").open("a") as stream:
            stream.write(json.dumps({"time": now(), "state": state["state"], "message": message}, ensure_ascii=False) + "\n")

    def submit(self, workflow, request, request_dir=None, *, task_id=None, readiness=None, before_prompt=None):
        snapshot, manifest = self._snapshot(workflow)
        original_request = copy.deepcopy(request)
        request = normalize(self._request(request, request_dir), workflow)
        request.setdefault('output_size', {'mode': 'follow_image'} if workflow.endswith('edit') else {'mode': 'explicit', 'width': 1024, 'height': 1024})
        if workflow.endswith('edit'):
            request.setdefault('reference_resolution', 1024)
        compile_graph(workflow, snapshot, request)  # validate before copying or uploading
        task_id = task_id or str(uuid.uuid4())
        self._directory(task_id)  # validate a caller-reserved ID before writing admission state
        with self.runtime.submitting(task_id, readiness) as service_instance_id:
            return self._submit_prepared(workflow, request, snapshot, manifest, original_request,
                                         task_id=task_id, service_instance_id=service_instance_id,
                                         before_prompt=before_prompt)

    def _submit_prepared(self, workflow, request, snapshot, manifest, original_request, task_id=None,
                         service_instance_id=None, before_prompt=None):
        task_id = task_id or str(uuid.uuid4())
        directory = self._directory(task_id)
        directory.mkdir(parents=True, exist_ok=False)
        state = {"task_id": task_id, "workflow": workflow, "snapshot": manifest,
                 "state": "preparing", "created_at": now(), "prompt_id": None,
                 "service_instance_id": service_instance_id, "last_progress_at": now(),
                 "base_url": self.client.base_url, 'request_used': request, 'seed_used': request['seed'], 'metadata': backend_metadata()}
        write_json(directory / 'original-request.json', original_request)
        write_json(directory / "request.json", request)
        write_json(directory / "source-workflow.json", snapshot)
        write_json(directory / "history.json", {})
        write_json(directory / "result.json", {"task_id": task_id, "outputs": []})
        self._save(directory, state, "Created task")
        try:
            copied = copy.deepcopy(request)
            uploaded = None
            if "images" in request:
                inputs_dir = self.data / "inputs" / task_id
                inputs_dir.mkdir(parents=True)
                copied["images"] = []
                for index, path in enumerate(request["images"], 1):
                    destination = inputs_dir / f"{index}.png"
                    shutil.copy2(path, destination)
                    copied["images"].append(str(destination))
                write_json(directory / "inputs.json", copied["images"])
                uploaded = [self.client.upload(path, task_id, i) for i, path in enumerate(copied["images"], 1)]
                write_json(directory / "uploads.json", uploaded)
            state['request_used'] = copied
            write_json(directory / 'effective-request.json', copied)
            graph = compile_graph(workflow, snapshot, copied, uploaded, "comfy-workflow-api/" + task_id + "/image")
            write_json(directory / "prompt.json", graph)
            state['sampling_config'] = sampling_configuration(graph, copied)
            write_json(directory / 'sampling.json', state['sampling_config'])
            state['model_config'] = {key: graph[key] for key in ('451', '453', '454', '479', '469', '495') if key in graph}
            # Current ComfyUI accepts a client-provided UUID. Persist before POST so a lost
            # response can be reconciled against history/queue without re-submitting.
            state.update(state="submitting", prompt_id=str(uuid.uuid4()), submitted_at=now())
            write_json(directory / "submission.json", {"prompt_id": state["prompt_id"], "acknowledged": False})
            self._save(directory, state, "Submitting once; UUID persisted before POST")
            self.runtime.ticket(task_id)
            if before_prompt is not None:
                before_prompt(state['prompt_id'])
            result = self.client.request("/prompt", "POST", {"prompt": graph,
                        "client_id": task_id, "prompt_id": state["prompt_id"]})
            write_json(directory / "submission.json", {**result, "acknowledged": True})
            if result.get("node_errors") or not result.get("prompt_id"):
                raise ValueError(f"Submission validation failed: {result}")
            state.update(state="queued", prompt_id=result["prompt_id"])
            self._save(directory, state, "Submission acknowledged")
            return state
        except Exception as exc:
            # A proxy 5xx may have been returned after the backend accepted the POST.
            definite_rejection = isinstance(exc, APIError) and 400 <= exc.status < 500
            uncertain = state["state"] == "submitting" and not definite_rejection
            state.update(state="submission_uncertain" if uncertain else "failed", error=str(exc))
            self._save(directory, state, "No automatic retry")
            if not uncertain:
                self.runtime.ticket(task_id, remove=True)
            if isinstance(exc, APIError):
                write_json(directory / "submission-error.json", {"status": exc.status, "body": exc.body})
            raise RuntimeError(f"Task {task_id}: {state['state']}: {exc}") from exc

    def abandon_unsubmitted(self, task_id):
        """Close a prepared run when the caller's durable journal proves no POST was attempted."""
        directory = self._directory(task_id)
        try:
            state = read_json(directory / 'task.json')
        except FileNotFoundError:
            return
        if state['state'] not in ('preparing', 'submitting'):
            return
        state.update(state='failed', ended_at=now(), error='Worker stopped before dispatch')
        self._save(directory, state, 'Abandoned before dispatch')
        self.runtime.ticket(task_id, remove=True)

    def pin_result(self, task_id, pinned, *, discarded=False):
        """Keep this run outside count-based cleanup until its consumer archives it."""
        directory = self._directory(task_id)
        if discarded and directory.is_dir():
            (directory / '.result-discarded').touch(mode=0o600)
        marker = result_pin(directory)
        if pinned:
            marker.touch(mode=0o600)
        else:
            marker.unlink(missing_ok=True)

    def status(self, task_id):
        directory = self._directory(task_id)
        state = read_json(directory / "task.json")
        if state["state"] in TERMINAL or not state["prompt_id"]:
            return decorate(self, state)
        if state["base_url"] != self.client.base_url:
            raise ValueError("Task belongs to a different ComfyUI base URL")
        prompt_id = state["prompt_id"]
        retired = self.runtime.retired(state.get('service_instance_id'))
        item = None
        if retired:
            # Completed jobs can be unpolled when a service restarts. Preserve
            # snapshotted or previously saved history before reconciling active jobs.
            saved_history = read_json(directory/'history.json') if (directory/'history.json').exists() else {}
            for candidate in (retired.get('history_snapshot', {}).get(prompt_id), saved_history):
                result_status = (candidate or {}).get('status', {})
                messages = result_status.get('messages', [])
                interrupted = any(m[0] == 'execution_interrupted' for m in messages if isinstance(m, (list, tuple)) and m)
                if result_status.get('completed') or result_status.get('status_str') == 'error' or interrupted:
                    item = candidate
                    break
            if not item:
                active = prompt_id in (retired.get('active_prompt_ids') or [])
                state.update(state='interrupted_by_restart' if active else 'submission_uncertain',
                             ended_at=retired.get('ended_at', now()),
                             error=('Confirmed active task ended with its service instance; not resubmitted' if active else
                                    'Service instance ended but prior completion cannot be determined; no history/active evidence, not resubmitted'))
                self.runtime.ticket(task_id, remove=True)
                decorate(self, state)
                self._save(directory, state, 'Service instance ended; reconciled only available evidence')
                return state
        else:
            item = self.client.request("/history/" + urllib.parse.quote(prompt_id)).get(prompt_id)
        previous_state = state['state']
        if item:
            write_json(directory / "history.json", item)
            result_status = item.get("status", {})
            messages = result_status.get('messages', [])
            interrupted = any(m[0] == 'execution_interrupted' for m in messages if isinstance(m, (list, tuple)) and m)
            event_at = retired.get('ended_at', now()) if retired else now()
            for message in reversed(messages):
                if isinstance(message, (list, tuple)) and len(message) > 1 and message[0] in ('execution_success', 'execution_error', 'execution_interrupted'):
                    timestamp = message[1].get('timestamp') if isinstance(message[1], dict) else None
                    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                        try:
                            event_at = datetime.fromtimestamp(timestamp / 1000 if timestamp > 10**11 else timestamp, timezone.utc).isoformat()
                        except (ValueError, OverflowError, OSError):
                            pass
                    break
            if interrupted:
                state.update(state='cancelled', ended_at=event_at, cancellation_confirmed=True)
            elif result_status.get("status_str") == "error":
                state.update(state="failed", ended_at=event_at, error="ComfyUI execution error; see history.json")
                state['readiness_invalidation'] = self.runtime.invalidate(state, 'execution_error')
            elif result_status.get("completed"):
                state.update(state="completed", completed_at=event_at)
            else:
                state["state"] = "running"
            # A changed execution event is actual progress; status polling is not.
            event_hash = digest(messages)
            if event_hash != state.get('execution_events_hash'):
                state.update(execution_events_hash=event_hash, last_progress_at=now())
        else:
            queue = self.client.request("/queue")
            if any(row[1] == prompt_id for row in queue.get("queue_running", [])):
                state["state"] = "running"
            elif any(row[1] == prompt_id for row in queue.get("queue_pending", [])):
                state["state"] = "queued"
            elif state.get('cancel_requested_at'):
                # A removed pending item is confirmed cancelled; disappearance of a
                # running item without history is ambiguous, never proof it stopped.
                if state.get('cancel_target_state') == 'queued' and state.get('cancel_dispatched'):
                    state.update(state='cancelled', ended_at=now(), cancellation_confirmed=True)
                else:
                    state['state'] = 'cancel_requested'
            else:
                state["state"] = "submission_uncertain"
        if state['state'] == 'running':
            state.setdefault('running_since', now())
            if previous_state not in ('running', 'cancel_requested'):
                state['last_progress_at'] = now()
            if state.get('cancel_requested_at'):
                state['state'] = 'cancel_requested'
        if state['state'] in TERMINAL:
            self.runtime.ticket(task_id, remove=True)
        decorate(self, state)
        if state.get('stalled') is True:
            state['readiness_invalidation'] = self.runtime.invalidate(state, 'suspected_stall_with_valid_evidence')
        self._save(directory, state, "Checked status")
        return state

    def cancel(self, task_id):
        state = self.status(task_id)
        if state['state'] in TERMINAL or not state.get('prompt_id'):
            return {**state, 'cancel_dispatched': False}
        directory = self._directory(task_id)
        if not state.get('cancel_requested_at'):
            state.update(cancel_requested_at=now(), cancel_target_state=state['state'],
                         cancellation_confirmed=False, cancel_dispatched=False)
            self._save(directory, state, 'Targeted cancel intent persisted before HTTP request')
            result = self.client.request('/api/jobs/' + urllib.parse.quote(state['prompt_id']) + '/cancel', 'POST', {})
            state.update(cancel_dispatched=bool(result.get('cancelled')), state='cancel_requested')
            self._save(directory, state, 'Targeted cooperative cancellation requested; not proof of termination')
        return self.status(task_id)

    def wait(self, task_id, timeout=None):
        timeout = self.settings["wait_timeout"] if timeout is None else timeout
        if timeout < 0:
            raise ValueError("timeout must be nonnegative")
        started = time.monotonic()
        while True:
            state = self.status(task_id)
            if state["state"] == "completed":
                return state
            if state["state"] in ("failed", "cancelled", "interrupted_by_restart"):
                raise RuntimeError(f"Task {task_id} failed: {state.get('error')}")
            if state["state"] == "submission_uncertain":
                raise RuntimeError(f"Task {task_id}: acceptance/existence unknown; not resubmitted")
            if time.monotonic() - started >= timeout:
                self._save(self._directory(task_id), state, "Wait timed out; server task retained")
                raise TimeoutError(f"Task {task_id} still {state['state']}; not cancelled")
            time.sleep(self.settings["poll_interval"])

    def fetch(self, task_id):
        with self.fetched(task_id) as result:
            return result

    @contextmanager
    def fetched(self, task_id, *, reuse_saved=False):
        """Keep verified output files protected while a caller materializes its result."""
        with self.runtime.fetching(task_id), cleanup_lock(self.data):
            saved = read_json(self._directory(task_id) / 'result.json') if reuse_saved else {}
            result = saved if saved.get('outputs') else self._fetch(task_id)
            cleanup_after_fetch(self, result, task_id)
            yield result

    def _fetch(self, task_id):
        state = self.status(task_id)
        if state["state"] != "completed":
            raise RuntimeError(f"Task is {state['state']}, not completed")
        if state["base_url"] != self.client.base_url:
            raise ValueError("Task belongs to a different ComfyUI base URL")
        directory = self._directory(task_id)
        history = read_json(directory / "history.json")
        output_dir = directory / "outputs"
        output_dir.mkdir(exist_ok=True)
        outputs = []
        for image in history.get("outputs", {}).get("save", {}).get("images", []):
            query = {key: image[key] for key in ("filename", "subfolder", "type")}
            data = self.client.request("/view?" + urllib.parse.urlencode(query), binary=True)
            size = png_size(data)
            path = output_dir / f"{len(outputs)+1}.png"
            temporary = path.with_suffix(".download")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            outputs.append({"path": str(path), "width": size[0], "height": size[1],
                            "sha256": hashlib.sha256(data).hexdigest(), "server_file": query})
        if not outputs:
            raise OutputUnavailable("Completed task has no saved PNG output")
        from datetime import datetime
        elapsed = (datetime.fromisoformat(state["completed_at"]) - datetime.fromisoformat(state["submitted_at"])).total_seconds()
        result = {"task_id": task_id, "prompt_id": state["prompt_id"], "workflow": state["workflow"],
                  "elapsed_seconds": round(elapsed, 3), "snapshot": state["snapshot"], "outputs": outputs}
        effective = state.get('request_used')
        if effective is None:
            graph = read_json(directory/'prompt.json')
            inputs = graph['458']['inputs']
            sampling = ({'seed': inputs['noise_seed'], 'steps': len(graph['pruna_sigmas']['inputs']['nodes'].split(',')), 'cfg': inputs['cfg']}
                        if graph['458']['class_type'] == 'SamplerCustom' else {key: inputs[key] for key in ('seed', 'steps', 'cfg')})
            effective = {**read_json(directory/'request.json'), **sampling}
        result.update(request_used=effective, seed_used=effective['seed'], metadata=backend_metadata(), model_config=state.get('model_config'))
        result['sampling_config'] = state.get('sampling_config')
        for output in outputs:
            output['seed_used'] = effective['seed']
        write_json(directory / "result.json", result)
        self._save(directory, state, "Downloaded and verified PNG outputs")
        return result

    def run(self, workflow, request, request_dir=None, timeout=None):
        task = self.submit(workflow, request, request_dir)
        self.wait(task["task_id"], timeout)
        return self.fetch(task["task_id"])

    def submit_batch(self, workflow, request, count=1, request_dir=None):
        from . import batches
        return batches.submit(self, workflow, self._request(request, request_dir), count)

    def batch_status(self, task_id):
        from . import batches
        return batches.status(self, task_id)

    def batch_fetch(self, task_id):
        from . import batches
        return batches.fetch(self, task_id)

    def batch_cancel(self, task_id):
        from . import batches
        return batches.cancel(self, task_id)
