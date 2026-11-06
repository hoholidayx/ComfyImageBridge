#!/bin/sh
# Manage only the finite local observer for a known plugin task.
# Stopping this observer does not stop ComfyUI or clear its queue.
set -eu
if [ "$#" -ne 3 ]; then
    echo "Usage: $0 start|stop|restart|status TASK_ID DATA_DIR" >&2
    exit 2
fi
COMFY_WORKER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "${COMFY_WORKER_PYTHON:-python3}" "$COMFY_WORKER_ROOT/worker.py" --action "$1" --task-id "$2" --data-dir "$3"
