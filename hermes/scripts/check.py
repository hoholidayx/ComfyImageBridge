#!/usr/bin/env python3
"""Run client tests without importing Hermes core or contacting Ubuntu."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    for directory in sorted((root / "plugins").iterdir()):
        if directory.is_dir() and (directory / "tests").is_dir():
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", str(directory / "tests"), "-v"],
                cwd=root,
            )
            if result.returncode:
                return result.returncode
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"],
        cwd=root,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
