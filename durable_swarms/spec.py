"""Validation and deterministic expansion of the application workflow format."""

import copy
import math
import random


FAULTS = {"worker_exit", "vm_loss", "stall", "retry_loop", "invalid_result", "effect_interruption"}


# Return a normalized copy of a workflow, validating workers, steps, dependencies,
# and fault targets. Seeded duration ranges are resolved without modifying the input;
# invalid specifications raise ValueError before a workload is created.
# Inputs:
#   spec: Workflow dictionary with leader, optional workers, seed, and faults.
# Output: Independent normalized dictionary with defaults and resolved durations;
#         raises ValueError for invalid workflow definitions.
def validate(spec):
    """Return a normalized spec. Seeded delays are independent of worker placement."""
    spec = copy.deepcopy(spec)
    if not isinstance(spec, dict):
        raise ValueError("workflow must be a JSON object")
    seed = spec.setdefault("seed", 0)
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if "leader" not in spec or not isinstance(spec["leader"], dict):
        raise ValueError("leader must be an object")
    children = spec.setdefault("workers", [])
    if not isinstance(children, list):
        raise ValueError("workers must be a list")
    nodes = [spec["leader"], *children]
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise ValueError("each worker needs a nonempty string id")
        if node["id"] in ids:
            raise ValueError("worker ids must be unique")
        ids.add(node["id"])
    leader = spec["leader"]["id"]
    for node in nodes:
        deps = node.setdefault("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise ValueError("depends_on must be a list of worker ids")
        if node["id"] == leader and deps:
            raise ValueError("leader dependencies are implicit: it waits for all children")
        if any(d not in ids or d == leader or d == node["id"] for d in deps):
            raise ValueError("subtask dependencies must refer to other subtasks")
        steps = node.setdefault("steps", [])
        if not isinstance(steps, list):
            raise ValueError("steps must be a list")
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("type") not in {"compute", "effect"}:
                raise ValueError("step type must be compute or effect")
            seconds = step.setdefault("seconds", 0)
            if isinstance(seconds, list) and len(seconds) == 2:
                low, high = seconds
                if any(type(x) not in (int, float) or not math.isfinite(x) for x in seconds) or not 0 <= low <= high:
                    raise ValueError("duration range must be finite and nonnegative")
                seconds = random.Random(f"{seed}:{node['id']}:{index}").uniform(low, high)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                raise ValueError("seconds must be a finite nonnegative number or range")
            step["seconds"] = seconds
            if step["type"] == "effect":
                if not isinstance(step.get("key"), str) or not step["key"] or type(step.get("delta")) is not int:
                    raise ValueError("effects require a nonempty key and an integer delta")
        node.setdefault("result", f"{node['id']} complete")
    graph = {n["id"]: n["depends_on"] for n in nodes}
    visiting, visited = set(), set()

    # Traverse dependency edges, detecting cycles via the current recursion path.
    # Previously completed nodes are skipped so shared dependencies are visited once.
    # Inputs:
    #   node: Worker ID in the dependency graph captured from validate.
    # Output: None; updates visiting/visited sets or raises ValueError for a cycle.
    def visit(node):
        if node in visiting:
            raise ValueError("subtask dependencies contain a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    faults = spec.setdefault("faults", [])
    if not isinstance(faults, list):
        raise ValueError("faults must be a list")
    for fault in faults:
        if not isinstance(fault, dict) or fault.get("worker") not in ids or fault.get("kind") not in FAULTS:
            raise ValueError("fault must name a known worker and supported kind")
        attempt, step = fault.setdefault("attempt", 1), fault.setdefault("step", 0)
        node = next(n for n in nodes if n["id"] == fault["worker"])
        if type(attempt) is not int or attempt < 1 or type(step) is not int or not 0 <= step < len(node["steps"]):
            raise ValueError("fault needs a positive attempt and a valid step index")
        if fault["kind"] == "effect_interruption" and node["steps"][step]["type"] != "effect":
            raise ValueError("effect_interruption must target an effect step")
    return spec
