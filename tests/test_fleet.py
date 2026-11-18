"""Tests for the `fleet` subcommand. Run: python3 -m unittest discover tests"""

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402

MANIFEST = """repos:
  site:
facts:
  - name: price
    canonical:
      file: a.md
      pattern: 'Price: (\\d+)'
    copies:
      - file: b.md
        pattern: 'Only (\\d+)'
"""


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def make_repo(parent, name, copy="4", manifest=MANIFEST):
    root = os.path.join(parent, name)
    write(os.path.join(root, "a.md"), "Price: 4\n")
    write(os.path.join(root, "b.md"), f"Only {copy}\n")
    if manifest is not None:
        write(os.path.join(root, ".ssot.yaml"), manifest)
    return root


class FleetTests(unittest.TestCase):
    def test_all_in_sync(self):
        with tempfile.TemporaryDirectory() as parent:
            a, b = make_repo(parent, "a"), make_repo(parent, "b")
            r = sc.fleet([{"path": a}, {"path": b}], env={})
            self.assertEqual(r["exit_code"], 0)
            self.assertEqual(r["summary"]["ok"], 2)

    def test_one_drifted_repo_fails_the_fleet(self):
        with tempfile.TemporaryDirectory() as parent:
            a, b = make_repo(parent, "a"), make_repo(parent, "b", copy="5")
            r = sc.fleet([{"path": a}, {"path": b}], env={})
            self.assertEqual(r["exit_code"], 1)
            self.assertEqual([x["status"] for x in r["repos"]],
                             ["ok", "problems"])

    def test_missing_dir_fails_but_no_manifest_does_not(self):
        with tempfile.TemporaryDirectory() as parent:
            a = make_repo(parent, "a")
            plain = make_repo(parent, "plain", manifest=None)
            r = sc.fleet([{"path": a}, {"path": plain}], env={})
            self.assertEqual(r["exit_code"], 0)
            self.assertEqual(r["repos"][1]["status"], "no_manifest")
            r = sc.fleet([{"path": a}, {"path": os.path.join(parent, "x")}],
                         env={})
            self.assertEqual(r["exit_code"], 1)
            self.assertEqual(r["repos"][1]["status"], "repo_missing")

    def test_config_error_is_exit_2(self):
        with tempfile.TemporaryDirectory() as parent:
            a = make_repo(parent, "a")
            bad = make_repo(parent, "bad", manifest="facts: 3\n")
            r = sc.fleet([{"path": a}, {"path": bad}], env={})
            self.assertEqual(r["exit_code"], 2)
            self.assertEqual(r["repos"][1]["status"], "config_error")

    def test_each_repo_uses_its_own_root(self):
        # The same relative manifest paths resolve inside each repo.
        with tempfile.TemporaryDirectory() as parent:
            a = make_repo(parent, "a", copy="4")
            b = make_repo(parent, "b", copy="5")
            r = sc.fleet([{"path": b}, {"path": a}], env={})
            self.assertEqual([x["status"] for x in r["repos"]],
                             ["problems", "ok"])

    def test_fleet_file(self):
        with tempfile.TemporaryDirectory() as parent:
            make_repo(parent, "a")
            make_repo(parent, "b", manifest=None)
            write(os.path.join(parent, "b", "docs", "ssot.yaml"), MANIFEST)
            write(os.path.join(parent, "b", "docs", "a.md"), "Price: 4\n")
            write(os.path.join(parent, "b", "docs", "b.md"), "Only 4\n")
            fpath = os.path.join(parent, "fleet.yaml")
            write(fpath, "repos:\n  - a\n  - path: b/docs\n"
                         "    manifest: ssot.yaml\n")
            entries = sc.load_fleet_file(fpath)
            self.assertEqual([e["path"] for e in entries],
                             [os.path.join(parent, "a"),
                              os.path.join(parent, "b", "docs")])
            r = sc.fleet(entries, env={})
            self.assertEqual(r["summary"]["ok"], 2, r)
            for bad in ("repos: []\n", "facts: []\n", "repos:\n  - path:\n"
                        "      x: 1\n",
                        # misspelled keys must not fall back to defaults
                        "repos:\n  - path: b/docs\n    manifst: ssot.yaml\n",
                        "repos:\n  - a\nrepo:\n  - b\n"):
                write(fpath, bad)
                with self.subTest(bad=bad), \
                        self.assertRaises(sc.ManifestError):
                    sc.load_fleet_file(fpath)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as parent:
            a = make_repo(parent, "a")
            b = make_repo(parent, "b", copy="5")
            out = io.StringIO()
            with unittest.mock.patch("sys.stdout", out):
                self.assertEqual(sc.main(["fleet", a]), 0)
                self.assertEqual(sc.main(["fleet", a, b]), 1)
            text = out.getvalue()
            self.assertIn("ok        a: 1 facts in sync", text)
            self.assertIn("PROBLEMS  b: 1 problem in 1 facts", text)
            self.assertIn("== b (", text)
            out = io.StringIO()
            with unittest.mock.patch("sys.stdout", out):
                self.assertEqual(sc.main(["fleet", a, b, "--json"]), 1)
            self.assertEqual(json.loads(out.getvalue())["summary"]
                             ["problems"], 1)
            err = io.StringIO()
            with unittest.mock.patch("sys.stderr", err):
                self.assertEqual(sc.main(["fleet"]), 2)
                self.assertEqual(sc.main(["fleet", a, "--repo",
                                          "nowhere=/x"]), 2)
            self.assertIn("no manifest declares", err.getvalue())

    def test_repo_override_applies_to_declaring_manifests(self):
        with tempfile.TemporaryDirectory() as parent:
            m = MANIFEST.replace("      - file: b.md\n",
                                 "      - repo: site\n        file: b.md\n")
            a = make_repo(parent, "a", manifest=m)
            site = os.path.join(parent, "elsewhere")
            write(os.path.join(site, "b.md"), "Only 4\n")
            r = sc.fleet([{"path": a}], env={})
            self.assertEqual(r["exit_code"], 1)  # ../site not found
            r = sc.fleet([{"path": a}], env={},
                         repo_overrides={"site": site})
            self.assertEqual(r["exit_code"], 0)


    def test_named_manifest_that_is_missing_is_a_config_error(self):
        # A fleet file that names a manifest (a typo here) must not pass as a
        # skipped repo; only an unnamed default .ssot.yaml may be absent.
        with tempfile.TemporaryDirectory() as parent:
            make_repo(parent, "a")
            make_repo(parent, "cfg", manifest=None)
            write(os.path.join(parent, "cfg", "config", "ssot.yaml"), MANIFEST)
            fpath = os.path.join(parent, "fleet.yaml")
            write(fpath, "repos:\n  - a\n  - path: cfg\n"
                         "    manifest: config/ssot.yml\n")
            r = sc.fleet(sc.load_fleet_file(fpath), env={})
            self.assertEqual(r["exit_code"], 2)
            self.assertEqual(r["repos"][1]["status"], "config_error")
            self.assertIn("config/ssot.yml", r["repos"][1]["note"])
            write(fpath, "repos:\n  - a\n  - cfg\n")  # unnamed: a skip
            r = sc.fleet(sc.load_fleet_file(fpath), env={})
            self.assertEqual(r["exit_code"], 0)
            self.assertEqual(r["repos"][1]["status"], "no_manifest")
            # naming the default makes it required (README: Fleet)
            write(fpath, "repos:\n  - a\n  - path: cfg\n"
                         "    manifest: .ssot.yaml\n")
            r = sc.fleet(sc.load_fleet_file(fpath), env={})
            self.assertEqual(r["exit_code"], 2)
            self.assertEqual(r["repos"][1]["status"], "config_error")

    def test_a_fleet_that_checks_nothing_fails(self):
        with tempfile.TemporaryDirectory() as parent:
            p1 = make_repo(parent, "p1", manifest=None)
            p2 = make_repo(parent, "p2", manifest=None)
            r = sc.fleet([{"path": p1}, {"path": p2}], env={})
            self.assertEqual(r["exit_code"], 2)
            out = io.StringIO()
            with unittest.mock.patch("sys.stdout", out):
                self.assertEqual(sc.main(["fleet", p1, p2]), 2)
            self.assertIn("nothing was checked", out.getvalue())
            a = make_repo(parent, "a")
            self.assertEqual(sc.fleet([{"path": a}, {"path": p1}],
                                      env={})["exit_code"], 0)

    def test_cli_fleet_file_and_repo_acceptance(self):
        with tempfile.TemporaryDirectory() as parent:
            m = MANIFEST.replace("      - file: b.md\n",
                                 "      - repo: site\n        file: b.md\n")
            make_repo(parent, "a", manifest=m)
            site = os.path.join(parent, "elsewhere")
            write(os.path.join(site, "b.md"), "Only 4\n")
            fpath = os.path.join(parent, "fleet.yaml")
            write(fpath, "repos:\n  - a\n")
            out, err = io.StringIO(), io.StringIO()
            with unittest.mock.patch("sys.stdout", out), \
                    unittest.mock.patch("sys.stderr", err):
                # an accepted --repo through the CLI resolves the sibling
                self.assertEqual(sc.main(["fleet", "--fleet", fpath, "--repo",
                                          f"site={site}"]), 0)
                self.assertEqual(sc.main(["fleet", "--fleet", fpath]), 1)
                write(fpath, "repos:\n  - path: a\n    manifst: x.yaml\n")
                self.assertEqual(sc.main(["fleet", "--fleet", fpath]), 2)
                self.assertEqual(sc.main(["fleet", "--fleet",
                                          os.path.join(parent, "none.yaml")]),
                                 2)
            self.assertIn("did you mean 'manifest'", err.getvalue())

    def test_a_named_manifest_resolves_from_its_own_directory(self):
        # As `check -f config/ssot.yaml` does: file paths in the manifest are
        # relative to config/, not to the repo top.
        with tempfile.TemporaryDirectory() as parent:
            repo = make_repo(parent, "r", manifest=None)
            m = MANIFEST.replace("file: a.md", "file: ../a.md") \
                        .replace("file: b.md", "file: ../b.md")
            write(os.path.join(repo, "config", "ssot.yaml"), m)
            r = sc.fleet([{"path": repo, "manifest": "config/ssot.yaml",
                           "explicit": True}], env={})
            self.assertEqual(r["exit_code"], 0, r)
            self.assertEqual(r["summary"]["ok"], 1)
            # an absolute manifest path is used as given
            r = sc.fleet([{"path": repo, "explicit": True,
                           "manifest": os.path.join(repo, "config",
                                                    "ssot.yaml")}], env={})
            self.assertEqual(r["exit_code"], 0, r)

    def test_repo_flag_does_not_hide_a_manifest_error(self):
        with tempfile.TemporaryDirectory() as parent:
            bad = make_repo(parent, "bad", manifest="facts: 3\n")
            out, err = io.StringIO(), io.StringIO()
            with unittest.mock.patch("sys.stdout", out), \
                    unittest.mock.patch("sys.stderr", err):
                self.assertEqual(sc.main(["fleet", bad, "--repo",
                                          "site=/x"]), 2)
            self.assertNotIn("no manifest declares", err.getvalue())
            self.assertIn("ERROR", out.getvalue())


if __name__ == "__main__":
    unittest.main()
