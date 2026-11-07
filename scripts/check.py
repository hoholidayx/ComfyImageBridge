#!/usr/bin/env python3
"""Verify the Ubuntu MCP release and the frozen Hermes rollback offline."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", help="Path to the existing uv executable")
    parser.add_argument("--offline", action="store_true", help="Require cached Python/build dependencies")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    uv = args.uv or shutil.which("uv")
    if not uv:
        candidate = Path.home() / ".local/bin/uv"
        if candidate.is_file():
            uv = str(candidate)
    if not uv:
        parser.error("uv not found; install/configure uv or supply --uv")
    offline = ["--offline"] if args.offline else []
    for package in ("ubuntu", "hermes"):
        print(f"Checking {package}: locked environment and offline unit tests", flush=True)
        extras = ["--extra", "mcp"] if package == "ubuntu" else []
        subprocess.run([uv, "sync", "--project", str(root / package), "--locked", *extras, *offline], check=True)
        subprocess.run([uv, "run", "--project", str(root / package), "--no-sync", *offline,
                        "python", str(root / package / "scripts/check.py")], cwd=root, check=True)
    environment = dict(os.environ)
    # Cross-package tests use the Ubuntu package without installing it in Hermes.
    environment["PYTHONPATH"] = str(root / "ubuntu/src")
    subprocess.run([uv, "run", "--project", str(root / "ubuntu"), "--no-sync", *offline,
                    "python", "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"],
                   cwd=root, env=environment, check=True)
    print("Release checks passed; no live SSH, wake or generation operations were performed.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
