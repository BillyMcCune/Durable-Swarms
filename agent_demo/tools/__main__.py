"""Shell commands an agent uses to work with subagents.

The wrappers in tools/bin put spawn_subagent, subagent_status and wait_subagents
on the agent's PATH. Each reads the agent's current identity from the harness
directory, so its requests are fenced like any other worker request.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from urllib.error import URLError

from durable_swarms.client import RemoteError

from ..runtime import TOOL_RETRY_DELAYS, robust_request


# Tool requests ride out a short orchestrator outage like any other worker request.
def request(url, path, body, timeout=30):
    return robust_request(url, path, body, timeout=timeout, delays=TOOL_RETRY_DELAYS)


def main():
    parser = argparse.ArgumentParser(prog=sys.argv[1] if len(sys.argv) > 1 else "tool")
    parser.add_argument("command", choices=["spawn_subagent", "subagent_status", "wait_subagents"])
    parser.add_argument("--problems", help="comma-separated problem ids to hand over")
    parser.add_argument("--instructions", default="", help="extra guidance for the subagent")
    args = parser.parse_args()
    harness = Path(os.environ["AGENT_DEMO_HARNESS"])
    identity = json.loads((harness / "identity.json").read_text())
    url, turn = identity.pop("url"), identity.pop("turn")
    try:
        if args.command == "spawn_subagent":
            if not args.problems:
                parser.error("--problems is required, for example: spawn_subagent --problems two_sum,lru_cache")
            problems = [p.strip() for p in args.problems.split(",") if p.strip()]
            # The operation id depends only on durable facts (agent, turn, position in
            # the turn), so re-running this command after a crash cannot spawn twice.
            counter = harness / "spawn_count"
            index = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(index + 1))
            op_id = hashlib.sha256(json.dumps([identity["task_id"], identity["worker_id"], turn, index]).encode()).hexdigest()
            receipt = request(url, "/spawn", {**identity, "op_id": op_id, "problems": problems,
                                              "instructions": args.instructions}, timeout=30)
            print(f"started subagent {receipt['child_id']} ({receipt['label']}) on: {', '.join(receipt['problems'])}")
        elif args.command == "subagent_status":
            children = request(url, "/manage", identity, timeout=30)["children"]
            if not children:
                print("you have no subagents")
            for child_id, child in sorted(children.items()):
                grades = (child.get("result") or {}).get("problems") or {}
                results = ", ".join(f"{p} {g['passed']}/{g['total']}" for p, g in grades.items()) or "no results yet"
                print(f"{child_id}: state={child['state']} vm={child['vm_id']} problems={','.join(child['problems'])} | {results}")
        else:
            (harness / "suspend").write_text("1")
            print("Pausing until all your subagents have finished. Their results will be in the next message.")
    except RemoteError as exc:
        print(f"error: {exc}")
        return 1
    except (URLError, OSError) as exc:
        print(f"error: the orchestrator could not be reached ({exc}); run the command again")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
