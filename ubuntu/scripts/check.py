import subprocess
import sys
from pathlib import Path
from comfy_workflow_api import WorkflowAPI

root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"], check=True)
api = WorkflowAPI(root)
for alias in api.list_workflows()["registered"]:
    api._snapshot(alias)
print("Unit tests and snapshot integrity checks passed")
