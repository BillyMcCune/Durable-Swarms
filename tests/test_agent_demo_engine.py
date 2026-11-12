"""In-process checks of AgentEngine: spawning, the durable log, suspension, and scoring."""

import inspect
from pathlib import Path
import tempfile
import unittest

from agent_demo.engine import AgentEngine
from agent_demo.problems import bank, references
from durable_swarms.engine import APIError
from durable_swarms.store import Store


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


def result(**grades):
    return {"exit_status": "Submitted", "turns": 1,
            "problems": {p: {"passed": passed, "total": total, "error": None, "code": "code"}
                         for p, (passed, total) in grades.items()}}


class AgentEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.path = str(Path(self.tmp.name) / "state.sqlite3")
        self.store = Store(self.path)
        self.config = {"vms": {"a": 2, "b": 2}, "lease_seconds": 5, "progress_timeout_seconds": 10, "max_attempts": 3}
        self.engine = AgentEngine(self.store, self.config, self.clock)
        self.engine.register({"vm_id": "a", "session": "a1"})
        self.engine.register({"vm_id": "b", "session": "b1"})

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def worker(self, worker_id):
        return self.engine.status("task")["workers"][worker_id]

    # Identity of a worker's current attempt, as its VM daemon would present it.
    def identity(self, worker_id):
        worker = self.worker(worker_id)
        return {"task_id": "task", "worker_id": worker_id, "attempt": worker["attempt"],
                "vm_id": worker["vm_id"], "session": worker["session"]}

    def begin(self, problems=("two_sum", "coin_change", "edit_distance"), **spec):
        self.engine.submit({"task_id": "task", "spec": {"problems": list(problems), **spec}})
        self.engine.tick()
        lead = self.identity("agent")
        self.engine.start(lead)
        return lead

    def run_next(self, worker_id):
        self.engine.tick()
        identity = self.identity(worker_id)
        self.engine.start(identity)
        return identity

    def complete(self, identity, **grades):
        self.engine.checkpoint({**identity, "step": 0, "result": result(**grades)})
        return self.engine.finish(identity)["state"]

    def test_submit_samples_ten_problems_and_is_idempotent(self):
        first = self.engine.submit({"task_id": "task", "spec": {"seed": 4}})
        self.assertEqual(first, self.engine.submit({"task_id": "task", "spec": {"seed": 4}}))
        with self.assertRaises(APIError):
            self.engine.submit({"task_id": "task", "spec": {"seed": 5}})
        node = self.worker("agent")["node"]
        self.assertEqual(len(node["problems"]), 10)
        self.assertEqual((node["depth"], node["can_spawn"], node["step_limit"]), (0, True, 60))
        for bad in ({"problems": ["nope"]}, {"problems": []}, {"limits": {"bogus": 1}}, {"seed": "x"}, {"instructions": 5}):
            with self.assertRaises(ValueError):
                self.engine.submit({"task_id": "other", "spec": bad})

    def test_spawn_is_exactly_once_and_validated(self):
        lead = self.begin()
        request = {**lead, "op_id": "op-1", "problems": ["coin_change", "edit_distance"], "instructions": "go"}
        receipt = self.engine.spawn(request)
        self.assertEqual(receipt, {"child_id": "agent.1", "label": "Alice Sub 1", "problems": ["coin_change", "edit_distance"]})
        self.assertEqual(self.engine.spawn(request), receipt)
        self.assertEqual(sorted(self.engine.status("task")["workers"]), ["agent", "agent.1"])
        child = self.worker("agent.1")
        self.assertEqual((child["parent_id"], child["node"]["depth"], child["node"]["instructions"]), ("agent", 1, "go"))
        self.assertEqual(self.engine.spawn({**lead, "op_id": "op-2", "problems": ["two_sum"]})["label"], "Alice Sub 2")
        self.engine.submit({"task_id": "second", "spec": {"seed": 1}})
        second = self.engine.status("second")
        self.assertEqual((self.engine.status("task")["name"], second["name"], second["workers"]["agent"]["label"]),
                         ("Alice", "Bob", "Bob Lead"))
        for problems in (["valid_parentheses"], [], ["two_sum", "two_sum"], "two_sum"):
            with self.assertRaises(APIError) as caught:
                self.engine.spawn({**lead, "op_id": "bad", "problems": problems})
            self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(APIError):
            self.engine.spawn({**lead, "attempt": 99, "op_id": "op-3", "problems": ["two_sum"]})

    def test_depth_and_agent_count_limits(self):
        lead = self.begin(limits={"leader_steps": 9, "sub_steps": 7, "max_depth": 1, "max_workers": 2})
        self.engine.spawn({**lead, "op_id": "a", "problems": ["two_sum"]})
        with self.assertRaises(APIError) as caught:
            self.engine.spawn({**lead, "op_id": "b", "problems": ["coin_change"]})
        self.assertIn("maximum of 2 agents", str(caught.exception))
        child = self.run_next("agent.1")
        self.assertEqual((self.worker("agent.1")["node"]["step_limit"], self.worker("agent.1")["node"]["can_spawn"]), (7, False))
        with self.assertRaises(APIError) as caught:
            self.engine.spawn({**child, "op_id": "c", "problems": ["two_sum"]})
        self.assertIn("cannot start subagents", str(caught.exception))

    def test_log_is_ordered_idempotent_fenced_and_survives_restart(self):
        lead = self.begin()
        record = {**lead, "seq": 0, "kind": "init", "data": {"messages": [{"role": "system", "content": "hi"}]}}
        self.assertEqual(self.engine.agent_log(record), {"seq": 0})
        self.assertEqual(self.engine.agent_log(record), {"seq": 0})
        with self.assertRaises(APIError):
            self.engine.agent_log({**record, "data": {"messages": []}})
        with self.assertRaises(APIError):
            self.engine.agent_log({**record, "seq": 2})
        self.engine.agent_log({**lead, "seq": 1, "kind": "observation",
                               "data": {"messages": [], "checks": {"two_sum": {"passed": 2, "total": 3}}}})
        self.assertEqual(self.worker("agent")["progress"], 2)
        self.clock.now += 1
        self.engine.stop({"task_id": "task", "worker_id": "agent"})
        with self.assertRaises(APIError):
            self.engine.agent_log({**lead, "seq": 2, "kind": "query", "data": {}})
        replacement = self.run_next("agent")
        self.assertEqual(replacement["attempt"], 2)
        self.assertEqual([r["kind"] for r in self.engine.agent_resume(replacement)["records"]], ["init", "observation"])
        # A restarted orchestrator rebuilds workloads from the event log and keeps the agent log.
        self.store.db.close()
        self.store = Store(self.path)
        self.store.rebuild()
        self.engine = AgentEngine(self.store, self.config, self.clock)
        view = self.engine.trajectory("task", "agent")
        self.assertEqual((len(view["records"]), view["lost_attempts"]), (2, [1]))
        self.assertEqual(self.engine.demo_state()["tasks"][0]["workers"]["agent"]["checks"], {"two_sum": {"passed": 2, "total": 3}})

    def test_nested_agents_suspend_wake_and_score(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["coin_change", "edit_distance"]})
        self.assertEqual(self.engine.suspend(lead), {"state": "waiting"})
        with self.assertRaises(APIError):
            self.engine.manage(lead)  # a waiting agent holds no live attempt
        child = self.run_next("agent.1")
        self.engine.spawn({**child, "op_id": "2", "problems": ["edit_distance"]})
        self.assertEqual(self.worker("agent.1.1")["label"], "Alice Sub 2")  # numbered in spawn order
        self.assertEqual(self.engine.suspend(child), {"state": "waiting"})
        grandchild = self.run_next("agent.1.1")
        self.assertEqual(self.worker("agent.1.1")["node"]["can_spawn"], False)
        self.assertEqual(self.complete(grandchild, edit_distance=(16, 16)), "completed")
        # Only the grandchild's parent wakes; the lead still waits for its own child.
        self.engine.tick()
        self.assertEqual((self.worker("agent.1")["state"], self.worker("agent")["state"]), ("assigned", "waiting"))
        child = self.identity("agent.1")
        self.assertEqual(child["attempt"], 2)
        self.engine.start(child)
        ready = self.engine.suspend(child)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["children"]["agent.1.1"]["result"]["problems"]["edit_distance"]["passed"], 16)
        self.assertEqual(self.complete(child, coin_change=(3, 16), edit_distance=(0, 16)), "completed")
        lead = self.run_next("agent")
        self.assertEqual(self.engine.suspend(lead)["state"], "ready")
        with self.assertRaises(APIError):
            self.engine.finish(lead)  # nothing checkpointed yet
        self.assertEqual(self.complete(lead, two_sum=(14, 14), coin_change=(0, 16), edit_distance=(0, 16)), "completed")
        task = self.engine.status("task")
        self.assertEqual(task["status"], "completed")
        self.assertEqual((task["result"]["solved"], task["result"]["total"]), (2, 3))
        card = task["result"]["problems"]
        self.assertEqual((card["edit_distance"]["solved_by"], card["two_sum"]["solved_by"]), ("agent.1.1", "agent"))
        self.assertEqual((card["coin_change"]["solved"], card["coin_change"]["passed"], card["coin_change"]["solved_by"]), (False, 3, "agent.1"))
        self.assertEqual(self.worker("agent")["failures"], 0)  # waiting is not a failure
        statuses = {p: v["status"] for p, v in self.engine.demo_state()["tasks"][0]["problems"].items()}
        self.assertEqual(statuses, {"two_sum": "solved", "coin_change": "failed", "edit_distance": "solved"})

    def test_finishing_with_running_subagents_waits_for_them(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.assertEqual(self.complete(lead, two_sum=(0, 14), coin_change=(16, 16), edit_distance=(16, 16)), "waiting")
        child = self.run_next("agent.1")
        self.assertEqual(self.complete(child, two_sum=(14, 14)), "completed")
        lead = self.run_next("agent")
        self.assertEqual(self.worker("agent")["next_step"], 1)
        self.assertEqual(self.engine.finish(lead)["state"], "completed")
        self.assertEqual(self.engine.status("task")["result"]["solved"], 3)

    def test_failed_subagent_wakes_its_waiting_parent_for_authorization(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.engine.suspend(lead)
        child = self.run_next("agent.1")
        first_vm = child["vm_id"]
        self.engine.agent_log({**child, "seq": 0, "kind": "init", "data": {"messages": []}})
        self.engine.llm_call({"call_id": "c1", "phase": "start", "task_id": "task", "worker_id": "agent.1", "vm_id": first_vm})
        self.assertEqual(self.engine.demo_state()["llm"]["in_flight"], 1)
        self.engine.recreate({"vm_id": first_vm})
        self.assertEqual(self.engine.demo_state()["llm"]["in_flight"], 0)  # the lost attempt's call is closed
        self.assertEqual((self.worker("agent.1")["state"], self.worker("agent")["state"]), ("needs_recovery", "queued"))
        with self.assertRaises(APIError):
            self.engine.agent_log({**child, "seq": 1, "kind": "query", "data": {}})
        lead = self.run_next("agent")
        self.assertEqual(self.worker("agent.1")["state"], "needs_recovery")  # the scheduler alone does not requeue it
        self.assertEqual(self.engine.suspend(lead), {"state": "waiting"})  # authorizes, then waits again
        child = self.run_next("agent.1")
        self.assertEqual(child["attempt"], 2)
        self.assertNotEqual(child["vm_id"], first_vm)
        self.assertEqual(len(self.engine.agent_resume(child)["records"]), 1)
        self.assertEqual(self.complete(child, two_sum=(14, 14)), "completed")
        lead = self.run_next("agent")
        self.assertEqual(self.complete(lead, two_sum=(0, 14)), "completed")
        self.assertEqual(self.engine.status("task")["result"]["problems"]["two_sum"]["solved_by"], "agent.1")

    # Heartbeat a VM as its daemon would, listing the attempts whose processes are alive.
    def beat(self, vm_id, *alive):
        self.engine.heartbeat({"vm_id": vm_id, "session": vm_id + "1", "active": [
            {"task_id": i["task_id"], "worker_id": i["worker_id"], "attempt": i["attempt"], "pid": 7} for i in alive]})

    def drill(self, descriptor):
        view = self.engine.drill({k: [str(v)] for k, v in descriptor.items()})
        return view, {s["key"]: s for s in view["steps"]}

    def test_an_agent_may_start_at_most_three_subagents(self):
        lead = self.begin(problems=("two_sum", "coin_change", "edit_distance", "climbing_stairs"))
        self.assertEqual(self.worker("agent")["node"]["max_children"], 3)
        for index, problem in enumerate(("two_sum", "coin_change", "edit_distance")):
            self.engine.spawn({**lead, "op_id": str(index), "problems": [problem]})
        with self.assertRaises(APIError) as caught:
            self.engine.spawn({**lead, "op_id": "3", "problems": ["climbing_stairs"]})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("already started 3 subagents", str(caught.exception))
        self.assertEqual(self.engine.spawn({**lead, "op_id": "0", "problems": ["two_sum"]})["child_id"], "agent.1")  # replay still works
        self.assertEqual(sorted(self.engine.status("task")["workers"]), ["agent", "agent.1", "agent.2", "agent.3"])
        child = self.run_next("agent.1")
        self.assertEqual(self.worker("agent.1")["node"]["max_children"], 3)  # the cap is per agent, not per run
        self.engine.clear_runs({"all": True})
        lead = self.begin(limits={"max_children": 1})
        self.engine.spawn({**lead, "op_id": "x", "problems": ["two_sum"]})
        with self.assertRaises(APIError):
            self.engine.spawn({**lead, "op_id": "y", "problems": ["coin_change"]})

    def test_crashed_vm_is_only_noticed_when_its_lease_runs_out_and_rejoins_after_its_downtime(self):
        lead = self.begin()
        crashed = lead["vm_id"]
        other = "b" if crashed == "a" else "a"
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": [], "files": {"problems/two_sum/solution.py": "x"}}})
        reply = self.engine.crash_vm({"vm_id": crashed, "seconds": 30})
        descriptor = reply["drill"]
        self.assertEqual((reply["ok"], reply["down_seconds"], descriptor["kind"], descriptor["vm_id"]), (True, 30, "vm", crashed))
        # The VM is cut off, but the orchestrator has not noticed: nothing is recovered yet.
        for refused in (lambda: self.beat(crashed, lead), lambda: self.engine.poll({"vm_id": crashed, "session": crashed + "1"}),
                        lambda: self.engine.agent_log({**lead, "seq": 1, "kind": "observation", "data": {}}),
                        lambda: self.engine.register({"vm_id": crashed, "session": "reboot"}),
                        lambda: self.engine.crash_vm({"vm_id": crashed})):
            with self.assertRaises(APIError):
                refused()
        self.engine.tick()
        self.assertEqual((self.worker("agent")["state"], self.worker("agent")["failures"]), ("running", 0))
        vms = {vm["id"]: vm for vm in self.engine.demo_state()["vms"]}
        self.assertEqual((vms[crashed]["online"], vms[crashed]["silent"], vms[crashed]["down_for"], vms[other]["silent"]), (True, True, 30, False))
        view, steps = self.drill(descriptor)
        self.assertEqual([s["key"] for s in view["steps"]], ["inject", "detect", "fence", "authorize", "place", "start", "replay", "continue", "rejoin"])
        self.assertEqual((steps["inject"]["state"], steps["detect"]["state"], steps["fence"]["state"], view["done"]), ("done", "active", "pending", False))
        self.assertEqual(steps["detect"]["wait"]["total"], 5)
        self.assertEqual(steps["inject"]["items"][0]["label"], "Alice Lead")
        # The lease runs out without a heartbeat: now it is detected and recovered on the other VM.
        self.clock.now += 4
        self.beat(other)
        self.clock.now += 1
        self.engine.tick()
        self.assertEqual(self.worker("agent")["failures"], 1)
        resumed = self.identity("agent")
        self.assertEqual((resumed["vm_id"], resumed["attempt"]), (other, 2))
        view, steps = self.drill(descriptor)
        self.assertEqual([steps[k]["state"] for k in ("detect", "fence", "authorize", "place", "start")], ["done", "done", "done", "done", "active"])
        self.assertIn("VM heartbeat lost", steps["fence"]["items"][0]["text"])
        self.assertEqual(steps["detect"]["at"], 5)
        self.engine.start(resumed)
        self.assertEqual(len(self.engine.agent_resume(resumed)["records"]), 1)
        self.engine.agent_resume(resumed)  # a retried request does not record a second replay
        events = [e["type"] for e in self.store.events("task") if e["type"] in ("process_lost", "attempt_superseded", "log_replayed")]
        self.assertEqual(events, ["process_lost", "attempt_superseded", "log_replayed"])
        self.engine.agent_log({**resumed, "seq": 1, "kind": "observation", "data": {}})
        view, steps = self.drill(descriptor)
        self.assertIn("1 log records replayed: 1 model replies, 1 files restored", steps["replay"]["items"][0]["text"])
        self.assertIn("run again", steps["replay"]["items"][0]["text"])
        self.assertEqual((steps["continue"]["state"], steps["rejoin"]["state"], view["done"]), ("done", "active", False))
        self.clock.now += 26
        self.engine.register({"vm_id": crashed, "session": "reboot"})
        view, steps = self.drill(descriptor)
        self.assertEqual((steps["rejoin"]["state"], steps["rejoin"]["at"], view["done"]), ("done", 30, True))
        vms = {vm["id"]: vm for vm in self.engine.demo_state()["vms"]}
        self.assertEqual((vms[crashed]["online"], vms[crashed]["silent"], vms[crashed]["down_for"]), (True, False, 0))
        for bad in ({"vm_id": "nope"}, {"vm_id": crashed, "seconds": 0}, {"vm_id": crashed, "seconds": "x"}):
            with self.assertRaises(APIError):
                self.engine.crash_vm(bad)

    def test_crashed_agent_is_noticed_by_its_expired_lease_and_rebuilt_from_its_log(self):
        lead = self.begin()
        with self.assertRaises(APIError) as caught:
            self.engine.scenario({"kind": "sub"})
        self.assertIn("No subagent is running", str(caught.exception))
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        child = self.run_next("agent.1")
        self.engine.agent_log({**child, "seq": 0, "kind": "query", "data": {"messages": []}})
        self.engine.agent_log({**child, "seq": 1, "kind": "observation", "data": {"files": {"a.py": "1", "b.py": "2"}, "deleted": ["b.py"]}})
        descriptor = self.engine.scenario({"kind": "sub"})
        self.assertEqual((descriptor["kind"], descriptor["worker_id"]), ("agent", "agent.1"))
        # Its VM daemon is no longer given the job, and the dead attempt is refused, but nothing is recovered yet.
        self.assertNotIn("agent.1", [j["worker"]["id"] for j in self.engine.poll({"vm_id": child["vm_id"], "session": child["session"]})["jobs"]])
        with self.assertRaises(APIError):
            self.engine.agent_log({**child, "seq": 2, "kind": "query", "data": {}})
        with self.assertRaises(APIError):
            self.engine.crash_agent({"task_id": "task", "worker_id": "agent.1"})
        self.engine.tick()
        self.assertEqual(self.worker("agent.1")["state"], "running")
        state = self.engine.demo_state()["tasks"][0]["workers"]
        self.assertEqual((state["agent.1"]["killed"], state["agent"]["killed"]), (True, False))
        view, steps = self.drill(descriptor)
        self.assertEqual((view["title"], steps["detect"]["state"], steps["detect"]["wait"]["total"]), ("Alice Sub 1 crashes", "active", 5))
        self.clock.now += 4
        for vm_id in ("a", "b"):  # both VMs are healthy; only the lead's process is still reported
            self.beat(vm_id, *([lead] if lead["vm_id"] == vm_id else []))
        self.clock.now += 1
        self.engine.tick()
        self.assertEqual((self.worker("agent.1")["state"], self.worker("agent")["state"]), ("needs_recovery", "running"))
        view, steps = self.drill(descriptor)
        self.assertIn("worker lease expired", steps["detect"]["text"])
        self.assertEqual((steps["fence"]["state"], steps["authorize"]["state"]), ("done", "active"))
        self.assertIn("Alice Lead", steps["authorize"]["items"][0]["text"])
        self.engine.manage(lead)
        again = self.run_next("agent.1")
        self.assertEqual(again["attempt"], 2)
        self.assertEqual(len(self.engine.agent_resume(again)["records"]), 2)
        self.engine.agent_log({**again, "seq": 2, "kind": "query", "data": {"messages": []}})
        view, steps = self.drill(descriptor)
        self.assertEqual(([s["state"] for s in view["steps"]], view["done"]), (["done"] * 8, True))
        self.assertIn("approved by Alice Lead", steps["authorize"]["items"][0]["text"])
        self.assertIn("2 log records replayed: 1 model replies, 1 files restored", steps["replay"]["items"][0]["text"])
        self.assertNotIn("run again", steps["replay"]["items"][0]["text"])
        self.assertIn("turn 2", steps["continue"]["items"][0]["text"])
        # A lead can be crashed while it runs, not while it is paused for its subagents.
        self.assertEqual(self.engine.scenario({"kind": "lead"})["worker_id"], "agent")
        self.assertEqual(self.drill(self.engine.scenario({"kind": "sub"}))[1]["authorize"]["state"], "pending")
        with self.assertRaises(APIError) as caught:
            self.engine.scenario({"kind": "lead"})
        self.assertIn("No lead agent is running", str(caught.exception))
        with self.assertRaises(APIError):
            self.engine.scenario({"kind": "bogus"})

    def test_spawn_walkthrough_follows_a_subagent_from_the_command_to_the_result(self):
        lead = self.begin()
        with self.assertRaises(APIError) as caught:
            self.engine.scenario({"kind": "spawn"})
        self.assertIn("No subagent has been started yet", str(caught.exception))
        reply = "THOUGHT: delegate.\n\n```mswea_bash_command\nspawn_subagent --problems two_sum\n```"
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": [{"role": "assistant", "content": reply}]}})
        self.engine.spawn({**lead, "op_id": "abcdef0123456789", "problems": ["two_sum"]})
        descriptor = self.engine.scenario({"kind": "spawn"})
        self.assertEqual((descriptor["kind"], descriptor["worker_id"]), ("spawn", "agent.1"))
        self.assertEqual(self.engine.scenario({"kind": "spawn", "task_id": "task", "worker_id": "agent.1"}), descriptor)
        with self.assertRaises(APIError):
            self.engine.scenario({"kind": "spawn", "after": descriptor["since"] + 1})
        view, steps = self.drill(descriptor)
        self.assertEqual(view["title"], "Alice Lead starts Alice Sub 1")
        self.assertEqual(steps["decide"]["code"], "spawn_subagent --problems two_sum")
        self.assertIn("turn 1", steps["decide"]["text"])
        self.assertIn("operation id abcdef0123…", steps["receipt"]["text"])
        self.assertEqual([s["state"] for s in view["steps"]], ["done", "done", "active", "pending", "pending", "pending", "pending", "pending"])
        child = self.run_next("agent.1")
        self.engine.agent_log({**child, "seq": 0, "kind": "query", "data": {"messages": []}})
        self.assertEqual(self.engine.suspend(lead), {"state": "waiting"})
        view, steps = self.drill(descriptor)
        self.assertEqual([steps[k]["state"] for k in ("place", "start", "work", "pause", "check")], ["done", "done", "done", "done", "active"])
        self.assertEqual(self.complete(child, two_sum=(14, 14)), "completed")
        view, steps = self.drill(descriptor)
        self.assertIn("two_sum 14/14", steps["check"]["items"][0]["text"])
        self.assertEqual((steps["report"]["state"], [i["state"] for i in steps["report"]["items"]], view["done"]), ("active", ["done", "pending"], False))
        lead = self.run_next("agent")
        self.assertEqual(len(self.engine.agent_resume(lead)["records"]), 1)
        self.engine.agent_log({**lead, "seq": 1, "kind": "children", "data": {"messages": []}})
        view, steps = self.drill(descriptor)
        self.assertEqual((steps["report"]["state"], view["done"], view["ended"]), ("done", True, None))
        replays = [e["payload"]["after"] for e in self.store.events("task") if e["type"] == "log_replayed"]
        self.assertEqual(replays, ["pause"])  # the lead was rebuilt from its log after a pause, not a failure

    def test_reserved_slots_are_kept_for_recovery_work(self):
        self.engine = AgentEngine(self.store, {**self.config, "recovery_reserve": 2}, self.clock)
        lead = self.begin()
        for index, problem in enumerate(("two_sum", "coin_change", "edit_distance")):
            self.engine.spawn({**lead, "op_id": str(index), "problems": [problem]})
        for _ in range(3):
            self.engine.tick()
        # Four slots, two reserved: the lead and one subagent run, the others are held back.
        states = lambda: {w["id"]: w["state"] for w in self.engine.status("task")["workers"].values()}
        self.assertEqual(states(), {"agent": "running", "agent.1": "assigned", "agent.2": "queued", "agent.3": "queued"})
        view = self.engine.demo_state()
        self.assertEqual(view["reserve"], {"slots": 2, "free": 2})
        self.assertEqual({w["id"]: w["held"] for w in view["tasks"][0]["workers"].values()},
                         {"agent": False, "agent.1": False, "agent.2": True, "agent.3": True})
        # A VM is lost with the subagent on it. Its restart takes a reserved slot; ordinary work still waits.
        child = self.identity("agent.1")
        self.engine.start(child)
        self.assertNotEqual(child["vm_id"], lead["vm_id"])
        self.clock.now += 4
        self.beat(lead["vm_id"], lead)
        self.clock.now += 1
        self.engine.tick()
        self.assertEqual(states()["agent.1"], "needs_recovery")
        self.engine.manage(lead)
        self.engine.tick()
        self.engine.tick()
        again = self.identity("agent.1")
        self.assertEqual((again["attempt"], again["vm_id"]), (2, lead["vm_id"]))
        self.assertEqual(states(), {"agent": "running", "agent.1": "assigned", "agent.2": "queued", "agent.3": "queued"})
        self.assertFalse(self.engine.demo_state()["tasks"][0]["workers"]["agent.1"]["held"])
        with self.assertRaises(ValueError):
            AgentEngine(self.store, {**self.config, "recovery_reserve": -1}, self.clock)

    def test_reserve_is_waived_when_nothing_is_running(self):
        self.engine = AgentEngine(self.store, {**self.config, "vms": {"a": 1, "b": 1}, "recovery_reserve": 2}, self.clock)
        lead = self.begin()  # placed although only two slots are free
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.engine.tick()
        self.assertEqual(self.worker("agent.1")["state"], "queued")
        self.assertEqual(self.engine.suspend(lead), {"state": "waiting"})
        self.engine.tick()
        self.assertEqual(self.worker("agent.1")["state"], "assigned")

    def test_stopping_a_run_fences_its_agents_and_keeps_finished_results(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.engine.spawn({**lead, "op_id": "2", "problems": ["coin_change"]})
        first = self.run_next("agent.1")
        self.assertEqual(self.complete(first, two_sum=(14, 14)), "completed")
        second = self.run_next("agent.2")
        self.engine.llm_call({"call_id": "c1", "phase": "start", "task_id": "task", "worker_id": "agent.2", "vm_id": second["vm_id"]})
        self.assertEqual(self.engine.stop_run({"task_id": "task"}), {"ok": True, "status": "stopped"})
        task = self.engine.status("task")
        self.assertEqual((task["status"], task["reason"]), ("stopped", "stopped by operator"))
        self.assertEqual({w["id"]: w["state"] for w in task["workers"].values()},
                         {"agent": "superseded", "agent.1": "completed", "agent.2": "superseded"})
        self.assertEqual((task["result"]["solved"], task["result"]["problems"]["two_sum"]["solved_by"]), (1, "agent.1"))
        for stale in (lead, second):
            with self.assertRaises(APIError):
                self.engine.agent_log({**stale, "seq": 0, "kind": "init", "data": {}})
            self.assertEqual(self.engine.poll({"vm_id": stale["vm_id"], "session": stale["session"]})["jobs"], [])
        self.engine.tick()
        view = self.engine.demo_state()
        self.assertEqual((view["llm"]["in_flight"], sum(len(vm["assignments"]) for vm in view["vms"])), (0, 0))
        self.assertEqual({p: v["status"] for p, v in view["tasks"][0]["problems"].items()},
                         {"two_sum": "solved", "coin_change": "failed", "edit_distance": "failed"})
        self.assertEqual(self.engine.stop_run({"task_id": "task"})["status"], "stopped")  # idempotent
        with self.assertRaises(APIError):
            self.engine.stop_run({"task_id": "missing"})

    def test_review_sends_an_unfinished_agent_back_then_takes_its_final_result(self):
        lead = self.begin(problems=("two_sum", "climbing_stairs"), limits={"max_reviews": 1})
        good = inspect.getsource(references.two_sum)
        stub = bank.load_bank()["climbing_stairs"]["stub"]
        first = self.engine.review({**lead, "solutions": {"two_sum": good, "climbing_stairs": stub}, "turns": 3})
        self.assertEqual((first["accepted"], first["final"]), (False, False))
        self.assertIn("- climbing_stairs: not solved", first["feedback"])
        self.assertNotIn("- two_sum", first["feedback"])
        self.assertIn("check 1 of 1", first["feedback"])
        worker = self.worker("agent")
        self.assertEqual((worker["next_step"], worker["reviews"], worker["state"]), (0, 1, "running"))
        self.assertEqual(worker["review"]["two_sum"], {"passed": 14, "total": 14})
        with self.assertRaises(APIError):
            self.engine.finish(lead)  # sent back to work, so nothing is checkpointed
        wrong = "def climbing_stairs(n):\n    return n\n"
        second = self.engine.review({**lead, "solutions": {"two_sum": good, "climbing_stairs": wrong}, "turns": 5})
        self.assertEqual((second["accepted"], second["final"], second["feedback"]), (False, True, None))
        self.assertTrue(0 < second["grades"]["climbing_stairs"]["passed"] < second["grades"]["climbing_stairs"]["total"])
        self.assertEqual(self.engine.review({**lead, "solutions": {}})["final"], True)  # already checkpointed
        self.assertEqual(self.engine.finish(lead)["state"], "completed")
        task = self.engine.status("task")
        self.assertEqual((task["result"]["solved"], task["workers"]["agent"]["result"]["turns"]), (1, 5))
        self.assertEqual(task["workers"]["agent"]["result"]["problems"]["climbing_stairs"]["code"], wrong)
        kinds = [e["type"] for e in self.store.events("task")]
        self.assertEqual(kinds.count("review_rejected"), 1)

    def test_review_accepts_correct_work_and_explains_a_wrong_answer(self):
        lead = self.begin(problems=("two_sum",))
        wrong = self.engine.review({**lead, "solutions": {"two_sum": "def two_sum(nums, target):\n    return [0, 1]\n"}})
        self.assertRegex(wrong["feedback"], r"- two_sum: \d+/14 hidden tests pass; for example the arguments \[")
        self.assertNotIn("expected", wrong["feedback"])
        ok = self.engine.review({**lead, "solutions": {"two_sum": inspect.getsource(references.two_sum)}})
        self.assertEqual((ok["accepted"], ok["final"]), (True, True))
        self.assertEqual(self.engine.finish(lead)["state"], "completed")
        self.assertEqual(self.engine.status("task")["result"]["solved"], 1)
        for bad in ({"solutions": "x"}, {"solutions": {"two_sum": 5}}, {}):
            with self.assertRaises(APIError):
                self.engine.review({**lead, **bad})

    def test_review_waits_for_subagents_and_counts_what_they_solved(self):
        lead = self.begin(problems=("two_sum", "climbing_stairs"))
        stubs = {p: bank.load_bank()[p]["stub"] for p in ("two_sum", "climbing_stairs")}
        mine = {**stubs, "climbing_stairs": inspect.getsource(references.climbing_stairs)}
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.assertEqual(self.engine.review({**lead, "solutions": mine}), {"wait": True})
        self.assertEqual(self.engine.suspend(lead), {"state": "waiting"})
        child = self.run_next("agent.1")
        self.assertEqual(self.engine.review({**child, "solutions": {"two_sum": inspect.getsource(references.two_sum)}})["accepted"], True)
        self.assertEqual(self.engine.finish(child)["state"], "completed")
        lead = self.run_next("agent")
        stale = {**lead, "attempt": lead["attempt"] - 1}
        with self.assertRaises(APIError):
            self.engine.review({**stale, "solutions": mine})
        verdict = self.engine.review({**lead, "solutions": mine})  # two_sum is a stub here, but the subagent solved it
        self.assertEqual((verdict["accepted"], verdict["final"]), (True, True))
        self.assertEqual(self.engine.finish(lead)["state"], "completed")
        self.assertEqual(self.engine.status("task")["result"]["solved"], 2)

    def test_clearing_runs_removes_them_and_frees_their_names(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.engine.agent_log({**lead, "seq": 0, "kind": "init", "data": {"messages": []}})
        self.engine.llm_call({"call_id": "c1", "phase": "start", "task_id": "task", "worker_id": "agent", "vm_id": lead["vm_id"]})
        self.engine.submit({"task_id": "second", "spec": {"seed": 2}})
        self.engine.tick()
        self.assertEqual(self.engine.clear_runs({"task_id": "task"}), {"ok": True, "cleared": ["task"]})
        with self.assertRaises(APIError) as caught:
            self.engine.status("task")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(APIError):
            self.engine.agent_log({**lead, "seq": 1, "kind": "query", "data": {}})
        for table in ("events", "agent_log", "spawns", "llm_calls"):
            self.assertEqual(self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id='task'").fetchone()[0], 0, table)
        jobs = [j for vm, session in (("a", "a1"), ("b", "b1")) for j in self.engine.poll({"vm_id": vm, "session": session})["jobs"]]
        self.assertEqual([j["task_id"] for j in jobs], ["second"])  # the cleared run's agents lose their assignments
        self.engine.submit({"task_id": "task", "spec": {"seed": 9}})  # the id and the name can be used again
        self.assertEqual((self.engine.status("second")["name"], self.engine.status("task")["name"]), ("Bob", "Alice"))
        self.assertEqual(sorted(self.engine.clear_runs({"all": True})["cleared"]), ["second", "task"])
        view = self.engine.demo_state()
        self.assertEqual((view["tasks"], view["score"], view["llm"]["total"], len(view["vms"])), ([], {"solved": 0, "total": 0}, 0, 2))
        self.assertEqual(self.engine.clear_runs({"all": True})["cleared"], [])
        with self.assertRaises(APIError):
            self.engine.clear_runs({"task_id": "missing"})
        self.engine.submit({"task_id": "fresh", "spec": {"seed": 1}})
        self.assertEqual(self.engine.status("fresh")["name"], "Alice")

    def reopen(self):
        self.store.db.close()
        self.store = Store(self.path)
        self.store.rebuild()
        self.engine = AgentEngine(self.store, self.config, self.clock)

    def kinds(self, *wanted):
        return [e["type"] for e in self.store.events("task") if e["type"] in wanted]

    def test_scenario_cards_name_their_target_or_say_why_not(self):
        cards = self.engine.demo_state()["scenarios"]
        self.assertEqual((cards["agent"]["available"], cards["agent"]["reason"]), (False, "No agent is running right now"))
        self.assertEqual((cards["spawn"]["available"], cards["spawn"]["reason"]), (False, "No subagent has been started yet"))
        self.assertEqual(sorted(cards), ["agent", "spawn", "spawn_crash", "vm"])
        self.assertEqual((cards["spawn_crash"]["available"], cards["spawn_crash"]["armed"]), (True, False))
        self.assertEqual((cards["vm"]["available"], cards["vm"]["target"]), (True, "VM 1 · 0 agents on it"))
        lead = self.begin()
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": []}})
        cards = self.engine.demo_state()["scenarios"]
        name = "VM 1" if lead["vm_id"] == "a" else "VM 2"
        self.assertEqual((cards["agent"]["target"], cards["vm"]["target"]), ("Alice Lead · turn 1", f"{name} · 1 agent on it"))
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        self.run_next("agent.1")
        cards = self.engine.demo_state()["scenarios"]
        # With a subagent running, "crash an agent" prefers it to the lead.
        self.assertEqual((cards["spawn"]["target"], cards["agent"]["target"]), ("Alice Sub 1", "Alice Sub 1 · turn 0"))
        self.assertEqual(self.engine.scenario({"kind": "agent"})["worker_id"], "agent.1")
        self.assertEqual(self.engine.scenario({"kind": "agent"})["worker_id"], "agent")
        self.assertEqual(self.engine.demo_state()["scenarios"]["agent"]["available"], False)
        for vm_id in ("a", "b"):
            self.engine.crash_vm({"vm_id": vm_id})
        cards = self.engine.demo_state()["scenarios"]
        self.assertEqual((cards["vm"]["available"], cards["vm"]["reason"]), (False, "No VM is online right now"))
        for gone in ("cut", "orchestrator"):
            with self.assertRaises(APIError) as caught:
                self.engine.scenario({"kind": gone})
            self.assertEqual(caught.exception.code, 400)

    def test_replay_reports_the_model_work_it_saved(self):
        lead = self.begin()
        for index, (latency, prompt, completion) in enumerate(((2.0, 100, 10), (3.5, 200, 20), (9.0, 300, 30))):
            call = {"call_id": f"call-{index}", "task_id": "task", "worker_id": "agent", "vm_id": lead["vm_id"]}
            self.engine.llm_call({**call, "phase": "start"})
            self.clock.now += 0.1
            self.engine.llm_call({**call, "phase": "end", "ok": True, "latency": latency,
                                  "prompt_tokens": prompt, "completion_tokens": completion})
        # Two of the three replies reached the log before the crash; the third did not.
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": []}})
        self.engine.agent_log({**lead, "seq": 1, "kind": "query", "data": {"messages": []}})
        self.assertEqual(self.engine.demo_state()["saved"], {"recoveries": 0, "calls": 0, "seconds": 0, "tokens": 0})
        descriptor = self.engine.scenario({"kind": "lead"})
        self.clock.now += 4
        self.beat("a")
        self.beat("b")
        self.clock.now += 1
        self.engine.tick()
        resumed = self.identity("agent")
        self.engine.start(resumed)
        self.engine.agent_resume(resumed)
        payload = next(e["payload"] for e in self.store.events("task") if e["type"] == "log_replayed")
        self.assertEqual((payload["saved_calls"], payload["saved_seconds"], payload["saved_tokens"]), (2, 5.5, 330))
        self.assertEqual(self.engine.demo_state()["saved"], {"recoveries": 1, "calls": 2, "seconds": 5.5, "tokens": 330})
        view, steps = self.drill(descriptor)
        self.assertIn("6s of model time and 330 tokens not spent again", steps["replay"]["items"][0]["text"])
        self.assertEqual(steps["replay"]["items"][0]["open"], {"task": "task", "worker": "agent"})
        self.assertEqual((view["done"], view["summary"]), (False, None))
        self.engine.agent_log({**resumed, "seq": 2, "kind": "query", "data": {"messages": []}})
        view, steps = self.drill(descriptor)
        self.assertTrue(view["done"])
        self.assertIn("Alice Lead was back at work", view["summary"])
        self.assertIn("2 model replies (6s of model time) came from the log and none were requested again", view["summary"])
        self.assertIn("without repeating any model call", view["shows"])
        self.assertEqual(view["focus"], {"agents": ["task/agent"]})

    def test_an_end_report_closes_a_call_whose_start_was_lost_and_a_logged_reply_closes_open_calls(self):
        lead = self.begin()
        call = {"task_id": "task", "worker_id": "agent", "vm_id": lead["vm_id"]}
        self.engine.llm_call({**call, "call_id": "no-start", "phase": "end", "ok": True, "latency": 2, "completion_tokens": 5})
        self.engine.llm_call({**call, "call_id": "no-end", "phase": "start"})
        llm = self.engine.demo_state()["llm"]
        self.assertEqual((llm["total"], llm["in_flight"]), (2, 1))
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": []}})
        self.assertEqual(self.engine.demo_state()["llm"]["in_flight"], 0)

    def test_crash_mid_spawn_reruns_the_command_without_creating_a_second_subagent(self):
        descriptor = self.engine.scenario({"kind": "spawn_crash"})
        self.assertEqual(self.engine.scenario({"kind": "spawn_crash"}), descriptor)
        view, steps = self.drill(descriptor)
        self.assertEqual((view["done"], steps, view["subtitle"]), (False, {}, "armed"))
        self.assertIn("Waiting for any agent to run spawn_subagent", view["waiting"])
        self.assertTrue(self.engine.demo_state()["scenarios"]["spawn_crash"]["armed"])
        lead = self.begin()
        reply = "I will split the work.\n\n```mswea_bash_command\nspawn_subagent --problems two_sum && spawn_subagent --problems coin_change\n```"
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": [{"role": "assistant", "content": reply}]}})
        first = {"op_id": "turn1-0", "problems": ["two_sum"]}
        second = {"op_id": "turn1-1", "problems": ["coin_change"]}
        receipt = self.engine.spawn({**lead, **first})
        # The receipt is durable and the trap has fired: the spawner is dead from here on.
        self.assertIsNone(self.engine.spawn_trap)
        for dead in (lambda: self.engine.spawn({**lead, **second}),
                     lambda: self.engine.agent_log({**lead, "seq": 1, "kind": "observation", "data": {}})):
            with self.assertRaises(APIError):
                dead()
        view, steps = self.drill(descriptor)
        self.assertEqual(list(steps), ["decide", "receipt", "inject", "detect", "fence", "authorize", "place", "start",
                                       "replay", "rerun", "continue"])
        self.assertEqual(steps["decide"]["code"], "spawn_subagent --problems two_sum && spawn_subagent --problems coin_change")
        self.assertEqual((view["title"], steps["detect"]["state"], steps["rerun"]["state"]),
                         ("Alice Lead crashes mid-spawn", "active", "pending"))
        self.assertEqual(view["focus"], {"agents": ["task/agent", "task/agent.1"]})
        self.clock.now += 4
        self.beat("a")
        self.beat("b")
        self.clock.now += 1
        self.engine.tick()
        resumed = self.identity("agent")
        self.assertEqual((resumed["attempt"], self.worker("agent")["failures"]), (2, 1))
        self.engine.start(resumed)
        self.assertEqual([r["kind"] for r in self.engine.agent_resume(resumed)["records"]], ["query"])
        # Rebuilt from a log that ends with the command, the agent runs the whole command again.
        self.assertEqual(self.engine.spawn({**resumed, **first}), receipt)
        self.assertEqual(self.engine.spawn({**resumed, **first}), receipt)
        self.assertEqual(self.engine.spawn({**resumed, **second})["child_id"], "agent.2")
        self.assertEqual(sorted(self.engine.status("task")["workers"]), ["agent", "agent.1", "agent.2"])
        self.assertEqual(self.kinds("subtask_spawned", "spawn_replayed"), ["subtask_spawned", "spawn_replayed", "subtask_spawned"])
        view, steps = self.drill(descriptor)
        self.assertEqual((steps["rerun"]["state"], steps["continue"]["state"], view["done"]), ("done", "active", False))
        self.assertIn("returned from the receipt, not created again · Alice Lead has 2 subagents", steps["rerun"]["items"][0]["text"])
        self.assertIn("run again", steps["replay"]["items"][0]["text"])
        self.engine.agent_log({**resumed, "seq": 1, "kind": "observation", "data": {}})
        view, steps = self.drill(descriptor)
        self.assertEqual((view["done"], view["summary"]),
                         (True, "Alice Lead ran its spawn command twice, before and after the crash, and Alice Sub 1 exists exactly once."))
        # Only the first spawn after arming is trapped.
        self.assertEqual(self.engine.spawn({**resumed, "op_id": "turn2-0", "problems": ["edit_distance"]})["child_id"], "agent.3")
        self.assertEqual(self.worker("agent")["state"], "running")

    def test_restarted_orchestrator_gives_every_valid_lease_a_fresh_window(self):
        lead = self.begin()
        self.engine.agent_log({**lead, "seq": 0, "kind": "query", "data": {"messages": []}})
        # The orchestrator is down for a minute, far longer than the lease and the stall limit.
        self.clock.now += 60
        self.reopen()
        self.engine.tick()
        self.assertEqual([(self.worker("agent")[k]) for k in ("state", "failures", "attempt")], ["running", 0, 1])
        restarted = next(e["payload"] for e in self.store.events("task") if e["type"] == "orchestrator_restarted")
        self.assertEqual(restarted, {"down": 60, "leases": {"vms": 2, "agents": 1}})
        # The VM and the agent carry on under the session and attempt they already had.
        self.beat(lead["vm_id"], lead)
        self.engine.agent_log({**lead, "seq": 1, "kind": "observation", "data": {}})
        # A VM that really died while the orchestrator was down is recovered one lease later.
        self.clock.now += 5
        self.engine.tick()
        self.assertEqual((self.worker("agent")["failures"], self.kinds("attempt_superseded")), (1, ["attempt_superseded"]))

    def test_parent_is_told_the_best_result_reached_anywhere_below_each_subagent(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum", "coin_change"]})
        middle = self.run_next("agent.1")
        self.engine.spawn({**middle, "op_id": "2", "problems": ["two_sum"]})
        leaf = self.run_next("agent.1.1")
        self.complete(leaf, two_sum=(9, 9))
        # The middle agent handed two_sum on and left its own copy untouched; it failed coin_change itself.
        self.complete(middle, two_sum=(0, 9), coin_change=(3, 8))
        told = self.engine.manage(lead)["children"]["agent.1"]["result"]["problems"]
        self.assertEqual({p: (g["passed"], g["total"], g["by"]) for p, g in told.items()},
                         {"two_sum": (9, 9, "agent.1.1"), "coin_change": (3, 8, "agent.1")})

    def test_ancestor_authorizes_recovery_of_a_deeper_subagent(self):
        lead = self.begin()
        self.engine.spawn({**lead, "op_id": "1", "problems": ["two_sum"]})
        child = self.run_next("agent.1")
        self.engine.spawn({**child, "op_id": "2", "problems": ["two_sum"]})
        self.run_next("agent.1.1")
        self.engine.stop({"task_id": "task", "worker_id": "agent.1.1"})
        self.assertEqual(self.worker("agent.1.1")["state"], "needs_recovery")
        children = self.engine.manage(lead)["children"]
        self.assertEqual(list(children), ["agent.1"])
        self.assertEqual(self.worker("agent.1.1")["state"], "queued")


if __name__ == "__main__":
    unittest.main()
