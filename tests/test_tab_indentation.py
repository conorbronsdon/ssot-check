"""Tab indentation is rejected with its line number, as documented.

_Parser._cur measured indentation in spaces only and then looked for a tab
inside those spaces, so the check could never fire: a tab-indented line
silently parsed at the wrong level ('a:\\n\\tb: 1' gave two top-level keys).
Run: python3 -m unittest tests.test_tab_indentation -v
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402

SEED = 20261117


class TabIndentationTests(unittest.TestCase):
    def assert_tab_error(self, text, line):
        with self.assertRaises(sc.ManifestError) as ctx:
            sc.parse_manifest(text)
        self.assertEqual(ctx.exception.lineno, line)
        self.assertIn("tab in indentation", ctx.exception.message)

    def test_tab_indented_key_is_an_error(self):
        # Before the fix: {'a': None, 'b': '1'}.
        self.assert_tab_error("a:\n\tb: 1\n", 2)

    def test_tab_after_spaces_is_an_error(self):
        # Before the fix: {'a': {'b': '1'}}.
        self.assert_tab_error("a:\n  \tb: 1\n", 2)

    def test_tab_indented_sequence_reports_tab(self):
        # Before the fix: "sequence item where a mapping key was expected".
        self.assert_tab_error("facts:\n\t- name: x\n", 2)

    def test_tabs_that_are_not_indentation_still_parse(self):
        m = sc.parse_manifest("a: x\ty\t# c\nb:\n  c: 1\n\t\n"
                              "d: |\n  p\tq\n")
        self.assertEqual(m, {"a": "x\ty", "b": {"c": "1"}, "d": "p\tq"})

    def test_tab_line_anywhere_is_reported_at_its_line(self):
        rng = random.Random(SEED)
        for i in range(200):
            n = rng.randint(1, 6)
            lines = [f"k{j}: v{j}" for j in range(n)]
            at = rng.randint(0, n)
            lead = rng.choice(["\t", " \t", "\t ", "  \t"])
            lines.insert(at, lead + "x: 1")
            with self.subTest(seed=SEED, example=i):
                self.assert_tab_error("\n".join(lines) + "\n", at + 1)


if __name__ == "__main__":
    unittest.main()
