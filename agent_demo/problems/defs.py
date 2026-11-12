"""Problem definitions: statements, hand-checked visible tests, and hidden inputs.

Statements are written in our own words. `visible` holds (args, expected) pairs
whose expected values were worked out by hand, so they independently check the
reference solutions. `hidden` holds argument lists only; build_bank.py fills in
their expected values (plus seeded random cases) from the reference solutions.
"""


def problem(id, title, difficulty, signature, statement, visible, hidden,
            entry=None, kind="function", comparator="exact"):
    return {"id": id, "title": title, "difficulty": difficulty, "signature": signature,
            "statement": statement.strip(), "visible": visible, "hidden": hidden,
            "entry": entry or id, "kind": kind, "comparator": comparator}


LRU_A = [["LRUCache", "put", "put", "get", "put", "get", "put", "get", "get", "get"],
         [[2], [1, 1], [2, 2], [1], [3, 3], [2], [4, 4], [1], [3], [4]]]
LRU_B = [["LRUCache", "put", "get", "put", "get", "get"], [[1], [2, 1], [2], [3, 2], [2], [3]]]
LADDER = ["hot", "dot", "dog", "lot", "log", "cog"]

PROBLEMS = [
    # ------------------------------------------------------------------ easy
    problem("two_sum", "Pair With Target Sum", "easy", "def two_sum(nums, target):", """
Given a list of integers `nums` and an integer `target`, return the indices of the
two different positions whose values add up to `target`. Exactly one such pair
exists. Return the two indices as a list, in either order.
""", [([[2, 7, 11, 15], 9], [0, 1]), ([[3, 2, 4], 6], [1, 2]), ([[3, 3], 6], [0, 1])],
        [[[1, 2], 3], [[-3, 4, 3, 90], 0], [[0, 4, 3, 0], 0], [[5, 75, 25], 100], [[-1, -2, -3, -4, -5], -8]],
        comparator="unordered"),
    problem("valid_parentheses", "Balanced Brackets", "easy", "def valid_parentheses(s):", """
The string `s` contains only the characters `()[]{}`. Return True if every opening
bracket is closed by the same kind of bracket in the correct order, otherwise False.
An empty string is balanced.
""", [(["()"], True), (["()[]{}"], True), (["(]"], False), (["([)]"], False)],
        [[""], ["("], ["]"], ["{[]}"], ["(("], ["()))"], ["{[()()]}"], ["([{}])(("], ["}{"]]),
    problem("merge_sorted_lists", "Merge Two Sorted Lists", "easy", "def merge_sorted_lists(a, b):", """
`a` and `b` are lists of integers, each already sorted in non-decreasing order.
Return one new list containing every element of both, sorted in non-decreasing order.
""", [([[1, 2, 4], [1, 3, 4]], [1, 1, 2, 3, 4, 4]), ([[], []], []), ([[], [0]], [0])],
        [[[1], []], [[5], [1, 2, 3]], [[1, 1, 1], [1, 1]], [[-3, -1], [-2, 0]], [[1, 2, 3], [4, 5, 6]]]),
    problem("best_time_stock", "Best Single Trade", "easy", "def best_time_stock(prices):", """
`prices[i]` is the price of a stock on day i. You may buy on one day and sell on a
later day. Return the largest profit you can make, or 0 if no trade makes a profit.
""", [([[7, 1, 5, 3, 6, 4]], 5), ([[7, 6, 4, 3, 1]], 0), ([[1, 2]], 1)],
        [[[]], [[5]], [[2, 2, 2]], [[3, 1, 4, 1, 5, 9, 2, 6]], [[2, 4, 1]], [[1, 100, 1, 100]]]),
    problem("valid_palindrome", "Alphanumeric Palindrome", "easy", "def valid_palindrome(s):", """
Return True if the string `s` reads the same forwards and backwards once you ignore
letter case and drop every character that is not a letter or a digit. Otherwise
return False. A string with no letters or digits counts as a palindrome.
""", [(["A man, a plan, a canal: Panama"], True), (["race a car"], False), ([" "], True)],
        [[""], ["a"], ["ab"], ["0P"], ["No lemon, no melon"], ["12321"], ["1231"], [".,"], ["Was it a car or a cat I saw?"]]),
    problem("climbing_stairs", "Ways To Climb Stairs", "easy", "def climbing_stairs(n):", """
A staircase has `n` steps (n >= 1). Each move climbs either 1 or 2 steps. Return the
number of different sequences of moves that reach exactly the top.
""", [([2], 2), ([3], 3), ([5], 8)],
        [[1], [4], [6], [10], [20], [30], [45]]),
    problem("majority_element", "Majority Value", "easy", "def majority_element(nums):", """
`nums` is a non-empty list of integers in which one value appears more than half
the time. Return that value.
""", [([[3, 2, 3]], 3), ([[2, 2, 1, 1, 1, 2, 2]], 2), ([[1]], 1)],
        [[[5, 5]], [[-1, -1, 2]], [[0, 0, 0, 1, 1]], [[7, 8, 7]]]),
    problem("missing_number", "Missing Number In Range", "easy", "def missing_number(nums):", """
`nums` holds n different integers taken from the range 0..n inclusive, so exactly
one number of that range is absent. Return the absent number.
""", [([[3, 0, 1]], 2), ([[0, 1]], 2), ([[9, 6, 4, 2, 3, 5, 7, 0, 1]], 8)],
        [[[0]], [[1]], [[]], [[1, 2]], [[0, 2]]]),
    problem("roman_to_int", "Roman Numeral To Integer", "easy", "def roman_to_int(s):", """
Convert the Roman numeral string `s` (using I, V, X, L, C, D, M, value 1 to 3999)
to an integer. A smaller symbol written before a larger one is subtracted, as in
IV = 4, IX = 9, XL = 40, XC = 90, CD = 400 and CM = 900.
""", [(["III"], 3), (["LVIII"], 58), (["MCMXCIV"], 1994)],
        [["I"], ["IV"], ["IX"], ["XL"], ["MMMCMXCIX"], ["CDXLIV"], ["DCCC"]]),
    problem("longest_common_prefix", "Longest Shared Prefix", "easy", "def longest_common_prefix(strs):", """
Return the longest string that is a prefix of every string in the list `strs`.
Return an empty string if they share no prefix or the list is empty.
""", [([["flower", "flow", "flight"]], "fl"), ([["dog", "racecar", "car"]], ""), ([["a"]], "a")],
        [[[]], [[""]], [["", "b"]], [["same", "same"]], [["ab", "a"]], [["interview", "internet", "interval", "into"]],
         [["x", "y"]], [["prefix", "prefixes", "prefixed"]]]),

    # ---------------------------------------------------------------- medium
    problem("group_anagrams", "Group Anagrams", "medium", "def group_anagrams(strs):", """
Group the words in the list `strs` so that words made of exactly the same letters
(anagrams of each other) end up together. Return a list of groups, each group a
list of words. Groups and the words inside them may be in any order.
""", [([["eat", "tea", "tan", "ate", "nat", "bat"]], [["bat"], ["nat", "tan"], ["ate", "eat", "tea"]]),
      ([[""]], [[""]]), ([["a"]], [["a"]])],
        [[[]], [["ab", "ba", "ab"]], [["abc", "def", "ghi"]], [["", ""]], [["aab", "aba", "baa", "abb"]]],
        comparator="unordered_nested"),
    problem("longest_substring_no_repeat", "Longest Substring Without Repeats", "medium",
            "def longest_substring_no_repeat(s):", """
Return the length of the longest contiguous substring of `s` in which no character
appears more than once.
""", [(["abcabcbb"], 3), (["bbbbb"], 1), (["pwwkew"], 3), ([""], 0)],
        [["a"], ["au"], ["dvdf"], ["abba"], ["tmmzuxt"], ["abcdef"], [" "]]),
    problem("merge_intervals", "Merge Overlapping Intervals", "medium", "def merge_intervals(intervals):", """
`intervals` is a list of `[start, end]` pairs with start <= end, in any order. Merge
every group of intervals that overlap or touch (for example [1, 4] and [4, 5]) and
return the resulting intervals sorted by start.
""", [([[[1, 3], [2, 6], [8, 10], [15, 18]]], [[1, 6], [8, 10], [15, 18]]),
      ([[[1, 4], [4, 5]]], [[1, 5]]), ([[[4, 7], [1, 4]]], [[1, 7]])],
        [[[]], [[[1, 1]]], [[[1, 10], [2, 3], [4, 5]]], [[[5, 6], [1, 2], [3, 4]]], [[[1, 4], [0, 0]]],
         [[[2, 3], [2, 2], [3, 3], [1, 3], [5, 7], [2, 2], [4, 6]]]]),
    problem("product_except_self", "Product Of All Other Elements", "medium", "def product_except_self(nums):", """
Return a list `out` of the same length as `nums` where `out[i]` is the product of
every element of `nums` except `nums[i]`. `nums` has at least two elements and may
contain zeros and negative numbers.
""", [([[1, 2, 3, 4]], [24, 12, 8, 6]), ([[-1, 1, 0, -3, 3]], [0, 0, 9, 0, 0])],
        [[[0, 0]], [[1, 1]], [[2, 3]], [[0, 4, 5]], [[-2, -3, -4]], [[1, 0, 0, 2]]]),
    problem("coin_change", "Fewest Coins", "medium", "def coin_change(coins, amount):", """
`coins` lists the available coin values (positive integers); you may use each value
any number of times. Return the smallest number of coins that add up to exactly
`amount`, or -1 if that amount cannot be formed. An amount of 0 needs 0 coins.
""", [([[1, 2, 5], 11], 3), ([[2], 3], -1), ([[1], 0], 0)],
        [[[2], 1], [[1], 7], [[2, 5, 10, 1], 27], [[186, 419, 83, 408], 6249], [[3, 7], 5], [[1, 3, 4], 6], [[5], 0]]),
    problem("num_islands", "Count Islands", "medium", "def num_islands(grid):", """
`grid` is a non-empty rectangular list of rows containing the integers 1 (land) and
0 (water). An island is a group of land cells connected horizontally or vertically
(not diagonally). Return the number of islands.
""", [([[[1, 1, 1, 1, 0], [1, 1, 0, 1, 0], [1, 1, 0, 0, 0], [0, 0, 0, 0, 0]]], 1),
      ([[[1, 1, 0, 0, 0], [1, 1, 0, 0, 0], [0, 0, 1, 0, 0], [0, 0, 0, 1, 1]]], 3), ([[[0]]], 0)],
        [[[[1]]], [[[1, 0, 1, 0, 1]]], [[[1], [0], [1]]], [[[1, 0], [0, 1]]], [[[1, 1], [1, 1]]],
         [[[1, 1, 1], [0, 0, 1], [1, 1, 1], [1, 0, 0], [1, 1, 1]]]]),
    problem("top_k_frequent", "Most Frequent Values", "medium", "def top_k_frequent(nums, k):", """
Return the `k` values that occur most often in the list `nums`, in any order. The
answer is always unique: no tie crosses the k-th position.
""", [([[1, 1, 1, 2, 2, 3], 2], [1, 2]), ([[1], 1], [1]), ([[4, 4, 4, 6, 6, 7, 7, 7, 7], 1], [7])],
        [[[5, 5, 6], 1], [[-1, -1, 2, 2, 2, 3], 2], [[1, 2, 3], 3], [[9, 9, 9, 8, 8, 7], 3]],
        comparator="unordered"),
    problem("three_sum", "Triplets Summing To Zero", "medium", "def three_sum(nums):", """
Return every distinct triplet of values from `nums`, taken from three different
positions, that adds up to 0. Each triplet is a list of three numbers. Do not
repeat a triplet that uses the same three values; the triplets and the numbers
inside them may be in any order.
""", [([[-1, 0, 1, 2, -1, -4]], [[-1, -1, 2], [-1, 0, 1]]), ([[0, 1, 1]], []), ([[0, 0, 0]], [[0, 0, 0]])],
        [[[]], [[0]], [[0, 0, 0, 0]], [[-2, 0, 1, 1, 2]], [[1, 2, -2, -1]], [[3, -1, -2, -1, 2, 0, -3]],
         [[-4, -2, -2, -2, 0, 1, 2, 2, 2, 3, 3, 4, 4, 6, 6]]],
        comparator="unordered_nested"),
    problem("lru_cache", "Least Recently Used Cache", "medium", "class LRUCache:", """
Write a class `LRUCache` holding integer keys and values:

- `LRUCache(capacity)` creates an empty cache that keeps at most `capacity` keys
  (capacity >= 1).
- `get(key)` returns the value stored for `key`, or -1 if the key is absent.
- `put(key, value)` stores or updates the value. If this makes the cache exceed its
  capacity, remove the least recently used key.

Both a successful `get` and any `put` count as using that key. Each test is a list of
method names and a list of their arguments; `put` returns None.
""", [(LRU_A, [None, None, None, 1, None, -1, None, -1, 3, 4]), (LRU_B, [None, None, 1, None, -1, 2])],
        [[["LRUCache", "get"], [[1], [5]]],
         [["LRUCache", "put", "put", "get", "get"], [[2], [1, 1], [1, 9], [1], [2]]],
         [["LRUCache", "put", "put", "put", "get", "get", "get"], [[2], [1, 1], [2, 2], [1, 5], [1], [2], [3]]],
         [["LRUCache", "put", "put", "get", "put", "put", "get", "get", "get"],
          [[3], [1, 1], [2, 2], [1], [3, 3], [4, 4], [2], [1], [4]]]],
        entry="LRUCache", kind="ops"),
    problem("course_schedule", "Can All Courses Be Taken", "medium",
            "def course_schedule(num_courses, prerequisites):", """
There are `num_courses` courses numbered 0 to num_courses - 1. Each pair `[a, b]` in
`prerequisites` means course b must be finished before course a. Return True if it
is possible to finish every course, and False if the requirements contain a cycle.
""", [([2, [[1, 0]]], True), ([2, [[1, 0], [0, 1]]], False), ([4, [[1, 0], [2, 1], [3, 2]]], True)],
        [[1, []], [3, []], [3, [[0, 1], [1, 2], [2, 0]]], [4, [[1, 0], [2, 0], [3, 1], [3, 2]]],
         [2, [[0, 0]]], [5, [[1, 0], [2, 1], [1, 2], [4, 3]]]]),

    # ------------------------------------------------------------------ hard
    problem("trapping_rain_water", "Trapped Rain Water", "hard", "def trapping_rain_water(heights):", """
`heights` gives the heights of adjacent bars of width 1. After rain, water collects
in the dips between taller bars. Return the total number of units of water held.
""", [([[0, 1, 0, 2, 1, 0, 1, 3, 2, 1, 2, 1]], 6), ([[4, 2, 0, 3, 2, 5]], 9), ([[]], 0)],
        [[[3]], [[1, 2]], [[2, 0, 2]], [[5, 4, 3, 2, 1]], [[1, 2, 3, 4, 5]], [[3, 0, 0, 0, 3]], [[5, 0, 1, 0, 5, 0, 2]]]),
    problem("median_two_sorted", "Median Of Two Sorted Lists", "hard", "def median_two_sorted(a, b):", """
`a` and `b` are lists of integers, each sorted in non-decreasing order, and at least
one of them is non-empty. Return the median of all the values taken together, as a
float. For an even total count, the median is the average of the two middle values.
""", [([[1, 3], [2]], 2.0), ([[1, 2], [3, 4]], 2.5), ([[], [1]], 1.0)],
        [[[2], []], [[1, 1], [1, 1]], [[1, 2, 3], [4, 5, 6, 7]], [[-5, -3], [-4]], [[1], [2, 3, 4, 5, 6]],
         [[0, 0], [0, 0]], [[1, 4], [2, 3]]],
        comparator="float"),
    problem("edit_distance", "Edit Distance", "hard", "def edit_distance(a, b):", """
Return the smallest number of single-character edits that turn string `a` into
string `b`. An edit inserts one character, deletes one character, or replaces one
character with another.
""", [(["horse", "ros"], 3), (["intention", "execution"], 5), (["", "abc"], 3)],
        [["", ""], ["a", "a"], ["a", "b"], ["abc", ""], ["kitten", "sitting"], ["sunday", "saturday"], ["abcdef", "azced"]]),
    problem("min_window_substring", "Smallest Covering Window", "hard", "def min_window_substring(s, t):", """
Return the shortest contiguous substring of `s` that contains every character of
`t`, counting repeats (if `t` has two 'a's the window needs at least two). If
several windows share the shortest length, return the leftmost one. Return an empty
string if no window qualifies. Matching is case-sensitive.
""", [(["ADOBECODEBANC", "ABC"], "BANC"), (["a", "a"], "a"), (["a", "aa"], "")],
        [["", "a"], ["ab", "b"], ["aa", "aa"], ["abc", "cba"], ["bba", "ab"], ["cabwefgewcwaefgcf", "cae"],
         ["aXbXc", "abc"], ["abab", "ab"]]),
    problem("largest_rectangle_histogram", "Largest Rectangle In Histogram", "hard",
            "def largest_rectangle_histogram(heights):", """
`heights` gives the heights of adjacent histogram bars of width 1. Return the area
of the largest axis-aligned rectangle that fits entirely inside the bars. An empty
list has area 0.
""", [([[2, 1, 5, 6, 2, 3]], 10), ([[2, 4]], 4), ([[1]], 1)],
        [[[]], [[0]], [[3, 3, 3]], [[1, 2, 3, 4, 5]], [[5, 4, 3, 2, 1]], [[2, 1, 2]], [[4, 2, 0, 3, 2, 5]], [[6, 2, 5, 4, 5, 1, 6]]]),
    problem("n_queens_count", "Count N-Queens Placements", "hard", "def n_queens_count(n):", """
Return the number of ways to place `n` chess queens on an n x n board so that no
two queens share a row, a column or a diagonal (1 <= n <= 9).
""", [([1], 1), ([4], 2), ([6], 4)],
        [[2], [3], [5], [7], [8], [9]]),
    problem("word_ladder_length", "Shortest Word Ladder", "hard",
            "def word_ladder_length(begin, end, word_list):", """
A ladder goes from the word `begin` to the word `end` by changing one letter at a
time, and every word after `begin` must appear in `word_list`. All words have the
same length and use lowercase letters. Return the number of words in the shortest
ladder, counting both `begin` and `end`, or 0 if there is no ladder.
""", [(["hit", "cog", LADDER], 5), (["hit", "cog", LADDER[:-1]], 0), (["a", "c", ["a", "b", "c"]], 2)],
        [["hot", "dog", ["hot", "dog"]], ["hot", "dot", ["dot"]], ["hit", "hot", ["hot", "hit"]],
         ["aaa", "bbb", ["aab", "abb", "bbb", "baa", "bba"]], ["lost", "cost", ["most", "fist", "lost", "cost", "fish"]],
         ["leet", "code", ["lest", "leet", "lose", "code", "lode", "robe", "lost"]],
         ["qa", "sq", ["si", "go", "se", "cm", "so", "ph", "mt", "db", "mb", "sb", "kr", "ln", "tm", "le", "av", "sm",
                       "ar", "ci", "ca", "br", "ti", "ba", "to", "ra", "fa", "yo", "ow", "sn", "ya", "cr", "po", "fe",
                       "ho", "ma", "re", "or", "rn", "au", "ur", "rh", "sr", "tc", "lt", "lo", "as", "fr", "nb", "yb",
                       "if", "pb", "ge", "th", "pm", "rb", "sh", "co", "ga", "li", "ha", "hz", "no", "bi", "di", "hi",
                       "qa", "pi", "os", "uh", "wm", "an", "me", "mo", "na", "la", "st", "er", "sc", "ne", "mn", "mi",
                       "am", "ex", "pt", "io", "be", "fm", "ta", "tb", "ni", "mr", "pa", "he", "lr", "sq", "ye"]]]),
    problem("regex_matching", "Regular Expression Match", "hard", "def regex_matching(s, p):", """
Return True if the pattern `p` matches the whole string `s`, otherwise False. In the
pattern, `.` matches any single character and `x*` matches zero or more copies of
the element `x` just before it (a letter or `.`). `s` holds lowercase letters; `p` is
a valid pattern of lowercase letters, `.` and `*`. Do not use the `re` module.
""", [(["aa", "a"], False), (["aa", "a*"], True), (["ab", ".*"], True), (["aab", "c*a*b"], True)],
        [["", ""], ["", "a*"], ["", "."], ["mississippi", "mis*is*p*."], ["mississippi", "mis*is*ip*."], ["ab", ".*c"],
         ["aaa", "a*a"], ["aaa", "ab*a*c*a"], ["a", "ab*"], ["bbbba", ".*a*a"]]),
    problem("merge_k_sorted", "Merge K Sorted Lists", "hard", "def merge_k_sorted(lists):", """
`lists` is a list of integer lists, each sorted in non-decreasing order. Return one
list with all their elements in non-decreasing order. `lists` may be empty and may
contain empty lists.
""", [([[[1, 4, 5], [1, 3, 4], [2, 6]]], [1, 1, 2, 3, 4, 4, 5, 6]), ([[]], []), ([[[]]], [])],
        [[[[1]]], [[[], [1], []]], [[[1, 2, 3]]], [[[5], [4], [3], [2], [1]]], [[[-2, -1], [-3], [0, 0]]], [[[1, 1], [1, 1], [1]]]]),
    problem("sliding_window_max", "Sliding Window Maximum", "hard", "def sliding_window_max(nums, k):", """
A window of size `k` slides across the list `nums` one position at a time
(1 <= k <= len(nums)). Return a list with the largest value of each window, from
the leftmost window to the rightmost.
""", [([[1, 3, -1, -3, 5, 3, 6, 7], 3], [3, 3, 5, 5, 6, 7]), ([[1], 1], [1]), ([[9, 8, 7], 2], [9, 8])],
        [[[1, -1], 1], [[7, 2, 4], 2], [[1, 2, 3, 4, 5], 5], [[5, 4, 3, 2, 1], 3], [[1, 3, 1, 2, 0, 5], 3], [[4, 4, 4, 4], 2]]),
]
