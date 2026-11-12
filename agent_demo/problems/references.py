"""Reference solutions, one per problem, used only to build and validate the bank.

Each definition is self-contained (imports live inside the body) because the
build script extracts its source with inspect.getsource and grades it as a
standalone solution.py through the real grader.
"""


def two_sum(nums, target):
    seen = {}
    for i, value in enumerate(nums):
        if target - value in seen:
            return [seen[target - value], i]
        seen[value] = i
    return []


def valid_parentheses(s):
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in s:
        if ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
        else:
            stack.append(ch)
    return not stack


def merge_sorted_lists(a, b):
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    return out + a[i:] + b[j:]


def best_time_stock(prices):
    best, low = 0, float("inf")
    for price in prices:
        low = min(low, price)
        best = max(best, price - low)
    return best


def valid_palindrome(s):
    kept = [ch.lower() for ch in s if ch.isalnum()]
    return kept == kept[::-1]


def climbing_stairs(n):
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def majority_element(nums):
    candidate, count = None, 0
    for value in nums:
        if count == 0:
            candidate = value
        count += 1 if value == candidate else -1
    return candidate


def missing_number(nums):
    n = len(nums)
    return n * (n + 1) // 2 - sum(nums)


def roman_to_int(s):
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for i, ch in enumerate(s):
        if i + 1 < len(s) and values[ch] < values[s[i + 1]]:
            total -= values[ch]
        else:
            total += values[ch]
    return total


def longest_common_prefix(strs):
    if not strs:
        return ""
    prefix = strs[0]
    for word in strs[1:]:
        while not word.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def group_anagrams(strs):
    groups = {}
    for word in strs:
        groups.setdefault("".join(sorted(word)), []).append(word)
    return list(groups.values())


def longest_substring_no_repeat(s):
    last, start, best = {}, 0, 0
    for i, ch in enumerate(s):
        if ch in last and last[ch] >= start:
            start = last[ch] + 1
        last[ch] = i
        best = max(best, i - start + 1)
    return best


def merge_intervals(intervals):
    out = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def product_except_self(nums):
    out = [1] * len(nums)
    left = 1
    for i, value in enumerate(nums):
        out[i] = left
        left *= value
    right = 1
    for i in range(len(nums) - 1, -1, -1):
        out[i] *= right
        right *= nums[i]
    return out


def coin_change(coins, amount):
    best = [0] + [float("inf")] * amount
    for total in range(1, amount + 1):
        for coin in coins:
            if coin <= total and best[total - coin] + 1 < best[total]:
                best[total] = best[total - coin] + 1
    return best[amount] if best[amount] != float("inf") else -1


def num_islands(grid):
    grid = [row[:] for row in grid]
    count = 0
    for r in range(len(grid)):
        for c in range(len(grid[0])):
            if grid[r][c] != 1:
                continue
            count += 1
            stack = [(r, c)]
            grid[r][c] = 0
            while stack:
                y, x = stack.pop()
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < len(grid) and 0 <= nx < len(grid[0]) and grid[ny][nx] == 1:
                        grid[ny][nx] = 0
                        stack.append((ny, nx))
    return count


def top_k_frequent(nums, k):
    from collections import Counter
    return [value for value, _ in Counter(nums).most_common(k)]


def three_sum(nums):
    nums = sorted(nums)
    out = []
    for i in range(len(nums) - 2):
        if i and nums[i] == nums[i - 1]:
            continue
        lo, hi = i + 1, len(nums) - 1
        while lo < hi:
            total = nums[i] + nums[lo] + nums[hi]
            if total < 0:
                lo += 1
            elif total > 0:
                hi -= 1
            else:
                out.append([nums[i], nums[lo], nums[hi]])
                lo += 1
                hi -= 1
                while lo < hi and nums[lo] == nums[lo - 1]:
                    lo += 1
    return out


class LRUCache:
    def __init__(self, capacity):
        from collections import OrderedDict
        self.capacity = capacity
        self.items = OrderedDict()

    def get(self, key):
        if key not in self.items:
            return -1
        self.items.move_to_end(key)
        return self.items[key]

    def put(self, key, value):
        self.items[key] = value
        self.items.move_to_end(key)
        if len(self.items) > self.capacity:
            self.items.popitem(last=False)


def course_schedule(num_courses, prerequisites):
    needs = [0] * num_courses
    unlocks = [[] for _ in range(num_courses)]
    for course, before in prerequisites:
        needs[course] += 1
        unlocks[before].append(course)
    ready = [c for c in range(num_courses) if needs[c] == 0]
    taken = 0
    while ready:
        course = ready.pop()
        taken += 1
        for nxt in unlocks[course]:
            needs[nxt] -= 1
            if needs[nxt] == 0:
                ready.append(nxt)
    return taken == num_courses


def trapping_rain_water(heights):
    lo, hi = 0, len(heights) - 1
    left = right = total = 0
    while lo < hi:
        if heights[lo] < heights[hi]:
            left = max(left, heights[lo])
            total += left - heights[lo]
            lo += 1
        else:
            right = max(right, heights[hi])
            total += right - heights[hi]
            hi -= 1
    return total


def median_two_sorted(a, b):
    if len(a) > len(b):
        a, b = b, a
    total = len(a) + len(b)
    half = (total + 1) // 2
    lo, hi = 0, len(a)
    while True:
        i = (lo + hi) // 2
        j = half - i
        a_left = a[i - 1] if i > 0 else float("-inf")
        a_right = a[i] if i < len(a) else float("inf")
        b_left = b[j - 1] if j > 0 else float("-inf")
        b_right = b[j] if j < len(b) else float("inf")
        if a_left > b_right:
            hi = i - 1
        elif b_left > a_right:
            lo = i + 1
        else:
            if total % 2:
                return float(max(a_left, b_left))
            return (max(a_left, b_left) + min(a_right, b_right)) / 2


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        prev, row[0] = row[0], i
        for j in range(1, len(b) + 1):
            cur = row[j]
            row[j] = prev if a[i - 1] == b[j - 1] else 1 + min(prev, row[j], row[j - 1])
            prev = cur
    return row[len(b)]


def min_window_substring(s, t):
    from collections import Counter
    if not t or len(t) > len(s):
        return ""
    need = Counter(t)
    missing = len(t)
    best = (float("inf"), 0, 0)
    start = 0
    for end, ch in enumerate(s):
        if need[ch] > 0:
            missing -= 1
        need[ch] -= 1
        while missing == 0:
            if end - start + 1 < best[0]:
                best = (end - start + 1, start, end + 1)
            need[s[start]] += 1
            if need[s[start]] > 0:
                missing += 1
            start += 1
    return s[best[1]:best[2]] if best[0] != float("inf") else ""


def largest_rectangle_histogram(heights):
    stack, best = [], 0
    for i, height in enumerate(heights + [0]):
        start = i
        while stack and stack[-1][1] >= height:
            start, top = stack.pop()
            best = max(best, top * (i - start))
        stack.append((start, height))
    return best


def n_queens_count(n):
    def place(row, cols, diag_a, diag_b):
        if row == n:
            return 1
        total = 0
        for col in range(n):
            if col in cols or row - col in diag_a or row + col in diag_b:
                continue
            total += place(row + 1, cols | {col}, diag_a | {row - col}, diag_b | {row + col})
        return total
    return place(0, frozenset(), frozenset(), frozenset())


def word_ladder_length(begin, end, word_list):
    from collections import deque
    words = set(word_list)
    if end not in words:
        return 0
    queue = deque([(begin, 1)])
    seen = {begin}
    while queue:
        word, steps = queue.popleft()
        if word == end:
            return steps
        for i in range(len(word)):
            for ch in "abcdefghijklmnopqrstuvwxyz":
                nxt = word[:i] + ch + word[i + 1:]
                if nxt in words and nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, steps + 1))
    return 0


def regex_matching(s, p):
    match = [[False] * (len(p) + 1) for _ in range(len(s) + 1)]
    match[0][0] = True
    for j in range(2, len(p) + 1):
        if p[j - 1] == "*":
            match[0][j] = match[0][j - 2]
    for i in range(1, len(s) + 1):
        for j in range(1, len(p) + 1):
            if p[j - 1] == "*":
                match[i][j] = match[i][j - 2] or (p[j - 2] in (s[i - 1], ".") and match[i - 1][j])
            else:
                match[i][j] = p[j - 1] in (s[i - 1], ".") and match[i - 1][j - 1]
    return match[len(s)][len(p)]


def merge_k_sorted(lists):
    import heapq
    heap = [(values[0], index, 0) for index, values in enumerate(lists) if values]
    heapq.heapify(heap)
    out = []
    while heap:
        value, index, pos = heapq.heappop(heap)
        out.append(value)
        if pos + 1 < len(lists[index]):
            heapq.heappush(heap, (lists[index][pos + 1], index, pos + 1))
    return out


def sliding_window_max(nums, k):
    from collections import deque
    window, out = deque(), []
    for i, value in enumerate(nums):
        while window and nums[window[-1]] <= value:
            window.pop()
        window.append(i)
        if window[0] <= i - k:
            window.popleft()
        if i >= k - 1:
            out.append(nums[window[0]])
    return out
