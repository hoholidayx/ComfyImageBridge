#!/usr/bin/env python3
"""Export client version/hash expectations for the separate Ubuntu package."""
import argparse
import hashlib
import json
from pathlib import Path
import re

PLUGINS = ("comfy-workflows", "ubuntu-runtime")


def plugin_metadata(directory):
    directory = Path(directory)
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.py")) + [directory / "plugin.yaml"]:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    version = re.search(r"^version:\s*(\S+)\s*$", (directory / "plugin.yaml").read_text(), re.MULTILINE)
    if not version:
        raise ValueError(f"Missing version: {directory / 'plugin.yaml'}")
    return {"version": version[1], "hash": digest.hexdigest()}


def contract(plugin_root):
    root = Path(plugin_root)
    return {"schema_version": 1, "contract_version": 7,
            "plugins": {name: plugin_metadata(root / name) for name in PLUGINS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, default=Path(__file__).resolve().parents[1] / "plugins")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = contract(args.plugin_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name("." + args.output.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output.resolve()), **value}, indent=2))


if __name__ == "__main__":
    main()
