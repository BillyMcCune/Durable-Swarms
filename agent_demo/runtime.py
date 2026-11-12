"""Worker process for one agent attempt on a VM.

The VM daemon (durable_swarms.runtime.run_vm) starts this process for each
assignment. It rebuilds the agent's workspace and conversation from the durable
log, runs the agent, and hands its solutions to the orchestrator, which grades
them on the hidden tests. It keeps no state the orchestrator does not also hold.
"""

import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.error import URLError

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
os.environ.setdefault("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "4")

from durable_swarms.client import RemoteError, request

from .problems import bank

HERE = Path(__file__).parent
# A minute of retries at short intervals: long enough for an agent to sit out an
# orchestrator restart or a network cut, and frequent enough that it carries on
# (or learns that it has been superseded) as soon as it can be heard again.
RETRY_DELAYS = (0.2,) * 300
TOOL_RETRY_DELAYS = (0.2,) * 90   # tools run inside a command, which has a 30s time limit


# Call the orchestrator, retrying connection failures and 503s. All worker
# endpoints are idempotent for a given attempt, so a retry is always safe.
# A 409 (stale attempt) or other API error is raised immediately.
def robust_request(url, path, body, timeout=30, delays=RETRY_DELAYS):
    for delay in (*delays, None):
        try:
            return request(url, path, body, timeout=timeout)
        except RemoteError as exc:
            if exc.code != 503 or delay is None:
                raise
        except (URLError, OSError, ValueError):
            if delay is None:
                raise
        time.sleep(delay)


def workspace_for(vm_id, task_id, worker_id):
    home = Path(os.environ.get("AGENT_DEMO_HOME", Path.home() / ".agent_demo"))
    return home / "ws" / vm_id / task_id / worker_id


# Run one assignment to completion, suspension, or supersession.
# Inputs:
#   url: Orchestrator base URL.
#   job: Assignment from /poll: {"task_id", "worker", "faults"}.
#   vm_id, session: Identity of the hosting VM daemon, used to fence requests.
# Output: Exit code 0; request and agent failures propagate as exceptions.
def execute(url, job, vm_id, session, interval=None):
    from minisweagent.environments.local import LocalEnvironment

    from .agent import DurableAgent, restore
    from .llm import build_model

    worker, node = job["worker"], job["worker"]["node"]
    identity = {"task_id": job["task_id"], "worker_id": worker["id"], "attempt": worker["attempt"],
                "vm_id": vm_id, "session": session}

    def call(name, _timeout=30, **fields):
        return robust_request(url, "/" + name, {**identity, **fields}, timeout=_timeout)

    call("start")
    call("manage")
    if worker["next_step"] == 1:
        # Already graded and checkpointed; rescheduled only to complete after subagents.
        call("finish")
        return 0

    records = call("agent_resume")["records"]
    workspace = workspace_for(vm_id, job["task_id"], worker["id"])
    shutil.rmtree(workspace, ignore_errors=True)
    bank.materialize(node["problems"], workspace)
    restore(workspace, records)
    harness = workspace / ".harness"
    harness.mkdir(exist_ok=True)

    def before_action(turn):
        (harness / "identity.json").write_text(json.dumps({**identity, "url": url, "turn": turn}))
        (harness / "spawn_count").unlink(missing_ok=True)
        call("manage")

    def report(**fields):
        try:
            request(url, "/llm_call", {"task_id": job["task_id"], "worker_id": worker["id"], "vm_id": vm_id, **fields}, timeout=3)
        except Exception:
            logging.debug("could not report LLM telemetry", exc_info=True)

    problems = {p: bank.load_bank()[p] for p in node["problems"]}
    env = {"PATH": os.pathsep.join([str(HERE / "tools" / "bin"), str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
           "PYTHONPATH": str(HERE.parent), "PYTHONDONTWRITEBYTECODE": "1",
           "AGENT_DEMO_PYTHON": sys.executable, "AGENT_DEMO_HARNESS": str(harness), "PAGER": "cat"}
    agent = DurableAgent(
        build_model(node["model"], report), LocalEnvironment(cwd=str(workspace), env=env, timeout=30),
        call=call, workspace=workspace, problems=problems, before_action=before_action,
        system_template=(HERE / "prompts" / "system.j2").read_text(),
        instance_template=(HERE / "prompts" / "instance.j2").read_text(),
        step_limit=node["step_limit"], cost_limit=0, max_consecutive_format_errors=4)
    status = agent.run_durable(
        records, worker_id=worker["id"], parent_id=worker["parent_id"], depth=node["depth"],
        instructions=node["instructions"], can_spawn=node["can_spawn"],
        max_subagents=node.get("max_children", 3), problems=list(problems.values()))
    if status == "suspended":
        logging.info("%s suspended until its subagents finish", worker["id"])
        return 0

    # The orchestrator grades the solutions on the hidden tests and checkpoints the
    # result. An agent that said it was done has already been through this check.
    review = agent.final_review or call("review", _timeout=300, solutions=agent.solutions(), final=True,
                                        exit_status=status, turns=agent.n_calls)
    agent.record("grade", [], grades=review["grades"], exit_status=status, accepted=review["accepted"])
    state = call("finish")["state"]
    logging.info("%s finished (%s): %s", worker["id"], state,
                 ", ".join(f"{p} {g['passed']}/{g['total']}" for p, g in review["grades"].items()))
    return 0


# Read an assignment from stdin and run it. A stale attempt stops quietly; any
# other failure exits non-zero so the lease expires and the orchestrator recovers.
# Inputs:
#   url: Orchestrator base URL; the assignment JSON arrives on stdin.
# Output: Process exit code: 0 on completion, suspension or supersession, else 23.
def worker_main(url):
    payload = json.load(sys.stdin)
    for noisy in ("LiteLLM", "litellm", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return execute(url, **payload)
    except RemoteError as exc:
        logging.warning("agent stopped: %s", exc)
        return 0 if exc.code == 409 else 23
    except Exception:
        logging.exception("agent failed or lost contact; its lease will expire")
        return 23
