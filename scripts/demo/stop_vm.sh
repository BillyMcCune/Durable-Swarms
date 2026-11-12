#!/usr/bin/env bash
# Kill an agent VM daemon and its agents on a remote machine: the demo's "VM loss".
# Usage: scripts/demo/stop_vm.sh user@host vm-id
set -euo pipefail
HOST=${1:?usage: stop_vm.sh user@host vm-id}
VM=${2:?usage: stop_vm.sh user@host vm-id}
ssh "$HOST" "cd Durable-Swarms && [ -f .runtime/$VM.pid ] && kill \$(cat .runtime/$VM.pid) && rm .runtime/$VM.pid && echo 'stopped $VM' || echo '$VM was not running'"
