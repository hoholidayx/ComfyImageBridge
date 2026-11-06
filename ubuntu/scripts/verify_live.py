"""Run three real jobs, then resume status/wait/fetch in fresh CLI processes."""
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from comfy_workflow_api import WorkflowAPI
from comfy_workflow_api.client import png_size
from comfy_workflow_api.compiler import digest, WORKFLOWS
from comfy_workflow_api.storage import now, read_json, write_json

root = Path(__file__).resolve().parents[1]
api = WorkflowAPI(root)
if any(api.health()["queue"].values()):
    raise RuntimeError("Wait for existing jobs before starting live verification")
before = {alias: digest(api.client.workflow(source)) for alias, source in WORKFLOWS.items()}
records = []
cases = [("t2i.json", "qwen-image-2.1-t2i", (512, 512)),
         ("edit-single.json", "qwen-image-2.1-edit", (352, 288)),
         ("edit-multi.json", "qwen-image-2.1-edit", (544, 384))]
report_dir = api.data / "verification"
report_dir.mkdir(parents=True, exist_ok=True)
for filename, workflow, expected in cases:
    task = api.submit(workflow, read_json(root / "examples" / filename), root / "examples")
    task_id = task["task_id"]
    print(json.dumps({"submitted": task_id, "case": filename}), flush=True)
    # Different processes read task IDs and recover all state from disk.
    for command in ("status", "wait", "fetch"):
        completed = subprocess.run([sys.executable, "-m", "comfy_workflow_api.cli", "--root", str(root), command, task_id],
                                   check=True, capture_output=True, text=True)
        payload = json.loads(completed.stdout)
        write_json(api._directory(task_id) / ("cli-" + command + ".json"), payload)
    result = payload
    for output in result["outputs"]:
        assert png_size(Path(output["path"]).read_bytes()) == expected
        assert (output["width"], output["height"]) == expected
    # Re-fetch from another SDK instance and confirm stable content.
    again = WorkflowAPI(root).fetch(task_id)
    assert [o["sha256"] for o in result["outputs"]] == [o["sha256"] for o in again["outputs"]]
    records.append({"case": filename, "passed": True, **result})
    write_json(report_dir / "live.json", {"verified_at": now(), "cases": records})
    print(json.dumps({"passed": filename, "elapsed_seconds": result["elapsed_seconds"], "size": expected}), flush=True)
after = {alias: digest(api.client.workflow(source)) for alias, source in WORKFLOWS.items()}
assert before == after
assert not any(api.health()["queue"].values())
write_json(report_dir / "live.json", {"verified_at": now(), "cases": records,
    "workflows_unchanged": True, "source_hashes": before, "queue_empty": True,
    "new_process_resume_verified": True,
    "installed_packages": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions())})
