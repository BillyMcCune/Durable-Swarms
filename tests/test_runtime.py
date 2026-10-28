"""End-to-end checks with real HTTP servers and disposable worker processes."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from durable_swarms.client import request


ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.processes = []
        self.addCleanup(self.cleanup_processes)
        self.output = open(Path(self.temp.name) / "processes.log", "w+")
        self.addCleanup(self.output.close)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.config = Path(self.temp.name) / "cluster.json"
        self.config.write_text(json.dumps({
            "port": port, "database": str(Path(self.temp.name) / "state.sqlite3"),
            "vms": {"vm-a": 2, "vm-b": 2}, "lease_seconds": 1.5,
            "progress_timeout_seconds": 2, "tick_seconds": 0.05, "max_attempts": 3,
        }))
        self.server = self.start("orchestrator", "--config", str(self.config))
        self.wait_for(lambda: request(self.url, "/health"), 10)
        for vm in ("vm-a", "vm-b"):
            self.start("vm", "--id", vm, "--url", self.url, "--interval", "0.1")
        self.wait_for(lambda: len(request(self.url, "/vms")) == 2, 10)

    def start(self, *args):
        process = subprocess.Popen([sys.executable, "-m", "durable_swarms", *args], cwd=ROOT,
                                   stdout=self.output, stderr=self.output, start_new_session=True)
        self.processes.append(process)
        return process

    def cleanup_processes(self):
        for process in reversed(self.processes):
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def wait_for(self, fn, timeout=20):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = fn()
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            time.sleep(0.05)
        self.output.flush()
        self.output.seek(0)
        self.fail(f"condition timed out ({last_error})\n{self.output.read()[-6000:]}")

    def status(self, task):
        return request(self.url, "/status?task_id=" + task)

    def test_all_faults_with_concurrent_workloads_and_dependency_results(self):
        faults = ["worker_exit", "vm_loss", "stall", "retry_loop", "invalid_result", "effect_interruption"]
        for kind in faults:
            spec = {"leader": {"id": "lead", "steps": [
                {"type": "effect", "key": "writes", "delta": 1}, {"type": "compute", "seconds": 0.3}]},
                "faults": [{"worker": "lead", "attempt": 1, "step": 0, "kind": kind}]}
            request(self.url, "/submit", {"task_id": kind, "spec": spec})
        with open(ROOT / "examples" / "workflow.json") as source:
            spec = json.load(source)
        request(self.url, "/submit", {"task_id": "children", "spec": spec})
        names = [*faults, "children"]
        self.wait_for(lambda: all(self.status(name)["status"] in {"completed", "failed", "escalated"} for name in names), 35)
        for kind in faults:
            status = self.status(kind)
            self.assertEqual(status["status"], "failed" if kind == "invalid_result" else "completed", status)
            if kind != "invalid_result":
                self.assertEqual(status["effects"]["writes"], 1)
        status = self.status("children")
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual(status["effects"], {"patches_applied": 1})
        self.assertEqual(status["result"]["subtasks"], {"implement": "patch ready", "review": "review complete"})
        events = request(self.url, "/events?task_id=vm_loss&limit=500")
        placements = [e["payload"]["vm_id"] for e in events if e["type"] == "worker_assigned"]
        self.assertGreaterEqual(len(set(placements)), 2)

    def test_orchestrator_process_restart_preserves_completed_steps(self):
        spec = {"leader": {"id": "lead", "steps": [
            {"type": "effect", "key": "writes", "delta": 1}, {"type": "compute", "seconds": 2}]}}
        request(self.url, "/submit", {"task_id": "restart", "spec": spec})
        self.wait_for(lambda: self.status("restart")["workers"]["lead"]["next_step"] == 1)
        self.server.kill()
        self.server.wait(timeout=5)
        self.server = self.start("orchestrator", "--config", str(self.config))
        self.wait_for(lambda: self.status("restart")["status"] == "completed", 20)
        status = self.status("restart")
        self.assertEqual(status["effects"], {"writes": 1})
        events = request(self.url, "/events?task_id=restart&limit=500")
        self.assertEqual(sum(e["type"] == "effect_completed" for e in events), 1)
        self.assertEqual(sum(e["type"] == "step_completed" and e["payload"]["step"] == 0 for e in events), 1)


if __name__ == "__main__":
    unittest.main()
