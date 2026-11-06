#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if (( $# == 0 )); then set -- status; fi
exec "${COMFY_UV_BIN:-$HOME/.local/bin/uv}" run --locked comfy-workflow sleep-guard "$@"
