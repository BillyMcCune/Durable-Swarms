#!/usr/bin/env bash
# Check that everything the agent demo needs is up before presenting it.
# Usage: scripts/demo/preflight.sh [orchestrator-url]   (default http://127.0.0.1:<port from config.json>)
# Prints one line per check and exits non-zero if any of them failed. It changes
# nothing except loading the model into memory with one short request.
set -uo pipefail
cd "$(dirname "$0")/../.."
PY=${PYTHON:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}
MODEL=$("$PY" -c 'import json; print(json.load(open("agent_demo/config.json"))["model"])')
PORT=$("$PY" -c 'import json; print(json.load(open("agent_demo/config.json"))["port"])')
URL=${1:-http://127.0.0.1:$PORT}
OLLAMA=${LLM_URL:-http://127.0.0.1:11434}
failed=0
check() {  # check "description" command...
  local what=$1; shift
  if out=$("$@" 2>&1); then echo "ok    $what${out:+ ($out)}"; else echo "FAIL  $what${out:+: $out}"; failed=1; fi
}

check "agent harness is installed in $PY" "$PY" -c 'import importlib.util, sys; sys.exit(not importlib.util.find_spec("minisweagent"))'
check "Ollama answers at $OLLAMA" curl -sf -m 3 -o /dev/null "$OLLAMA/api/version"
check "model $MODEL is pulled" sh -c "curl -sf -m 5 '$OLLAMA/api/tags' | grep -q '\"$MODEL\"'"
check "model answers (this also loads it into memory)" "$PY" - "$OLLAMA" "$MODEL" <<'PYEOF'
import json, sys, time, urllib.request
url, model = sys.argv[1:]
body = json.dumps({"model": model, "prompt": "Reply with the single word: ready", "stream": False,
                   "options": {"num_predict": 4}}).encode()
started = time.time()
with urllib.request.urlopen(urllib.request.Request(url + "/api/generate", body, {"Content-Type": "application/json"}), timeout=120) as r:
    json.load(r)
print(f"{time.time() - started:.1f}s")
PYEOF
check "orchestrator answers at $URL" curl -sf -m 3 -o /dev/null "$URL/health"
check "VMs and board" "$PY" - "$URL" <<'PYEOF'
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1] + "/demo/state", timeout=5) as r:
    state = json.load(r)
offline = [vm["id"] for vm in state["vms"] if not vm["online"]]
if offline:
    sys.exit("not online: " + ", ".join(offline) + " (start them with scripts/demo/local_vms.sh)")
running = [t["name"] for t in state["tasks"] if t["status"] in ("queued", "running", "recovering")]
slots = sum(vm["capacity"] for vm in state["vms"])
print(f"{len(state['vms'])} VMs online, {slots} slots, {len(state['tasks'])} runs on the board"
      + (f", still running: {', '.join(running)}" if running else ""))
PYEOF
[ "$failed" -eq 0 ] && echo "Ready: open $URL/" || echo "Not ready: fix the FAIL lines above."
exit "$failed"
