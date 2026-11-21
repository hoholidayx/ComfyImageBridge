"""Publish and read the validated uni workflow used by jobs and render probes."""
import re
from pathlib import Path

from .compiler import UNIFIED_WORKFLOW, WORKFLOWS, digest, topology
from .metadata import backend_metadata
from .storage import cleanup_lock, now, read_json, write_json


def list_workflows(root):
    manifest = root / "workflows/manifest.json"
    return {"registered": WORKFLOWS, "snapshots": read_json(manifest) if manifest.exists() else {}, 'metadata': backend_metadata()}


def sync_workflows(backend):
    """Fetch explicitly; only accept the topology verified with these adapters."""
    supported = read_json(backend.root / "workflows/supported.json")
    snapshot = backend.client.workflow(UNIFIED_WORKFLOW)
    signature = topology(snapshot)
    if any(signature != supported[alias] for alias in WORKFLOWS):
        raise ValueError("Unsupported uni topology; existing snapshot retained")
    sha = digest(snapshot)
    relative = Path("workflows/snapshots") / sha / UNIFIED_WORKFLOW
    manifest_path = backend.root / "workflows/manifest.json"
    with cleanup_lock(backend.data):
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        current = backend.root / relative
        write_json(current, snapshot)
        if set(manifest) != set(WORKFLOWS) or any(
            (item.get("source"), item.get("path"), item.get("sha256"), item.get("topology")) !=
            (UNIFIED_WORKFLOW, relative.as_posix(), sha, signature) for item in manifest.values()
        ):
            entry = {"source": UNIFIED_WORKFLOW, "path": relative.as_posix(), "sha256": sha,
                     "topology": signature, "synced_at": now()}
            manifest = dict.fromkeys(WORKFLOWS, entry)
            write_json(manifest_path, manifest)
        _retire_snapshots(current)
    return manifest


def _retire_snapshots(current):
    for directory in current.parent.parent.iterdir():
        if directory.is_symlink() or not directory.is_dir() or directory == current.parent:
            continue
        if directory.name in WORKFLOWS:
            for legacy in directory.glob('*.json'):
                if re.fullmatch('[0-9a-f]{64}', legacy.stem):
                    legacy.unlink()
        elif re.fullmatch('[0-9a-f]{64}', directory.name):
            (directory / UNIFIED_WORKFLOW).unlink(missing_ok=True)
        else:
            continue
        if not any(directory.iterdir()):
            directory.rmdir()


def read_snapshot(root, data, alias):
    if alias not in WORKFLOWS:
        raise ValueError(f"Unknown workflow: {alias}")

    def read_current():
        manifest = read_json(root / "workflows/manifest.json")[alias]
        return read_json(root / manifest["path"]), manifest

    try:
        snapshot, manifest = read_current()
    except FileNotFoundError:
        # A concurrent sync can retire the version selected by the first manifest read.
        with cleanup_lock(data):
            snapshot, manifest = read_current()
    supported = read_json(root / "workflows/supported.json")
    if digest(snapshot) != manifest["sha256"] or topology(snapshot) != supported[alias]:
        raise ValueError("Snapshot integrity or topology check failed; sync or update adapter explicitly")
    return snapshot, manifest
