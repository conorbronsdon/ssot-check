"""Tests for json locators and floor copies.
Run: python3 -m unittest discover tests
"""

import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def manifest(text):
    m = sc.parse_manifest(textwrap.dedent(text))
    errors = sc.validate_manifest(m)
    if errors:
        raise AssertionError(errors)
    return m


DATA = {"episodes_published": 82, "combined_reach": 170020,
        "nested": {"list": [{"price": 4.5}, {"price": 400.0}]},
        "flag": True, "episodes": [{"n": 1}, {"n": 2}, {"n": 3}]}


class JsonExtractTests(unittest.TestCase):
    def value(self, path, data=DATA):
        return sc.extract_json(json.dumps(data, indent=2), path)

    def test_scalars(self):
        self.assertEqual(self.value("episodes_published")[0], "82")
        self.assertEqual(self.value("nested.list.0.price")[0], "4.5")
        self.assertEqual(self.value("nested.list.1.price")[0], "400")
        self.assertEqual(self.value("flag")[0], "true")

    def test_length(self):
        self.assertEqual(self.value("episodes.#")[0], "3")
        self.assertEqual(self.value("nested.#")[0], "1")

    def test_line_is_where_the_last_key_appears(self):
        value, line, err = self.value("combined_reach")
        text = json.dumps(DATA, indent=2).splitlines()
        self.assertIn('"combined_reach"', text[line - 1])

    def test_errors(self):
        for path, needle in (("missing", "not found"),
                             ("nested.list.9", "not found"),
                             ("nested", "not a value"),
                             ("flag.#", "no length")):
            with self.subTest(path=path):
                value, _, err = self.value(path)
                self.assertIsNone(value)
                self.assertIn(needle, err)
        self.assertIn("not valid JSON",
                      sc.extract_json("{oops", "a")[2])

    def test_path_syntax(self):
        for bad in ("", "a..b", "#.a", "a#"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                sc.parse_json_path(bad)


JSON_FACT = """\
    facts:
      - name: episode-count
        type: integer
        canonical:
          file: data.json
          json: episodes_published
        copies:
          - file: feed.json
            json: episodes.#
          - file: README.md
            pattern: 'Total Episodes:\\*\\* (\\d+)'
    """


class JsonCheckTests(unittest.TestCase):
    def layout(self, tmp, feed_len=82, readme=82):
        write(os.path.join(tmp, "data.json"), json.dumps(DATA))
        write(os.path.join(tmp, "feed.json"),
              json.dumps({"episodes": [{}] * feed_len}))
        write(os.path.join(tmp, "README.md"),
              f"**Total Episodes:** {readme}\n")

    def test_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.layout(tmp)
            r = sc.check(tmp, manifest(JSON_FACT), env={})
            self.assertEqual(r["exit_code"], 0, r)

    def test_json_copy_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.layout(tmp, feed_len=81)
            r = sc.check(tmp, manifest(JSON_FACT), env={})
            bad = [c for c in r["facts"][0]["copies"]
                   if c["status"] != "in_sync"]
            self.assertEqual([(c["file"], c["status"], c["value"])
                              for c in bad], [("feed.json", "drifted", "81")])

    def test_missing_key_is_canonical_moved_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.layout(tmp)
            write(os.path.join(tmp, "data.json"), json.dumps({"other": 1}))
            r = sc.check(tmp, manifest(JSON_FACT), env={})
            f = r["facts"][0]
            self.assertEqual(f["status"], "canonical_moved")
            self.assertIn("json path not found", f["canonical"]["error"])

    def test_validation(self):
        def errs(text):
            return sc.validate_manifest(sc.parse_manifest(
                textwrap.dedent(text)))
        both = JSON_FACT.replace("json: episodes_published",
                                 "json: episodes_published\n"
                                 "          pattern: '(x)'")
        self.assertTrue(any("not both" in e for e in errs(both)))
        badpath = JSON_FACT.replace("json: episodes.#", "json: '#.x'")
        self.assertTrue(any("last segment" in e for e in errs(badpath)))
        neither = JSON_FACT.replace("          json: episodes_published\n",
                                    "")
        self.assertTrue(any("'json'" in e for e in errs(neither)))


FLOOR_FACT = """\
    facts:
      - name: subscribers
        type: integer
        canonical:
          file: goals.md
          pattern: 'Subscribers: ([\\d,]+)'
        copies:
          - file: ad.md
            pattern: '([\\d,.]+[KkMm]?)\\+ readers'
            relation: floor
            max_gap_pct: 10
    """


class FloorTests(unittest.TestCase):
    def run_floor(self, canonical, copy, text=FLOOR_FACT):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "goals.md"), f"Subscribers: {canonical}\n")
            write(os.path.join(tmp, "ad.md"), f"Join {copy}+ readers\n")
            r = sc.check(tmp, manifest(text), env={})
            return r, r["facts"][0]["copies"][0]

    def test_within_gap_is_in_sync(self):
        for copy in ("1,200", "1.2K", "1,268", "1,150"):
            with self.subTest(copy=copy):
                r, cp = self.run_floor("1,268", copy)
                self.assertEqual(cp["status"], "in_sync", cp)
                self.assertEqual(r["exit_code"], 0)

    def test_lagging_floor_fires(self):
        r, cp = self.run_floor("1,268", "1,100")   # 13.2% below
        self.assertEqual((cp["status"], cp["direction"]),
                         ("drifted", "floor_lagging"))
        self.assertIn("13.2%", cp["note"])
        self.assertEqual(r["exit_code"], 1)

    def test_overstated_floor_fires(self):
        r, cp = self.run_floor("1,268", "1.3K")
        self.assertEqual((cp["status"], cp["direction"]),
                         ("drifted", "overstated"))

    def test_no_gap_only_checks_overstatement(self):
        text = FLOOR_FACT.replace("            max_gap_pct: 10\n", "")
        _, cp = self.run_floor("1,268", "500", text)
        self.assertEqual(cp["status"], "in_sync")
        _, cp = self.run_floor("1,268", "1,269", text)
        self.assertEqual(cp["direction"], "overstated")

    def test_equal_relation_is_still_exact(self):
        # Complement: without relation floor, '1,200' vs '1,268' is drift.
        text = FLOOR_FACT.replace("            relation: floor\n"
                                  "            max_gap_pct: 10\n", "")
        _, cp = self.run_floor("1,268", "1,200", text)
        self.assertEqual(cp["status"], "drifted")
        self.assertNotIn("direction", cp)

    def test_render_tags(self):
        r, _ = self.run_floor("1,268", "1,100")
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            sc.render_check(r)
        self.assertIn("(floor lagging)", buf.getvalue())
        self.assertIn("13.2% below", buf.getvalue())

    def test_floor_number_parsing(self):
        self.assertEqual(sc._floor_number("13.4K+"), 13400)
        self.assertEqual(sc._floor_number("$5k"), 5000)
        self.assertEqual(sc._floor_number("2M"), 2_000_000)
        with self.assertRaises(ValueError):
            sc._floor_number("lots")

    def test_validation(self):
        def errs(text):
            return sc.validate_manifest(sc.parse_manifest(
                textwrap.dedent(text)))
        self.assertTrue(any("integer or currency" in e for e in errs(
            FLOOR_FACT.replace("type: integer", "type: string"))))
        self.assertTrue(any("cannot be combined" in e for e in errs(
            FLOOR_FACT.replace("max_gap_pct: 10",
                               "max_gap_pct: 10\n"
                               "            rounding: floor-100"))))
        self.assertTrue(any("needs relation: floor" in e for e in errs(
            FLOOR_FACT.replace("            relation: floor\n", ""))))
        self.assertTrue(any("between 0 and 100" in e for e in errs(
            FLOOR_FACT.replace("max_gap_pct: 10", "max_gap_pct: 150"))))
        self.assertTrue(any("relation" in e for e in errs(
            FLOOR_FACT.replace("relation: floor", "relation: ceiling"))))



class FlowYamlTests(unittest.TestCase):
    def parse(self, text):
        return sc.parse_manifest(textwrap.dedent(text))

    def test_flow_collections_are_rejected_with_line(self):
        for text, needle, line in (
                ("ignore_paths: [a, b]\nfacts:\n", "list [...]", 1),
                ("facts:\n  - name: x\n    canonical: {file: a}\n",
                 "mapping {...}", 3),
                ("facts:\n  - {name: x}\n", "mapping {...}", 2)):
            with self.subTest(text=text):
                with self.assertRaises(sc.ManifestError) as ctx:
                    self.parse(text)
                self.assertIn(needle, str(ctx.exception))
                self.assertEqual(ctx.exception.lineno, line)

    def test_other_unsupported_forms_are_rejected(self):
        for text, needle in (("a: &x 1\n", "anchors"),
                             ("a: *x\n", "aliases"),
                             ("a: !!str 1\n", "tags"),
                             ("a: - 1\n", "own line"),
                             ("a:\n  - *x\n", "aliases")):
            with self.subTest(text=text):
                with self.assertRaises(sc.ManifestError) as ctx:
                    self.parse(text)
                self.assertIn(needle, str(ctx.exception))
        # Complement: the same characters quoted, or later in a value, parse.
        m = self.parse("a: '*x'\nb: 'x & y'\nc: x - y\nd: '- 1'\n")
        self.assertEqual((m["a"], m["b"], m["c"], m["d"]),
                         ("*x", "x & y", "x - y", "- 1"))

    def test_empty_flow_and_regex_lookalikes_still_parse(self):
        m = self.parse("""\
            a: []
            b: {}
            c: '[abc]'
            d: [\\d,]+
            e: "{x}"
            """)
        self.assertEqual(m["a"], [])
        self.assertEqual(dict(m["b"]), {})
        self.assertEqual(m["c"], "[abc]")
        self.assertEqual(m["d"], "[\\d,]+")
        self.assertEqual(m["e"], "{x}")

    def test_validate_cli_reports_flow_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".ssot.yaml")
            write(path, "ignore_paths: [a]\nfacts:\n")
            err = io.StringIO()
            with unittest.mock.patch("sys.stderr", err):
                self.assertEqual(sc.main(["validate", "-f", path]), 2)
            self.assertIn("line 1", err.getvalue())
            self.assertIn("one '- item' per line", err.getvalue())


if __name__ == "__main__":
    unittest.main()
