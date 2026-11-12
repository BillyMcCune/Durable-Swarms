"""Standalone test runner: load a solution file and check it against test cases.

Used two ways. `sandbox.grade` pipes a JSON request to this script on stdin, and
`bank.materialize` embeds this source in every workspace's `test_visible.py`.
Standard library only, so it runs anywhere Python does.
"""

import copy
import importlib.util
import json
import signal
import sys
import traceback

CASE_SECONDS = 2


class CaseTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise CaseTimeout()


# Import a solution file by path without touching sys.path.
def load(path):
    spec = importlib.util.spec_from_file_location("solution", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Turn tuples into lists so a solution may return either.
def normalize(value):
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


# Compare an answer with the expected value under the problem's comparator:
# exact, unordered (list order ignored), unordered_nested, or float (tolerance).
def same(comparator, got, expected):
    if comparator == "float":
        return isinstance(got, (int, float)) and not isinstance(got, bool) and abs(got - expected) <= 1e-6
    got, expected = normalize(got), normalize(expected)
    try:
        if comparator == "unordered":
            return sorted(got) == sorted(expected)
        if comparator == "unordered_nested":
            return sorted(sorted(x) for x in got) == sorted(sorted(x) for x in expected)
    except TypeError:
        return False
    return type(got) is type(expected) and got == expected


# Run one case. `function` problems call entry(*args). `ops` problems build an
# object from the first operation and return the result of every later method call.
def run_case(module, entry, kind, args):
    args = copy.deepcopy(args)
    if kind == "function":
        return getattr(module, entry)(*args)
    names, call_args = args
    target = getattr(module, entry)(*call_args[0])
    out = [None]
    for name, arg in zip(names[1:], call_args[1:]):
        out.append(getattr(target, name)(*arg))
    return out


# Grade a solution against tests. Never raises: load errors and per-case
# exceptions or timeouts are reported in the returned dictionary.
def run_all(path, entry, kind, comparator, tests):
    cases = []
    result = {"passed": 0, "total": len(tests), "cases": cases, "error": None}
    try:
        module = load(path)
    except BaseException as exc:
        result["error"] = "could not load solution: " + "".join(traceback.format_exception_only(type(exc), exc)).strip()[-500:]
        return result
    if not callable(getattr(module, entry, None)):
        found = ", ".join(n for n, v in vars(module).items() if callable(v) and not n.startswith("_")) or "nothing"
        result["error"] = f"solution.py must define `{entry}` with exactly that name; it defines: {found}"
        return result
    signal.signal(signal.SIGALRM, _alarm)
    for test in tests:
        case = {"args": test["args"], "expected": test["expected"], "ok": False}
        try:
            signal.setitimer(signal.ITIMER_REAL, CASE_SECONDS)
            got = run_case(module, entry, kind, test["args"])
            signal.setitimer(signal.ITIMER_REAL, 0)
            case["ok"] = same(comparator, got, test["expected"])
            case["got"] = repr(got)[:300]
        except CaseTimeout:
            case["error"] = f"timed out after {CASE_SECONDS}s"
        except BaseException as exc:
            signal.setitimer(signal.ITIMER_REAL, 0)
            case["error"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()[-300:]
        result["passed"] += case["ok"]
        cases.append(case)
    return result


# --- entry point ---
# Read {"path", "entry", "kind", "comparator", "tests"} from stdin and print the
# grade as JSON. Anything the solution prints goes to stderr, not the result.
def main():
    request = json.load(sys.stdin)
    out, sys.stdout = sys.stdout, sys.stderr
    result = run_all(request["path"], request["entry"], request["kind"], request["comparator"], request["tests"])
    json.dump(result, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
