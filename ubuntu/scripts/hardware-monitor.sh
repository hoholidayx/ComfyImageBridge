#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
action="${1:-status}"
if (( $# )); then shift; fi
case "$action" in
  check|test-email)
    exec .venv/bin/python -m comfy_workflow_api.hardware_monitor --root "$PWD" "$action" "$@"
    ;;
  status)
    exec /usr/bin/python3 scripts/hardware-service.py --root "$PWD" status "$@"
    ;;
  install|uninstall|start|stop|restart|retry|abort)
    exec /usr/bin/python3 scripts/hardware-service.py --root "$PWD" "$@" "$action"
    ;;
  *)
    printf 'Usage: %s {install|uninstall|start|stop|restart|status|check|test-email|retry|abort}\n' "$0" >&2
    exit 2
    ;;
esac
