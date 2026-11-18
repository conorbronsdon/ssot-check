"""Trailing comments next to quote characters that do not start a quoted
scalar.

_strip_comment treated every ' or " as a quote boundary. An apostrophe in a
plain value ("it's fine # note") then hid the comment, which became part of
the value, and an escaped \\" inside a double-quoted value closed the quote
early, so a '#' after it cut the value short.
Run: python3 -m unittest tests.test_comment_quotes -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402


def value(text):
    return sc.parse_manifest(text)["k"]


class CommentQuoteTests(unittest.TestCase):
    def test_apostrophe_in_plain_value_does_not_hide_comment(self):
        # Before the fix: "it's fine # comment".
        self.assertEqual(value("k: it's fine # comment\n"), "it's fine")
        self.assertEqual(
            value("k: Conor's count: (\\d+)  # canonical\n"),
            "Conor's count: (\\d+)")
        self.assertEqual(sc.parse_manifest("s:\n  - don't # c\n")["s"],
                         ["don't"])

    def test_quotes_inside_plain_value_do_not_hide_comment(self):
        # Before the fix: 'say "hi # there" # c'.
        self.assertEqual(value('k: say "hi # there" # c\n'), 'say "hi')

    def test_escaped_double_quote_does_not_end_the_value(self):
        # Before the fix: 'x \\' (the quote closed at \" and '# y"' was cut).
        self.assertEqual(value('k: "x \\" # y"\n'), 'x " # y')
        self.assertEqual(value('k: "x \\" # y" # c\n'), 'x " # y')

    def test_quoted_values_unchanged(self):
        for text, want in (("k: 'a # b'  # c\n", "a # b"),
                           ("k: 'it''s # x'\n", "it's # x"),
                           ("k: ''\n", ""),
                           ('k: "a # b" # c\n', "a # b"),
                           ('k: "C:\\\\dir\\\\" # c\n', "C:\\dir\\"),
                           ("k: plain#not-a-comment # c\n",
                            "plain#not-a-comment"),
                           ("k: it's\n", "it's")):
            with self.subTest(text=text):
                self.assertEqual(value(text), want)
        m = sc.parse_manifest("s:\n  - 'a # b' # c\n  - name: \"x # y\"\n")
        self.assertEqual(m["s"], ["a # b", {"name": "x # y"}])

    def test_strip_comment_keeps_indentation(self):
        self.assertEqual(sc._strip_comment("    - 'x # y'  # z"),
                         "    - 'x # y'")
        self.assertEqual(sc._strip_comment("  # only"), "")


if __name__ == "__main__":
    unittest.main()
