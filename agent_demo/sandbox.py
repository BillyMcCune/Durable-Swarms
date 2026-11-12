"""Grade a candidate solution in a separate, resource-limited subprocess.

This contains runaway or crashing code well enough for a demo on trusted VMs.
It is not a security boundary: the subprocess runs as the same user.
"""

import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

from .grade_runner import CASE_SECONDS

RUNNER = Path(__file__).with_name("grade_runner.py")
MEMORY_BYTES = 512 * 1024 * 1024


# Apply CPU and memory limits in the child before it starts the runner.
def _limits(cpu_seconds):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if sys.platform.startswith("linux"):
            # macOS does not enforce RLIMIT_AS reliably; only set it on Linux.
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    return apply


# Grade solution_path against a problem's tests (hidden tests by default).
# Inputs:
#   solution_path: Path to the candidate solution.py.
#   problem: Problem dictionary from the bank.
#   tests: Optional explicit test list; defaults to problem["hidden_tests"].
# Output: {"passed", "total", "cases", "error"}; never raises for bad solutions.
def grade(solution_path, problem, tests=None):
    tests = problem["hidden_tests"] if tests is None else tests
    failed = {"passed": 0, "total": len(tests), "cases": [], "error": None}
    solution_path = Path(solution_path)
    if not solution_path.is_file():
        return {**failed, "error": "solution.py is missing"}
    request = {"path": str(solution_path.resolve()), "entry": problem["entry"], "kind": problem["kind"],
               "comparator": problem["comparator"], "tests": tests}
    budget = CASE_SECONDS * len(tests) + 5
    with tempfile.TemporaryDirectory() as scratch:
        try:
            done = subprocess.run([sys.executable, "-I", str(RUNNER)], input=json.dumps(request).encode(),
                                  capture_output=True, cwd=scratch, timeout=budget, preexec_fn=_limits(budget))
        except subprocess.TimeoutExpired:
            return {**failed, "error": f"grading timed out after {budget}s"}
    try:
        return json.loads(done.stdout)
    except ValueError:
        tail = done.stderr.decode(errors="replace").strip()[-400:]
        return {**failed, "error": f"grader crashed (exit {done.returncode}): {tail}"}
