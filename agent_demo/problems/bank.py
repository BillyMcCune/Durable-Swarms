"""Load the problem bank, sample a problem set, and lay out agent workspaces."""

import functools
import json
from pathlib import Path
import random

HERE = Path(__file__).parent
RUNNER = HERE.parent / "grade_runner.py"
DEFAULT_MIX = {"easy": 4, "medium": 4, "hard": 2}
MARKER = "# --- entry point ---"

FOOTER = '''
if __name__ == "__main__":
    import os
    TESTS = json.loads({tests!r})
    result = run_all(os.path.join(os.path.dirname(os.path.abspath(__file__)), "solution.py"),
                     {entry!r}, {kind!r}, {comparator!r}, TESTS)
    if result["error"]:
        print("ERROR:", result["error"])
    for number, case in enumerate(result["cases"], 1):
        if case["ok"]:
            print(f"PASS test {{number}}")
        else:
            print(f"FAIL test {{number}}: args={{case['args']!r}} expected={{case['expected']!r}} "
                  f"got={{case.get('got', case.get('error'))}}")
    print(f"{{result['passed']}}/{{result['total']}} visible tests passed")
    sys.exit(0 if result["passed"] == result["total"] and not result["error"] else 1)
'''


# Return the bank as {problem_id: problem}, read once per process.
@functools.lru_cache(maxsize=1)
def load_bank():
    return {p["id"]: p for p in json.loads((HERE / "bank.json").read_text())}


# Pick a deterministic problem set for a seed: `mix` gives the count per difficulty.
# Inputs:
#   seed: Any value; the same seed and mix always give the same list.
#   mix: {"easy": n, "medium": n, "hard": n}; defaults to 4/4/2.
# Output: List of problem ids, easy first; raises ValueError for an impossible mix.
def sample(seed, mix=None):
    mix = DEFAULT_MIX if mix is None else mix
    bank = load_bank()
    rng = random.Random(f"problems:{seed}")
    chosen = []
    for difficulty in ("easy", "medium", "hard"):
        pool = sorted(p["id"] for p in bank.values() if p["difficulty"] == difficulty)
        count = mix.get(difficulty, 0)
        if type(count) is not int or not 0 <= count <= len(pool):
            raise ValueError(f"mix[{difficulty}] must be between 0 and {len(pool)}")
        chosen += rng.sample(pool, count)
    if not chosen:
        raise ValueError("mix selects no problems")
    return chosen


def _example(problem, test):
    if problem["kind"] == "ops":
        return f"operations {test['args'][0]!r}\n    arguments  {test['args'][1]!r}\n    returns    {test['expected']!r}"
    return f"{problem['entry']}({', '.join(repr(a) for a in test['args'])})  ->  {test['expected']!r}"


# Write problems/<id>/{PROBLEM.md, solution.py, test_visible.py} under dest for
# each id. Existing solution.py files are kept so a restored workspace is not reset.
def materialize(problem_ids, dest):
    bank = load_bank()
    runner = RUNNER.read_text().split(MARKER)[0]
    for problem_id in problem_ids:
        problem = bank[problem_id]
        folder = Path(dest) / "problems" / problem_id
        folder.mkdir(parents=True, exist_ok=True)
        examples = "\n\n".join("    " + _example(problem, t) for t in problem["visible_tests"])
        (folder / "PROBLEM.md").write_text(
            f"# {problem['title']} ({problem['difficulty']})\n\n{problem['statement']}\n\n"
            f"## Signature\n\n    {problem['signature']}\n\n"
            f"Write your solution in `solution.py` in this directory. Use only the Python standard library.\n\n"
            f"## Examples\n\n{examples}\n\n"
            f"## Testing\n\n    python3 problems/{problem_id}/test_visible.py\n")
        if not (folder / "solution.py").exists():
            (folder / "solution.py").write_text(problem["stub"])
        (folder / "test_visible.py").write_text(runner + FOOTER.format(
            tests=json.dumps(problem["visible_tests"]), entry=problem["entry"],
            kind=problem["kind"], comparator=problem["comparator"]))
