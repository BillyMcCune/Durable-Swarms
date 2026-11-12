#!/usr/bin/env bash
# Start the laptop side of the agent demo: Ollama (if it is not already serving)
# and the demo orchestrator with its dashboard. Extra arguments go to the orchestrator.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${PYTHON:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}
MODEL=$("$PY" -c 'import json; print(json.load(open("agent_demo/config.json"))["model"])')
PORT=$("$PY" -c 'import json; print(json.load(open("agent_demo/config.json"))["port"])')

if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null; then
  echo "starting ollama serve (log: /tmp/ollama-agent-demo.log)"
  OLLAMA_NUM_PARALLEL=${OLLAMA_NUM_PARALLEL:-4} OLLAMA_CONTEXT_LENGTH=${OLLAMA_CONTEXT_LENGTH:-16384} \
    nohup ollama serve >/tmp/ollama-agent-demo.log 2>&1 &
  for _ in $(seq 30); do curl -sf http://127.0.0.1:11434/api/version >/dev/null && break; sleep 0.5; done
fi
ollama list | grep -q "^${MODEL}[[:space:]]" || ollama pull "$MODEL"

echo "dashboard: http://localhost:${PORT}/"
# Keep the laptop awake while the orchestrator runs: if it sleeps, every VM lease
# expires at once and the running agents are treated as lost.
if command -v caffeinate >/dev/null; then
  exec caffeinate -ims "$PY" -m agent_demo orchestrator "$@"
fi
exec "$PY" -m agent_demo orchestrator "$@"
