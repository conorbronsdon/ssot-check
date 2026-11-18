"""Tests for hooks/pre-commit (issue #26). Run: python3 -m unittest discover tests"""

import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOOK = os.path.join(REPO, "hooks", "pre-commit")
CLI = os.path.join(REPO, "ssot_check.py")

MANIFEST = """facts:
  - name: price
    canonical:
      file: a.md
      pattern: 'Price: (\\d+)'
    copies:
      - file: b.md
        pattern: 'Only (\\d+)'
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"),
                     "needs bash and git")
class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        subprocess.run(["git", "init", "-q", self.root], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_hook(self, cli=True, **env):
        if cli:
            shutil.copy(CLI, os.path.join(self.root, "ssot_check.py"))
        e = {k: v for k, v in os.environ.items()
             if k not in ("SSOT_STRICT", "SSOT_MANIFEST")}
        # Keep git and python3 but hide any ssot-check installed on PATH.
        e["PATH"] = os.pathsep.join(
            os.path.dirname(shutil.which(t)) for t in ("git", "python3"))
        e.update(env)
        return subprocess.run(["bash", HOOK], cwd=self.root, env=e,
                              capture_output=True, text=True)

    def in_sync(self):
        self.write(".ssot.yaml", MANIFEST)
        self.write("a.md", "Price: 4\n")
        self.write("b.md", "Only 4\n")

    def test_missing_cli(self):
        self.in_sync()
        self.assertEqual(self.run_hook(cli=False).returncode, 0)
        r = self.run_hook(cli=False, SSOT_STRICT="1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found and SSOT_STRICT=1", r.stderr)

    def test_explicit_manifest_missing(self):
        self.in_sync()
        self.assertEqual(
            self.run_hook(SSOT_MANIFEST="typo.yaml").returncode, 0)
        r = self.run_hook(SSOT_MANIFEST="typo.yaml", SSOT_STRICT="1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("typo.yaml does not exist", r.stderr)

    def test_default_manifest_missing_still_passes_in_strict(self):
        self.assertEqual(self.run_hook(SSOT_STRICT="1").returncode, 0)

    def test_drift_and_manifest_error(self):
        self.in_sync()
        self.assertEqual(self.run_hook(SSOT_STRICT="1").returncode, 0)
        self.write("b.md", "Only 5\n")
        self.assertEqual(self.run_hook().returncode, 0)  # soft-fail
        r = self.run_hook(SSOT_STRICT="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("drift found", r.stderr)
        self.write(".ssot.yaml", "facts: 3\n")
        r = self.run_hook(SSOT_STRICT="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("manifest error", r.stderr)


if __name__ == "__main__":
    unittest.main()
