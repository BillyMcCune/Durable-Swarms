"""Checks for the demo problem bank, workspace layout, and sandboxed grader."""

from collections import Counter
import inspect
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_demo import sandbox
from agent_demo.problems import bank, references


class BankTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bank = bank.load_bank()

    def tearDown(self):
        self.tmp.cleanup()

    def solution(self, text):
        path = self.dir / "solution.py"
        path.write_text(text)
        return path

    def test_bank_has_ten_problems_per_difficulty_with_tests(self):
        self.assertEqual(Counter(p["difficulty"] for p in self.bank.values()), {"easy": 10, "medium": 10, "hard": 10})
        for problem in self.bank.values():
            self.assertGreaterEqual(len(problem["visible_tests"]), 2, problem["id"])
            self.assertGreaterEqual(len(problem["hidden_tests"]), 6, problem["id"])

    def test_every_reference_solution_passes_all_tests_in_the_sandbox(self):
        for problem in self.bank.values():
            path = self.solution(inspect.getsource(getattr(references, problem["entry"])))
            for tests in (problem["visible_tests"], problem["hidden_tests"]):
                grade = sandbox.grade(path, problem, tests)
                self.assertIsNone(grade["error"], problem["id"])
                self.assertEqual((grade["passed"], grade["total"]), (len(tests), len(tests)), problem["id"])

    def test_sample_is_deterministic_and_follows_the_mix(self):
        first = bank.sample(7)
        self.assertEqual(first, bank.sample(7))
        self.assertNotEqual(first, bank.sample(8))
        self.assertEqual(Counter(self.bank[p]["difficulty"] for p in first), {"easy": 4, "medium": 4, "hard": 2})
        self.assertEqual(len(bank.sample(1, {"hard": 3})), 3)
        with self.assertRaises(ValueError):
            bank.sample(1, {"easy": 11})
        with self.assertRaises(ValueError):
            bank.sample(1, {})

    def test_grader_reports_wrong_missing_crashing_and_misnamed_solutions(self):
        problem = self.bank["two_sum"]
        self.assertEqual(sandbox.grade(self.dir / "absent.py", problem)["error"], "solution.py is missing")
        wrong = sandbox.grade(self.solution("def two_sum(nums, target):\n    return [0, 1]\n"), problem)
        self.assertIsNone(wrong["error"])
        self.assertTrue(0 < wrong["passed"] < wrong["total"])
        crash = sandbox.grade(self.solution("def two_sum(nums, target):\n    raise RuntimeError('boom')\n"), problem)
        self.assertEqual(crash["passed"], 0)
        self.assertIn("boom", crash["cases"][0]["error"])
        misnamed = sandbox.grade(self.solution("def twoSum(nums, target):\n    return []\n"), problem)
        self.assertIn("must define `two_sum`", misnamed["error"])
        self.assertIn("twoSum", misnamed["error"])
        broken = sandbox.grade(self.solution("def two_sum(:\n"), problem)
        self.assertIn("could not load solution", broken["error"])
        self.assertEqual(sandbox.grade(self.solution(problem["stub"]), problem)["passed"], 0)

    def test_grader_stops_an_infinite_loop_and_ignores_printing(self):
        problem = self.bank["climbing_stairs"]
        one = problem["hidden_tests"][:1]
        loop = sandbox.grade(self.solution("def climbing_stairs(n):\n    while True:\n        pass\n"), problem, one)
        self.assertEqual(loop["passed"], 0)
        self.assertIn("timed out", loop["cases"][0]["error"])
        noisy = inspect.getsource(references.climbing_stairs).replace("a, b = 1, 1", "print('debug'); a, b = 1, 1")
        self.assertEqual(sandbox.grade(self.solution(noisy), problem, one)["passed"], 1)

    def test_materialize_writes_a_runnable_workspace_and_keeps_existing_solutions(self):
        bank.materialize(["two_sum", "lru_cache"], self.dir)
        folder = self.dir / "problems" / "two_sum"
        self.assertIn("two_sum(", (folder / "PROBLEM.md").read_text())
        self.assertNotIn("hidden", (folder / "test_visible.py").read_text())
        run = lambda: subprocess.run([sys.executable, str(folder / "test_visible.py")], capture_output=True, text=True, cwd=self.dir)
        self.assertEqual(run().returncode, 1)
        (folder / "solution.py").write_text(inspect.getsource(references.two_sum))
        bank.materialize(["two_sum"], self.dir)
        done = run()
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("3/3 visible tests passed", done.stdout)
        self.assertIn("class LRUCache", (self.dir / "problems" / "lru_cache" / "solution.py").read_text())


if __name__ == "__main__":
    unittest.main()
