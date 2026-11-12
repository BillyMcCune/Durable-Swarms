"""Command line for the agent demo: orchestrator, VM daemon, worker, and client."""

import argparse
import json
import logging
from pathlib import Path
import sys

from durable_swarms.client import RemoteError, request

HERE = Path(__file__).parent
DEFAULT_URL = "http://127.0.0.1:8081"
STATIC = (HERE / "static", {"/": ("index.html", "text/html; charset=utf-8"),
                            "/demo.js": ("demo.js", "text/javascript; charset=utf-8"),
                            "/demo.css": ("demo.css", "text/css; charset=utf-8")})


# Parse the CLI and run the selected service or client request.
# Output: Process exit status; client commands print JSON.
def main():
    parser = argparse.ArgumentParser(prog="agent_demo", description="Durable Swarms with real coding agents")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("orchestrator", help="run the orchestrator and dashboard")
    server.add_argument("--config", default=str(HERE / "config.json"))
    for name in ("vm", "worker", "submit", "status"):
        child = sub.add_parser(name)
        child.add_argument("--url", default=DEFAULT_URL)
        if name == "vm":
            child.add_argument("--id", required=True)
            child.add_argument("--interval", type=float, default=0.5)
        if name == "submit":
            child.add_argument("--agents", type=int, default=1, help="number of independent lead agents")
            child.add_argument("--seed", type=int, default=1, help="problem-set seed of the first agent")
            child.add_argument("--model", help="Ollama model tag; defaults to the orchestrator's")
            child.add_argument("--problems", help="comma-separated problem ids, instead of a seeded sample")
            child.add_argument("--name", default="run", help="task id prefix; reuse it to retry safely")
            child.add_argument("--instructions", help="extra operator guidance given to each lead agent")
        if name == "status":
            child.add_argument("task_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "orchestrator":
        from durable_swarms.server import serve

        from .engine import AgentEngine
        with open(args.config) as source:
            config = json.load(source)
        serve(config, engine_cls=AgentEngine,
              post_routes=("spawn", "agent_log", "agent_resume", "suspend", "llm_call", "crash_vm", "crash_agent", "scenario", "stop_run",
                           "clear_runs", "review"),
              get_routes={"/demo/state": lambda engine, store, query: engine.demo_state(),
                          "/demo/trajectory": lambda engine, store, query:
                              engine.trajectory(query["task_id"][0], query["worker_id"][0]),
                          "/demo/drill": lambda engine, store, query: engine.drill(query)},
              static=STATIC)
        return 0
    if args.command == "vm":
        from durable_swarms.runtime import run_vm
        if args.interval <= 0:
            parser.error("interval must be positive")
        return run_vm(args.url, args.id, args.interval,
                      worker_cmd=[sys.executable, "-m", "agent_demo", "worker", "--url", args.url])
    if args.command == "worker":
        from .runtime import worker_main
        return worker_main(args.url)
    if args.command == "submit":
        created = []
        for index in range(args.agents):
            spec = {"seed": args.seed + index}
            if args.model:
                spec["model"] = args.model
            if args.instructions:
                spec["instructions"] = args.instructions
            if args.problems:
                spec["problems"] = [p.strip() for p in args.problems.split(",") if p.strip()]
            body = {"spec": spec, "task_id": f"{args.name}-{args.seed + index}"}
            created.append(request(args.url, "/submit", body)["task_id"])
        result = {"submitted": created}
    else:
        task = request(args.url, f"/status?task_id={args.task_id}")
        result = {"name": task["name"], "status": task["status"], "reason": task["reason"], "result": task["result"],
                  "workers": {w["id"]: {"label": w["label"], "state": w["state"], "vm_id": w["vm_id"], "attempt": w["attempt"],
                                        "problems": w["node"]["problems"]} for w in task["workers"].values()}}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RemoteError, OSError, ValueError) as exc:
        sys.exit(str(exc))
