"""Tests for fact guards (superseded / restated) and named repos.

Every carve-out has a must-fire complement: a guard that is silent in the
carve-out case must still fire on the same text outside it.
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


GUARDED = """\
    facts:
      - name: ui-cap
        type: integer
        canonical:
          file: owner.md
          pattern: 'Weekly cap: \\$([\\d,]+)'
        guard:
          anchors:
            - UI
            - unemployment
          superseded:
            - "1,152"
          restated: true
          also_current:
            - "31K"
    """


class GuardTests(unittest.TestCase):
    def run_check(self, tmp, m, **kw):
        return sc.check(tmp, m, env={}, **kw)

    def findings(self, result, kind=None):
        out = []
        for f in result["facts"]:
            for g in f.get("guard", {}).get("findings", []):
                if kind is None or g["kind"] == kind:
                    out.append(g)
        return out

    def setup_owner(self, tmp):
        write(os.path.join(tmp, "owner.md"), "UI weekly cap: $1,208\n"
              "Weekly cap: $1,208 (26 weeks: 31K)\n")

    def test_clean_repo_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "notes.md"),
                  "UI cap: see owner.md\nThe 1,152 widgets shipped.\n")
            r = self.run_check(tmp, manifest(GUARDED))
            self.assertEqual(self.findings(r), [])
            self.assertEqual(r["exit_code"], 0)
            self.assertEqual(r["facts"][0]["status"], "in_sync")

    def test_superseded_value_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "plan.md"), "x\nUI pays $1,152 a week.\n")
            r = self.run_check(tmp, manifest(GUARDED))
            [g] = self.findings(r, "stale_value")
            self.assertEqual((g["file"], g["line"], g["value"]),
                             ("plan.md", 2, "1,152"))
            self.assertEqual(r["exit_code"], 1)
            self.assertEqual(r["facts"][0]["status"], "stale_value")

    def test_restated_canonical_value_and_alias_fire(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "budget.md"),
                  "UI: $1,208 a week.\nunemployment total 31K.\n")
            r = self.run_check(tmp, manifest(GUARDED))
            got = [(g["line"], g["value"]) for g in
                   self.findings(r, "restated")]
            self.assertEqual(got, [(1, "1,208"), (2, "31K")])
            self.assertEqual(r["summary"]["restated"], 2)

    def test_owner_file_is_allowed_to_hold_current_value(self):
        # Must-not-fire: owner.md holds 1,208 on an anchor line.
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            r = self.run_check(tmp, manifest(GUARDED))
            self.assertEqual(self.findings(r, "restated"), [])

    def test_canonical_file_may_record_superseded_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "owner.md"),
                  "Weekly cap: $1,208\nUI cap raised from $1,152 in July.\n")
            r = self.run_check(tmp, manifest(GUARDED))
            self.assertEqual(self.findings(r), [])
            # Complement: the same history line elsewhere is a stale value.
            write(os.path.join(tmp, "notes.md"),
                  "UI cap raised from $1,152 in July.\n")
            r = self.run_check(tmp, manifest(GUARDED))
            self.assertEqual([g["file"] for g in self.findings(r)],
                             ["notes.md"])

    def test_anchor_must_be_on_same_line_and_whole_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            # 'UI' inside 'rebuilt' is not an anchor; anchor on another line
            # does not count.
            write(os.path.join(tmp, "a.md"),
                  "Cash rebuilt to $1,208.\nUI\n$1,152\n")
            r = self.run_check(tmp, manifest(GUARDED))
            self.assertEqual(self.findings(r), [])
            # Complement: the same values with the anchor on the line fire.
            write(os.path.join(tmp, "a.md"), "UI rebuilt to $1,208 and $1,152\n")
            r = self.run_check(tmp, manifest(GUARDED))
            kinds = sorted(g["kind"] for g in self.findings(r))
            self.assertEqual(kinds, ["restated", "stale_value"])

    def test_value_boundaries(self):
        cases = [
            ("UI 11,152 total", False),     # inside a longer number
            ("UI 1,1520", False),
            ("UI 1,152.5", False),          # decimal continues the number
            ("UI was 1,152.", True),        # sentence end
            ("UI was 1,152, then", True),   # comma then space
            ("UI was $1,152", True),        # leading currency symbol
        ]
        for text, fires in cases:
            with self.subTest(text=text), \
                    tempfile.TemporaryDirectory() as tmp:
                self.setup_owner(tmp)
                write(os.path.join(tmp, "x.md"), text + "\n")
                r = self.run_check(tmp, manifest(GUARDED))
                self.assertEqual(bool(self.findings(r, "stale_value")), fires)

    def test_declared_copy_is_not_restated_but_is_still_compared(self):
        m = manifest(GUARDED.replace("        guard:", """\
        copies:
          - file: kit.md
            pattern: 'cap \\$([\\d,]+)'
        guard:"""))
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "kit.md"), "UI cap $1,208\n")
            r = self.run_check(tmp, m)
            self.assertEqual(self.findings(r), [])
            self.assertEqual(r["exit_code"], 0)
            # Complement: an undeclared file with the same line still fires.
            write(os.path.join(tmp, "kit2.md"), "UI cap $1,208\n")
            r = self.run_check(tmp, m)
            self.assertEqual([g["file"] for g in
                              self.findings(r, "restated")], ["kit2.md"])

    def test_exempt_and_ignore_paths(self):
        m = manifest(GUARDED.replace("          restated: true", """\
          restated: true
          exempt:
            - history/
            - "*.log.md\""""))
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "history", "2025.md"), "UI $1,152\n")
            write(os.path.join(tmp, "week.log.md"), "UI $1,152\n")
            r = self.run_check(tmp, m)
            self.assertEqual(self.findings(r), [])
            # Complement: same line outside the exempt paths fires.
            write(os.path.join(tmp, "historyish.md"), "UI $1,152\n")
            r = self.run_check(tmp, m)
            self.assertEqual([g["file"] for g in self.findings(r)],
                             ["historyish.md"])

    def test_skip_marker(self):
        m = manifest("guard_skip_marker: '^\\*\\*Status:\\*\\* *snapshot'\n" +
                     textwrap.dedent(GUARDED))
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "snap.md"),
                  "**Status:** snapshot\nUI $1,152\n")
            write(os.path.join(tmp, "live.md"),
                  "**Status:** live\nUI $1,152\n")
            r = self.run_check(tmp, m)
            self.assertEqual([g["file"] for g in self.findings(r)],
                             ["live.md"])

    def test_superseded_only_guard_allows_copies_anywhere(self):
        # "mirror" use: no restated scan.
        m = manifest(GUARDED.replace("          restated: true\n", "")
                     .replace("          also_current:\n            - \"31K\"\n",
                              ""))
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "plan.md"), "UI $1,208\n")
            r = self.run_check(tmp, m)
            self.assertEqual(self.findings(r), [])
            write(os.path.join(tmp, "plan.md"), "UI $1,152\n")
            r = self.run_check(tmp, m)
            self.assertEqual(len(self.findings(r, "stale_value")), 1)

    def test_default_paths_are_prose_and_manifest_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "script.py"), "# UI 1,152\n")
            mpath = os.path.join(tmp, ".ssot.yaml")
            write(mpath, textwrap.dedent(GUARDED))
            r = self.run_check(tmp, sc.load_manifest(mpath),
                               manifest_path=mpath)
            self.assertEqual(self.findings(r), [])
            # Complement: widen paths to *.py and *.yaml; the .py fires, the
            # manifest (which lists the superseded value) still does not.
            m = manifest(GUARDED.replace("          restated: true", """\
          restated: true
          paths:
            - "**/*.py"
            - "**/*.yaml\""""))
            write(mpath, textwrap.dedent(GUARDED))
            r = self.run_check(tmp, m, manifest_path=mpath)
            self.assertEqual([g["file"] for g in self.findings(r)],
                             ["script.py"])

    def test_guard_runs_when_canonical_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "owner.md"), "reworded\n")
            write(os.path.join(tmp, "a.md"), "UI $1,152 and 31K\n")
            r = self.run_check(tmp, manifest(GUARDED))
            self.assertEqual(r["facts"][0]["status"], "canonical_moved")
            kinds = sorted(g["kind"] for g in self.findings(r))
            # No canonical value, but superseded and also_current still scan.
            self.assertEqual(kinds, ["restated", "stale_value"])

    def test_render_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_owner(tmp)
            write(os.path.join(tmp, "plan.md"), "UI $1,152\nUI $1,208\n")
            r = self.run_check(tmp, manifest(GUARDED))
            buf = io.StringIO()
            with unittest.mock.patch("sys.stdout", buf):
                sc.render_check(r)
            out = buf.getvalue()
            self.assertIn("STALE VALUE (1)", out)
            self.assertIn("RESTATED (1)", out)
            self.assertIn("plan.md:1", out)
            self.assertIn("1 stale values, 1 restated", out)
            json.dumps(r)


class GuardValidationTests(unittest.TestCase):
    def errors(self, text):
        return sc.validate_manifest(sc.parse_manifest(textwrap.dedent(text)))

    def test_guard_without_copies_is_valid(self):
        self.assertEqual(self.errors(GUARDED), [])

    def test_no_copies_and_no_guard_is_invalid(self):
        errs = self.errors("""\
            facts:
              - name: x
                canonical:
                  file: a.md
                  pattern: '(x)'
            """)
        self.assertTrue(any("copies" in e for e in errs), errs)

    def test_guard_needs_anchors_and_something_to_scan(self):
        errs = self.errors("""\
            facts:
              - name: x
                canonical:
                  file: a.md
                  pattern: '(x)'
                guard:
                  restated: false
            """)
        self.assertTrue(any("anchors" in e for e in errs), errs)
        self.assertTrue(any("nothing to scan" in e for e in errs), errs)

    def test_also_current_requires_restated(self):
        errs = self.errors(GUARDED.replace("          restated: true\n", ""))
        self.assertTrue(any("also_current" in e for e in errs), errs)

    def test_bad_skip_marker(self):
        errs = self.errors("guard_skip_marker: '('\n" +
                           textwrap.dedent(GUARDED))
        self.assertTrue(any("guard_skip_marker" in e for e in errs), errs)


NAMED = """\
    repos:
      site:
        path: ../site
    facts:
      - name: price
        type: currency
        canonical:
          file: pricing.md
          pattern: 'Price: \\$([\\d,]+)'
        copies:
          - repo: site
            file: src/consts.ts
            pattern: "price = '\\$([\\d,]+)'"
    """


class NamedRepoTests(unittest.TestCase):
    def layout(self, parent, site_dir="site", price="400"):
        root = os.path.join(parent, "main")
        write(os.path.join(root, "pricing.md"), "Price: $400\n")
        write(os.path.join(parent, site_dir, "src", "consts.ts"),
              f"export const price = '${price}'\n")
        return root

    def test_manifest_default_path(self):
        with tempfile.TemporaryDirectory() as parent:
            root = self.layout(parent)
            r = sc.check(root, manifest(NAMED), env={})
            cp = r["facts"][0]["copies"][0]
            self.assertEqual(cp["status"], "in_sync")
            self.assertEqual(cp["file"], "site:src/consts.ts")
            self.assertEqual(r["repos"]["site"]["via"], "manifest default")

    def test_named_repo_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as parent:
            root = self.layout(parent, price="450")
            r = sc.check(root, manifest(NAMED), env={})
            self.assertEqual(r["facts"][0]["copies"][0]["status"], "drifted")
            self.assertEqual(r["exit_code"], 1)

    def test_missing_repo_is_its_own_status(self):
        with tempfile.TemporaryDirectory() as parent:
            root = self.layout(parent, site_dir="elsewhere")
            r = sc.check(root, manifest(NAMED), env={})
            cp = r["facts"][0]["copies"][0]
            self.assertEqual(cp["status"], "repo_missing")
            self.assertIn(os.path.join(parent, "site"), cp["note"])
            self.assertEqual(r["summary"]["repo_missing"], 1)
            self.assertEqual(r["exit_code"], 1)
            buf = io.StringIO()
            with unittest.mock.patch("sys.stdout", buf):
                sc.render_check(r)
            self.assertIn("REPO NOT FOUND (1)", buf.getvalue())

    def test_missing_canonical_repo(self):
        text = """\
            repos:
              owner: {}
            facts:
              - name: x
                canonical:
                  repo: owner
                  file: a.md
                  pattern: 'v(\\d)'
                copies:
                  - file: b.md
                    pattern: 'v(\\d)'
            """.replace(" {}", "")
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "main")
            write(os.path.join(root, "b.md"), "v1\n")
            r = sc.check(root, manifest(text), env={})
            self.assertEqual(r["facts"][0]["status"], "repo_missing")
            self.assertEqual(r["exit_code"], 1)
            # Complement: once the default ../owner exists, it is read.
            write(os.path.join(parent, "owner", "a.md"), "v1\n")
            r = sc.check(root, manifest(text), env={})
            self.assertEqual(r["facts"][0]["status"], "in_sync")

    def test_resolution_order(self):
        with tempfile.TemporaryDirectory() as parent:
            root = self.layout(parent)                   # ../site has $400
            write(os.path.join(parent, "dir", "site", "src", "consts.ts"),
                  "export const price = '$401'\n")       # $SSOT_REPOS_DIR
            write(os.path.join(parent, "env", "src", "consts.ts"),
                  "export const price = '$402'\n")       # $SSOT_REPO_SITE
            write(os.path.join(parent, "cli", "src", "consts.ts"),
                  "export const price = '$403'\n")       # --repo
            m = manifest(NAMED)
            env = {"SSOT_REPOS_DIR": os.path.join(parent, "dir"),
                   "SSOT_REPO_SITE": os.path.join(parent, "env")}

            def value(**kw):
                r = sc.check(root, m, **kw)
                return r["facts"][0]["copies"][0]["value"]

            self.assertEqual(value(env={}), "400")
            self.assertEqual(value(env={"SSOT_REPOS_DIR": env[
                "SSOT_REPOS_DIR"]}), "401")
            self.assertEqual(value(env=env), "402")
            self.assertEqual(value(env=env, repo_overrides={
                "site": os.path.join(parent, "cli")}), "403")

    def test_repos_dir_falls_back_but_explicit_override_does_not(self):
        with tempfile.TemporaryDirectory() as parent:
            root = self.layout(parent)
            m = manifest(NAMED)
            # $SSOT_REPOS_DIR without a 'site' inside falls back to ../site.
            r = sc.check(root, m, env={"SSOT_REPOS_DIR":
                                       os.path.join(parent, "nope")})
            self.assertEqual(r["facts"][0]["copies"][0]["status"], "in_sync")
            # An explicit override that does not exist never falls back.
            r = sc.check(root, m, env={"SSOT_REPO_SITE":
                                       os.path.join(parent, "typo")})
            self.assertEqual(r["facts"][0]["copies"][0]["status"],
                             "repo_missing")
            r = sc.check(root, m, env={}, repo_overrides={
                "site": os.path.join(parent, "typo")})
            self.assertEqual(r["facts"][0]["copies"][0]["status"],
                             "repo_missing")

    def test_validation(self):
        bad_undeclared = NAMED.replace("    repos:\n      site:\n"
                                       "        path: ../site\n", "")
        errs = sc.validate_manifest(sc.parse_manifest(
            textwrap.dedent(bad_undeclared)))
        self.assertTrue(any("not declared" in e for e in errs), errs)
        for bad in ("../x.ts", "/abs/x.ts", "src/*.ts"):
            errs = sc.validate_manifest(sc.parse_manifest(textwrap.dedent(
                NAMED.replace("file: src/consts.ts", f"file: {bad}"))))
            self.assertTrue(errs, bad)

    def test_cli_repo_flag(self):
        with tempfile.TemporaryDirectory() as parent:
            root = self.layout(parent, site_dir="moved")
            mpath = os.path.join(root, ".ssot.yaml")
            write(mpath, textwrap.dedent(NAMED))
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SSOT_REPOS_DIR", None)
                os.environ.pop("SSOT_REPO_SITE", None)
                with unittest.mock.patch("sys.stdout", io.StringIO()):
                    self.assertEqual(sc.main(["check", "-f", mpath]), 1)
                    self.assertEqual(sc.main([
                        "check", "-f", mpath, "--repo",
                        "site=" + os.path.join(parent, "moved")]), 0)
                with unittest.mock.patch("sys.stderr", io.StringIO()):
                    self.assertEqual(sc.main(["check", "-f", mpath,
                                              "--repo", "site"]), 2)


if __name__ == "__main__":
    unittest.main()
