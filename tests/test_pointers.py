"""Tests for the `pointers` subcommand.

Each accepted form, each rejected look-alike, and each resolution route has a
case; skip rules (code, placeholders, skip marker) have must-fire complements.
Run: python3 -m unittest discover tests
"""

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class FindPointerTests(unittest.TestCase):
    def found(self, text):
        return [(line, target, repo) for line, target, repo
                in sc.find_pointers(text)]

    def test_accepted_forms(self):
        cases = {
            "⏳ cap: `a.md`": ("a.md", None),
            "⏳ **Gate:** `a.md`.": ("a.md", None),
            "| ⏳ `a.md` |": ("a.md", None),
            "⏳ status: [plan](a.md)": ("a.md", None),
            "⏳ downloads (`a.md`): more text": ("a.md", None),
            "⏳ pipeline: cot-production `p/x.md`": ("p/x.md", "cot-production"),
            "⏳ pricing: https://example.com/p": ("https://example.com/p", None),
        }
        for text, (target, repo) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.found(text), [(1, target, repo)])

    def test_wrapped_pointers(self):
        self.assertEqual(self.found("x ⏳ Live\nstatus: `plan.md`. Do"),
                         [(1, "plan.md", None)])
        self.assertEqual(self.found("⏳ cost:\n`exp.md`"),
                         [(1, "exp.md", None)])

    def test_not_pointers(self):
        # "Open: the `x`" parses with a repo word; resolution rejects it
        # (CheckPointersTests.test_missing_named_repo).
        for text in ("⏳ pointer.",
                     "- ⏳ **Acquisitions** (18 months)",
                     "⏳ cash: ledger",
                     "| ⏳ | date in `a.md` |"):
            with self.subTest(text=text):
                self.assertEqual(self.found(text), [(1, None, None)])

    def test_several_markers_on_one_line(self):
        self.assertEqual(
            self.found("⏳ terms: `a.md` · ⏳ hours: `b.md` · ⏳ cash: ledger"),
            [(1, "a.md", None), (1, "b.md", None), (1, None, None)])

    def test_code_is_documentation_not_a_pointer(self):
        text = ("Write `⏳ <what>: <owner>` instead.\n"
                "```\n⏳ cap: `gone.md`\n```\n"
                "⏳ cap: `real.md`\n")
        self.assertEqual(self.found(text), [(5, "real.md", None)])


class CheckPointersTests(unittest.TestCase):
    def run_pointers(self, root, **kw):
        kw.setdefault("env", {})
        return sc.check_pointers(root, **kw)

    def statuses(self, result):
        return [(p["file"], p["line"], p["status"])
                for p in result["pointers"]]

    def test_resolution_routes(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "main")
            write(os.path.join(root, "finance", "owner.md"), "x\n")
            write(os.path.join(root, "finance", "local.md"), "x\n")
            write(os.path.join(parent, "prod", "analytics", "goals.md"), "x\n")
            write(os.path.join(root, "notes", "a.md"), "\n".join([
                "⏳ cap: `finance/owner.md`",          # repo root
                "⏳ cap: `../finance/local.md`",       # file's directory
                "⏳ goals: `~/prod/analytics/goals.md`",  # ~/repo
                "⏳ goals: prod `analytics/goals.md`",    # repo word
                "⏳ goals: `prod/analytics/goals.md`",    # repo/path
                "⏳ goals: `prod:analytics/goals.md`",    # repo:path
                "⏳ cap: `finance/owner.md#limits`",   # fragment stripped
            ]) + "\n")
            r = self.run_pointers(root)
            self.assertEqual([p["status"] for p in r["pointers"]],
                             ["ok"] * 7, r["pointers"])
            self.assertEqual(sc.pointers_exit_code(r), 0)

    def test_missing_target_fails(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "a.md"), "⏳ cap: `gone.md`\n")
            r = self.run_pointers(root)
            self.assertEqual(self.statuses(r), [("a.md", 1, "missing")])
            self.assertEqual(sc.pointers_exit_code(r), 1)

    def test_missing_named_repo(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "main")
            write(os.path.join(root, "a.md"),
                  "⏳ goals: `~/prod/goals.md`\n")
            r = self.run_pointers(root)
            self.assertEqual(r["pointers"][0]["status"], "repo_missing")
            self.assertEqual(sc.pointers_exit_code(r), 1)
            # A repo word that is no repo is prose, not a missing repo...
            write(os.path.join(root, "a.md"), "⏳ Open: the `branch` x\n")
            r = self.run_pointers(root)
            self.assertEqual(r["pointers"][0]["status"], "malformed")
            # ...unless the manifest declares it.
            m = sc.parse_manifest("repos:\n  prod:\nfacts: []\n")
            write(os.path.join(root, "a.md"), "⏳ goals: prod `goals.md`\n")
            r = self.run_pointers(root, manifest=m)
            self.assertEqual(r["pointers"][0]["status"], "repo_missing")

    def test_malformed_is_advisory_unless_strict(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "a.md"), "⏳ pointer.\n")
            r = self.run_pointers(root)
            self.assertEqual(r["summary"]["malformed"], 1)
            self.assertEqual(sc.pointers_exit_code(r), 0)
            self.assertEqual(sc.pointers_exit_code(r, strict=True), 1)

    def test_placeholders_and_urls_are_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "a.md"),
                  "⏳ x: `<owner>`\n⏳ y: https://example.com\n")
            r = self.run_pointers(root)
            self.assertEqual(r["summary"]["skipped"], 2)
            self.assertEqual(sc.pointers_exit_code(r), 0)

    def test_skip_marker_and_ignore(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "snap.md"),
                  "**Status:** snapshot\n⏳ cap: `gone.md`\n")
            write(os.path.join(root, "old", "x.md"), "⏳ cap: `gone.md`\n")
            r = self.run_pointers(root, skip_marker=r"^\*\*Status:\*\* "
                                  r"*snapshot", ignore=["old/"])
            self.assertEqual(r["pointers"], [])
            # Complement: without them, both are missing.
            r = self.run_pointers(root)
            self.assertEqual(r["summary"]["missing"], 2)

    def test_manifest_skip_marker_and_ignore_paths_apply(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "snap.md"),
                  "**Status:** snapshot\n⏳ cap: `gone.md`\n")
            m = sc.parse_manifest(
                "guard_skip_marker: '^\\*\\*Status:\\*\\* *snapshot'\n"
                "facts: []\n")
            self.assertEqual(self.run_pointers(root, manifest=m)["pointers"],
                             [])

    def test_only_prose_files_by_default(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "a.py"), "# ⏳ cap: `gone.md`\n")
            self.assertEqual(self.run_pointers(root)["pointers"], [])
            r = self.run_pointers(root, paths=["**/*.py"])
            self.assertEqual(r["summary"]["missing"], 1)

    def test_custom_marker(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "a.md"), "SSOT-> cap: `gone.md`\n")
            r = self.run_pointers(root, marker="SSOT->")
            self.assertEqual(r["summary"]["missing"], 1)


class PointersCliTests(unittest.TestCase):
    def test_cli_exit_codes_and_output(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "owner.md"), "x\n")
            write(os.path.join(root, "a.md"), "⏳ cap: `owner.md`\n")
            out = io.StringIO()
            with unittest.mock.patch("sys.stdout", out), \
                    unittest.mock.patch.dict(os.environ, {}, clear=False):
                self.assertEqual(sc.main(["pointers", "--root", root]), 0)
            self.assertIn("1 resolve, 0 missing", out.getvalue())
            write(os.path.join(root, "a.md"), "⏳ cap: `gone.md`\n⏳ x.\n")
            out = io.StringIO()
            with unittest.mock.patch("sys.stdout", out):
                self.assertEqual(sc.main(["pointers", "--root", root]), 1)
                self.assertEqual(sc.main(["pointers", "--root", root,
                                          "--json"]), 1)
            text = out.getvalue()
            self.assertIn("MISSING (1)", text)
            self.assertIn("a.md:1 -> gone.md", text)
            self.assertIn("MALFORMED (advisory; --strict fails) (1)", text)
            payload = json.loads(text[text.index("{"):])
            self.assertEqual(payload["summary"]["missing"], 1)

    def test_cli_config_errors(self):
        with tempfile.TemporaryDirectory() as root:
            err = io.StringIO()
            with unittest.mock.patch("sys.stderr", err):
                self.assertEqual(sc.main(["pointers", "--root", root,
                                          "--repo", "x"]), 2)
                self.assertEqual(sc.main(["pointers", "--root", root,
                                          "--skip-marker", "("]), 2)


if __name__ == "__main__":
    unittest.main()
