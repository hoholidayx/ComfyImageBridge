import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from export_contract import contract, plugin_metadata
from install import install


class PackagingTest(unittest.TestCase):
    def test_contract_hash_matches_the_plugins_frozen_hash_algorithm(self):
        value = contract(ROOT / "plugins")
        self.assertEqual(value["contract_version"], 7)
        for name in ("comfy-workflows", "ubuntu-runtime"):
            path = ROOT / "plugins" / name / "metadata.py"
            spec = importlib.util.spec_from_file_location("contract_" + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            meta = module.client_metadata() if name == "comfy-workflows" else module.metadata()
            self.assertEqual(value["plugins"][name], {"version": meta["plugin_version"], "hash": meta["plugin_hash"]})

    def test_explicit_install_preserves_profile_data_and_backs_up_old_code(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.yaml"
            config.write_text("private configuration\n")
            data = home / "plugin-data" / "comfy-workflows" / "receipt.json"
            data.parent.mkdir(parents=True)
            data.write_text('{"unchanged": true}')
            result = install(ROOT / "plugins", home)
            self.assertFalse(result["config_changed"])
            self.assertFalse(result["gateway_restarted"])
            self.assertIsNone(result["backup"])
            marker = home / "plugins" / "comfy-workflows" / "old-marker.txt"
            marker.write_text("old")
            with self.assertRaises(FileExistsError):
                install(ROOT / "plugins", home)
            result = install(ROOT / "plugins", home, replace=True)
            self.assertEqual((Path(result["backup"]) / "comfy-workflows" / marker.name).read_text(), "old")
            self.assertFalse(marker.exists())
            self.assertEqual(config.read_text(), "private configuration\n")
            self.assertEqual(json.loads(data.read_text()), {"unchanged": True})
            for name in ("comfy-workflows", "ubuntu-runtime"):
                self.assertEqual(plugin_metadata(home / "plugins" / name), plugin_metadata(ROOT / "plugins" / name))

    def test_symlinked_destination_is_rejected_before_any_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            other = home / "elsewhere"
            other.mkdir()
            (home / "plugins").symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                install(ROOT / "plugins", home)
            self.assertFalse(list(other.iterdir()))


if __name__ == "__main__":
    unittest.main()
