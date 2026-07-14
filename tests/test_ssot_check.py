"""Unit tests for ssot_check. Run: python3 -m unittest discover tests"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(os.path.dirname(HERE), "ssot_check.py")
FIXTURES = os.path.join(HERE, "fixtures")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# --------------------------------------------------------------------------- #
class ParserTests(unittest.TestCase):
    def test_nested_maps_and_sequences(self):
        text = textwrap.dedent("""\
            ignore_paths:
              - "*.lock"
              - vendor/**
            facts:
              - name: price
                type: currency
                canonical:
                  file: pricing.md
                  pattern: 'Price: \\$([\\d,]+)'
                copies:
                  - file: home.md
                    pattern: '\\$([\\d,]+) per month'
            """)
        m = sc.parse_manifest(text)
        self.assertEqual(m["ignore_paths"], ["*.lock", "vendor/**"])
        self.assertEqual(len(m["facts"]), 1)
        fact = m["facts"][0]
        self.assertEqual(fact["name"], "price")
        self.assertEqual(fact["type"], "currency")
        self.assertEqual(fact["canonical"]["file"], "pricing.md")
        self.assertEqual(fact["canonical"]["pattern"], "Price: \\$([\\d,]+)")
        self.assertEqual(fact["copies"][0]["file"], "home.md")

    def test_quote_aware_comment_stripping(self):
        # A '#' inside a single-quoted regex must survive; a trailing comment
        # after whitespace must be removed.
        text = textwrap.dedent("""\
            facts:
              - name: anchor
                canonical:
                  file: a.md
                  pattern: 'id="sec#3">([\\d]+)'   # trailing comment removed
                copies:
                  - file: b.md
                    pattern: '([\\d]+) items'
            """)
        m = sc.parse_manifest(text)
        self.assertEqual(m["facts"][0]["canonical"]["pattern"],
                         'id="sec#3">([\\d]+)')

    def test_folded_block_scalar(self):
        text = textwrap.dedent("""\
            facts:
              - name: x
                note: >
                  This note spans
                  several lines and
                  folds to one.
                canonical:
                  file: a.md
                  pattern: '([\\d]+)'
                copies:
                  - file: b.md
                    pattern: '([\\d]+)'
            """)
        m = sc.parse_manifest(text)
        self.assertEqual(m["facts"][0]["note"],
                         "This note spans several lines and folds to one.")

    def test_line_numbers_tracked(self):
        text = "facts:\n  - name: x\n    canonical:\n      file: a.md\n"
        m = sc.parse_manifest(text)
        self.assertEqual(m.lines["facts"], 1)
        self.assertEqual(m["facts"][0].lines["name"], 2)

    def test_malformed_tab_indentation(self):
        with self.assertRaises(sc.ManifestError) as cm:
            sc.parse_manifest("facts:\n\t- name: x\n")
        self.assertIsNotNone(cm.exception.lineno)

    def test_malformed_missing_colon(self):
        # A top-level mapping line with no colon is not 'key: value'.
        with self.assertRaises(sc.ManifestError) as cm:
            sc.parse_manifest("notacolon here\nfacts:\n  - name: x\n")
        self.assertIn("key: value", str(cm.exception))

    def test_empty_manifest_raises(self):
        with self.assertRaises(sc.ManifestError):
            sc.parse_manifest("# just a comment\n")


# --------------------------------------------------------------------------- #
class NormalizationTests(unittest.TestCase):
    def test_integer_thousands_separator(self):
        ok, _ = sc.values_match("1,234", "1234", "integer")
        self.assertTrue(ok)

    def test_integer_trailing_plus(self):
        ok, _ = sc.values_match("62", "62+", "integer")
        self.assertTrue(ok)

    def test_integer_mismatch(self):
        ok, _ = sc.values_match("62", "61", "integer")
        self.assertFalse(ok)

    def test_string_is_literal(self):
        # Under string type commas are NOT stripped.
        ok, _ = sc.values_match("1,234", "1234", "string")
        self.assertFalse(ok)
        ok2, _ = sc.values_match("hello", "hello", "string")
        self.assertTrue(ok2)

    def test_currency_symbol_and_separator(self):
        ok, _ = sc.values_match("$1,500", "1500", "currency")
        self.assertTrue(ok)

    def test_currency_stays_string_for_k_suffix(self):
        # "$5k" is not a plain currency amount -> unparseable, reported.
        ok, detail = sc.values_match("$5k", "5000", "currency")
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_semver_v_prefix(self):
        ok, _ = sc.values_match("v1.2.3", "1.2.3", "semver")
        self.assertTrue(ok)
        ok2, _ = sc.values_match("v1.2.3", "1.2.4", "semver")
        self.assertFalse(ok2)

    def test_date_formats(self):
        ok, _ = sc.values_match("2026-07-13", "July 13, 2026", "date")
        self.assertTrue(ok)
        ok2, _ = sc.values_match("2026-07-13", "07/13/2026", "date")
        self.assertTrue(ok2)
        ok3, _ = sc.values_match("2026-07-13", "2026-07-14", "date")
        self.assertFalse(ok3)

    def test_rounding_floor_1000(self):
        ok, _ = sc.values_match("156703", "156000", "integer", "floor-1000")
        self.assertTrue(ok)

    def test_rounding_floor_1000_as_k(self):
        ok, _ = sc.values_match("156703", "156", "integer", "floor-1000-as-K")
        self.assertTrue(ok)
        bad, _ = sc.values_match("156703", "155", "integer", "floor-1000-as-K")
        self.assertFalse(bad)

    def test_rounding_floor_10(self):
        ok, _ = sc.values_match("2865", "2860", "integer", "floor-10")
        self.assertTrue(ok)


# --------------------------------------------------------------------------- #
class ExtractionTests(unittest.TestCase):
    def test_capture_group_and_line(self):
        content = "line one\nTotal Episodes: 62\nfooter\n"
        val, line, count = sc.extract(content, r"Total Episodes: (\d+)")
        self.assertEqual(val, "62")
        self.assertEqual(line, 2)
        self.assertEqual(count, 1)

    def test_cross_line_pattern(self):
        content = '<span class="n">1,000</span>\n<span class="label">Subs</span>'
        val, _, _ = sc.extract(
            content, r'<span class="n">([\d,]+)</span>\s*<span class="label">')
        self.assertEqual(val, "1,000")

    def test_multi_match_count(self):
        content = "5 apples and 7 apples"
        val, _, count = sc.extract(content, r"(\d+) apples")
        self.assertEqual(val, "5")
        self.assertEqual(count, 2)

    def test_no_match(self):
        val, line, count = sc.extract("nothing here", r"(\d+) apples")
        self.assertIsNone(val)
        self.assertEqual(count, 0)


# --------------------------------------------------------------------------- #
class ValidationTests(unittest.TestCase):
    BASE = textwrap.dedent("""\
        facts:
          - name: episode-count
            type: integer
            canonical:
              file: index.md
              pattern: 'Total: (\\d+)'
            copies:
              - file: kit.md
                pattern: '(\\d+) episodes'
        """)

    def test_valid_manifest(self):
        m = sc.parse_manifest(self.BASE)
        self.assertEqual(sc.validate_manifest(m), [])

    def test_missing_facts(self):
        m = sc.parse_manifest("ignore_paths:\n  - x\n")
        errors = sc.validate_manifest(m)
        self.assertTrue(any("facts" in e for e in errors))

    def test_two_capture_groups_rejected(self):
        m = sc.parse_manifest(textwrap.dedent("""\
            facts:
              - name: x
                canonical:
                  file: a.md
                  pattern: '(\\d+)-(\\d+)'
                copies:
                  - file: b.md
                    pattern: '(\\d+)'
            """))
        errors = sc.validate_manifest(m)
        self.assertTrue(any("one capture group" in e for e in errors))

    def test_bad_type_rejected(self):
        m = sc.parse_manifest(self.BASE.replace("type: integer", "type: float"))
        errors = sc.validate_manifest(m)
        self.assertTrue(any("type" in e for e in errors))

    def test_bad_rounding_rejected(self):
        m = sc.parse_manifest(self.BASE.replace(
            "pattern: '(\\d+) episodes'",
            "pattern: '(\\d+) episodes'\n        rounding: floor-5"))
        errors = sc.validate_manifest(m)
        self.assertTrue(any("rounding" in e for e in errors))

    def test_non_kebab_name_flagged(self):
        m = sc.parse_manifest(self.BASE.replace("episode-count", "Episode_Count"))
        errors = sc.validate_manifest(m)
        self.assertTrue(any("kebab" in e for e in errors))

    def test_empty_copies_flagged(self):
        m = sc.parse_manifest(textwrap.dedent("""\
            facts:
              - name: x
                canonical:
                  file: a.md
                  pattern: '(\\d+)'
                copies: []
            """))
        # copies: [] parses to None here (empty block) -> reported as missing/empty
        errors = sc.validate_manifest(m)
        self.assertTrue(errors)


# --------------------------------------------------------------------------- #
class CheckTests(unittest.TestCase):
    def _repo(self, tmp, canonical="Total Episodes: 62", copy="62 episodes"):
        write(os.path.join(tmp, "index.md"), f"# Index\n{canonical}\n")
        write(os.path.join(tmp, "kit.md"), f"# Kit\nWe have {copy}.\n")
        manifest = textwrap.dedent("""\
            facts:
              - name: episode-count
                type: integer
                canonical:
                  file: index.md
                  pattern: 'Total Episodes: (\\d+)'
                copies:
                  - file: kit.md
                    pattern: '(\\d+) episodes'
            """)
        write(os.path.join(tmp, ".ssot.yaml"), manifest)
        return sc.parse_manifest(manifest)

    def test_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._repo(tmp)
            result = sc.check(tmp, m)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["facts"][0]["status"], "in_sync")

    def test_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._repo(tmp, copy="61 episodes")
            result = sc.check(tmp, m)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["facts"][0]["status"], "drifted")
            copy = result["facts"][0]["copies"][0]
            self.assertEqual(copy["value"], "61")
            self.assertEqual(copy["canonical_value"], "62")

    def test_canonical_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._repo(tmp)
            write(os.path.join(tmp, "index.md"), "# Index\nEpisodes now: 62\n")
            result = sc.check(tmp, m)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["facts"][0]["status"], "canonical_moved")

    def test_stale_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._repo(tmp)
            write(os.path.join(tmp, "kit.md"), "# Kit\nReworded, no number here.\n")
            result = sc.check(tmp, m)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["facts"][0]["copies"][0]["status"],
                             "stale_entry")

    def test_globbed_copy_and_ignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "index.md"), "Total Episodes: 62\n")
            write(os.path.join(tmp, "a.md"), "62 episodes shipped\n")
            write(os.path.join(tmp, "b.md"), "61 episodes shipped\n")
            write(os.path.join(tmp, "skip.md"), "99 episodes shipped\n")
            manifest = textwrap.dedent("""\
                ignore_paths:
                  - skip.md
                facts:
                  - name: episode-count
                    type: integer
                    canonical:
                      file: index.md
                      pattern: 'Total Episodes: (\\d+)'
                    copies:
                      - file: '*.md'
                        pattern: '(\\d+) episodes shipped'
                """)
            m = sc.parse_manifest(manifest)
            result = sc.check(tmp, m)
            files = {c["file"]: c["status"] for c in result["facts"][0]["copies"]}
            self.assertIn("a.md", files)
            self.assertIn("b.md", files)
            self.assertNotIn("skip.md", files)  # ignored
            self.assertEqual(files["a.md"], "in_sync")
            self.assertEqual(files["b.md"], "drifted")

    def test_monotonic_canonical_suspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "index.md"), "Followers: 10000\n")
            write(os.path.join(tmp, "kit.md"), "12000 followers\n")
            manifest = textwrap.dedent("""\
                facts:
                  - name: followers
                    type: integer
                    monotonic: true
                    canonical:
                      file: index.md
                      pattern: 'Followers: (\\d+)'
                    copies:
                      - file: kit.md
                        pattern: '(\\d+) followers'
                """)
            m = sc.parse_manifest(manifest)
            result = sc.check(tmp, m)
            copy = result["facts"][0]["copies"][0]
            self.assertEqual(copy["status"], "drifted")
            self.assertEqual(copy.get("direction"), "canonical_suspect")

    def test_rounding_in_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "data.md"), "reach: 156703\n")
            write(os.path.join(tmp, "readme.md"), "156,000+ views\n")
            manifest = textwrap.dedent("""\
                facts:
                  - name: reach
                    type: integer
                    canonical:
                      file: data.md
                      pattern: 'reach: (\\d+)'
                    copies:
                      - file: readme.md
                        pattern: '([\\d,]+)\\+ views'
                        rounding: floor-1000
                """)
            m = sc.parse_manifest(manifest)
            result = sc.check(tmp, m)
            self.assertEqual(result["exit_code"], 0)


# --------------------------------------------------------------------------- #
class CrossRepoTests(unittest.TestCase):
    def test_is_cross_repo(self):
        self.assertTrue(sc.is_cross_repo("/repo", "../sibling/x.html"))
        self.assertTrue(sc.is_cross_repo("/repo", "/abs/x.html"))
        self.assertFalse(sc.is_cross_repo("/repo", "docs/x.md"))

    def test_cross_repo_read_only_local(self):
        with tempfile.TemporaryDirectory() as parent:
            repo = os.path.join(parent, "repo")
            sib = os.path.join(parent, "sibling")
            write(os.path.join(repo, "index.md"), "Price: 49\n")
            write(os.path.join(sib, "page.html"), 'data-price="49"\n')
            manifest = textwrap.dedent("""\
                facts:
                  - name: price
                    type: integer
                    canonical:
                      file: index.md
                      pattern: 'Price: (\\d+)'
                    copies:
                      - file: ../sibling/page.html
                        pattern: 'data-price="(\\d+)"'
                """)
            m = sc.parse_manifest(manifest)
            result = sc.check(repo, m)
            copy = result["facts"][0]["copies"][0]
            self.assertTrue(copy["cross_repo"])
            self.assertEqual(copy["status"], "in_sync")


# --------------------------------------------------------------------------- #
class FreshnessTests(unittest.TestCase):
    def test_skips_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.md")
            write(path, "hi\n")
            result = sc.check_freshness(
                path, {"owner": "team", "max_age_days": 30})
            self.assertTrue(result["skipped"])


# --------------------------------------------------------------------------- #
class DiscoverTests(unittest.TestCase):
    def test_proposes_repeated_value(self):
        tree = os.path.join(FIXTURES, "discover_tree")
        result = sc.discover(tree)
        names = {p["name"] for p in result["proposals"]}
        # 12,500 users appears in 3 prose files -> proposed.
        self.assertTrue(any(p["value"] == "12500" for p in result["proposals"]),
                        f"proposals: {[p['value'] for p in result['proposals']]}")
        self.assertTrue(names)

    def test_flags_live_drift(self):
        tree = os.path.join(FIXTURES, "discover_tree")
        result = sc.discover(tree)
        # integrations: 88 (README, landing) vs 90 (media-kit) -> drift.
        integ = [d for d in result["drift"] if d["unit"] == "integration"]
        self.assertTrue(integ, f"drift: {result['drift']}")
        self.assertEqual(set(integ[0]["values"]), {"88", "90"})

    def test_skips_vendor_dir(self):
        tree = os.path.join(FIXTURES, "discover_tree")
        result = sc.discover(tree)
        for p in result["proposals"]:
            for occ in p["occurrences"]:
                self.assertNotIn("vendor/", occ["file"])


# --------------------------------------------------------------------------- #
class CLIExitCodeTests(unittest.TestCase):
    def _run(self, args, cwd):
        return subprocess.run([sys.executable, CLI] + args, cwd=cwd,
                              capture_output=True, text=True)

    def test_check_exit_codes_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "index.md"), "Total Episodes: 62\n")
            write(os.path.join(tmp, "kit.md"), "62 episodes\n")
            manifest = textwrap.dedent("""\
                facts:
                  - name: episode-count
                    type: integer
                    canonical:
                      file: index.md
                      pattern: 'Total Episodes: (\\d+)'
                    copies:
                      - file: kit.md
                        pattern: '(\\d+) episodes'
                """)
            write(os.path.join(tmp, ".ssot.yaml"), manifest)

            r = self._run(["check"], tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            # default subcommand is check
            r0 = self._run([], tmp)
            self.assertEqual(r0.returncode, 0)

            write(os.path.join(tmp, "kit.md"), "61 episodes\n")
            r1 = self._run(["check"], tmp)
            self.assertEqual(r1.returncode, 1)
            self.assertIn("DRIFTED", r1.stdout)

            r2 = self._run(["check", "--json"], tmp)
            self.assertEqual(r2.returncode, 1)
            self.assertIn('"status": "drifted"', r2.stdout)

            rv = self._run(["validate"], tmp)
            self.assertEqual(rv.returncode, 0)

    def test_config_error_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(["check"], tmp)  # no manifest
            self.assertEqual(r.returncode, 2)

    def test_invalid_manifest_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, ".ssot.yaml"),
                  "facts:\n  - name: x\n    canonical:\n      file: a.md\n"
                  "      pattern: '(\\d+)-(\\d+)'\n    copies:\n"
                  "      - file: b.md\n        pattern: '(\\d+)'\n")
            r = self._run(["validate"], tmp)
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
