import argparse
import json
import logging
import sys
from urllib.parse import urlencode

from .client import RemoteError, request


# Parse the CLI and dispatch to a long-running service or a single API request.
# Client commands print JSON; service and worker commands return their exit status.
def main():
    parser = argparse.ArgumentParser(description="Durable workflow simulations")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("orchestrator")
    server.add_argument("--config", default="examples/cluster.json")
    for name in ("vm", "worker", "submit", "status", "events", "vms", "stop", "recreate"):
        child = sub.add_parser(name)
        child.add_argument("--url", default="http://127.0.0.1:8080")
        if name == "vm":
            child.add_argument("--id", required=True)
            child.add_argument("--interval", type=float, default=0.5)
        if name == "submit":
            child.add_argument("spec")
            child.add_argument("--task-id", help="reuse this id to safely retry submission")
        if name in {"status", "events", "stop"}:
            child.add_argument("task_id")
        if name == "events":
            child.add_argument("--worker-id")
            child.add_argument("--after", type=int, default=0)
            child.add_argument("--limit", type=int, default=100)
        if name == "stop":
            child.add_argument("worker_id")
        if name == "recreate":
            child.add_argument("vm_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "orchestrator":
        from .server import serve
        with open(args.config) as source:
            serve(json.load(source))
        return 0
    if args.command == "vm":
        from .runtime import run_vm
        if args.interval <= 0:
            parser.error("interval must be positive")
        return run_vm(args.url, args.id, args.interval)
    if args.command == "worker":
        from .runtime import worker_main
        return worker_main(args.url)
    if args.command == "submit":
        with open(args.spec) as source:
            body = {"spec": json.load(source)}
        if args.task_id:
            body["task_id"] = args.task_id
        result = request(args.url, "/submit", body)
    elif args.command in {"status", "events"}:
        query = {"task_id": args.task_id}
        if args.command == "events":
            query.update(after=args.after, limit=args.limit)
            if args.worker_id:
                query["worker_id"] = args.worker_id
        result = request(args.url, "/" + args.command + "?" + urlencode(query))
    elif args.command == "vms":
        result = request(args.url, "/vms")
    elif args.command == "stop":
        result = request(args.url, "/stop", {"task_id": args.task_id, "worker_id": args.worker_id})
    else:
        result = request(args.url, "/recreate", {"vm_id": args.vm_id})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RemoteError, OSError, ValueError) as exc:
        sys.exit(str(exc))
