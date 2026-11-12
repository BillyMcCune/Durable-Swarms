#!/usr/bin/env bash
# Start an agent VM daemon on a remote machine (set up with setup_vm.sh, with
# tunnel.sh running). Several daemons with different ids can share one machine.
# Usage: scripts/demo/start_vm.sh user@host vm-id
set -euo pipefail
HOST=${1:?usage: start_vm.sh user@host vm-id}
VM=${2:?usage: start_vm.sh user@host vm-id}
ssh "$HOST" "set -e; cd Durable-Swarms; mkdir -p .runtime
if [ -f .runtime/$VM.pid ] && kill -0 \$(cat .runtime/$VM.pid) 2>/dev/null; then echo '$VM is already running'; exit 0; fi
LLM_URL=http://localhost:11434 nohup .venv/bin/python -m agent_demo vm --id $VM --url http://localhost:8081 > .runtime/$VM.log 2>&1 &
echo \$! > .runtime/$VM.pid
sleep 2; tail -n 3 .runtime/$VM.log"
