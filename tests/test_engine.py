import copy
import tempfile
import unittest
from pathlib import Path

from durable_swarms.engine import APIError, Engine
from durable_swarms.spec import validate
from durable_swarms.store import Store


class Clock:
    # Start a controllable clock so lease tests do not depend on wall-clock sleeps.
    # Inputs:
    #   self: Clock instance being initialized.
    # Output: None; sets the initial simulated time to 1000 seconds.
    def __init__(self):
        self.now = 1000

    # Supply the current simulated timestamp through the engine's clock interface.
    # Inputs:
    #   self: Clock instance holding the current simulated timestamp.
    # Output: Numeric timestamp from self.now, in seconds.
    def __call__(self):
        return self.now


class RecoveryTests(unittest.TestCase):
    # Create an isolated durable store, deterministic clock, and two registered VMs.
    # Inputs:
    #   self: RecoveryTests instance receiving the per-test fixtures.
    # Output: None; initializes the temporary database, engine, clock, and sample spec.
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
    # Inputs:
    #   self: RecoveryTests instance holding the open store and temporary directory.
    # Output: None; releases the fixtures created by setUp.
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    # Submit and schedule a test workflow, returning the leader's assigned identity.
    # Inputs:
    #   self: RecoveryTests instance with a configured engine.
    #   spec: Optional workflow specification; defaults to the fixture specification.
    # Output: Identity dictionary with task_id, worker_id, attempt, vm_id, and session.
    def begin(self, spec=None):
        self.engine.submit({"task_id": "task", "spec": spec or self.spec})
        self.engine.tick()
        return self.identity("lead")

    # Build API credentials for the worker's current VM session and assignment attempt.
    # Inputs:
    #   self: RecoveryTests instance whose engine holds the "task" workload.
    #   worker_id: Logical worker ID within that workload.
    # Output: Identity dictionary with task_id, worker_id, attempt, vm_id, and session.
    def identity(self, worker_id):
        worker = self.engine.status("task")["workers"][worker_id]
        return {"task_id": "task", "worker_id": worker_id, "attempt": worker["attempt"], "vm_id": worker["vm_id"], "session": worker["session"]}

    # Recover after an effect commits but its step does not, proving the receipt is
    # reused on another VM and the original attempt can no longer apply effects.
    # Inputs:
    #   self: RecoveryTests instance with an isolated database and simulated clock.
    # Output: None on success; raises AssertionError if replay duplicates an effect
    #         or permits the superseded attempt to write.
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
    # Inputs:
    #   self: RecoveryTests instance with registered VM fixtures.
    # Output: None on success; raises AssertionError if an absent attempt stays valid.
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
    # Inputs:
    #   self: RecoveryTests instance with an engine and controllable clock.
    # Output: None on success; raises AssertionError if the stalled worker is not detected.
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
    # Inputs:
    #   self: RecoveryTests instance whose pool is reduced to a single slot in the test.
    # Output: None on success; raises AssertionError if slot yielding, child recovery
    #         authorization, or result collection fails.
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
    # Inputs:
    #   self: RecoveryTests instance with two single-slot VMs.
    # Output: None on success; raises AssertionError for premature or over-capacity placement.
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
    # Inputs:
    #   self: RecoveryTests instance with its configured failed-attempt budget.
    # Output: None on success; raises AssertionError if either terminal state is incorrect.
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
    # Inputs:
    #   self: RecoveryTests instance with a scheduled worker and registered VMs.
    # Output: None on success; raises AssertionError if fencing or replacement fails.
    def test_recreate_fences_old_session(self):
        old = self.begin()
        self.engine.recreate({"vm_id": "a"})
        with self.assertRaises(APIError):
            self.engine.progress({**old, "step": 0, "progress": 1})
        self.engine.register({"vm_id": "a", "session": "new"})
        self.engine.tick()
        self.assertEqual(self.identity("lead")["attempt"], 2)

    # Reject effects without durable intent and checkpoints without durable receipts.
    # Inputs:
    #   self: RecoveryTests instance with the counter-effect workflow fixture.
    # Output: None on success; raises AssertionError if either invalid operation succeeds.
    def test_effect_requires_intent_and_checkpoint_requires_receipt(self):
        identity = self.begin()
        with self.assertRaises(APIError):
            self.engine.effect({**identity, "step": 0})
        with self.assertRaises(APIError):
            self.engine.checkpoint({**identity, "step": 0})
        self.assertEqual(self.engine.status("task")["effects"], {})

    # Verify task-ID reuse accepts only the same specification and log pagination
    # advances by sequence number without duplicating workload creation.
    # Inputs:
    #   self: RecoveryTests instance with an engine and durable event store.
    # Output: None on success; raises AssertionError for conflicting ID reuse,
    #         duplicate creation events, or incorrect pagination order.
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
    # Inputs:
    #   self: RecoveryTests instance supplying a base workflow specification.
    # Output: None on success; raises AssertionError if normalization is inconsistent,
    #         mutates the input, or accepts a dependency cycle.
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
