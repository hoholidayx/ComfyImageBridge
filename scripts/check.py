#!/usr/bin/env python3
"""Verify the Ubuntu SDK, MCP client and release offline."""
import argparse
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
    project = str(root / "ubuntu")
    subprocess.run([uv, "sync", "--project", project, "--locked", "--extra", "mcp", *offline], check=True)
    python = [uv, "run", "--project", project, "--no-sync", *offline, "python"]
    subprocess.run([*python, str(root / "ubuntu/scripts/check.py")], cwd=root, check=True)
    subprocess.run([*python, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"],
                   cwd=root, check=True)
    print("Release checks passed; no live SSH, wake or generation operations were performed.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
