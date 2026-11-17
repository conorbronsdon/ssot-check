"""Tests for the `fix` subcommand. Run: python3 -m unittest discover tests"""

import io
import os
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402


def write(path, content, newline=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(content)


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


BASE = """\
    facts:
      - name: subs
        type: integer
        canonical:
          file: owner.md
          pattern: 'Subscribers: ([\\d,]+)'
        copies:
          - file: kit.md
            pattern: '([\\d,]+) readers'
"""


class FormatLikeTests(unittest.TestCase):
    def test_carries_the_copy_format(self):
        cases = [("1268", "1,100+", "integer", "1,268+"),
                 ("1500", "1200", "integer", "1500"),
                 ("170020", "170 000", "integer", "170 020"),
                 ("$2,400", "2,200", "currency", "2,400"),
                 ("2400", "$2,200.00", "currency", "$2,400.00"),
                 ("1.2.3", "v1.2.0", "semver", "v1.2.3"),
                 ("1.2.3", "1.2.0", "semver", "1.2.3"),
                 (" new ", "old", "string", "new")]
        for canon, copy, ftype, want in cases:
            with self.subTest(canon=canon, copy=copy):
                self.assertEqual(sc.format_like(canon, copy, ftype), want)


class FixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.mpath = os.path.join(self.root, ".ssot.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def setup_repo(self, manifest=BASE, owner="Subscribers: 1,268\n",
                   kit="Join 1,100 readers today.\n", newline=None):
        write(self.mpath, textwrap.dedent(manifest))
        write(os.path.join(self.root, "owner.md"), owner)
        write(os.path.join(self.root, "kit.md"), kit, newline=newline)

    def run_fix(self, *extra):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            code = sc.main(["fix", "-f", self.mpath] + list(extra))
        return code, out.getvalue()

    def kit(self):
        return read(os.path.join(self.root, "kit.md"))

    def test_dry_run_shows_diff_and_writes_nothing(self):
        self.setup_repo()
        code, out = self.run_fix()
        self.assertEqual(code, 1)
        self.assertIn("-Join 1,100 readers today.", out)
        self.assertIn("+Join 1,268 readers today.", out)
        self.assertIn("Re-run with --write", out)
        self.assertEqual(self.kit(), "Join 1,100 readers today.\n")

    def test_write_rewrites_and_rechecks(self):
        self.setup_repo()
        code, out = self.run_fix("--write")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.kit(), "Join 1,268 readers today.\n")
        self.assertIn("all rewritten copies in sync", out)
        m = sc.load_manifest(self.mpath)
        self.assertEqual(sc.check(self.root, m, env={})["exit_code"], 0)

    def test_in_sync_is_nothing_to_fix(self):
        self.setup_repo(kit="Join 1,268 readers today.\n")
        code, out = self.run_fix("--write")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to fix.", out)

    def test_crlf_and_surroundings_preserved(self):
        self.setup_repo(kit="a\r\nJoin 1,100 readers\r\nb\r\n", newline="")
        self.run_fix("--write")
        self.assertEqual(self.kit(), "a\r\nJoin 1,268 readers\r\nb\r\n")

    def assert_refused(self, manifest, needle, kit="Join 1,100 readers\n",
                       owner="Subscribers: 1,268\n"):
        self.setup_repo(manifest=manifest, kit=kit, owner=owner)
        before = self.kit()
        code, out = self.run_fix("--write")
        self.assertEqual(code, 1, out)
        self.assertIn("NOT FIXED", out)
        self.assertIn(needle, out)
        self.assertEqual(self.kit(), before)

    def test_refuses_floor_rounding_and_json(self):
        self.assert_refused(BASE + "            relation: floor\n"
                                   "            max_gap_pct: 5\n",
                            "floor")
        self.assert_refused(BASE + "            rounding: floor-100\n",
                            "rounded copy", kit="Join 1,100 readers\n",
                            owner="Subscribers: 1,268\n")

    def test_refuses_canonical_suspect(self):
        self.assert_refused(BASE.replace("type: integer",
                                         "type: integer\n        "
                                         "monotonic: true"),
                            "canonical suspect", kit="Join 1,300 readers\n")

    def test_refuses_ambiguous_pattern(self):
        self.assert_refused(BASE, "matches 2x",
                            kit="Join 1,100 readers\nor 1,100 readers\n")

    def test_refuses_dates(self):
        m = textwrap.dedent("""\
            facts:
              - name: launch
                type: date
                canonical:
                  file: owner.md
                  pattern: 'Launch: (\\S+)'
                copies:
                  - file: kit.md
                    pattern: 'on (\\S+) readers'
            """)
        self.assert_refused(m, "type date", kit="on 2026-01-01 readers\n",
                            owner="Launch: 2026-02-02\n")

    def test_refuses_other_repo(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "main")
            write(os.path.join(root, "owner.md"), "Subscribers: 1,268\n")
            write(os.path.join(parent, "site", "kit.md"), "1,100 readers\n")
            mpath = os.path.join(root, ".ssot.yaml")
            write(mpath, textwrap.dedent(BASE).replace(
                "      - file: kit.md",
                "      - repo: site\n        file: kit.md").replace(
                "facts:", "repos:\n  site:\nfacts:"))
            out = io.StringIO()
            with unittest.mock.patch("sys.stdout", out), \
                    unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SSOT_REPOS_DIR", None)
                os.environ.pop("SSOT_REPO_SITE", None)
                self.assertEqual(sc.main(["fix", "-f", mpath, "--write"]), 1)
            self.assertIn("in another repo", out.getvalue())
            self.assertEqual(read(os.path.join(parent, "site", "kit.md")),
                             "1,100 readers\n")

    def test_two_facts_in_one_file(self):
        m = BASE + textwrap.indent(textwrap.dedent("""\
            - name: price
              type: currency
              canonical:
                file: owner.md
                pattern: 'Price: \\$([\\d,]+)'
              copies:
                - file: kit.md
                  pattern: 'for \\$([\\d,.]+)'
            """), "      ")
        self.setup_repo(manifest=m, owner="Subscribers: 1,268\nPrice: $40\n",
                        kit="Join 1,100 readers for $35.00\n")
        code, out = self.run_fix("--write")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.kit(), "Join 1,268 readers for $40.00\n")


if __name__ == "__main__":
    unittest.main()
