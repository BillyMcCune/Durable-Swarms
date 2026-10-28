"""Single-orchestrator scheduling, fencing, and durable recovery."""

import hashlib
import json
import math
import time
import uuid

from .spec import validate


TERMINAL = {"completed", "failed", "escalated"}
ACTIVE = {"assigned", "running"}


class APIError(Exception):
    # Carry an API error message and HTTP status to the transport layer.
    # Inputs:
    #   self: APIError instance being initialized.
    #   message: Human-readable error description.
    #   code: HTTP status code; defaults to 409.
    # Output: None; initializes the exception message and code attribute.
    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


class Engine:
    # Configure the fixed VM pool and recovery policy, rejecting invalid capacities
    # or timeouts. The injected clock lets recovery checks advance time explicitly.
    # Inputs:
    #   self: Engine instance being initialized.
    #   store: Store holding durable state and transactions.
    #   config: Dictionary with vms and optional lease/progress timeouts and retry policy.
    #   clock: Callable returning seconds; defaults to time.time.
    # Output: None; initializes the engine or raises ValueError for invalid settings.
    def __init__(self, store, config, clock=time.time):
        self.store, self.config, self.clock = store, config, clock
        self.pool = config["vms"]
        self.lease = config.get("lease_seconds", 10)
        self.stall = config.get("progress_timeout_seconds", 20)
        self.max_attempts = config.get("max_attempts", 3)
        if not isinstance(self.pool, dict) or not self.pool or any(not isinstance(k, str) or type(v) is not int or v < 1 for k, v in self.pool.items()):
            raise ValueError("vms must map VM ids to positive slot counts")
        if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in (self.lease, self.stall, config.get("tick_seconds", 0.2))) or type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("timeouts and max_attempts must be positive")

    # Load the workload projection or raise HTTP 404 if it does not exist.
    # Callers hold the store transaction while reading or changing this state.
    # Inputs:
    #   self: Engine whose store supplies the workload.
    #   task_id: Workload identifier string.
    # Output: Workload dictionary; raises APIError(404) when absent.
    def task(self, task_id):
        task = self.store.get("workloads", task_id)
        if task is None:
            raise APIError("unknown task", 404)
        return task

    # Return the registered VM only if the request's session matches its live lease.
    # An expired or replaced session receives HTTP 409 and cannot authorize writes.
    # Inputs:
    #   self: Engine providing stored sessions and the current time.
    #   body: Request dictionary containing vm_id and session.
    # Output: VM dictionary; raises APIError(409) for an invalid or expired session.
    def vm(self, body):
        vm = self.store.get("vms", body.get("vm_id"))
        if not vm or vm["session"] != body.get("session") or vm["expires"] <= self.clock():
            raise APIError("VM session expired or superseded")
        return vm

    # Resolve a request to its workload and active worker attempt, checking both
    # VM and worker leases. Reject stale attempts before they can change state.
    # Inputs:
    #   self: Engine providing workload state and lease validation.
    #   body: Request dictionary with task_id, worker_id, attempt, vm_id, and session.
    # Output: (workload, worker) dictionaries; raises APIError for invalid identities.
    def worker(self, body):
        self.vm(body)
        task = self.task(body.get("task_id"))
        worker = task["workers"].get(body.get("worker_id"))
        if (not worker or task["status"] in TERMINAL or worker["state"] not in ACTIVE
                or worker["attempt"] != body.get("attempt") or worker["vm_id"] != body.get("vm_id")
                or worker["session"] != body.get("session") or worker["lease_until"] <= self.clock()):
            raise APIError("worker attempt expired or superseded")
        return task, worker

    # Append an event using the orchestrator clock within the caller's transaction.
    # Inputs:
    #   self: Engine providing the store and clock.
    #   task: Mutable workload dictionary whose version is advanced.
    #   kind: Event type string.
    #   worker: Optional worker dictionary supplying attempt and parent context.
    #   **payload: Event-specific JSON-serializable fields.
    # Output: None; appends an event in the open transaction.
    def event(self, task, kind, worker=None, **payload):
        self.store.event(task, kind, self.clock(), worker, **payload)

    # Save a recovery snapshot and its projection within the caller's transaction.
    # Inputs:
    #   self: Engine providing the store and clock.
    #   task: Mutable workload dictionary to checkpoint.
    # Output: None; advances the version and writes the snapshot and projection.
    def save(self, task):
        self.store.save(task, self.clock())

    # Build an unassigned worker record at step zero; parent identifies a subtask.
    # The caller adds this record to a workload and persists it.
    # Inputs:
    #   self: Engine providing the initial timestamps.
    #   node: Normalized worker specification with id, steps, dependencies, and result.
    #   parent: Parent worker ID, or None for a leader.
    # Output: New queued worker dictionary; does not persist it.
    def new_worker(self, node, parent=None):
        return {"id": node["id"], "parent_id": parent, "node": node, "state": "queued",
                "attempt": 0, "next_step": 0, "progress": 0, "last_progress": self.clock(),
                "vm_id": None, "session": None, "lease_until": 0, "intents": {}, "results": [],
                "recovery_reason": None, "failures": 0, "avoid_vm": None, "last_write": self.clock()}

    # Validate and durably create a workload with its queued leader, returning its ID.
    # Reusing an ID is idempotent only when the normalized specification matches.
    # Inputs:
    #   self: Engine owning submission and durable storage.
    #   body: Dictionary with spec and optional task_id for idempotent submission.
    # Output: {"task_id": str}; rejects invalid specs or conflicting existing IDs.
    def submit(self, body):
        spec = validate(body["spec"])
        task_id = body.get("task_id") or str(uuid.uuid4())
        if not isinstance(task_id, str) or not task_id:
            raise APIError("task_id must be a nonempty string", 400)
        with self.store.transaction():
            existing = self.store.get("workloads", task_id)
            if existing:
                if existing["spec"] != spec:
                    raise APIError("task_id already has a different specification")
                return {"task_id": task_id}
            leader = self.new_worker(spec["leader"])
            task = {"id": task_id, "spec": spec, "status": "queued", "version": 0,
                    "workers": {leader["id"]: leader}, "result": None, "reason": None}
            self.event(task, "workload_created", spec=spec)
            self.save(task)
        return {"task_id": task_id}

    # Register a configured VM session and its slot capacity. Replacing an expired
    # session initiates recovery; a competing live session is rejected.
    # Inputs:
    #   self: Engine providing the configured pool and lease duration.
    #   body: Dictionary with configured vm_id and nonempty session string.
    # Output: VM dictionary with id, session, slots, and expires.
    def register(self, body):
        vm_id, session = body["vm_id"], body["session"]
        if vm_id not in self.pool or not isinstance(session, str) or not session:
            raise APIError("unknown VM or invalid session", 400)
        with self.store.transaction():
            old = self.store.get("vms", vm_id)
            if old and old["session"] != session and old["expires"] > self.clock():
                raise APIError("VM already registered; stop it or explicitly recreate it first")
            if old and (old["session"] != session or old["expires"] <= self.clock()):
                self.lose_vm(vm_id, "VM session replaced")
            vm = {"id": vm_id, "session": session, "slots": self.pool[vm_id], "expires": self.clock() + self.lease}
            self.store.put("vms", vm_id, vm)
        return vm

    # Renew a live VM lease and the unexpired worker attempts it reports as alive.
    # Missing or expired attempts are not revived by VM health alone.
    # Inputs:
    #   self: Engine owning VM and worker leases.
    #   body: Dictionary with vm_id, session, and optional active list of
    #         {task_id, worker_id, attempt} records; active defaults to an empty list.
    # Output: {"ok": True}; persists valid lease renewals.
    def heartbeat(self, body):
        with self.store.transaction():
            vm = self.vm(body)
            vm["expires"] = self.clock() + self.lease
            self.store.put("vms", vm["id"], vm)
            # Only running attempts listed by the daemon get renewed. VM health alone
            # must never renew a worker which exited on an otherwise healthy machine.
            reports = {(r["task_id"], r["worker_id"], r["attempt"]) for r in body.get("active", [])}
            for task in self.store.all("workloads"):
                changed = False
                for worker in task["workers"].values():
                    if (worker["state"] in ACTIVE and worker["vm_id"] == vm["id"] and worker["session"] == vm["session"]
                            and worker["lease_until"] > self.clock() and (task["id"], worker["id"], worker["attempt"]) in reports):
                        worker["lease_until"] = vm["expires"]
                        self.event(task, "lease_renewed", worker, expires=vm["expires"])
                        changed = True
                if changed:
                    self.save(task)
        return {"ok": True}

    # Set a workload's terminal outcome and fence every unfinished worker.
    # The caller must save the changed workload in its current transaction.
    # Inputs:
    #   self: Engine recording the terminal event.
    #   task: Mutable workload dictionary to terminate.
    #   status: Terminal status selected by the caller, such as failed or escalated.
    #   reason: Human-readable explanation for the terminal outcome.
    # Output: None; mutates task and records its terminal event without saving it.
    def terminate(self, task, status, reason):
        task["status"], task["reason"] = status, reason
        for worker in task["workers"].values():
            if worker["state"] != "completed":
                worker["state"] = "superseded"
        self.event(task, "workload_" + status, reason=reason)

    # Supersede an active attempt while preserving its checkpoint: leaders queue
    # directly, children await authorization, and exhausted budgets cause escalation.
    # The caller persists these changes.
    # Inputs:
    #   self: Engine providing the failed-attempt budget and event store.
    #   task: Mutable workload dictionary containing the worker.
    #   worker: Mutable worker dictionary for the failed attempt.
    #   reason: Human-readable failure explanation.
    # Output: None; updates recovery state, or does nothing for an inactive attempt.
    def recover(self, task, worker, reason):
        if task["status"] in TERMINAL or worker["state"] not in ACTIVE:
            return
        worker["avoid_vm"] = worker["vm_id"]
        worker["recovery_reason"] = reason
        worker["failures"] += 1
        self.event(task, "attempt_superseded", worker, reason=reason)
        if worker["failures"] >= self.max_attempts:
            self.terminate(task, "escalated", f"{worker['id']} exhausted recovery attempts: {reason}")
            return
        worker["state"] = "needs_recovery" if worker["parent_id"] else "queued"
        task["status"] = "recovering"
        self.event(task, "recovery_started", worker, next_step=worker["next_step"], reason=reason)
        if worker["parent_id"]:
            leader = task["workers"][worker["parent_id"]]
            if leader["state"] == "waiting":
                leader["state"] = "queued"

    # Initiate recovery for active attempts on a lost VM and save affected workloads.
    # Runs inside the registration or recreation transaction that invalidates the VM.
    # Inputs:
    #   self: Engine providing workload records and recovery policy.
    #   vm_id: Identifier of the VM whose active attempts must be recovered.
    #   reason: Explanation attached to each affected attempt.
    # Output: None; saves all affected workloads in the caller's transaction.
    def lose_vm(self, vm_id, reason):
        for task in self.store.all("workloads"):
            changed = False
            for worker in task["workers"].values():
                if worker["vm_id"] == vm_id and worker["state"] in ACTIVE:
                    self.recover(task, worker, reason)
                    changed = True
            if changed:
                self.save(task)

    # Fence a VM's workers and retire its session so the daemon can register again.
    # This resets the worker service's registration; it does not boot a physical VM.
    # Inputs:
    #   self: Engine managing VM registration and worker recovery.
    #   body: Dictionary containing the vm_id to reset.
    # Output: {"ok": True, "action": str} describing the required service/VM restart.
    def recreate(self, body):
        # Reset the worker service lease; the daemon will register a fresh session.
        with self.store.transaction():
            vm = self.store.get("vms", body["vm_id"])
            if not vm:
                raise APIError("unknown VM", 404)
            self.lose_vm(vm["id"], "VM recreation requested")
            vm["session"], vm["expires"] = None, 0
            self.store.put("vms", vm["id"], vm)
        return {"ok": True, "action": "restart worker service or reboot VM; re-registration is enabled"}

    # Let a live leader create its declared subtasks, authorize child retries, and
    # collect completed child results. Repeated calls do not duplicate children.
    # Inputs:
    #   self: Engine managing the workload's child workers.
    #   body: Leader identity with task_id, worker_id, attempt, vm_id, and session.
    # Output: {"results": {child_id: result}} for completed children.
    def manage(self, body):
        with self.store.transaction():
            task, leader = self.worker(body)
            if leader["parent_id"]:
                raise APIError("only the leader may manage subtasks")
            changed = False
            for node in task["spec"]["workers"]:
                if node["id"] not in task["workers"]:
                    child = self.new_worker(node, leader["id"])
                    task["workers"][child["id"]] = child
                    self.event(task, "subtask_created", child)
                    changed = True
            for worker in task["workers"].values():
                if worker["state"] == "needs_recovery":
                    worker["state"] = "queued"
                    self.event(task, "subtask_recovery_authorized", worker, leader_attempt=leader["attempt"])
                    changed = True
            results = {w["id"]: w.get("result") for w in task["workers"].values() if w["parent_id"] and w["state"] == "completed"}
            if changed:
                self.save(task)
        return {"results": results}

    # Atomically detect lost/stalled workers, wake waiting leaders, and assign ready
    # work within live VM capacities. Placement favors a different VM after failure;
    # every assignment increments the attempt ID used to reject stale requests.
    # Inputs:
    #   self: Engine containing pool configuration, clock, leases, and workload state.
    # Output: None; commits recovery transitions and newly assigned worker attempts.
    def tick(self):
        with self.store.transaction():
            now = self.clock()
            vms = {v["id"]: v for v in self.store.all("vms")}
            tasks = self.store.all("workloads")
            dirty = set()
            for task in tasks:
                if task["status"] in TERMINAL:
                    continue
                for worker in task["workers"].values():
                    if worker["state"] in ACTIVE:
                        vm = vms.get(worker["vm_id"])
                        reason = None
                        if not vm or vm["expires"] <= now or vm["session"] != worker["session"]:
                            reason = "VM heartbeat lost"
                        elif worker["lease_until"] <= now:
                            reason = "worker lease expired"
                        elif now - worker["last_progress"] >= self.stall:
                            reason = "worker made no progress"
                        if reason:
                            self.recover(task, worker, reason)
                            dirty.add(task["id"])
                leader = task["workers"][task["spec"]["leader"]["id"]]
                children = [w for w in task["workers"].values() if w["parent_id"]]
                if task["status"] not in TERMINAL and leader["state"] == "waiting" and (
                        all(w["state"] == "completed" for w in children) or any(w["state"] == "needs_recovery" for w in children)):
                    leader["state"] = "queued"
                    dirty.add(task["id"])
            used = {vm: 0 for vm in vms}
            for task in tasks:
                for worker in task["workers"].values():
                    if worker["state"] in ACTIVE:
                        used[worker["vm_id"]] = used.get(worker["vm_id"], 0) + 1
            # Round-robin over workloads: allocate at most one worker per workload per pass.
            pending = [t for t in tasks if t["status"] not in TERMINAL]
            while pending:
                next_pass = []
                for task in pending:
                    candidates = [w for w in task["workers"].values() if w["state"] == "queued" and
                                  all(task["workers"].get(d, {}).get("state") == "completed" for d in w["node"]["depends_on"])]
                    available = [v for v in vms.values() if v["id"] in self.pool and v["expires"] > now and used[v["id"]] < self.pool[v["id"]]]
                    if not candidates or not available:
                        continue
                    worker = candidates[0]
                    vm = min(available, key=lambda v: (v["id"] == worker["avoid_vm"], used[v["id"]] / self.pool[v["id"]], v["id"]))
                    worker.update(state="assigned", attempt=worker["attempt"] + 1, vm_id=vm["id"], session=vm["session"],
                                  lease_until=now + self.lease, last_progress=now, progress=0)
                    used[vm["id"]] += 1
                    if task["status"] == "queued":
                        task["status"] = "running"
                    self.event(task, "worker_assigned", worker, vm_id=vm["id"], next_step=worker["next_step"])
                    dirty.add(task["id"])
                    next_pass.append(task)
                pending = next_pass
            for task in tasks:
                if task["id"] in dirty:
                    self.save(task)

    # Return this VM session's live assignments, including resume state and faults.
    # Polling reads assignments without claiming new work or renewing leases.
    # Inputs:
    #   self: Engine supplying active workload assignments.
    #   body: Dictionary with vm_id and session identifying the polling daemon.
    # Output: {"jobs": [{"task_id": str, "worker": dict, "faults": list}, ...]}.
    def poll(self, body):
        with self.store.transaction():
            vm = self.vm(body)
            jobs = []
            for task in self.store.all("workloads"):
                for worker in task["workers"].values():
                    if (task["status"] not in TERMINAL and worker["state"] in ACTIVE and worker["vm_id"] == vm["id"]
                            and worker["session"] == vm["session"] and worker["lease_until"] > self.clock()):
                        jobs.append({"task_id": task["id"], "worker": worker, "faults": task["spec"]["faults"]})
        return {"jobs": jobs}

    # Acknowledge an assignment as running and record a recovery restart if needed.
    # Repeated acknowledgments of the same running attempt do not add start events.
    # Inputs:
    #   self: Engine managing worker lifecycle events.
    #   body: Worker identity with task_id, worker_id, attempt, vm_id, and session.
    # Output: {"ok": True}; records the start transition when still assigned.
    def start(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            if worker["state"] == "assigned":
                worker["state"] = "running"
                self.event(task, "worker_started", worker)
                if worker["recovery_reason"]:
                    self.event(task, "recovery_completed", worker, next_step=worker["next_step"])
                if all(w["state"] not in {"queued", "needs_recovery", "assigned"} for w in task["workers"].values()):
                    task["status"] = "running"
                self.save(task)
        return {"ok": True}

    # Record strictly advancing progress for the current incomplete step.
    # Repeated or lower values do not reset the stall-detection timer.
    # Inputs:
    #   self: Engine tracking durable worker progress and timestamps.
    #   body: Worker identity (task_id, worker_id, attempt, vm_id, session),
    #         current step index, and a nonnegative integer progress value.
    # Output: {"ok": True}; persists only progress that exceeds the prior value.
    def progress(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            step, progress = body["step"], body["progress"]
            if type(progress) is not int or progress < 0 or step != worker["next_step"]:
                raise APIError("invalid progress")
            if progress > worker["progress"]:
                worker["progress"], worker["last_progress"], worker["last_write"] = progress, self.clock(), self.clock()
                self.event(task, "progress", worker, step=step, progress=progress)
                self.save(task)
        return {"ok": True}

    # Validate an effect step and return its stable operation ID and counter payload.
    # The ID excludes attempt and VM identity so recovery can reuse the same receipt.
    # Inputs:
    #   self: Engine instance; this helper does not access its stored state.
    #   task: Workload dictionary supplying its logical ID.
    #   worker: Worker dictionary supplying its ID and step definitions.
    #   step: Zero-based integer index of an effect step.
    # Output: (operation_id, {"key": str, "delta": int}); rejects invalid steps.
    def operation(self, task, worker, step):
        if type(step) is not int or not 0 <= step < len(worker["node"]["steps"]):
            raise APIError("invalid step")
        definition = worker["node"]["steps"][step]
        if definition["type"] != "effect":
            raise APIError("not an effect step")
        op = hashlib.sha256(json.dumps([task["id"], worker["id"], step]).encode()).hexdigest()
        return op, {"key": definition["key"], "delta": definition["delta"]}

    # Persist intent for the next effect step before it may be applied.
    # Repeated requests return the same operation ID without duplicating the intent.
    # Inputs:
    #   self: Engine owning the durable intent log.
    #   body: Worker identity (task_id, worker_id, attempt, vm_id, session)
    #         and the next effect step index in step.
    # Output: {"operation_id": str}; returned only after the intent transaction commits.
    def intent(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            step = body["step"]
            if step != worker["next_step"]:
                raise APIError("effect is not the next step")
            op, payload = self.operation(task, worker, step)
            if str(step) not in worker["intents"]:
                worker["intents"][str(step)] = op
                self.event(task, "effect_intent", worker, operation_id=op, step=step, effect=payload)
                self.save(task)
        return {"operation_id": op}

    # Apply an intended counter mutation and commit its receipt atomically, or return
    # the existing receipt on retry. Conflicting or missing completed-step receipts
    # escalate the workload rather than risk applying an effect again.
    # Inputs:
    #   self: Engine owning the counter world and effect receipt registry.
    #   body: Worker identity (task_id, worker_id, attempt, vm_id, session)
    #         and effect step index in step; an intent must already exist.
    # Output: Receipt {"operation_id": str, "key": str, "value": int}, or
    #         {"escalated": True} if durable effect records are inconsistent.
    def effect(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            op, payload = self.operation(task, worker, body["step"])
            if worker["intents"].get(str(body["step"])) != op:
                raise APIError("effect intent must be durable before applying the effect")
            row = self.store.db.execute("SELECT payload,receipt FROM effects WHERE operation_id=?", (op,)).fetchone()
            if row:
                if json.loads(row["payload"]) != payload:
                    self.terminate(task, "escalated", "effect receipt conflicts with recorded intent")
                    self.save(task)
                    return {"escalated": True}
                return json.loads(row["receipt"])
            if body["step"] != worker["next_step"]:
                self.terminate(task, "escalated", "completed effect has no durable receipt")
                self.save(task)
                return {"escalated": True}
            row = self.store.db.execute("SELECT value FROM world WHERE task_id=? AND key=?", (task["id"], payload["key"])).fetchone()
            value = (row[0] if row else 0) + payload["delta"]
            receipt = {"operation_id": op, "key": payload["key"], "value": value}
            self.store.db.execute("INSERT INTO world VALUES(?,?,?) ON CONFLICT(task_id,key) DO UPDATE SET value=excluded.value", (task["id"], payload["key"], value))
            self.store.db.execute("INSERT INTO effects VALUES(?,?,?,?)", (op, task["id"], json.dumps(payload), json.dumps(receipt)))
            self.event(task, "effect_completed", worker, step=body["step"], receipt=receipt)
            self.save(task)
        return receipt

    # Advance the durable cursor only after the next step completes; effect steps
    # require a stored receipt. Retrying an already checkpointed step is a no-op.
    # Inputs:
    #   self: Engine owning the worker's durable cursor and results.
    #   body: Worker identity (task_id, worker_id, attempt, vm_id, session)
    #         and the completed step index in step.
    # Output: {"next_step": int}, the durable cursor after the request.
    def checkpoint(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            step = body["step"]
            if type(step) is not int or step < 0:
                raise APIError("invalid step")
            if step < worker["next_step"]:
                return {"next_step": worker["next_step"]}
            if step != worker["next_step"] or step >= len(worker["node"]["steps"]):
                raise APIError("checkpoint must complete the next step")
            result = {"step": step, "completed": True}
            if worker["node"]["steps"][step]["type"] == "effect":
                op, _ = self.operation(task, worker, step)
                row = self.store.db.execute("SELECT receipt FROM effects WHERE operation_id=?", (op,)).fetchone()
                if not row:
                    raise APIError("effect receipt is not durable")
                result = json.loads(row[0])
            worker["results"].append(result)
            worker.update(next_step=step + 1, progress=0, last_progress=self.clock(), last_write=self.clock())
            self.event(task, "step_completed", worker, step=step, result=result)
            self.save(task)
        return {"next_step": step + 1}

    # Validate completion, failing the workload for premature or unexpected results.
    # A leader releases its slot while children remain unfinished, then aggregates
    # their final results.
    # Inputs:
    #   self: Engine managing completion and parent/child results.
    #   body: Worker identity (task_id, worker_id, attempt, vm_id, session)
    #         and result matching the worker's declared result.
    # Output: {"state": "completed" | "waiting" | "failed"}.
    def finish(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            if worker["next_step"] != len(worker["node"]["steps"]) or body.get("result") != worker["node"]["result"]:
                self.terminate(task, "failed", "worker returned an invalid or premature result")
                self.save(task)
                return {"state": "failed"}
            if not worker["parent_id"]:
                if len(task["workers"]) != len(task["spec"]["workers"]) + 1:
                    raise APIError("leader must create its subtasks before finishing")
                children = [w for w in task["workers"].values() if w["parent_id"]]
                if any(w["state"] != "completed" for w in children):
                    worker["state"] = "waiting"
                    self.event(task, "leader_waiting", worker)
                    self.save(task)
                    return {"state": "waiting"}
                task["status"] = "completed"
                task["result"] = {"leader": body["result"], "subtasks": {w["id"]: w["result"] for w in children}}
                self.event(task, "workload_completed", result=task["result"])
            worker["state"], worker["result"] = "completed", body["result"]
            self.event(task, "worker_completed", worker, result=body["result"])
            self.save(task)
        return {"state": "completed"}

    # Record a live worker's failure report and initiate recovery in one transaction.
    # Inputs:
    #   self: Engine providing failure logging and recovery policy.
    #   body: Worker identity (task_id, worker_id, attempt, vm_id, session)
    #         and optional reason string, defaulting to "worker failure".
    # Output: {"ok": True}; commits the failure report and recovery/escalation state.
    def fail(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            reason = body.get("reason", "worker failure")
            self.event(task, "worker_failed", worker, reason=reason)
            self.recover(task, worker, reason)
            self.save(task)
        return {"ok": True}

    # Handle an operator stop by fencing the selected active attempt and recovering
    # its work. The VM daemon observes the revoked assignment and stops the process.
    # Inputs:
    #   self: Engine owning workload and worker state.
    #   body: Dictionary containing task_id and worker_id; no worker lease is required.
    # Output: {"ok": True}; initiates recovery only when the selected attempt is active.
    def stop(self, body):
        with self.store.transaction():
            task = self.task(body["task_id"])
            worker = task["workers"].get(body["worker_id"])
            if not worker:
                raise APIError("unknown worker", 404)
            self.recover(task, worker, "worker stopped by operator")
            self.save(task)
        return {"ok": True}

    # Return a consistent workload snapshot enriched with its simulated counter values.
    # Adding the effects view does not change the stored workload projection.
    # Inputs:
    #   self: Engine providing workload projections and simulated counter values.
    #   task_id: Identifier of the workload to inspect.
    # Output: Workload dictionary augmented with effects mapping counter keys to values.
    def status(self, task_id):
        with self.store.transaction():
            task = self.task(task_id)
            task["effects"] = {r["key"]: r["value"] for r in self.store.db.execute("SELECT key,value FROM world WHERE task_id=?", (task_id,))}
            return task
