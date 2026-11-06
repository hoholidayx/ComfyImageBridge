#!/usr/bin/env python3
"""Explicitly copy both plugins to one Hermes profile; never change its config."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from export_contract import PLUGINS, plugin_metadata


def install(source, home, replace=False):
    source, home = Path(source).resolve(), Path(home).expanduser().resolve()
    destination = home / "plugins"
    if destination.is_symlink():
        raise ValueError("Refusing a symlinked plugins directory")
    metadata = {name: plugin_metadata(source / name) for name in PLUGINS}
    for name in PLUGINS:
        target = destination / name
        if target.is_symlink():
            raise ValueError(f"Refusing a symlinked plugin: {target}")
        if target.exists() and not replace:
            raise FileExistsError(f"{target} already exists; finish active jobs and use --replace to back it up")
    destination.mkdir(parents=True, exist_ok=True)
    backup = home / "plugin-backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    installed, backed_up = [], []
    with tempfile.TemporaryDirectory(prefix=".myhermes2comfy-", dir=destination) as directory:
        staging = Path(directory)
        for name in PLUGINS:
            shutil.copytree(source / name, staging / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        try:
            for name in PLUGINS:
                target = destination / name
                if target.exists():
                    backup.mkdir(parents=True, exist_ok=True)
                    target.rename(backup / name)
                    backed_up.append(name)
                (staging / name).rename(target)
                installed.append(name)
        except BaseException:
            for name in reversed(installed):
                shutil.rmtree(destination / name)
            for name in backed_up:
                (backup / name).rename(destination / name)
            raise
    return {"installed": [str(destination / name) for name in installed],
            "backup": str(backup) if backed_up else None, "plugins": metadata,
            "config_changed": False, "gateway_restarted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True, help="The exact Hermes profile directory")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "plugins")
    parser.add_argument("--replace", action="store_true", help="Back up and replace existing plugin directories")
    args = parser.parse_args()
    print(json.dumps(install(args.source, args.hermes_home, args.replace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
