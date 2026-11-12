#!/usr/bin/env bash
# Copy this repository to a remote VM and install the agent runtime there.
# Usage: scripts/demo/setup_vm.sh user@host
set -euo pipefail
HOST=${1:?usage: setup_vm.sh user@host}
cd "$(dirname "$0")/../.."
rsync -az --delete --exclude .git --exclude .venv --exclude .runtime --exclude '__pycache__' \
      --exclude '*.sqlite3*' --exclude report --exclude output ./ "$HOST:Durable-Swarms/"
ssh "$HOST" 'set -e; cd Durable-Swarms
python3 -c "import sys; assert sys.version_info >= (3, 10), \"Python 3.10+ is required, found \" + sys.version.split()[0]"
# Ubuntu ships python3 without the venv module; fall back to a user-level virtualenv, which needs no sudo.
if ! python3 -m venv .venv >/dev/null 2>&1; then
  rm -rf .venv
  { python3 -m pip install -q --user virtualenv && python3 -m virtualenv -q .venv; } ||
    { echo "could not create a virtualenv; on Ubuntu run: sudo apt install -y python3-venv"; exit 1; }
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r agent_demo/requirements.txt
.venv/bin/python -c "import minisweagent" >/dev/null
echo "ready: $(hostname), $(python3 --version)"'
