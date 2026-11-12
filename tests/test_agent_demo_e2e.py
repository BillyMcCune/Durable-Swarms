"""End-to-end agent demo run with real processes and a scripted stand-in for Ollama.

A lead agent hands one problem to a subagent and waits. The VM hosting the
subagent is killed part-way through; the subagent must resume on the other VM
from its durable log without asking the model again for any logged turn. The
orchestrator grades each agent when it says it is done and sends the lead back
once because one of its problems is still unsolved.
Requires mini-swe-agent (run with the project's virtualenv); skipped otherwise.
"""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode

from agent_demo.problems import references
from durable_swarms.client import request

ROOT = Path(__file__).resolve().parents[1]
BLOCK = "THOUGHT: scripted step.\n\n```mswea_bash_command\n{}\n```"
SOLVE = ("cat <<'EOF' > problems/two_sum/solution.py\n" + inspect.getsource(references.two_sum)
         + "EOF\npython3 problems/two_sum/test_visible.py")
LEAD = ["spawn_subagent --problems two_sum --instructions 'solve it'", "wait_subagents",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
SUB = ["sleep 0.4; echo one", "sleep 0.4; echo two", "sleep 0.4; echo three", "sleep 0.4; echo four", SOLVE,
       "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOllama:
    """Answer /api/chat from a script chosen by who is asking and how far they are."""

    def __init__(self):
        self.calls = Counter()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply({})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not self.path.endswith("/api/chat"):
                    return self.reply({})
                messages = body["messages"]
                who = re.search(r"You are `([^`]+)`", messages[1]["content"]).group(1)
                turn = sum(m["role"] == "assistant" for m in messages)
                fake.calls[(who, turn)] += 1
                script = LEAD if who == "agent" else SUB
                time.sleep(0.2)
                self.reply({"model": body.get("model"), "created_at": "2026-01-01T00:00:00Z", "done": True,
                            "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5,
                            "message": {"role": "assistant", "content": BLOCK.format(script[min(turn, len(script) - 1)])}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(importlib.util.find_spec("minisweagent"), "mini-swe-agent is not installed in this interpreter")
class AgentDemoEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ollama = FakeOllama()
        self.url = f"http://127.0.0.1:{free_port()}"
        config = {"host": "127.0.0.1", "port": int(self.url.rsplit(":", 1)[1]), "database": str(self.dir / "demo.sqlite3"),
                  "vms": {"vm-a": 2, "vm-b": 2}, "lease_seconds": 2, "progress_timeout_seconds": 60, "max_attempts": 3,
                  "tick_seconds": 0.1, "model": "scripted"}
        (self.dir / "config.json").write_text(json.dumps(config))
        self.env = {**os.environ, "LLM_URL": self.ollama.url, "AGENT_DEMO_HOME": str(self.dir / "home"),
                    "PYTHONPATH": str(ROOT), "MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "1"}
        self.processes = {}
        self.spawn("orchestrator", "orchestrator", "--config", str(self.dir / "config.json"))
        self.wait_for(self.healthy, 15)
        for vm in ("vm-a", "vm-b"):
            self.spawn(vm, "vm", "--id", vm, "--url", self.url, "--interval", "0.2")

    def tearDown(self):
        for process in self.processes.values():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in self.processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        self.ollama.close()
        self.tmp.cleanup()

    def spawn(self, name, *args):
        with open(self.dir / f"{name}.log", "w") as log:
            self.processes[name] = subprocess.Popen([sys.executable, "-m", "agent_demo", *args], cwd=ROOT, env=self.env,
                                                    stdout=log, stderr=subprocess.STDOUT)

    def healthy(self):
        try:
            return request(self.url, "/health")["ok"]
        except OSError:
            return False

    def wait_for(self, check, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.1)
        logs = "\n".join(f"--- {p.name}\n{p.read_text()[-1500:]}" for p in sorted(self.dir.glob("*.log")))
        self.fail(f"condition not met within {seconds}s\n{logs}")

    def task(self):
        return request(self.url, "/status?task_id=e2e")

    def queries(self, worker_id):
        view = request(self.url, f"/demo/trajectory?task_id=e2e&worker_id={worker_id}")
        return sum(r["kind"] == "query" for r in view["records"])

    def submit(self):
        request(self.url, "/submit", {"task_id": "e2e", "spec": {"problems": ["two_sum", "valid_parentheses"],
                                                             "limits": {"max_reviews": 1}}})

    # The subagent's record once it is running and has logged at least two model replies.
    def subagent_midway(self):
        worker = self.task()["workers"].get("agent.1")
        return worker if worker and worker["state"] == "running" and self.queries("agent.1") >= 2 else None

    def finished(self):
        task = self.wait_for(lambda: (lambda t: t if t["status"] not in ("queued", "running", "recovering") else None)(self.task()), 90)
        self.assertEqual(task["status"], "completed", task.get("reason"))
        return task

    def walkthrough(self, descriptor):
        view = request(self.url, "/demo/drill?" + urlencode(descriptor))
        return view, {step["key"]: step for step in view["steps"]}

    def test_orchestrator_killed_outright_and_restarted_loses_no_agent_and_repeats_no_turn(self):
        self.submit()
        before = self.wait_for(self.subagent_midway, 60)
        self.processes["orchestrator"].kill()
        self.processes["orchestrator"].wait()
        time.sleep(3)  # longer than the 2s lease: without a fresh lease window every agent would be recovered
        self.spawn("orchestrator", "orchestrator", "--config", str(self.dir / "config.json"))
        self.wait_for(self.healthy, 15)
        task = self.finished()
        self.assertEqual({w["id"]: (w["failures"], w["attempt"]) for w in task["workers"].values()}["agent.1"], (0, before["attempt"]))
        self.assertEqual(task["workers"]["agent"]["failures"], 0)
        self.assertEqual(task["workers"]["agent.1"]["vm_id"], before["vm_id"])
        self.assertEqual(max(self.ollama.calls.values()), 1)
        self.assertEqual((task["result"]["solved"], task["result"]["total"]), (1, 2))
        view = request(self.url, "/demo/trajectory?task_id=e2e&worker_id=agent.1")
        self.assertEqual((view["lost_attempts"], {r["attempt"] for r in view["records"]}), ([], {before["attempt"]}))

    def test_lead_killed_mid_spawn_runs_its_command_again_and_gets_the_same_subagent(self):
        descriptor = request(self.url, "/scenario", {"kind": "spawn_crash"})
        self.submit()
        task = self.finished()
        self.assertEqual(sorted(task["workers"]), ["agent", "agent.1"])
        self.assertEqual((task["workers"]["agent"]["failures"], task["workers"]["agent.1"]["failures"]), (1, 0))
        # The reply holding the spawn command was logged before the crash, so it is not requested again.
        self.assertEqual(max(self.ollama.calls.values()), 1)
        view, steps = self.walkthrough(descriptor)
        self.assertEqual((view["done"], steps["rerun"]["state"]), (True, "done"))
        self.assertIn("exists exactly once", view["summary"])
        self.assertIn("run again", steps["replay"]["items"][0]["text"])
        self.assertEqual((task["result"]["solved"], task["result"]["total"]), (1, 2))

    def test_subagent_resumes_on_another_vm_without_repeating_logged_turns(self):
        self.submit()
        before = self.wait_for(self.subagent_midway, 60)
        self.assertEqual(self.task()["workers"]["agent"]["state"], "waiting")
        logged = self.queries("agent.1")
        self.processes[before["vm_id"]].send_signal(signal.SIGTERM)

        task = self.finished()
        self.assertEqual(sorted(task["workers"]), ["agent", "agent.1"])
        sub = task["workers"]["agent.1"]
        self.assertEqual((sub["failures"], sub["state"]), (1, "completed"))
        self.assertNotEqual(sub["vm_id"], before["vm_id"])
        card = task["result"]["problems"]
        self.assertEqual((card["two_sum"]["solved"], card["two_sum"]["solved_by"]), (True, "agent.1"))
        self.assertFalse(card["valid_parentheses"]["solved"])
        self.assertEqual((task["result"]["solved"], task["result"]["total"]), (1, 2))

        # Every turn that was durable when the VM died was asked of the model exactly once.
        self.assertGreaterEqual(logged, 2)
        for turn in range(logged):
            self.assertEqual(self.ollama.calls[("agent.1", turn)], 1, f"turn {turn} was requested again")
        # The lead said it was done with one problem unsolved, was sent back once, then gave up.
        self.assertEqual([self.ollama.calls[("agent", turn)] for turn in range(5)], [1, 1, 1, 1, 0])
        self.assertEqual(max(self.ollama.calls.values()), max(1, self.ollama.calls[("agent.1", logged)]))

        view = request(self.url, "/demo/trajectory?task_id=e2e&worker_id=agent.1")
        self.assertEqual(view["lost_attempts"], [1])
        self.assertEqual({r["vm_id"] for r in view["records"]}, {"vm-a", "vm-b"})
        self.assertEqual([r["seq"] for r in view["records"]], list(range(len(view["records"]))))
        self.assertEqual(view["records"][-1]["kind"], "grade")
        lead = request(self.url, "/demo/trajectory?task_id=e2e&worker_id=agent")
        self.assertIn("agent.1 / two_sum: PASSED", next(r for r in lead["records"] if r["kind"] == "children")["data"]["messages"][0]["content"])
        sent_back = [r for r in lead["records"] if r["kind"] == "review"]
        self.assertEqual(len(sent_back), 1)
        self.assertIn("- valid_parentheses: not solved", sent_back[0]["data"]["messages"][0]["content"])
        self.assertNotIn("- two_sum", sent_back[0]["data"]["messages"][0]["content"])
        self.assertEqual(task["workers"]["agent"]["reviews"], 1)
        state = request(self.url, "/demo/state")
        self.assertEqual(state["score"], {"solved": 1, "total": 2})
        self.assertEqual(state["llm"]["in_flight"], 0)


if __name__ == "__main__":
    unittest.main()
