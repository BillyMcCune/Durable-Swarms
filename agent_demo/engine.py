"""Orchestrator engine for real coding agents.

AgentEngine reuses the simulation Engine's leases, attempt fencing, placement,
failure detection and recovery unchanged. It replaces the fixed workflow format
with agents that decide at run time what to do: each workload is one lead agent
given a set of problems, and any agent may spawn subagents, suspend until they
finish, and append its conversation to a durable per-agent log.
"""

import json
from pathlib import Path
import tempfile
import time
import uuid

from durable_swarms.engine import ACTIVE, TERMINAL, APIError, Engine

from . import sandbox
from .problems import bank

DEFAULT_LIMITS = {"leader_steps": 60, "sub_steps": 40, "max_depth": 2, "max_workers": 16, "max_reviews": 2,
                  "max_children": 3}
DEFAULT_MODEL = "qwen2.5-coder:7b"
FINISHED = TERMINAL | {"stopped"}
# What each scenario is meant to demonstrate, shown at the top of its walkthrough.
SHOWS = {
    "agent": "The orchestrator finds a dead agent by its lease, and the agent is rebuilt from the durable log without repeating any model call.",
    "vm": "Every agent on a lost VM is found by the VM's lease and rebuilt on other VMs, using the slots kept free for recovery.",
    "spawn_crash": "Exactly-once: a spawn command that runs twice, before and after a crash, still creates one subagent.",
    "spawn": "How a subagent comes to exist, from the model's command to the result reaching its parent.",
}
KEPT = ("crashed_at", "down_until")   # VM facts that outlive a re-registration
NAMES = ("Alice", "Bob", "Charlie", "Diana", "Ethan", "Fiona", "George", "Hannah", "Ivan", "Julia", "Kevin",
         "Laura", "Mason", "Nina", "Oscar", "Priya", "Quinn", "Rosa", "Sam", "Tara", "Umar", "Vera", "Will",
         "Xena", "Yusuf", "Zoe")


class AgentEngine(Engine):
    # Create the demo-only tables beside the base store's tables.
    # Inputs:
    #   store, config, clock: As for Engine; config may also carry "model" and "limits".
    # Output: None; raises ValueError for invalid base configuration.
    def __init__(self, store, config, clock=time.time):
        super().__init__(store, config, clock)
        self.model = config.get("model", DEFAULT_MODEL)
        self.limits = {**DEFAULT_LIMITS, **config.get("limits", {})}
        self.reserve = config.get("recovery_reserve", 0)
        if type(self.reserve) is not int or self.reserve < 0:
            raise ValueError("recovery_reserve must be a nonnegative integer")
        for statement in (
            """CREATE TABLE IF NOT EXISTS agent_log (
                task_id TEXT NOT NULL, worker_id TEXT NOT NULL, seq INTEGER NOT NULL,
                attempt INTEGER NOT NULL, vm_id TEXT NOT NULL, kind TEXT NOT NULL,
                ts REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY(task_id, worker_id, seq))""",
            """CREATE TABLE IF NOT EXISTS spawns (
                op_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL, receipt TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS llm_calls (
                call_id TEXT PRIMARY KEY, task_id TEXT, worker_id TEXT, vm_id TEXT, model TEXT,
                started REAL NOT NULL, ended REAL, latency REAL, prompt_tokens INTEGER,
                completion_tokens INTEGER, ok INTEGER)""",
            """CREATE TABLE IF NOT EXISTS problem_checks (
                task_id TEXT NOT NULL, worker_id TEXT NOT NULL, problem_id TEXT NOT NULL,
                passed INTEGER NOT NULL, total INTEGER NOT NULL, ts REAL NOT NULL,
                PRIMARY KEY(task_id, worker_id, problem_id))""",
        ):
            store.db.execute(statement)
        self.spawn_trap = None   # armed "crash mid-spawn" scenario: {"since", "t0"}
        self.resume_after_restart()

    # An orchestrator that has just started was not watching while it was down, so
    # it cannot hold the heartbeats it missed against anyone. Every VM and agent
    # lease that was still valid when it stopped gets one fresh lease period.
    # Nothing was reassigned in the meantime, so no agent can end up with two
    # owners; a VM that really died is still recovered when the fresh lease runs out.
    def resume_after_restart(self):
        with self.store.transaction():
            now = self.clock()
            vms, tasks = self.store.all("vms"), self.store.active_workloads()
            if not vms and not tasks:
                return
            heard = [v.get("last_heartbeat") or 0 for v in vms]
            heard.append(self.store.db.execute("SELECT COALESCE(MAX(timestamp), 0) FROM events").fetchone()[0])
            stopped = min(now, max(heard))
            down = now - stopped
            if down <= 0:
                return
            leases = {"vms": 0, "agents": 0}
            for vm in vms:
                if stopped < vm["expires"] < now + self.lease:
                    vm["expires"] = now + self.lease
                    self.store.put("vms", vm["id"], vm)
                    leases["vms"] += 1
            for task in tasks:
                for worker in task["workers"].values():
                    if worker["state"] in ACTIVE and stopped < worker["lease_until"] < now + self.lease:
                        worker["lease_until"] = now + self.lease
                        worker["last_progress"] += down
                        leases["agents"] += 1
            for task in tasks:
                self.event(task, "orchestrator_restarted", down=round(down, 1), leases=leases)
                self.save(task)

    # Turn a demo request into the workload specification stored in the log.
    # Inputs:
    #   spec: {"seed", "problems" or "mix", "model", "limits", "instructions"}; all optional.
    # Output: Normalized spec with a single lead agent; raises ValueError if invalid.
    def normalize(self, spec):
        if not isinstance(spec, dict):
            raise ValueError("spec must be a JSON object")
        seed = spec.get("seed", 0)
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        problems = spec["problems"] if spec.get("problems") is not None else bank.sample(seed, spec.get("mix"))
        known = bank.load_bank()
        if (not isinstance(problems, list) or not problems or len(set(problems)) != len(problems)
                or any(p not in known for p in problems)):
            raise ValueError("problems must be a nonempty list of distinct known problem ids")
        limits = {**self.limits, **(spec.get("limits") or {})}
        if set(limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v < 0 for v in limits.values()) \
                or min(limits["leader_steps"], limits["sub_steps"], limits["max_workers"]) < 1:
            raise ValueError("limits must be nonnegative integers for " + ", ".join(sorted(DEFAULT_LIMITS)))
        model = spec.get("model") or self.model
        if not isinstance(model, str):
            raise ValueError("model must be a string")
        instructions = spec.get("instructions") or ""
        if not isinstance(instructions, str):
            raise ValueError("instructions must be text")
        leader = self.node("agent", problems, instructions[:2000], 0, model, limits)
        return {"seed": seed, "model": model, "limits": limits, "problems": problems,
                "leader": leader, "workers": [], "faults": []}

    # Build the per-agent description handed to a VM with each assignment.
    def node(self, worker_id, problems, instructions, depth, model, limits):
        return {"id": worker_id, "steps": [{"type": "agent"}], "depends_on": [], "problems": problems,
                "instructions": instructions, "depth": depth, "model": model,
                "step_limit": limits["sub_steps"] if depth else limits["leader_steps"],
                "can_spawn": depth < limits["max_depth"],
                "max_children": limits.get("max_children", DEFAULT_LIMITS["max_children"])}

    # Durably create a workload with its queued lead agent. Reusing a task ID is
    # idempotent only when the normalized specification matches.
    # Inputs:
    #   body: {"spec": demo spec, "task_id": optional stable id}.
    # Output: {"task_id": str}.
    def submit(self, body):
        spec = self.normalize(body["spec"])
        task_id = body.get("task_id") or str(uuid.uuid4())
        if not isinstance(task_id, str) or not task_id:
            raise APIError("task_id must be a nonempty string", 400)
        with self.store.transaction():
            existing = self.store.get("workloads", task_id)
            if existing:
                if existing["spec"] != spec:
                    raise APIError("task_id already has a different specification")
                return {"task_id": task_id}
            # Workloads are named Alice, Bob, ... in submission order; agents are
            # "<Name> Lead" and "<Name> Sub n". Names are display labels, not identities.
            taken = {t.get("name") for t in self.store.all("workloads")}
            name = next(n for round_ in range(1, len(taken) + 2)
                        for n in (base if round_ == 1 else f"{base} {round_}" for base in NAMES) if n not in taken)
            leader = self.new_worker(spec["leader"])
            leader["label"] = f"{name} Lead"
            task = {"id": task_id, "name": name, "spec": spec, "status": "queued", "version": 0,
                    "workers": {leader["id"]: leader}, "result": None, "reason": None}
            self.event(task, "workload_created", spec=spec)
            self.save(task)
        return {"task_id": task_id}

    # Recover as the base engine does, and close the failed attempt's open LLM
    # calls so the dashboard does not show them as still in flight.
    def recover(self, task, worker, reason):
        lost = task["status"] not in TERMINAL and worker["state"] in ACTIVE
        if lost:
            self.store.db.execute("UPDATE llm_calls SET ended=?, ok=0 WHERE task_id=? AND worker_id=? AND ended IS NULL",
                                  (self.clock(), task["id"], worker["id"]))
        super().recover(task, worker, reason)
        if lost:
            worker["restart_after"] = worker["attempt"]  # marks it as recovery work until it is placed again

    # Recovery work: an agent whose last attempt was lost, or one that must run to
    # approve the restart of a lost agent below it.
    def urgent(self, task, worker):
        return (worker.get("restart_after") == worker["attempt"]
                or any(w["state"] == "needs_recovery" and self.descends(task, w, worker["id"]) for w in task["workers"].values()))

    # Keep `recovery_reserve` slots free for recovery work. Ordinary work is placed
    # only while more than that many slots are free; recovery work may use any free
    # slot and goes first. If nothing is running at all, the reserve is waived so
    # that a small pool cannot stall.
    def placeable(self, task, candidates, available, used):
        first = [w for w in candidates if self.urgent(task, w)]
        free = sum(self.pool[vm["id"]] - used[vm["id"]] for vm in available)
        if free > self.reserve or not any(used.values()):
            return first + [w for w in candidates if w not in first]
        return first

    # Refuse registration while a VM is down after an operator-triggered crash.
    def register(self, body):
        with self.store.transaction():
            old = self.store.get("vms", body.get("vm_id"))
            remaining = (old or {}).get("down_until", 0) - self.clock()
            if remaining > 0:
                raise APIError(f"VM is down after a crash and restarts in {remaining:.0f}s")
        vm = super().register(body)
        kept = {key: old[key] for key in KEPT if old and key in old}
        if kept:
            with self.store.transaction():  # keep when it last crashed, for the walkthrough
                vm = {**(self.store.get("vms", vm["id"]) or vm), **kept}
                self.store.put("vms", vm["id"], vm)
        return vm

    # A crashed VM cannot talk to the orchestrator: every request carrying its
    # session is rejected until its downtime has passed, exactly as if the machine
    # had lost power. The orchestrator is not told; it finds out from the silence.
    def vm(self, body):
        vm = super().vm(body)
        if vm.get("down_until", 0) > self.clock():
            raise APIError("VM is down")
        return vm

    # An attempt whose process was killed from the dashboard is dead: nothing it
    # might still send is accepted.
    def worker(self, body):
        task, worker = super().worker(body)
        if worker.get("killed_attempt") == worker["attempt"]:
            raise APIError("worker attempt expired or superseded")
        return task, worker

    # Hide a killed attempt from its VM daemon, which then terminates the process
    # and stops listing it in heartbeats, so the attempt's lease is no longer renewed.
    def poll(self, body):
        reply = super().poll(body)
        reply["jobs"] = [j for j in reply["jobs"] if j["worker"].get("killed_attempt") != j["worker"]["attempt"]]
        return reply

    def last_seq(self):
        return self.store.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def vm_label(self, vm_id):
        return f"VM {list(self.pool).index(vm_id) + 1}" if vm_id in self.pool else (vm_id or "no VM")

    # Note an injected failure in the event log. This is the operator's record of
    # what was done, not something the recovery logic reads: detection still has
    # to happen through the missing heartbeats.
    def lose_process(self, task, worker, vm, cause, **extra):
        self.store.db.execute("UPDATE llm_calls SET ended=?, ok=0 WHERE task_id=? AND worker_id=? AND ended IS NULL",
                              (self.clock(), task["id"], worker["id"]))
        self.event(task, "process_lost", worker, cause=cause, vm_id=vm["id"], pid=self.pid(vm, task, worker), **extra)

    @staticmethod
    def pid(vm, task, worker):
        return next((p.get("pid") for p in (vm or {}).get("processes", []) if p["task_id"] == task["id"]
                     and p["worker_id"] == worker["id"] and p["attempt"] == worker["attempt"]), None)

    # Crash a VM from the dashboard. The VM goes silent for a while: its daemon's
    # requests are refused, so it kills its agent processes and cannot rejoin until
    # the downtime has passed. Nothing is recovered here. The orchestrator notices
    # only when the VM's lease runs out without a heartbeat, as for a real outage.
    # Inputs:
    #   body: {"vm_id": str, "seconds": optional downtime, default config crash_seconds}.
    # Output: {"ok": True, "down_seconds": number, "drill": walkthrough descriptor}.
    def crash_vm(self, body):
        vm_id = body.get("vm_id")
        seconds = body.get("seconds", self.config.get("crash_seconds", 20))
        if vm_id not in self.pool or type(seconds) not in (int, float) or not 0 < seconds <= 3600:
            raise APIError("crash_vm needs a configured vm_id and a downtime of up to an hour", 400)
        with self.store.transaction():
            now = self.clock()
            vm = self.store.get("vms", vm_id)
            if not vm or vm["expires"] <= now or vm.get("down_until", 0) > now:
                raise APIError(f"{self.vm_label(vm_id)} is not online, so there is nothing to crash")
            since = self.last_seq()
            for task in self.store.active_workloads():
                lost = [w for w in task["workers"].values() if w["vm_id"] == vm_id and w["state"] in ACTIVE]
                for worker in lost:
                    self.lose_process(task, worker, vm, "VM crash")
                if lost:
                    self.save(task)
            vm.update(down_until=now + seconds, crashed_at=now)
            self.store.put("vms", vm_id, vm)
        return {"ok": True, "down_seconds": seconds,
                "drill": {"kind": "vm", "since": since, "t0": now, "vm_id": vm_id}}

    # Kill one agent's process from the dashboard while its VM stays healthy. The
    # orchestrator is not told: the VM's heartbeats stop listing the agent, its
    # lease is not renewed, and the scheduler recovers it when the lease expires.
    # Inputs:
    #   body: {"task_id": str, "worker_id": str}.
    # Output: {"ok": True, "drill": walkthrough descriptor}; 409 if nothing is running.
    def crash_agent(self, body):
        with self.store.transaction():
            now = self.clock()
            task = self.task(body.get("task_id"))
            worker = task["workers"].get(body.get("worker_id"))
            if not worker:
                raise APIError("unknown worker", 404)
            vm = self.store.get("vms", worker["vm_id"]) if worker["vm_id"] else None
            if (task["status"] in FINISHED or worker["state"] not in ACTIVE or not vm or vm["expires"] <= now
                    or vm.get("down_until", 0) > now or worker.get("killed_attempt") == worker["attempt"]):
                raise APIError(f"{worker['label']} has no running process to crash")
            since = self.last_seq()
            worker["killed_attempt"] = worker["attempt"]
            self.lose_process(task, worker, vm, "agent crash")
            self.save(task)
        return {"ok": True, "drill": {"kind": "agent", "since": since, "t0": now,
                                      "task_id": task["id"], "worker_id": worker["id"]}}

    # Stop a whole run from the dashboard: fence every unfinished agent so the VM
    # daemons kill their processes, and score whatever finished agents reported.
    # Stopping a run that has already ended changes nothing.
    # Inputs:
    #   body: {"task_id": str}.
    # Output: {"ok": True, "status": the run's status after the request}.
    def stop_run(self, body):
        with self.store.transaction():
            task = self.task(body.get("task_id"))
            if task["status"] not in FINISHED:
                self.store.db.execute("UPDATE llm_calls SET ended=?, ok=0 WHERE task_id=? AND ended IS NULL",
                                      (self.clock(), task["id"]))
                self.terminate(task, "stopped", "stopped by operator")
                task["result"] = self.scorecard(task)
                self.save(task)
            return {"ok": True, "status": task["status"]}

    # Stop runs and remove them from the board so a new round starts clean: their
    # agents lose their assignments (the VM daemons kill the processes) and every
    # record of the runs is deleted. VM registrations are kept.
    # Inputs:
    #   body: {"task_id": str} for one run, or {"all": true} for every run.
    # Output: {"ok": True, "cleared": [task ids]}.
    def clear_runs(self, body):
        with self.store.transaction():
            ids = ([t["id"] for t in self.store.all("workloads")] if body.get("all") is True
                   else [self.task(body.get("task_id"))["id"]])
            for task_id in ids:
                self.store.db.execute("DELETE FROM workloads WHERE id=?", (task_id,))
                for table in ("events", "worker_liveness", "effects", "world", "agent_log", "spawns", "llm_calls", "problem_checks"):
                    self.store.db.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
        return {"ok": True, "cleared": ids}

    def children(self, task, worker):
        return [w for w in task["workers"].values() if w["parent_id"] == worker["id"]]

    def descends(self, task, worker, ancestor_id):
        while worker["parent_id"]:
            if worker["parent_id"] == ancestor_id:
                return True
            worker = task["workers"][worker["parent_id"]]
        return False

    # Requeue failed descendants of a live agent. Returns True if anything changed;
    # the caller saves the workload.
    def authorize(self, task, caller):
        changed = False
        for worker in task["workers"].values():
            if worker["state"] == "needs_recovery" and self.descends(task, worker, caller["id"]):
                worker["state"] = "queued"
                self.event(task, "subtask_recovery_authorized", worker, by=caller["id"], by_attempt=caller["attempt"])
                changed = True
        return changed

    # What a parent is told about each of its subagents. A subagent may itself have
    # handed a problem on, so each problem's grade is the best one reached anywhere
    # below that subagent, with "by" naming the agent whose solution it is.
    def child_view(self, task, caller):
        def outcome(child):
            result = child.get("result")
            if not result:
                return result
            below = [w for w in task["workers"].values() if self.descends(task, w, child["id"])]
            problems = {}
            for problem_id, own in (result.get("problems") or {}).items():
                best, by = own, child["id"]
                for worker in below:
                    grade = ((worker.get("result") or {}).get("problems") or {}).get(problem_id)
                    if grade and grade["passed"] > best["passed"]:
                        best, by = grade, worker["id"]
                problems[problem_id] = {**best, "by": by}
            return {**result, "problems": problems}

        return {w["id"]: {"label": w["label"], "state": w["state"], "problems": w["node"]["problems"], "vm_id": w["vm_id"],
                          "attempt": w["attempt"], "result": outcome(w)}
                for w in self.children(task, caller)}

    # Start a subagent on a subset of the caller's problems. The operation ID makes
    # this exactly-once: a replayed request returns the original receipt.
    # Inputs:
    #   body: Worker identity plus op_id, problems, and optional instructions.
    # Output: {"child_id": str, "label": display name, "problems": list}.
    def spawn(self, body):
        with self.store.transaction():
            task, parent = self.worker(body)
            op_id, problems, instructions = body.get("op_id"), body.get("problems"), body.get("instructions") or ""
            if not isinstance(op_id, str) or not op_id or not isinstance(instructions, str):
                raise APIError("spawn needs an op_id string and text instructions", 400)
            row = self.store.db.execute("SELECT receipt FROM spawns WHERE op_id=?", (op_id,)).fetchone()
            if row:
                self.note_replay(task, parent, op_id)
                return json.loads(row[0])
            limits, node = task["spec"]["limits"], parent["node"]
            if (not isinstance(problems, list) or not problems or len(set(problems)) != len(problems)
                    or any(p not in node["problems"] for p in problems)):
                raise APIError("problems must be distinct ids from your own problem list: " + ", ".join(node["problems"]), 400)
            if not node["can_spawn"]:
                raise APIError(f"agents at depth {node['depth']} cannot start subagents", 400)
            if len(task["workers"]) >= limits["max_workers"]:
                raise APIError(f"this workload already has the maximum of {limits['max_workers']} agents", 400)
            cap = node.get("max_children", DEFAULT_LIMITS["max_children"])
            if len(self.children(task, parent)) >= cap:
                raise APIError(f"you have already started {cap} subagents, the most one agent may start. Solve the "
                               "remaining problems yourself, or wait for your subagents", 400)
            child_id = f"{parent['id']}.{len(self.children(task, parent)) + 1}"
            child = self.new_worker(self.node(child_id, problems, instructions[:2000], node["depth"] + 1,
                                              node["model"], limits), parent["id"])
            child["label"] = f"{task['name']} Sub {len(task['workers'])}"
            task["workers"][child_id] = child
            receipt = {"child_id": child_id, "label": child["label"], "problems": problems}
            self.store.db.execute("INSERT INTO spawns VALUES(?,?,?,?,?)",
                                  (op_id, task["id"], parent["id"], child_id, json.dumps(receipt)))
            self.event(task, "subtask_spawned", child, by=parent["id"], by_attempt=parent["attempt"],
                       problems=problems, operation_id=op_id)
            trapped = self.spawn_trap
            if trapped:
                # Scenario "crash mid-spawn": the receipt above is durable, and the spawner
                # dies before it can log that the command ran.
                parent["killed_attempt"] = parent["attempt"]
                self.lose_process(task, parent, self.store.get("vms", parent["vm_id"]), "crash mid-spawn",
                                  child_id=child_id, operation_id=op_id, trap=trapped["since"])
            self.save(task)
        if trapped:
            self.spawn_trap = None
        return receipt

    # Record the first time a later attempt presents an operation id that an earlier
    # attempt already used: the proof that a re-run spawn command created nothing.
    def note_replay(self, task, parent, op_id):
        row = self.store.db.execute("SELECT worker_id, payload FROM events WHERE task_id=? AND type='subtask_spawned' "
                                    "AND json_extract(payload, '$.operation_id')=?", (task["id"], op_id)).fetchone()
        first = json.loads(row["payload"]).get("by_attempt") if row else None
        if first is None or first >= parent["attempt"] or self.store.db.execute(
                "SELECT 1 FROM events WHERE task_id=? AND type='spawn_replayed' AND json_extract(payload, '$.operation_id')=?",
                (task["id"], op_id)).fetchone():
            return
        self.event(task, "spawn_replayed", parent, child_id=row["worker_id"], operation_id=op_id, first_attempt=first)
        self.save(task)

    # Let any live agent authorize recovery of its failed descendants and read
    # its direct children's states and results.
    # Inputs:
    #   body: Worker identity.
    # Output: {"children": {child_id: {state, problems, vm_id, attempt, result}}}.
    def manage(self, body):
        with self.store.transaction():
            task, caller = self.worker(body)
            if self.authorize(task, caller):
                self.save(task)
            return {"children": self.child_view(task, caller)}

    # Append one record to the caller's durable conversation log. Records are
    # strictly ordered; resending an identical record is a no-op and a different
    # record for a taken sequence number is rejected.
    # Inputs:
    #   body: Worker identity plus seq (int), kind (str), and data (object).
    # Output: {"seq": int}.
    def agent_log(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            seq, kind, data = body.get("seq"), body.get("kind"), body.get("data")
            if type(seq) is not int or seq < 0 or not isinstance(kind, str) or not isinstance(data, dict):
                raise APIError("log record needs an integer seq, a kind, and a data object", 400)
            blob = json.dumps(data, sort_keys=True)
            key = (task["id"], worker["id"])
            count = self.store.db.execute("SELECT COUNT(*) FROM agent_log WHERE task_id=? AND worker_id=?", key).fetchone()[0]
            if seq < count:
                row = self.store.db.execute("SELECT kind,data FROM agent_log WHERE task_id=? AND worker_id=? AND seq=?", (*key, seq)).fetchone()
                if row["kind"] == kind and row["data"] == blob:
                    return {"seq": seq}
                raise APIError("log record conflicts with durable history")
            if seq != count:
                raise APIError("log records must be appended in order")
            now = self.clock()
            self.store.db.execute("INSERT INTO agent_log VALUES(?,?,?,?,?,?,?,?)",
                                  (*key, seq, worker["attempt"], worker["vm_id"], kind, now, blob))
            for problem_id, check in (data.get("checks") or {}).items():
                self.store.db.execute(
                    """INSERT INTO problem_checks VALUES(?,?,?,?,?,?) ON CONFLICT(task_id,worker_id,problem_id)
                       DO UPDATE SET passed=excluded.passed, total=excluded.total, ts=excluded.ts""",
                    (*key, problem_id, int(check["passed"]), int(check["total"]), now))
            if kind == "query":
                # The reply is here, so any call still shown as open lost its end report.
                self.store.db.execute("UPDATE llm_calls SET ended=?, ok=1 WHERE task_id=? AND worker_id=? AND ended IS NULL", (now, *key))
            worker["progress"], worker["last_progress"], worker["last_write"] = seq + 1, now, now
            self.store.save_liveness(task["id"], worker)
        return {"seq": seq}

    # Return the caller's whole log so a new attempt can rebuild the agent.
    # Inputs:
    #   body: Worker identity.
    # Output: {"records": [{seq, kind, attempt, vm_id, ts, data}, ...]} in order.
    def agent_resume(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            records = self.records(task["id"], worker["id"])
            key = (task["id"], worker["id"])
            seen = self.store.db.execute("SELECT 1 FROM events WHERE task_id=? AND worker_id=? AND attempt=? AND type='log_replayed'",
                                         (*key, worker["attempt"])).fetchone()
            if records and not seen:
                # Record that this attempt was rebuilt from the log, for the dashboard.
                files = set()
                for record in records:
                    files = (files | set(record["data"].get("files") or {})) - set(record["data"].get("deleted") or [])
                lost = self.store.db.execute("SELECT 1 FROM events WHERE task_id=? AND worker_id=? AND attempt=? AND type='attempt_superseded'",
                                             (*key, worker["attempt"] - 1)).fetchone()
                turns = sum(r["kind"] == "query" for r in records)
                # What the log saved: these replies were paid for once and are not requested again.
                calls = self.store.db.execute(
                    """SELECT COALESCE(latency, 0) AS latency, COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) AS tokens
                       FROM llm_calls WHERE task_id=? AND worker_id=? AND ok=1 ORDER BY started LIMIT ?""", (*key, turns)).fetchall()
                self.event(task, "log_replayed", worker, records=len(records), files=len(files), turns=turns,
                           rerun_command=records[-1]["kind"] == "query", after="failure" if lost else "pause",
                           saved_calls=turns, saved_seconds=round(sum(c["latency"] for c in calls), 1),
                           saved_tokens=sum(c["tokens"] for c in calls))
                self.save(task)
            return {"records": records}

    def records(self, task_id, worker_id):
        rows = self.store.db.execute("SELECT seq,kind,attempt,vm_id,ts,data FROM agent_log WHERE task_id=? AND worker_id=? ORDER BY seq",
                                     (task_id, worker_id))
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    # Release the caller's slot until its subagents finish. If they already have,
    # report their results immediately and leave the caller running.
    # Inputs:
    #   body: Worker identity.
    # Output: {"state": "waiting"} or {"state": "ready", "children": {...}}.
    def suspend(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            changed = self.authorize(task, worker)
            children = self.children(task, worker)
            if all(c["state"] == "completed" for c in children):
                if changed:
                    self.save(task)
                return {"state": "ready", "children": self.child_view(task, worker)}
            worker["state"] = "waiting"
            self.event(task, "agent_suspended", worker, waiting_for=[c["id"] for c in children if c["state"] != "completed"])
            self.save(task)
        return {"state": "waiting"}

    # Wake suspended subagents whose children all finished or need recovery, then
    # run the base scheduler, which does the same for the lead agent.
    def tick(self):
        with self.store.transaction():
            for task in self.store.active_workloads():
                woken = False
                for worker in task["workers"].values():
                    if worker["state"] != "waiting" or not worker["parent_id"]:
                        continue
                    children = self.children(task, worker)
                    if all(c["state"] == "completed" for c in children) or any(c["state"] == "needs_recovery" for c in children):
                        worker["state"] = "queued"
                        woken = True
                if woken:
                    self.save(task)
        super().tick()

    # Record an agent's graded outcome as its single completed step. Retrying
    # after the step is durable is a no-op.
    # Inputs:
    #   body: Worker identity, step (must be 0), and result (object).
    # Output: {"next_step": 1}.
    def checkpoint(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            if body.get("step") != 0 or not isinstance(body.get("result"), dict):
                raise APIError("an agent checkpoints step 0 with a result object", 400)
            if worker["next_step"] == 0:
                worker["results"].append(body["result"])
                worker.update(next_step=1, progress=0, last_progress=self.clock(), last_write=self.clock())
                self.event(task, "step_completed", worker, step=0, summary=self.summary(body["result"]))
                self.save(task)
        return {"next_step": 1}

    # Check an agent that says it is done. The orchestrator grades the submitted
    # solutions on the hidden tests itself. If a problem still fails and neither the
    # agent nor a finished subagent solved it, the agent is sent back to work with
    # the failures, at most max_reviews times; otherwise the graded result becomes
    # the agent's durable checkpoint. An agent whose subagents are still running is
    # told to wait for them first.
    # Inputs:
    #   body: Worker identity, solutions {problem_id: source}, final (true when the
    #         agent cannot continue), exit_status, and turns.
    # Output: {"wait": True}, or {"accepted", "final", "feedback", "grades"}.
    def review(self, body):
        solutions, final = body.get("solutions"), bool(body.get("final"))
        if not isinstance(solutions, dict) or any(not isinstance(v, str) or len(v) > 100_000 for v in solutions.values()):
            raise APIError("review needs solutions as {problem_id: source text}", 400)
        done = {"accepted": True, "final": True, "feedback": None, "grades": {}}
        with self.store.transaction():
            task, worker = self.worker(body)
            if worker["next_step"] == 1:
                return done
            if not final and any(c["state"] != "completed" for c in self.children(task, worker)):
                return {"wait": True}
            problems = list(worker["node"]["problems"])
        known, grades = bank.load_bank(), {}
        with tempfile.TemporaryDirectory() as scratch:  # grading runs outside the store lock
            for problem_id in problems:
                path = Path(scratch) / f"{problem_id}.py"
                path.write_text(solutions.get(problem_id, ""))
                grades[problem_id] = sandbox.grade(path, known[problem_id])
        with self.store.transaction():
            task, worker = self.worker(body)  # the attempt may have been fenced while grading
            if worker["next_step"] == 1:
                return done
            family = [w for w in task["workers"].values() if self.descends(task, w, worker["id"])]
            solved_below = {p for w in family for p, g in ((w.get("result") or {}).get("problems") or {}).items()
                            if g["total"] and g["passed"] == g["total"]}
            failing = [p for p in problems if grades[p]["passed"] != grades[p]["total"] and p not in solved_below]
            limit = task["spec"]["limits"].get("max_reviews", self.limits["max_reviews"])
            used = worker.get("reviews", 0)
            worker["review"] = {p: {"passed": g["passed"], "total": g["total"]} for p, g in grades.items()}
            if failing and not final and used < limit:
                worker["reviews"] = used + 1
                self.event(task, "review_rejected", worker, failing=failing, check=used + 1, of=limit)
                self.save(task)
                return {"accepted": False, "final": False, "grades": grades,
                        "feedback": self.feedback(failing, grades, solutions, known, used + 1, limit)}
            result = {"exit_status": str(body.get("exit_status") or "Submitted"), "turns": body.get("turns") or 0, "reviews": used,
                      "problems": {p: {"passed": g["passed"], "total": g["total"], "error": g["error"],
                                       "code": "" if solutions.get(p, "") == known[p]["stub"] else solutions.get(p, "")[:20000]}
                                   for p, g in grades.items()}}
            worker["results"].append(result)
            worker.update(next_step=1, progress=0, last_progress=self.clock(), last_write=self.clock())
            self.event(task, "step_completed", worker, step=0, summary=self.summary(result), accepted=not failing)
            self.save(task)
        return {"accepted": not failing, "final": True, "feedback": None, "grades": grades}

    # Explain to an agent which problems failed the hidden tests, with one failing
    # call each (its input and what the solution returned, not the expected answer).
    @staticmethod
    def feedback(failing, grades, solutions, known, check, limit):
        lines = []
        for problem_id in failing:
            grade = grades[problem_id]
            case = next((c for c in grade["cases"] if not c["ok"]), None)
            if solutions.get(problem_id, "") == known[problem_id]["stub"]:
                detail = "not solved: solution.py is still the empty stub"
            elif grade["error"]:
                detail = grade["error"]
            else:
                detail = (f"{grade['passed']}/{grade['total']} hidden tests pass; for example the arguments "
                          f"{json.dumps(case['args'])[:300]} gave {case.get('got') or case.get('error')}")
            lines.append(f"- {problem_id}: {detail}")
        return ("The orchestrator checked your work on the hidden tests and it is NOT finished. These problems still fail:\n"
                + "\n".join(lines) + "\n\nFix them in problems/<id>/solution.py (your other problems are correct), "
                f"then finish again. This was check {check} of {limit}.")

    @staticmethod
    def summary(result):
        return {p: f"{g.get('passed', 0)}/{g.get('total', 0)}" for p, g in (result.get("problems") or {}).items()}

    # Complete an agent whose result is checkpointed. An agent with unfinished
    # subagents waits (releasing its slot) and is completed when rescheduled after
    # they finish. Completing the lead agent completes the workload.
    # Inputs:
    #   body: Worker identity.
    # Output: {"state": "completed" | "waiting"}.
    def finish(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            if worker["next_step"] != 1:
                raise APIError("agent result must be checkpointed before finishing")
            self.authorize(task, worker)
            if any(c["state"] != "completed" for c in self.children(task, worker)):
                worker["state"] = "waiting"
                self.event(task, "agent_suspended", worker, waiting_for="subagents before finishing")
                self.save(task)
                return {"state": "waiting"}
            worker["state"], worker["result"] = "completed", worker["results"][0]
            self.event(task, "worker_completed", worker, summary=self.summary(worker["result"]))
            if not worker["parent_id"]:
                task["status"], task["result"] = "completed", self.scorecard(task)
                self.event(task, "workload_completed", result=task["result"])
            self.save(task)
        return {"state": "completed"}

    # Score each of the workload's problems by the best result any agent reported.
    def scorecard(self, task):
        known, card = bank.load_bank(), {}
        for problem_id in task["spec"]["problems"]:
            best = {"passed": 0, "total": len(known[problem_id]["hidden_tests"]), "solved_by": None}
            for worker in task["workers"].values():
                grade = ((worker.get("result") or {}).get("problems") or {}).get(problem_id)
                if grade and grade.get("passed", 0) > best["passed"]:
                    best = {"passed": grade["passed"], "total": grade["total"], "solved_by": worker["id"]}
            card[problem_id] = {**best, "difficulty": known[problem_id]["difficulty"],
                                "solved": best["total"] > 0 and best["passed"] == best["total"]}
        return {"problems": card, "solved": sum(c["solved"] for c in card.values()), "total": len(card)}

    # Record the start or end of one LLM request for the dashboard. Telemetry
    # only: it is not fenced and never affects scheduling or recovery.
    # Inputs:
    #   body: {"call_id", "phase": "start" | "end", and call metadata}.
    # Output: {"ok": True}.
    def llm_call(self, body):
        call_id = body.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise APIError("llm_call needs a call_id", 400)
        with self.store.transaction():
            now = self.clock()
            if body.get("phase") == "start":
                self.store.db.execute(
                    "INSERT OR IGNORE INTO llm_calls(call_id,task_id,worker_id,vm_id,model,started) VALUES(?,?,?,?,?,?)",
                    (call_id, body.get("task_id"), body.get("worker_id"), body.get("vm_id"), body.get("model"), now))
            else:
                latency = body.get("latency")
                self.store.db.execute(  # the start report may have been lost in an outage
                    "INSERT OR IGNORE INTO llm_calls(call_id,task_id,worker_id,vm_id,model,started) VALUES(?,?,?,?,?,?)",
                    (call_id, body.get("task_id"), body.get("worker_id"), body.get("vm_id"), body.get("model"),
                     now - (latency if type(latency) in (int, float) else 0)))
                self.store.db.execute(
                    "UPDATE llm_calls SET ended=?, latency=?, prompt_tokens=?, completion_tokens=?, ok=? WHERE call_id=?",
                    (now, latency, body.get("prompt_tokens"), body.get("completion_tokens"),
                     int(bool(body.get("ok"))), call_id))
        return {"ok": True}

    # Everything the demo dashboard shows, in one consistent read.
    # Output: Base dashboard view plus per-agent log statistics, per-problem status,
    #         live visible-test checks, LLM traffic, and per-VM request counters.
    def demo_state(self):
        view = self.dashboard()
        known = bank.load_bank()
        with self.store.transaction():
            now = self.clock()
            stats = {(r["task_id"], r["worker_id"]): dict(r) for r in self.store.db.execute(
                """SELECT task_id, worker_id, COUNT(*) AS records, SUM(kind='query') AS turns,
                          MAX(ts) AS last_ts FROM agent_log GROUP BY task_id, worker_id""")}
            checks = {}
            for r in self.store.db.execute("SELECT * FROM problem_checks"):
                checks.setdefault((r["task_id"], r["worker_id"]), {})[r["problem_id"]] = {"passed": r["passed"], "total": r["total"]}
            calls = self.store.db.execute(
                """SELECT COUNT(*) AS total, COALESCE(SUM(ended IS NULL AND started > ?), 0) AS in_flight,
                          COALESCE(SUM(ended > ?), 0) AS last_minute,
                          AVG(CASE WHEN ended > ? THEN latency END) AS avg_latency,
                          COALESCE(SUM(CASE WHEN ended > ? THEN completion_tokens END), 0) AS minute_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                          COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(ok = 0), 0) AS failed FROM llm_calls""",
                (now - 300, now - 60, now - 60, now - 60)).fetchone()
            by_vm = {r["vm_id"]: {"total": r["total"], "in_flight": r["in_flight"], "done": r["done"]}
                     for r in self.store.db.execute(
                """SELECT vm_id, COUNT(*) AS total, COALESCE(SUM(ended IS NULL AND started > ?), 0) AS in_flight,
                          COALESCE(SUM(ended IS NOT NULL), 0) AS done FROM llm_calls GROUP BY vm_id""", (now - 300,))}
            open_calls = [dict(r) for r in self.store.db.execute(
                "SELECT task_id, worker_id, vm_id FROM llm_calls WHERE ended IS NULL AND started > ?", (now - 300,))]
            recent = [dict(r) for r in self.store.db.execute(
                """SELECT task_id, worker_id, vm_id, started, ended, latency, completion_tokens, ok
                   FROM llm_calls ORDER BY started DESC LIMIT 6""")]
            log_by_vm = {r[0]: r[1] for r in self.store.db.execute("SELECT vm_id, COUNT(*) FROM agent_log GROUP BY vm_id")}
            stored = {v["id"]: v for v in self.store.all("vms")}
            down = {vm_id: v.get("down_until", 0) for vm_id, v in stored.items()}
            saved = self.store.db.execute(
                """SELECT COUNT(*) AS recoveries, COALESCE(SUM(json_extract(payload, '$.saved_calls')), 0) AS calls,
                          COALESCE(SUM(json_extract(payload, '$.saved_seconds')), 0) AS seconds,
                          COALESCE(SUM(json_extract(payload, '$.saved_tokens')), 0) AS tokens
                   FROM events WHERE type='log_replayed' AND json_extract(payload, '$.after')='failure'""").fetchone()
            cards = self.cards()
            totals = {"events": self.store.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0],
                      "log_records": sum(log_by_vm.values()),
                      "spawns": self.store.db.execute("SELECT COUNT(*) FROM spawns").fetchone()[0]}
        for vm in view["vms"]:
            vm["down_for"] = max(0, round(down.get(vm["id"], 0) - now))
            # Crashed but not yet noticed: the lease is still running out.
            vm["silent"] = bool(vm["online"] and vm["down_for"])
            vm["lease_left"] = max(0, round(stored.get(vm["id"], {}).get("expires", 0) - now))
            vm["silent_for"] = round(now - (vm["last_heartbeat"] or now))
        # Slots the scheduler sees as free, and whether it is holding ordinary work back.
        free = sum(vm["capacity"] - len(vm["assignments"]) for vm in view["vms"] if vm["online"])
        held = free <= self.reserve and any(vm["assignments"] for vm in view["vms"])
        view["reserve"] = {"slots": self.reserve, "free": free}
        solved = total = 0
        for task in view["tasks"]:
            for worker in task["workers"].values():
                key = (task["id"], worker["id"])
                worker["log"] = stats.get(key, {"records": 0, "turns": 0, "last_ts": None})
                worker["checks"] = checks.get(key, {})
                worker["killed"] = worker["state"] in ACTIVE and worker.get("killed_attempt") == worker["attempt"]
                worker["held"] = (held and task["status"] not in FINISHED and worker["state"] == "queued"
                                  and not self.urgent(task, worker))
                worker["lease_left"] = max(0, round(worker["lease_until"] - now))
            task["problems"] = self.problem_status(task, known)
            solved += sum(p["status"] == "solved" for p in task["problems"].values())
            total += len(task["problems"])
        view["llm"] = {**dict(calls), "by_vm": by_vm, "open": open_calls, "recent": recent}
        view["log"] = {**totals, "by_vm": log_by_vm}
        view["score"] = {"solved": solved, "total": total}
        view["defaults"] = {"model": self.model, "limits": self.limits,
                            "timing": {"lease": self.lease, "crash": self.config.get("crash_seconds", 20)}}
        view["saved"] = dict(saved)
        view["scenarios"] = cards
        return view

    # Derive each problem's display status from the agents that hold it.
    def problem_status(self, task, known):
        out = {}
        done = task["status"] in FINISHED
        for problem_id in task["spec"]["problems"]:
            owners = [w for w in task["workers"].values() if problem_id in w["node"]["problems"]]
            grades = [(w, ((w.get("result") or {}).get("problems") or {}).get(problem_id)) for w in owners]
            winner = next((w for w, g in grades if g and g["total"] and g["passed"] == g["total"]), None)
            if winner:
                status = "solved"
            elif done or all(w["state"] == "completed" for w in owners):
                status = "failed"
            elif any(w["state"] in ACTIVE for w in owners):
                status = "working"
            else:
                status = "pending"
            best = max((g for _, g in grades if g), key=lambda g: g["passed"], default=None)
            out[problem_id] = {"status": status, "difficulty": known[problem_id]["difficulty"],
                               "title": known[problem_id]["title"], "owners": [w["id"] for w in owners],
                               "solved_by": winner["id"] if winner else None,
                               "passed": best["passed"] if best else None, "total": best["total"] if best else None}
        return out

    # One agent's durable conversation for the dashboard's trajectory view.
    # Inputs:
    #   task_id, worker_id: Agent to read. No lease is required.
    # Output: {"worker": current record, "records": ordered log, "lost_attempts": [int]}.
    def trajectory(self, task_id, worker_id):
        with self.store.transaction():
            task = self.task(task_id)
            worker = task["workers"].get(worker_id)
            if not worker:
                raise APIError("unknown worker", 404)
            lost = [row[0] for row in self.store.db.execute(
                "SELECT attempt FROM events WHERE task_id=? AND worker_id=? AND type='attempt_superseded'", (task_id, worker_id))]
            return {"worker": worker, "records": self.records(task_id, worker_id), "lost_attempts": lost}

    # ------------------------------------------------------------------ demo scenarios
    # A scenario is a failure (or a spawn) plus a walkthrough of how the system
    # handled it. The walkthrough is derived only from durable facts: events, the
    # agent log, and lease times. Nothing in it is scripted.

    # Start a scenario from the dashboard, choosing a target when none is given.
    # Inputs:
    #   body: {"kind": "agent" | "lead" | "sub" | "vm" | "spawn_crash" | "spawn"}
    #         plus an optional target ("vm_id", or "task_id" and "worker_id"). For
    #         "spawn", "after" limits the choice to subagents started after that event number.
    # Output: Walkthrough descriptor to pass to drill(); 409 if there is no target yet.
    def scenario(self, body):
        kind = body.get("kind")
        if kind == "vm":
            vm_id = body.get("vm_id")
            if not vm_id:
                with self.store.transaction():
                    vm_id = self.pick_vm()[0]
            return self.crash_vm({"vm_id": vm_id})["drill"]
        if kind in ("lead", "sub", "agent"):
            if body.get("worker_id"):
                target = {"task_id": body.get("task_id"), "worker_id": body["worker_id"]}
            else:
                with self.store.transaction():
                    task_id, worker = self.pick_agent({"lead": True, "sub": False}.get(kind))
                target = {"task_id": task_id, "worker_id": worker["id"]}
            return self.crash_agent(target)["drill"]
        if kind == "spawn_crash":
            with self.store.transaction():
                if not self.spawn_trap:
                    self.spawn_trap = {"since": self.last_seq(), "t0": self.clock()}
            return {"kind": "spawn_crash", **self.spawn_trap}
        if kind != "spawn":
            raise APIError("scenario kind must be agent, lead, sub, vm, spawn_crash or spawn", 400)
        with self.store.transaction():
            row = self.latest_spawn(body.get("after"), body.get("task_id"), body.get("worker_id"))
        if not row:
            raise APIError("No subagent has been started yet")
        return {"kind": "spawn", "since": row["seq"] - 1, "t0": row["timestamp"],
                "task_id": row["task_id"], "worker_id": row["worker_id"]}

    def latest_spawn(self, after=None, task_id=None, worker_id=None):
        sql, args = "SELECT * FROM events WHERE type='subtask_spawned' AND seq>?", [int(after or 0)]
        if worker_id:
            sql, args = sql + " AND task_id=? AND worker_id=?", [*args, task_id, worker_id]
        return self.store.db.execute(sql + " ORDER BY seq DESC LIMIT 1", args).fetchone()

    # The online VM hosting the most agents, and how many. The caller holds a transaction.
    def pick_vm(self):
        now = self.clock()
        load = {v["id"]: 0 for v in self.store.all("vms")
                if v["id"] in self.pool and v["expires"] > now and v.get("down_until", 0) <= now}
        for task in self.store.active_workloads():
            for worker in task["workers"].values():
                if worker["state"] in ACTIVE and worker["vm_id"] in load:
                    load[worker["vm_id"]] += 1
        if not load:
            raise APIError("No VM is online right now")
        vm_id = max(load, key=load.get)
        return vm_id, load[vm_id]

    # The running agent with the longest log, so there is something to replay.
    # The caller holds a transaction.
    # Inputs:
    #   lead: True for a lead agent, False for a subagent, None for a subagent if
    #         one is running and otherwise a lead.
    # Output: (task id, worker record).
    def pick_agent(self, lead=None):
        now = self.clock()
        up = {v["id"] for v in self.store.all("vms")
              if v["expires"] > now and v.get("down_until", 0) <= now}
        best = {}
        for task in self.store.active_workloads():
            for worker in task["workers"].values():
                if (worker["state"] == "running" and worker["vm_id"] in up
                        and worker.get("killed_attempt") != worker["attempt"]):
                    size = self.store.db.execute("SELECT COUNT(*) FROM agent_log WHERE task_id=? AND worker_id=?",
                                                 (task["id"], worker["id"])).fetchone()[0]
                    role = not worker["parent_id"]
                    if role not in best or size > best[role][0]:
                        best[role] = (size, task["id"], worker)
        choice = best.get(lead) if lead is not None else best.get(False) or best.get(True)
        if not choice:
            raise APIError("No lead agent is running right now (a lead is paused while its subagents work)" if lead
                           else "No subagent is running right now" if lead is False else "No agent is running right now")
        return choice[1], choice[2]

    # What each scenario would act on if it were started now, for the dashboard's
    # scenario cards. The caller holds a transaction.
    # Output: {kind: {"available": bool, "target": text or None, "reason": text or None}}.
    def cards(self):
        def attempt(pick):
            try:
                return pick(), None
            except APIError as error:
                return None, str(error)

        vm, no_vm = attempt(self.pick_vm)
        machine = {"available": bool(vm), "reason": no_vm,
                   "target": vm and f"{self.vm_label(vm[0])} · {vm[1]} agent{'' if vm[1] == 1 else 's'} on it"}
        agent, no_agent = attempt(self.pick_agent)
        spawn = self.latest_spawn()
        task = spawn and self.store.get("workloads", spawn["task_id"])
        return {
            "agent": {"available": bool(agent), "reason": no_agent,
                      "target": agent and f"{agent[1]['label']} · turn {self.turns_until(agent[0], agent[1]['id'], 1 << 62)}"},
            "vm": machine,
            "spawn_crash": {"available": True, "reason": None, "armed": bool(self.spawn_trap),
                            "target": "armed: waiting for the next spawn_subagent" if self.spawn_trap
                            else "the next agent to run spawn_subagent"},
            "spawn": {"available": bool(task), "reason": None if task else "No subagent has been started yet",
                      "target": task and task["workers"][spawn["worker_id"]]["label"]}}

    def events_after(self, since, task_id, worker_id=None):
        sql, args = "SELECT * FROM events WHERE seq>? AND task_id=? AND type!='checkpoint'", [since, task_id]
        if worker_id:
            sql, args = sql + " AND worker_id=?", [*args, worker_id]
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.store.db.execute(sql + " ORDER BY seq", args)]

    def turns_until(self, task_id, worker_id, seq):
        return self.store.db.execute("SELECT COUNT(*) FROM agent_log WHERE task_id=? AND worker_id=? AND kind='query' AND seq<=?",
                                     (task_id, worker_id, seq)).fetchone()[0]

    # Follow one agent through a recovery that began after event number `since`.
    # Output: The event (or log row) for each milestone it has reached, else None.
    def track(self, task, worker, since):
        events = self.events_after(since, task["id"], worker["id"])

        def first(kind, after=0, attempt=None):
            return next((e for e in events if e["type"] == kind and e["seq"] > after
                         and (attempt is None or e["attempt"] == attempt)), None)

        lost = first("process_lost")
        fenced = first("attempt_superseded", attempt=lost["attempt"] if lost else None)
        after = fenced["seq"] if fenced else 1 << 62
        placed = first("worker_assigned", after)
        while placed and first("attempt_superseded", placed["seq"], placed["attempt"]):
            placed = first("worker_assigned", placed["seq"])  # that attempt was lost too; follow the next
        attempt = placed["attempt"] if placed else -1
        row = self.store.db.execute("SELECT seq, ts AS timestamp FROM agent_log WHERE task_id=? AND worker_id=? AND attempt=? ORDER BY seq LIMIT 1",
                                    (task["id"], worker["id"], attempt)).fetchone()
        return {"lost": lost, "fenced": fenced, "authorized": first("subtask_recovery_authorized", after), "placed": placed,
                "started": first("worker_started", after, attempt), "replayed": first("log_replayed", after, attempt),
                "continued": dict(row) if row else None, "completed": first("worker_completed", after)}

    # The steps every recovery goes through once the failure has been detected.
    # Inputs:
    #   tracks: [(task, worker, milestones from track())]; t0: time of the failure.
    def recovery_steps(self, tracks, t0):
        vms = {v["id"]: v for v in self.store.all("vms")}

        def step(key, title, text, make, link=None):
            items = []
            for task, worker, m in tracks:
                event, done, pending = make(task, worker, m)
                items.append({"label": worker["label"], "state": "done" if event else "pending",
                              "text": (done if event else pending) or "", "at": round(event["timestamp"] - t0, 1) if event else None,
                              **({"open": {"task": task["id"], "worker": worker["id"]}} if link and event else {})})
            return {"key": key, "title": title, "text": text, "items": items}

        def fence(task, worker, m):
            e = m["fenced"]
            return e, e and f"attempt {e['attempt']} retired ({e['payload']['reason']}) · failure {worker['failures']} of {self.max_attempts} allowed", ""

        def authorize(task, worker, m):
            if not worker["parent_id"]:
                return m["fenced"], "a lead agent needs no approval: requeued by the orchestrator", ""
            parent, e = task["workers"][worker["parent_id"]], m["authorized"]
            waiting = {"queued": "is queued for a VM slot so that it can approve", "waiting": "is being woken to approve",
                       "needs_recovery": "was lost too and must be recovered first"}.get(parent["state"], "approves with its next turn")
            return e, e and f"restart approved by {task['workers'][e['payload']['by']]['label']}", m["fenced"] and f"{parent['label']} {waiting}"

        def place(task, worker, m):
            e = m["placed"]
            return e, e and f"attempt {e['attempt']} → {self.vm_label(e['payload']['vm_id'])}", \
                m["fenced"] and worker["state"] == "queued" and "queued: waiting for a free VM slot"

        def start(task, worker, m):
            e, vm_id = m["started"], m["placed"] and m["placed"]["payload"]["vm_id"]
            pid = e and next((p.get("pid") for p in vms.get(vm_id, {}).get("processes", []) if p["task_id"] == task["id"]
                              and p["worker_id"] == worker["id"] and p["attempt"] == e["attempt"]), None)
            return e, e and f"new process on {self.vm_label(vm_id)}" + (f" · pid {pid}" if pid else ""), ""

        def replay(task, worker, m):
            e = m["replayed"]
            if not e:
                return m["completed"], "nothing to replay: its graded result was already checkpointed, so it only had to finish", ""
            p = e["payload"]
            saved = (f" · {p['saved_seconds']:.0f}s of model time and {p['saved_tokens']} tokens not spent again"
                     if p.get("saved_seconds") else "")
            return e, (f"{p['records']} log records replayed: {p['turns']} model replies, {p['files']} files restored"
                       + saved + (" · the one command that had not finished is run again" if p["rerun_command"] else "")), ""

        def carry_on(task, worker, m):
            e = m["continued"]
            if not e:
                return m["completed"], "finished", ""
            return e, f"turn {self.turns_until(task['id'], worker['id'], e['seq'])} · back at work {e['timestamp'] - t0:.1f}s after the failure", ""

        return [
            step("fence", "The dead attempt is fenced off",
                 "Its attempt number is retired. If the old process were somehow still alive, every request it sent would be "
                 "rejected with HTTP 409, so it could never overwrite the new attempt's work.", fence),
            step("authorize", "The restart is approved",
                 "A lead agent is requeued by the orchestrator straight away. A subagent belongs to its parent agent, so the "
                 "parent is woken and approves the restart.", authorize),
            step("place", "A new attempt is placed on a VM",
                 "The scheduler gives the agent the next attempt number and a free slot, preferring a different VM from the "
                 "one where it failed.", place),
            step("start", "That VM starts a fresh process",
                 "The VM's daemon sees the assignment on its next poll and starts a new agent process. It knows nothing yet.", start),
            step("replay", "The agent is rebuilt from the durable log",
                 "The new process asks the orchestrator for the agent's log and replays it: the whole conversation and every "
                 "file the agent wrote come back. No model reply is requested a second time.", replay, "trajectory"),
            step("continue", "Work continues where it stopped",
                 "The agent carries on from the turn it had reached and logs each new turn as before.", carry_on)]

    # What a finished recovery achieved, in one sentence, for the walkthrough's result line.
    def recap(self, tracks, t0):
        back = [m["continued"]["timestamp"] - t0 for _, _, m in tracks if m["continued"]]
        if not back:
            return None
        one = len(tracks) == 1
        replays = [m["replayed"]["payload"] for _, _, m in tracks if m["replayed"]]
        calls = sum(p.get("saved_calls", 0) for p in replays)
        seconds = sum(p.get("saved_seconds", 0) for p in replays)
        text = (f"{tracks[0][1]['label']} was" if one else f"{len(back)} agents were") + f" back at work {max(back):.0f}s after the failure."
        if calls:
            text += (f" {calls} model repl{'y' if calls == 1 else 'ies'}" + (f" ({seconds:.0f}s of model time)" if seconds else "")
                     + f" came from the log and none {'was' if calls == 1 else 'were'} requested again. Without the log, "
                     + ("it" if one else "they") + " would have started again from turn 0.")
        return text

    def drill_vm(self, vm_id, since, t0, now):
        vm, name = self.store.get("vms", vm_id), self.vm_label(vm_id)
        if not vm:
            raise APIError("unknown VM", 404)
        tracks = []
        for task in self.store.all("workloads"):
            for worker in task["workers"].values():
                events = self.events_after(since, task["id"], worker["id"])
                if any(e["type"] == "process_lost" and e["payload"]["vm_id"] == vm_id for e in events):
                    tracks.append((task, worker, self.track(task, worker, since)))
        fenced = [m["fenced"]["timestamp"] for _, _, m in tracks if m["fenced"]]
        same = abs(vm.get("crashed_at", 0) - t0) < 0.001  # False once the VM has been crashed again
        noticed = min(fenced) if fenced else (min(vm["expires"], now) if vm["expires"] <= now or not same
                                              or vm.get("down_until", 0) <= now else None)
        back = not same or (vm.get("down_until", 0) <= now and vm["expires"] > now)
        heartbeat = vm.get("last_heartbeat") or t0
        steps = [
            {"key": "inject", "title": f"{name} loses power", "state": "done", "at": 0,
             "text": f"Every agent process on {name} dies at once and the VM stops talking. Nobody tells the orchestrator."
                     if tracks else f"{name} stops talking. It had no agents on it. Nobody tells the orchestrator.",
             "items": [{"label": w["label"], "state": "done", "at": 0,
                        "text": "process killed" + (f" (pid {m['lost']['payload']['pid']})" if m["lost"]["payload"]["pid"] else "")}
                       for _, w, m in tracks]},
            {"key": "detect", "title": "The orchestrator notices the silence", "items": [],
             "state": "done" if noticed else "active", "at": round(noticed - t0, 1) if noticed else None,
             "text": (f"{name} sent no heartbeat for {self.lease:.0f}s, so its lease ran out and the orchestrator declared it lost."
                      if noticed else
                      f"The orchestrator last heard from {name} {now - heartbeat:.0f}s ago. It cannot tell a dead VM from a slow "
                      f"network, so it waits for the {self.lease:.0f}s lease to run out before it acts."),
             "wait": None if noticed else {"elapsed": round(now - heartbeat, 1), "total": self.lease, "label": "lease"}},
            *(self.recovery_steps(tracks, t0) if tracks else []),
            {"key": "rejoin", "title": f"{name} comes back", "items": [], "state": "done" if back else "pending",
             "at": round(vm.get("down_until", now) - t0, 1) if back else None,
             "text": f"{name}'s daemon registered again with a new session and offers empty slots." if back else
                     f"{name} restarts in {max(0, vm.get('down_until', 0) - now):.0f}s. Its daemon then registers with a new "
                     "session and offers empty slots; nothing from the old session is trusted."}]
        return {"title": f"{name} crashes", "subtitle": f"{len(tracks)} agent{'' if len(tracks) == 1 else 's'} on it", "steps": steps,
                "focus": {"vms": [vm_id], "agents": [f"{t['id']}/{w['id']}" for t, w, _ in tracks]},
                "summary": self.recap(tracks, t0)}

    def drill_agent(self, task_id, worker_id, since, t0, now):
        task = self.task(task_id)
        worker = task["workers"].get(worker_id)
        if not worker:
            raise APIError("unknown worker", 404)
        m = self.track(task, worker, since)
        if not m["lost"]:
            raise APIError("no injected failure to follow for this agent", 404)
        vm, fenced = self.vm_label(m["lost"]["payload"]["vm_id"]), m["fenced"]
        pid = m["lost"]["payload"]["pid"]
        lease_end = worker["lease_until"] if worker["attempt"] == m["lost"]["attempt"] else t0 + self.lease
        steps = [
            {"key": "inject", "title": f"{worker['label']}'s process is killed", "state": "done", "at": 0, "items": [],
             "text": f"Its process on {vm}{f' (pid {pid})' if pid else ''} is terminated in the middle of its work. {vm} itself "
                     "stays healthy, and nobody tells the orchestrator."},
            {"key": "detect", "title": "The orchestrator notices the silence", "items": [],
             "state": "done" if fenced else "active", "at": round(fenced["timestamp"] - t0, 1) if fenced else None,
             "text": (f"{vm}'s heartbeats stopped listing this agent, so its lease was not renewed and ran out. "
                      f"Recorded reason: “{fenced['payload']['reason']}”." if fenced else
                      f"{vm} still sends heartbeats, but they no longer list this agent, so its {self.lease:.0f}s lease is not "
                      "being renewed. The orchestrator acts when the lease runs out."),
             "wait": None if fenced else {"elapsed": round(max(0, self.lease - (lease_end - now)), 1), "total": self.lease, "label": "lease"}},
            *self.recovery_steps([(task, worker, m)], t0)]
        role = "subagent" if worker["parent_id"] else "lead agent"
        return {"title": f"{worker['label']} crashes", "subtitle": f"{role} of {task['name']}", "steps": steps,
                "focus": {"agents": [f"{task_id}/{worker_id}"]}, "summary": self.recap([(task, worker, m)], t0),
                "ended": task["status"] if task["status"] in FINISHED and not m["completed"] and task["status"] != "completed" else None}

    def drill_spawn(self, task_id, worker_id, since, now):
        task = self.task(task_id)
        child = task["workers"].get(worker_id)
        if not child or not child["parent_id"]:
            raise APIError("unknown subagent", 404)
        parent = task["workers"][child["parent_id"]]
        own, above = self.events_after(since, task_id, worker_id), self.events_after(since, task_id, parent["id"])
        first = lambda events, kind, after=0: next((e for e in events if e["type"] == kind and e["seq"] > after), None)
        spawned = first(own, "subtask_spawned")
        if not spawned:
            raise APIError("no spawn to follow for this agent", 404)
        t0, key = spawned["timestamp"], (task_id, worker_id)
        at = lambda e: round(e["timestamp"] - t0, 1) if e else None
        state = lambda e: "done" if e else "pending"
        command, turn = self.spawn_command(task_id, parent["id"], t0)
        problems = ", ".join(child["node"]["problems"])
        placed, started = first(own, "worker_assigned"), first(own, "worker_started")
        work = self.store.db.execute("SELECT MIN(ts) AS timestamp, COUNT(*) AS turns FROM agent_log WHERE task_id=? AND worker_id=? AND kind='query'", key).fetchone()
        working = dict(work) if work["turns"] else None
        paused = first(above, "agent_suspended", spawned["seq"])
        rejected = [e for e in own if e["type"] == "review_rejected"]
        graded, completed = first(own, "step_completed"), first(own, "worker_completed")
        told = completed and self.store.db.execute(
            "SELECT ts AS timestamp, vm_id FROM agent_log WHERE task_id=? AND worker_id=? AND kind='children' AND ts>=? ORDER BY seq LIMIT 1",
            (task_id, parent["id"], completed["timestamp"])).fetchone()
        others = [w["label"] for w in self.children(task, parent) if w["id"] != worker_id and w["state"] != "completed"]
        limit = task["spec"]["limits"].get("max_reviews", self.limits["max_reviews"])
        summary = lambda e: ", ".join(f"{p} {v}" for p, v in (e["payload"].get("summary") or {}).items())
        steps = [
            {"key": "decide", "title": f"{parent['label']} decides to hand work out", "state": "done", "at": 0, "items": [],
             "text": f"No rule made it do this. In turn {turn}, the model chose to run this command:" if command else
                     "No rule made it do this: the model chose to run spawn_subagent.", "code": command},
            {"key": "receipt", "title": "The orchestrator creates the subagent exactly once", "state": "done", "at": 0,
             "text": f"In one transaction it stores a receipt under operation id {spawned['payload']['operation_id'][:10]}… (a hash of "
                     f"the agent, its turn, and the command's position in that turn) and creates {child['label']}. If "
                     f"{parent['label']} crashes and runs the command again after recovery, it gets this receipt back, not a second subagent.",
             "items": [{"label": child["label"], "state": "done", "at": 0, "text": f"created with: {problems}"}]},
            {"key": "place", "title": "It is placed on a VM", "state": state(placed), "at": at(placed),
             "text": "A subagent is scheduled like any other agent: the next free slot on the least busy VM.",
             "items": [{"label": child["label"], "state": state(placed), "at": at(placed),
                        "text": f"attempt {placed['attempt']} → {self.vm_label(placed['payload']['vm_id'])}" if placed
                        else "queued: waiting for a free VM slot"}]},
            {"key": "start", "title": "The VM starts its process", "state": state(started), "at": at(started), "items": [],
             "text": "The VM's daemon sees the assignment on its next poll and starts the agent process, which gets its own "
                     "prompt, workspace and turn budget."},
            {"key": "work", "title": "It works, logging every turn", "state": state(working), "at": at(working),
             "text": "Each model reply is written to the durable log before its command runs, so a subagent survives a crash "
                     "exactly as a lead agent does.",
             "items": [{"label": child["label"], "state": "done", "at": at(working),
                        "text": f"{working['turns']} model replies logged" + (f" · recovered ×{child['failures']}" if child["failures"] else "")}]
                      if working else []},
            {"key": "pause", "title": f"{parent['label']} gives up its VM slot", "items": [],
             "state": "done" if paused else "skipped" if completed else "pending", "at": at(paused),
             "text": f"{parent['label']} is waiting for its subagents, so it was paused and its slot freed for other agents. It holds "
                     "no process while paused; it will be rebuilt from its log." if paused else
                     f"{parent['label']} kept working instead of pausing." if completed else
                     f"If {parent['label']} runs wait_subagents, it is paused and its VM slot is freed for other agents."},
            {"key": "check", "title": "The orchestrator checks its work", "state": state(graded), "at": at(graded),
             "text": f"When {child['label']} says it is done, the orchestrator runs its solutions on the hidden tests itself. "
                     f"If any fail it is sent back to work, at most {limit} times.",
             "items": [*({"label": child["label"], "state": "done", "at": at(e),
                          "text": "sent back: still failing " + ", ".join(e["payload"]["failing"])} for e in rejected),
                       *([{"label": child["label"], "state": "done", "at": at(graded), "text": "hidden tests: " + summary(graded)}] if graded else [])]},
            {"key": "report", "title": f"It finishes and {parent['label']} is told", "state": state(told), "at": at(told),
             "text": f"Its slot is freed. Once all of {parent['label']}'s subagents have finished, {parent['label']} is scheduled "
                     "again, rebuilt from its log, and given the results.",
             "items": [{"label": child["label"], "state": state(completed), "at": at(completed), "text": "finished" if completed else ""},
                       {"label": parent["label"], "state": state(told), "at": at(told),
                        "text": f"rebuilt on {self.vm_label(told['vm_id'])} and given the results" if told else
                        ("still waiting for " + ", ".join(others) if others else "queued for a VM slot") if completed else ""}]}]
        return {"title": f"{parent['label']} starts {child['label']}", "subtitle": f"problems: {problems}", "steps": steps,
                "focus": {"agents": [f"{task_id}/{worker_id}", f"{task_id}/{parent['id']}"]},
                "ended": task["status"] if task["status"] in FINISHED and not told and task["status"] != "completed" else None}

    # The command an agent ran in the turn that was current at time `at`, and that turn's number.
    def spawn_command(self, task_id, worker_id, at):
        row = self.store.db.execute("SELECT seq, data FROM agent_log WHERE task_id=? AND worker_id=? AND kind='query' AND ts<=? ORDER BY seq DESC LIMIT 1",
                                    (task_id, worker_id, at + 0.001)).fetchone()
        if not row:
            return "", 0
        content = (json.loads(row["data"]).get("messages") or [{}])[0].get("content") or ""
        command = content.split("```mswea_bash_command", 1)[-1].split("```", 1)[0].strip() if "```mswea_bash_command" in content else ""
        return command, self.turns_until(task_id, worker_id, row["seq"])

    # Walk through "crash mid-spawn": the next agent to start a subagent is killed
    # right after the orchestrator has stored the spawn's receipt. After recovery
    # the agent runs the same command again and must not get a second subagent.
    def drill_spawn_crash(self, since, now):
        row = self.store.db.execute(
            "SELECT * FROM events WHERE seq>? AND type='process_lost' AND json_extract(payload, '$.trap')=? ORDER BY seq LIMIT 1",
            (since, since)).fetchone()
        if not row:
            if not self.spawn_trap or self.spawn_trap["since"] != since:
                raise APIError("This scenario is no longer armed (the orchestrator restarted, or it was replaced)", 404)
            return {"title": "Crash an agent mid-spawn", "subtitle": "armed", "steps": [],
                    "waiting": "Waiting for any agent to run spawn_subagent. The moment the orchestrator has stored that "
                               "spawn's receipt, the agent's process is killed, before it can log that the command ran."}
        lost = {**dict(row), "payload": json.loads(row["payload"])}
        task = self.task(lost["task_id"])
        parent, child = task["workers"][lost["worker_id"]], task["workers"][lost["payload"]["child_id"]]
        t0, op_id = lost["timestamp"], lost["payload"]["operation_id"]
        m = self.track(task, parent, since)
        command, turn = self.spawn_command(task["id"], parent["id"], t0)
        vm, pid, fenced = self.vm_label(lost["payload"]["vm_id"]), lost["payload"]["pid"], m["fenced"]
        again = next((e for e in self.events_after(since, task["id"], parent["id"])
                      if e["type"] == "spawn_replayed" and e["payload"]["operation_id"] == op_id), None)
        siblings = len(self.children(task, parent))
        lease_end = parent["lease_until"] if parent["attempt"] == lost["attempt"] else t0 + self.lease
        recovery = self.recovery_steps([(task, parent, m)], t0)
        steps = [
            {"key": "decide", "title": f"{parent['label']} decides to hand work out", "state": "done", "at": 0, "items": [],
             "text": f"No rule made it do this. In turn {turn}, the model chose to run this command:" if command else
                     "No rule made it do this: the model chose to run spawn_subagent.", "code": command},
            {"key": "receipt", "title": "The orchestrator creates the subagent and stores a receipt", "state": "done", "at": 0,
             "text": f"In one transaction it creates {child['label']} and stores a receipt under operation id {op_id[:10]}…, a hash "
                     "of the agent, its turn, and the command's position in that turn. The id depends only on facts that "
                     "are already in the log.",
             "items": [{"label": child["label"], "state": "done", "at": 0, "text": "created with: " + ", ".join(child["node"]["problems"])}]},
            {"key": "inject", "title": f"{parent['label']} is killed before it can log the result", "state": "done", "at": 0, "items": [],
             "text": f"Its process on {vm}{f' (pid {pid})' if pid else ''} dies right after the receipt is stored. Its log ends "
                     "with the model's reply that contains the command; nothing says whether the command ran. This is the "
                     f"worst moment to crash: {child['label']} already exists and carries on regardless."},
            {"key": "detect", "title": "The orchestrator notices the silence", "items": [],
             "state": "done" if fenced else "active", "at": round(fenced["timestamp"] - t0, 1) if fenced else None,
             "text": (f"{vm}'s heartbeats stopped listing {parent['label']}, so its lease was not renewed and ran out."
                      if fenced else
                      f"{vm} still sends heartbeats, but they no longer list {parent['label']}, so its {self.lease:.0f}s lease is "
                      "not being renewed. The orchestrator acts when the lease runs out."),
             "wait": None if fenced else {"elapsed": round(max(0, self.lease - (lease_end - now)), 1), "total": self.lease, "label": "lease"}},
            *recovery[:5],
            {"key": "rerun", "title": "The same command runs again and creates nothing", "state": "done" if again else "pending",
             "at": round(again["timestamp"] - t0, 1) if again else None,
             "text": (f"The rebuilt {parent['label']} did not know whether its command had run, so it ran it again. The command "
                      "produced the same operation id, the orchestrator found the receipt, and handed back the subagent that "
                      "already existed.") if again else
                     (f"The rebuilt {parent['label']} cannot know whether its command ran, so it will run it again. The command "
                      "will produce the same operation id as before."),
             "items": [{"label": child["label"], "state": "done", "at": round(again["timestamp"] - t0, 1),
                        "text": f"returned from the receipt, not created again · {parent['label']} has {siblings} "
                                f"subagent{'' if siblings == 1 else 's'}, each started once"}] if again else []},
            recovery[5]]
        return {"title": f"{parent['label']} crashes mid-spawn", "subtitle": f"while starting {child['label']}", "steps": steps, "t0": t0,
                "focus": {"agents": [f"{task['id']}/{parent['id']}", f"{task['id']}/{child['id']}"]},
                "summary": f"{parent['label']} ran its spawn command twice, before and after the crash, and {child['label']} exists "
                           "exactly once." if again else None,
                "ended": task["status"] if task["status"] in FINISHED and not m["completed"] and task["status"] != "completed" else None}

    # The current state of a scenario walkthrough.
    # Inputs:
    #   query: Parsed query string holding the descriptor returned by scenario().
    # Output: {"title", "subtitle", "shows", "summary", "focus", "waiting", "elapsed", "done", "ended",
    #          "steps": [{"key", "title", "text", "state", "at", "items": [{"label", "state", "text", "at"}], ...}]}.
    def drill(self, query):
        one = lambda name, default=None: (query.get(name) or [default])[0]
        try:
            kind, since, t0 = one("kind"), int(one("since", 0)), float(one("t0", 0))
        except ValueError:
            raise APIError("since and t0 must be numbers", 400)
        if kind not in SHOWS:
            raise APIError("unknown scenario kind", 400)
        with self.store.transaction():
            now = self.clock()
            if kind == "vm":
                view = self.drill_vm(one("vm_id"), since, t0, now)
            elif kind == "agent":
                view = self.drill_agent(one("task_id"), one("worker_id"), since, t0, now)
            elif kind == "spawn":
                view = self.drill_spawn(one("task_id"), one("worker_id"), since, now)
            else:
                view = self.drill_spawn_crash(since, now)
        current = None
        for step in view["steps"]:
            if "state" not in step:
                done = [i for i in step["items"] if i["state"] == "done"]
                step["state"] = "done" if done and len(done) == len(step["items"]) else "active" if done else "pending"
                step["at"] = max((i["at"] for i in done if i["at"] is not None), default=None) if step["state"] == "done" else None
            if step["state"] in ("pending", "active") and current is None:
                current = step
        if current:
            current["state"] = "active"
        for key in ("ended", "summary", "waiting", "focus"):
            view.setdefault(key, None)
        done = current is None and not view["waiting"]
        view.update(kind=kind, shows=SHOWS[kind], done=done, elapsed=round(now - view.pop("t0", t0), 1),
                    summary=view["summary"] if done else None)
        return view
