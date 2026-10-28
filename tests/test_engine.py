import copy
import tempfile
import unittest
from pathlib import Path

from durable_swarms.engine import APIError, Engine
from durable_swarms.spec import validate
from durable_swarms.store import Store


class Clock:
    # Start a controllable clock so lease tests do not depend on wall-clock sleeps.
    def __init__(self):
        self.now = 1000

    # Supply the current simulated timestamp through the engine's clock interface.
    def __call__(self):
        return self.now


class RecoveryTests(unittest.TestCase):
    # Create an isolated durable store, deterministic clock, and two registered VMs.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(str(Path(self.tmp.name) / "state.sqlite3"))
        self.config = {"vms": {"a": 1, "b": 1}, "lease_seconds": 5, "progress_timeout_seconds": 10, "max_attempts": 3}
        self.engine = Engine(self.store, self.config, self.clock)
        self.spec = {"leader": {"id": "lead", "steps": [{"type": "effect", "key": "writes", "delta": 1}]}}
        self.engine.register({"vm_id": "a", "session": "a1"})
        self.engine.register({"vm_id": "b", "session": "b1"})

    # Close SQLite before removing this test's temporary database directory.
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    # Submit and schedule a test workflow, returning the leader's assigned identity.
    def begin(self, spec=None):
        self.engine.submit({"task_id": "task", "spec": spec or self.spec})
        self.engine.tick()
        return self.identity("lead")

    # Build API credentials for the worker's current VM session and assignment attempt.
    def identity(self, worker_id):
        worker = self.engine.status("task")["workers"][worker_id]
        return {"task_id": "task", "worker_id": worker_id, "attempt": worker["attempt"], "vm_id": worker["vm_id"], "session": worker["session"]}

    # Recover after an effect commits but its step does not, proving the receipt is
    # reused on another VM and the original attempt can no longer apply effects.
    def test_effect_survives_worker_and_orchestrator_crash_without_duplication(self):
        old = self.begin()
        self.engine.start(old)
        self.engine.intent({**old, "step": 0})
        receipt = self.engine.effect({**old, "step": 0})
        # Simulate a lost response and restart: checkpoint is still at step zero.
        self.store.db.close()
        self.store = Store(str(Path(self.tmp.name) / "state.sqlite3"))
        self.store.rebuild()
        self.engine = Engine(self.store, self.config, self.clock)
        self.clock.now += 6
        self.engine.register({"vm_id": "b", "session": "b2"})
        self.engine.tick()
        new = self.identity("lead")
        self.assertNotEqual(old["vm_id"], new["vm_id"])
        self.assertEqual(new["attempt"], 2)
        with self.assertRaises(APIError):
            self.engine.effect({**old, "step": 0})
        self.engine.start(new)
        self.engine.manage(new)
        self.engine.intent({**new, "step": 0})
        self.assertEqual(receipt, self.engine.effect({**new, "step": 0}))
        self.engine.checkpoint({**new, "step": 0})
        self.engine.finish({**new, "result": "lead complete"})
        status = self.engine.status("task")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["effects"], {"writes": 1})
        events = self.store.events("task")
        self.assertEqual(sum(e["type"] == "effect_completed" for e in events), 1)
        self.assertTrue(any(e["type"] == "recovery_completed" for e in events))

    # Confirm VM heartbeats cannot renew an absent worker or authorize its stale writes.
    def test_dead_worker_not_renewed_by_healthy_vm(self):
        old = self.begin()
        self.clock.now += 4
        self.engine.heartbeat({"vm_id": "a", "session": "a1", "active": []})
        self.clock.now += 2
        self.engine.tick()
        self.assertEqual(self.identity("lead")["attempt"], 2)
        with self.assertRaises(APIError):
            self.engine.checkpoint({**old, "step": 0})

    # Confirm unchanged progress triggers recovery even when all leases keep renewing.
    def test_healthy_heartbeats_do_not_hide_stall(self):
        old = self.begin()
        self.engine.start(old)
        for _ in range(3):
            self.clock.now += 4
            self.engine.heartbeat({"vm_id": "a", "session": "a1", "active": [old]})
            self.engine.progress({**old, "step": 0, "progress": 0})
        self.engine.tick()
        self.assertEqual(self.engine.status("task")["workers"]["lead"]["recovery_reason"], "worker made no progress")

    # Exercise parent/child completion and leader-authorized retries using one slot,
    # proving that a waiting leader cannot block its own child's execution.
    def test_one_slot_parent_wait_releases_capacity_and_controls_child_retry(self):
        self.config["vms"] = {"a": 1}
        self.engine = Engine(self.store, self.config, self.clock)
        spec = {"leader": {"id": "lead", "steps": []}, "workers": [{"id": "child", "steps": []}]}
        leader = self.begin(spec)
        self.engine.start(leader)
        self.engine.manage(leader)
        self.assertEqual(self.engine.finish({**leader, "result": "lead complete"})["state"], "waiting")
        self.engine.tick()
        child = self.identity("child")
        self.engine.start(child)
        self.engine.fail({**child, "reason": "crash"})
        self.assertEqual(self.engine.status("task")["workers"]["child"]["state"], "needs_recovery")
        self.engine.tick()
        leader = self.identity("lead")
        self.engine.start(leader)
        self.engine.manage(leader)
        self.engine.finish({**leader, "result": "lead complete"})
        self.engine.tick()
        child = self.identity("child")
        self.engine.start(child)
        self.engine.finish({**child, "result": "child complete"})
        self.engine.tick()
        leader = self.identity("lead")
        self.engine.start(leader)
        self.assertEqual(self.engine.manage(leader)["results"], {"child": "child complete"})
        self.engine.finish({**leader, "result": "lead complete"})
        self.assertEqual(self.engine.status("task")["status"], "completed")

    # Verify ready workers respect slot capacity while dependent work stays queued.
    def test_dependency_and_slot_limits(self):
        spec = {"leader": {"id": "lead", "steps": []}, "workers": [
            {"id": "first", "steps": []}, {"id": "second", "depends_on": ["first"], "steps": []}]}
        leader = self.begin(spec)
        self.engine.start(leader)
        self.engine.manage(leader)
        self.engine.tick()
        workers = self.engine.status("task")["workers"]
        self.assertEqual(workers["first"]["state"], "assigned")
        self.assertEqual(workers["second"]["state"], "queued")
        self.assertNotEqual(workers["lead"]["vm_id"], workers["first"]["vm_id"])

    # Distinguish invalid-result failure from escalation after repeated recoverable faults.
    def test_invalid_result_fails_and_repeated_failure_escalates(self):
        old = self.begin()
        self.engine.finish({**old, "result": "wrong"})
        self.assertEqual(self.engine.status("task")["status"], "failed")
        self.engine.submit({"task_id": "other", "spec": self.spec})
        for _ in range(3):
            self.engine.tick()
            worker = self.engine.status("other")["workers"]["lead"]
            self.engine.fail({"task_id": "other", "worker_id": "lead", "attempt": worker["attempt"], "vm_id": worker["vm_id"], "session": worker["session"]})
        self.assertEqual(self.engine.status("other")["status"], "escalated")

    # Verify VM recreation rejects the old session and permits a replacement attempt.
    def test_recreate_fences_old_session(self):
        old = self.begin()
        self.engine.recreate({"vm_id": "a"})
        with self.assertRaises(APIError):
            self.engine.progress({**old, "step": 0, "progress": 1})
        self.engine.register({"vm_id": "a", "session": "new"})
        self.engine.tick()
        self.assertEqual(self.identity("lead")["attempt"], 2)

    # Reject effects without durable intent and checkpoints without durable receipts.
    def test_effect_requires_intent_and_checkpoint_requires_receipt(self):
        identity = self.begin()
        with self.assertRaises(APIError):
            self.engine.effect({**identity, "step": 0})
        with self.assertRaises(APIError):
            self.engine.checkpoint({**identity, "step": 0})
        self.assertEqual(self.engine.status("task")["effects"], {})

    # Verify task-ID reuse accepts only the same specification and log pagination
    # advances by sequence number without duplicating workload creation.
    def test_submission_idempotency_and_event_pagination(self):
        self.begin()
        self.engine.submit({"task_id": "task", "spec": self.spec})
        with self.assertRaises(APIError):
            self.engine.submit({"task_id": "task", "spec": {"leader": {"id": "different"}}})
        first = self.store.events("task", limit=1)
        second = self.store.events("task", after=first[0]["seq"], limit=1)
        self.assertGreater(second[0]["seq"], first[0]["seq"])
        self.assertEqual(sum(e["type"] == "workload_created" for e in self.store.events("task")), 1)

    # Check repeatable duration expansion without input mutation and reject cyclic tasks.
    def test_seeded_duration_and_dag_validation(self):
        spec = copy.deepcopy(self.spec)
        spec["leader"]["steps"][0]["seconds"] = [1, 2]
        self.assertEqual(validate(spec), validate(spec))
        self.assertIsInstance(spec["leader"]["steps"][0]["seconds"], list)
        with self.assertRaises(ValueError):
            validate({"leader": {"id": "lead"}, "workers": [
                {"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]})


if __name__ == "__main__":
    unittest.main()
