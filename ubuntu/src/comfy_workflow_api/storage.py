import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def is_job_id(name):
    try:
        return str(uuid.UUID(name)) == name
    except (ValueError, TypeError, AttributeError):
        return False


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value, *, create_parent=True):
    path = Path(path)
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
