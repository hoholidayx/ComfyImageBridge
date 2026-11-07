"""Cross-package contract, portability and publication checks (no live connections)."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import unittest

from comfy_workflow_api.contract import CONTRACT_VERSION, VERSION
from comfy_workflow_api.metadata import client_contract


ROOT = Path(__file__).resolve().parents[1]


def source_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseTests(unittest.TestCase):
    def test_client_manifest_matches_both_plugins(self):
        exporter = source_module("release_contract", ROOT / "hermes/scripts/export_contract.py")
        expected = exporter.contract(ROOT / "hermes/plugins")
        actual = json.loads((ROOT / "ubuntu/config/client-contract.json").read_text())
        self.assertEqual(actual, expected)
        self.assertEqual(actual["contract_version"], CONTRACT_VERSION)
        for plugin, item in actual["plugins"].items():
            self.assertEqual(client_contract(ROOT / "ubuntu", plugin), {**item, "error": None})

    def test_versions_and_dependency_boundary(self):
        for package, version in (("ubuntu", VERSION), ("hermes", "0.7.2")):
            project = tomllib.loads((ROOT / package / "pyproject.toml").read_text())["project"]
            self.assertEqual(project["version"], version)
            self.assertEqual(project["dependencies"], [])
            lock = tomllib.loads((ROOT / package / "uv.lock").read_text())
            locked = {p["name"]: p for p in lock["package"]}
            self.assertEqual(locked[project["name"]]["version"], version)
            if package == "hermes":
                self.assertEqual(set(locked), {project["name"]})
            else:
                self.assertIn("mcp", locked)
                self.assertIn("mcp", project["optional-dependencies"])
                self.assertTrue({"torch", "pytorch", "rocm", "comfyui"}.isdisjoint(locked))

    def test_clients_only_bootstrap_installed_server_modules(self):
        for plugin, module in (("comfy-workflows", "workflow_bridge"), ("ubuntu-runtime", "runtime_bridge")):
            path = ROOT / "hermes/plugins" / plugin / "remote_bridge.py"
            text = path.read_text()
            self.assertIn(f"from comfy_workflow_api.{module} import main", text)
            self.assertLess(len(text), 1000)
            self.assertTrue((ROOT / "ubuntu/src/comfy_workflow_api" / f"{module}.py").is_file())
        self.assertFalse((ROOT / "ubuntu/integrations").exists())
        self.assertTrue((ROOT / "ubuntu/src/comfy_workflow_api/runtime_operation_worker.py").is_file())

    def test_bootstrap_json_round_trip_without_live_service(self):
        environment = {"PYTHONPATH": str(ROOT / "ubuntu/src")}
        for plugin, packet in (("comfy-workflows", {"operation": "invalid"}),
                               ("ubuntu-runtime", {"action": "invalid"})):
            result = subprocess.run(
                [sys.executable, str(ROOT / "hermes/plugins" / plugin / "remote_bridge.py")],
                input=json.dumps(packet), capture_output=True, text=True, env=environment, timeout=5,
            )
            # Invalid input must reach the installed service and fail as structured JSON,
            # rather than failing to import a copied client/server directory.
            value = json.loads(result.stdout)
            self.assertIn("error", value)
            self.assertFalse(value.get("ok", value.get("success")))
            self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_documentation_local_links_exist(self):
        for path in ROOT.rglob("*.md"):
            if any(part in {".venv", ".git", "data"} for part in path.parts):
                continue
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                target = target.split("#", 1)[0].strip("<>")
                if target:
                    self.assertTrue((path.parent / target).exists(), f"Broken link in {path.relative_to(ROOT)}: {target}")

    def test_only_synthetic_example_images_are_distributed(self):
        images = sorted(str(path.relative_to(ROOT / "ubuntu")) for path in (ROOT / "ubuntu").rglob("*.png")
                        if not {".venv", "data", "__pycache__"}.intersection(path.parts))
        self.assertEqual(images, ["examples/palette.png", "examples/robot.png"])
        self.assertFalse((ROOT / "hermes/integrations").exists())


if __name__ == "__main__":
    unittest.main()
