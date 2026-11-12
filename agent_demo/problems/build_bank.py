"""Build bank.json from defs.py and references.py.

Run from the repository root:  python3 -m agent_demo.problems.build_bank

For each problem this checks the reference solution against the hand-written
visible tests and against an independent brute-force oracle on seeded random
inputs, then writes the hidden tests (hand-picked edge cases plus a few of the
random cases) with expected values taken from the reference.
"""

from collections import Counter
import copy
from functools import lru_cache
import itertools
import json
import os
from pathlib import Path
import random
import re
import statistics

from .. import grade_runner
from . import defs, references

SEED = 512
RANDOM_CHECKS = 300
RANDOM_HIDDEN = 8
ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
         (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
LRU_STUB = '''# Keep the class and method names exactly as given; the tests call them.
class LRUCache:
    def __init__(self, capacity):
        pass  # TODO

    def get(self, key):
        pass  # TODO

    def put(self, key, value):
        pass  # TODO
'''


def ints(rng, low, high, min_len, max_len):
    return [rng.randint(low, high) for _ in range(rng.randint(min_len, max_len))]


def text(rng, alphabet, min_len, max_len):
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(min_len, max_len)))


def to_roman(number):
    out = ""
    for value, symbol in ROMAN:
        while number >= value:
            out += symbol
            number -= value
    return out


# ---------------------------------------------------------------- generators
def gen_two_sum(rng):
    while True:
        nums = ints(rng, -20, 20, 2, 10)
        i, j = rng.sample(range(len(nums)), 2)
        target = nums[i] + nums[j]
        pairs = [1 for a, b in itertools.combinations(range(len(nums)), 2) if nums[a] + nums[b] == target]
        if len(pairs) == 1:
            return [nums, target]


def gen_parentheses(rng):
    def balanced(depth):
        if depth == 0 or rng.random() < 0.3:
            return ""
        left, right = rng.choice(["()", "[]", "{}"])
        return left + balanced(depth - 1) + right + balanced(depth - 1)
    s = balanced(4)
    if s and rng.random() < 0.5:
        i = rng.randrange(len(s))
        s = s[:i] + rng.choice("()[]{}") + s[i + 1:]
    return [s]


def gen_palindrome(rng):
    half = text(rng, "abA1 ,.", 0, 6)
    return [half + half[::-1] if rng.random() < 0.5 else half + text(rng, "abA1 ,.", 0, 6)]


def gen_majority(rng):
    n = rng.randint(1, 11)
    nums = [7] * (n // 2 + 1) + ints(rng, 0, 5, n - n // 2 - 1, n - n // 2 - 1)
    rng.shuffle(nums)
    return [nums]


def gen_missing(rng):
    nums = list(range(rng.randint(1, 12) + 1))
    nums.remove(rng.choice(nums))
    rng.shuffle(nums)
    return [nums]


def gen_prefix(rng):
    prefix = text(rng, "ab", 0, 3)
    return [[prefix + text(rng, "abc", 0, 3) for _ in range(rng.randint(1, 4))]]


def gen_intervals(rng):
    out = []
    for _ in range(rng.randint(0, 7)):
        start = rng.randint(0, 20)
        out.append([start, start + rng.randint(0, 5)])
    return [out]


def gen_grid(rng):
    rows, cols = rng.randint(1, 5), rng.randint(1, 5)
    return [[[int(rng.random() < 0.5) for _ in range(cols)] for _ in range(rows)]]


def gen_top_k(rng):
    values = rng.sample(range(-5, 10), rng.randint(1, 5))
    counts = rng.sample(range(1, 9), len(values))
    nums = [v for v, c in zip(values, counts) for _ in range(c)]
    rng.shuffle(nums)
    return [nums, rng.randint(1, len(values))]


def gen_lru(rng):
    names, args = ["LRUCache"], [[rng.randint(1, 3)]]
    for _ in range(rng.randint(3, 14)):
        if rng.random() < 0.5:
            names.append("get")
            args.append([rng.randint(0, 4)])
        else:
            names.append("put")
            args.append([rng.randint(0, 4), rng.randint(0, 99)])
    return [names, args]


def gen_courses(rng):
    n = rng.randint(1, 6)
    return [n, [[rng.randrange(n), rng.randrange(n)] for _ in range(rng.randint(0, 7))]]


def gen_median(rng):
    a, b = sorted(ints(rng, -9, 9, 0, 6)), sorted(ints(rng, -9, 9, 0, 6))
    return [a, b] if a or b else [a, [0]]


def gen_ladder(rng):
    words = sorted({text(rng, "abc", 3, 3) for _ in range(rng.randint(2, 12))})
    return [rng.choice(words), text(rng, "abc", 3, 3) if rng.random() < 0.3 else rng.choice(words), words]


def gen_regex(rng):
    pattern = "".join(rng.choice("ab.") + ("*" if rng.random() < 0.4 else "") for _ in range(rng.randint(0, 4)))
    return [text(rng, "ab", 0, 6), pattern]


def gen_window(rng):
    nums = ints(rng, -9, 9, 1, 10)
    return [nums, rng.randint(1, len(nums))]


GENERATORS = {
    "two_sum": gen_two_sum,
    "valid_parentheses": gen_parentheses,
    "merge_sorted_lists": lambda r: [sorted(ints(r, -9, 9, 0, 7)), sorted(ints(r, -9, 9, 0, 7))],
    "best_time_stock": lambda r: [ints(r, 0, 20, 0, 10)],
    "valid_palindrome": gen_palindrome,
    "majority_element": gen_majority,
    "missing_number": gen_missing,
    "roman_to_int": lambda r: [to_roman(r.randint(1, 3999))],
    "longest_common_prefix": gen_prefix,
    "group_anagrams": lambda r: [[text(r, "abc", 0, 3) for _ in range(r.randint(0, 7))]],
    "longest_substring_no_repeat": lambda r: [text(r, "abcd", 0, 14)],
    "merge_intervals": gen_intervals,
    "product_except_self": lambda r: [ints(r, -3, 3, 2, 8)],
    "coin_change": lambda r: [r.sample(range(1, 13), r.randint(1, 4)), r.randint(0, 60)],
    "num_islands": gen_grid,
    "top_k_frequent": gen_top_k,
    "three_sum": lambda r: [ints(r, -5, 5, 0, 10)],
    "lru_cache": gen_lru,
    "course_schedule": gen_courses,
    "trapping_rain_water": lambda r: [ints(r, 0, 8, 0, 12)],
    "median_two_sorted": gen_median,
    "edit_distance": lambda r: [text(r, "abc", 0, 7), text(r, "abc", 0, 7)],
    "min_window_substring": lambda r: [text(r, "abc", 0, 12), text(r, "abc", 1, 4)],
    "largest_rectangle_histogram": lambda r: [ints(r, 0, 8, 0, 12)],
    "word_ladder_length": gen_ladder,
    "regex_matching": gen_regex,
    "merge_k_sorted": lambda r: [[sorted(ints(r, -9, 9, 0, 5)) for _ in range(r.randint(0, 5))]],
    "sliding_window_max": gen_window,
}


# ------------------------------------------------- independent brute oracles
def brute_two_sum(nums, target):
    return next([i, j] for i, j in itertools.combinations(range(len(nums)), 2) if nums[i] + nums[j] == target)


def brute_parentheses(s):
    while any(pair in s for pair in ("()", "[]", "{}")):
        for pair in ("()", "[]", "{}"):
            s = s.replace(pair, "")
    return s == ""


def brute_palindrome(s):
    kept = re.sub(r"[^a-z0-9]", "", s.lower())
    return kept == kept[::-1]


def brute_roman(s):
    total = 0
    while s:
        value, symbol = next((v, sym) for v, sym in ROMAN if s.startswith(sym))
        total += value
        s = s[len(symbol):]
    return total


def brute_anagrams(strs):
    groups = []
    for word in strs:
        for group in groups:
            if Counter(group[0]) == Counter(word):
                group.append(word)
                break
        else:
            groups.append([word])
    return groups


def brute_substring(s):
    return max((j - i for i in range(len(s)) for j in range(i, len(s) + 1) if len(set(s[i:j])) == j - i), default=0)


def components(count, linked):
    parent = list(range(count))

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x
    for a, b in linked:
        parent[find(a)] = find(b)
    groups = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def brute_intervals(intervals):
    linked = [(i, j) for i, j in itertools.combinations(range(len(intervals)), 2)
              if intervals[i][0] <= intervals[j][1] and intervals[j][0] <= intervals[i][1]]
    return sorted([min(intervals[i][0] for i in g), max(intervals[i][1] for i in g)]
                  for g in components(len(intervals), linked))


def brute_product(nums):
    out = []
    for i in range(len(nums)):
        total = 1
        for j, value in enumerate(nums):
            if i != j:
                total *= value
        out.append(total)
    return out


def brute_coins(coins, amount):
    frontier, seen, steps = {0}, {0}, 0
    while frontier:
        if amount in frontier:
            return steps
        frontier = {t + c for t in frontier for c in coins if t + c <= amount} - seen
        seen |= frontier
        steps += 1
    return -1


def brute_islands(grid):
    cells = [(r, c) for r in range(len(grid)) for c in range(len(grid[0])) if grid[r][c] == 1]
    index = {cell: i for i, cell in enumerate(cells)}
    linked = [(index[(r, c)], index[n]) for r, c in cells for n in ((r + 1, c), (r, c + 1)) if n in index]
    return len(components(len(cells), linked))


def brute_three_sum(nums):
    return [list(t) for t in {tuple(sorted(c)) for c in itertools.combinations(nums, 3) if sum(c) == 0}]


class BruteLRU:
    def __init__(self, capacity):
        self.capacity, self.order, self.values = capacity, [], {}

    def touch(self, key):
        if key in self.order:
            self.order.remove(key)
        self.order.append(key)

    def get(self, key):
        if key not in self.values:
            return -1
        self.touch(key)
        return self.values[key]

    def put(self, key, value):
        self.values[key] = value
        self.touch(key)
        if len(self.order) > self.capacity:
            del self.values[self.order.pop(0)]


def brute_courses(num_courses, prerequisites):
    after = {c: [a for a, b in prerequisites if b == c] for c in range(num_courses)}
    state = {}

    def cyclic(course):
        if state.get(course) == "open":
            return True
        if state.get(course) == "done":
            return False
        state[course] = "open"
        found = any(cyclic(n) for n in after[course])
        state[course] = "done"
        return found
    return not any(cyclic(c) for c in range(num_courses))


def brute_water(heights):
    return sum(min(max(heights[:i + 1]), max(heights[i:])) - h for i, h in enumerate(heights))


def brute_edit(a, b):
    @lru_cache(maxsize=None)
    def solve(i, j):
        if i == len(a) or j == len(b):
            return len(a) - i + len(b) - j
        if a[i] == b[j]:
            return solve(i + 1, j + 1)
        return 1 + min(solve(i + 1, j), solve(i, j + 1), solve(i + 1, j + 1))
    return solve(0, 0)


def brute_min_window(s, t):
    for size in range(len(t), len(s) + 1):
        for start in range(len(s) - size + 1):
            if not Counter(t) - Counter(s[start:start + size]):
                return s[start:start + size]
    return ""


def brute_rectangle(heights):
    return max((min(heights[i:j + 1]) * (j - i + 1) for i in range(len(heights)) for j in range(i, len(heights))), default=0)


def brute_ladder(begin, end, word_list):
    words = set(word_list)
    if end not in words:
        return 0
    frontier, seen, length = {begin}, {begin}, 1
    while frontier:
        if end in frontier:
            return length
        frontier = {w for w in words - seen for f in frontier if sum(x != y for x, y in zip(w, f)) == 1}
        seen |= frontier
        length += 1
    return 0


BRUTES = {
    "two_sum": brute_two_sum,
    "valid_parentheses": brute_parentheses,
    "merge_sorted_lists": lambda a, b: sorted(a + b),
    "best_time_stock": lambda p: max([p[j] - p[i] for i in range(len(p)) for j in range(i + 1, len(p))] + [0]),
    "valid_palindrome": brute_palindrome,
    "majority_element": lambda nums: Counter(nums).most_common(1)[0][0],
    "missing_number": lambda nums: (set(range(len(nums) + 1)) - set(nums)).pop(),
    "roman_to_int": brute_roman,
    "longest_common_prefix": lambda strs: os.path.commonprefix(strs),
    "group_anagrams": brute_anagrams,
    "longest_substring_no_repeat": brute_substring,
    "merge_intervals": brute_intervals,
    "product_except_self": brute_product,
    "coin_change": brute_coins,
    "num_islands": brute_islands,
    "top_k_frequent": lambda nums, k: sorted(set(nums), key=lambda v: -nums.count(v))[:k],
    "three_sum": brute_three_sum,
    "lru_cache": BruteLRU,
    "course_schedule": brute_courses,
    "trapping_rain_water": brute_water,
    "median_two_sorted": lambda a, b: float(statistics.median(a + b)),
    "edit_distance": brute_edit,
    "min_window_substring": brute_min_window,
    "largest_rectangle_histogram": brute_rectangle,
    "word_ladder_length": brute_ladder,
    "regex_matching": lambda s, p: re.fullmatch(p, s) is not None,
    "merge_k_sorted": lambda lists: sorted(x for values in lists for x in values),
    "sliding_window_max": lambda nums, k: [max(nums[i:i + k]) for i in range(len(nums) - k + 1)],
}


class Holder:
    """Expose one callable under an attribute name so grade_runner.run_case can call it."""

    def __init__(self, name, value):
        setattr(self, name, value)


def call(target, spec, args):
    return grade_runner.run_case(Holder(spec["entry"], target), spec["entry"], spec["kind"], args)


# Validate every reference and return the finished bank as a list of problems.
def build():
    bank = []
    for spec in defs.PROBLEMS:
        reference = getattr(references, spec["entry"])
        for args, expected in spec["visible"]:
            got = call(reference, spec, args)
            assert grade_runner.same(spec["comparator"], got, expected), (spec["id"], "visible", args, got, expected)
        rng = random.Random(f"{SEED}:{spec['id']}")
        extra = []
        if spec["id"] in GENERATORS:
            for index in range(RANDOM_CHECKS):
                args = GENERATORS[spec["id"]](rng)
                got = call(reference, spec, args)
                want = call(BRUTES[spec["id"]], spec, args)
                assert grade_runner.same(spec["comparator"], got, want), (spec["id"], "brute", args, got, want)
                if index % (RANDOM_CHECKS // RANDOM_HIDDEN) == 0:
                    extra.append(args)
        hidden, seen = [], set()
        for args in spec["hidden"] + extra:
            key = json.dumps(args)
            if key not in seen:
                seen.add(key)
                hidden.append({"args": args, "expected": call(reference, spec, copy.deepcopy(args))})
        item = {k: spec[k] for k in ("id", "title", "difficulty", "signature", "statement", "entry", "kind", "comparator")}
        item["stub"] = LRU_STUB if spec["kind"] == "ops" else (f"# Keep the name `{spec['entry']}` exactly as given; the tests call it.\n"
                                                                   f"{spec['signature']}\n    pass  # TODO: write your solution\n")
        item["visible_tests"] = [{"args": a, "expected": e} for a, e in spec["visible"]]
        item["hidden_tests"] = hidden
        bank.append(item)
    return bank


def main():
    bank = build()
    path = Path(__file__).with_name("bank.json")
    path.write_text(json.dumps(bank, indent=1) + "\n")
    counts = Counter(p["difficulty"] for p in bank)
    print(f"wrote {path} with {len(bank)} problems {dict(counts)}; "
          f"{sum(len(p['hidden_tests']) for p in bank)} hidden tests")


if __name__ == "__main__":
    main()
