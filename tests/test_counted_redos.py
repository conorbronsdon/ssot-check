"""Counted-value regexes stay linear on long digit/comma and whitespace runs (#25).

Run: python3 -m unittest discover tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402


def _after(text):
    return [(m.group(1), m.group(2)) for m in sc._COUNTED_AFTER.finditer(text)]


def _before(text):
    return [(m.group(1), m.group(2)) for m in sc._COUNTED_BEFORE.finditer(text)]


def _discover_text(name, content):
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
            fh.write(content)
        start = time.perf_counter()
        result = sc.discover(tmp)
        return result, time.perf_counter() - start


class CountedAfterMatchesTests(unittest.TestCase):
    def test_must_fire_ordinary_prose(self):
        self.assertEqual(_after("Trusted by 1,200 users."), [("1,200", "users")])
        self.assertEqual(_after("Over 3,400+ customers"), [("3,400", "customers")])
        self.assertEqual(_after("we have 1,2,3 users"), [("1,2,3", "users")])
        self.assertEqual(_after("10,000users signed up"), [("10,000", "users")])
        self.assertEqual(_after("(12,500 users)"), [("12,500", "users")])

    def test_must_not_fire_mid_token(self):
        # The old regex matched the trailing "000 users" inside these tokens.
        self.assertEqual(_after("a1,000 users"), [])
        self.assertEqual(_after("v2,000 users"), [])


class CountedBeforeMatchesTests(unittest.TestCase):
    def test_must_fire_with_and_without_separator(self):
        self.assertEqual(_before("users: 1,200"), [("users", "1,200")])
        self.assertEqual(_before("users = 1,200"), [("users", "1,200")])
        self.assertEqual(_before("users 1,200+"), [("users", "1,200")])
        self.assertEqual(_before("users:1200"), [("users", "1200")])
        self.assertEqual(_before("users  :  1200"), [("users", "1200")])
        self.assertEqual(_before("users  \t  \n  1200"), [("users", "1200")])

    def test_must_not_fire_without_a_number(self):
        self.assertEqual(_before("users: none"), [])
        self.assertEqual(_before("users :: 1200"), [])


class CountedTimingTests(unittest.TestCase):
    # Before the fix the comma run took ~36 s and the whitespace run (quadratic,
    # ~1.7 s at 20,000 spaces) ~15 s. The fixed passes take milliseconds, so a
    # generous budget still separates linear from quadratic on a loaded runner.
    BUDGET_S = 5.0

    def test_long_comma_run_is_linear(self):
        points = ",".join(["1"] * 10_000)  # ~20 KB
        result, elapsed = _discover_text("data.html", f'<div data-points="{points}"></div>\n')
        self.assertLess(elapsed, self.BUDGET_S, f"discover took {elapsed:.2f}s")
        self.assertEqual(result["proposals"], [])

    def test_long_whitespace_run_after_unit_is_linear(self):
        result, elapsed = _discover_text("notes.md", "users" + " " * 60_000 + "\n")
        self.assertLess(elapsed, self.BUDGET_S, f"discover took {elapsed:.2f}s")
        self.assertEqual(result["proposals"], [])

    def test_long_comma_run_still_finds_a_real_count(self):
        # Must-still-fire next to the pathological run.
        points = ",".join(["1"] * 10_000)
        start = time.perf_counter()
        found = _after(f'<div data-points="{points}"></div> Trusted by 1,200 users')
        self.assertLess(time.perf_counter() - start, self.BUDGET_S)
        self.assertIn(("1,200", "users"), found)

    def test_long_comma_run_followed_by_a_unit_is_linear(self):
        points = ",".join(["1"] * 10_000)
        for tail, expected in ((" users", 1), ("+ users", 1), (" xusers", 0)):
            start = time.perf_counter()
            found = _after(f'data-points="{points}{tail}')
            self.assertLess(time.perf_counter() - start, self.BUDGET_S, tail)
            self.assertEqual(len(found), expected, tail)


if __name__ == "__main__":
    unittest.main()
