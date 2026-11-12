#!/usr/bin/env bash
# Keep a reverse SSH tunnel open so a remote VM reaches this laptop's orchestrator
# (8081) and Ollama (11434) on its own localhost. Run one per remote machine.
# Usage: scripts/demo/tunnel.sh user@host
set -uo pipefail
HOST=${1:?usage: tunnel.sh user@host}
while true; do
  echo "$(date +%T) tunnel to $HOST up (Ctrl-C to stop)"
  ssh -N -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
      -R 8081:localhost:8081 -R 11434:localhost:11434 "$HOST"
  echo "$(date +%T) tunnel to $HOST dropped; reconnecting in 2s"
  sleep 2
done
