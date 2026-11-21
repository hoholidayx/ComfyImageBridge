"""Dependency, portability and publication checks (no live connections)."""
from pathlib import Path
import re
import tomllib
import unittest

from comfy_workflow_api.contract import VERSION


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_versions_and_dependency_boundary(self):
        project = tomllib.loads((ROOT / "ubuntu/pyproject.toml").read_text())["project"]
        self.assertEqual(project["version"], VERSION)
        self.assertEqual(project["dependencies"], [])
        lock = tomllib.loads((ROOT / "ubuntu/uv.lock").read_text())
        locked = {p["name"]: p for p in lock["package"]}
        self.assertEqual(locked[project["name"]]["version"], VERSION)
        self.assertIn("mcp", project["optional-dependencies"])
        self.assertIn("mcp", locked)
        self.assertTrue({"torch", "pytorch", "rocm", "comfyui"}.isdisjoint(locked))

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


if __name__ == "__main__":
    unittest.main()
