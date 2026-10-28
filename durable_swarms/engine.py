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
    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


class Engine:
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

    def task(self, task_id):
        task = self.store.get("workloads", task_id)
        if task is None:
            raise APIError("unknown task", 404)
        return task

    def vm(self, body):
        vm = self.store.get("vms", body.get("vm_id"))
        if not vm or vm["session"] != body.get("session") or vm["expires"] <= self.clock():
            raise APIError("VM session expired or superseded")
        return vm

    def worker(self, body):
        self.vm(body)
        task = self.task(body.get("task_id"))
        worker = task["workers"].get(body.get("worker_id"))
        if (not worker or task["status"] in TERMINAL or worker["state"] not in ACTIVE
                or worker["attempt"] != body.get("attempt") or worker["vm_id"] != body.get("vm_id")
                or worker["session"] != body.get("session") or worker["lease_until"] <= self.clock()):
            raise APIError("worker attempt expired or superseded")
        return task, worker

    def event(self, task, kind, worker=None, **payload):
        self.store.event(task, kind, self.clock(), worker, **payload)

    def save(self, task):
        self.store.save(task, self.clock())

    def new_worker(self, node, parent=None):
        return {"id": node["id"], "parent_id": parent, "node": node, "state": "queued",
                "attempt": 0, "next_step": 0, "progress": 0, "last_progress": self.clock(),
                "vm_id": None, "session": None, "lease_until": 0, "intents": {}, "results": [],
                "recovery_reason": None, "failures": 0, "avoid_vm": None, "last_write": self.clock()}

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

    def terminate(self, task, status, reason):
        task["status"], task["reason"] = status, reason
        for worker in task["workers"].values():
            if worker["state"] != "completed":
                worker["state"] = "superseded"
        self.event(task, "workload_" + status, reason=reason)

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

    def lose_vm(self, vm_id, reason):
        for task in self.store.all("workloads"):
            changed = False
            for worker in task["workers"].values():
                if worker["vm_id"] == vm_id and worker["state"] in ACTIVE:
                    self.recover(task, worker, reason)
                    changed = True
            if changed:
                self.save(task)

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

    def operation(self, task, worker, step):
        if type(step) is not int or not 0 <= step < len(worker["node"]["steps"]):
            raise APIError("invalid step")
        definition = worker["node"]["steps"][step]
        if definition["type"] != "effect":
            raise APIError("not an effect step")
        op = hashlib.sha256(json.dumps([task["id"], worker["id"], step]).encode()).hexdigest()
        return op, {"key": definition["key"], "delta": definition["delta"]}

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

    def fail(self, body):
        with self.store.transaction():
            task, worker = self.worker(body)
            reason = body.get("reason", "worker failure")
            self.event(task, "worker_failed", worker, reason=reason)
            self.recover(task, worker, reason)
            self.save(task)
        return {"ok": True}

    def stop(self, body):
        with self.store.transaction():
            task = self.task(body["task_id"])
            worker = task["workers"].get(body["worker_id"])
            if not worker:
                raise APIError("unknown worker", 404)
            self.recover(task, worker, "worker stopped by operator")
            self.save(task)
        return {"ok": True}

    def status(self, task_id):
        with self.store.transaction():
            task = self.task(task_id)
            task["effects"] = {r["key"]: r["value"] for r in self.store.db.execute("SELECT key,value FROM world WHERE task_id=?", (task_id,))}
            return task
