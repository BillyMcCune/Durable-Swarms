#!/usr/bin/env bash
# Fallback without remote machines: run VM daemons on this laptop.
# Usage: scripts/demo/local_vms.sh [vm-id ...]   (default: vm-1 vm-2 vm-3). Ctrl-C stops them all.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${PYTHON:-.venv/bin/python}
URL=${ORCHESTRATOR_URL:-http://127.0.0.1:8081}
[ $# -gt 0 ] || set -- vm-1 vm-2 vm-3
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT INT TERM
for vm in "$@"; do
  "$PY" -m agent_demo vm --id "$vm" --url "$URL" &
done
wait
