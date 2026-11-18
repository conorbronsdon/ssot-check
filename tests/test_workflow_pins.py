"""Supply-chain guard for GitHub Actions. Run: python3 -m unittest discover tests

Every third-party step-level `uses:` in .github/workflows and .github/actions must
be pinned to a full 40-character commit SHA, with its tag in a trailing
`# vX.Y.Z` comment. Every workflow declares top-level `permissions:`, and
Dependabot watches the github-actions ecosystem on a schedule so the pins stay
current. A tag or branch ref can be moved to new code; a commit SHA cannot.

Stdlib only (no YAML parser), so it checks shape, not truth: it can't tell that a
SHA is the commit its comment names, or tell a SHA from a tag named with 40 hex
characters, and it doesn't resolve YAML anchors. The SHAs were resolved from each
action's tags with `git ls-remote` when they were written, and Dependabot
proposes updates. Comments and block-scalar bodies (`run: |`, `script: >`) are
skipped, so text that mentions `uses:` or `permissions:` neither trips nor
satisfies a check. Python 3.9 compatible, like the rest of the suite.
"""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GITHUB = ROOT / ".github"

USES_RE = re.compile(r"""^\s*(?:-\s+)?uses:\s*["']?([^\s"'#]+)["']?\s*(#.*)?$""")
# A step written as a flow mapping: `- {uses: owner/action@v4, with: ...}`.
FLOW_USES_RE = re.compile(r"""^\s*-\s*\{[^}]*?\buses:\s*["']?([^\s"'#,}]+)""")
BLOCK_SCALAR_RE = re.compile(r"""^(\s*)(?:-\s+)?[^\s#][^:#]*:\s*[|>][-+0-9]*\s*(?:#.*)?$""")
SHA_RE = re.compile(r"[0-9a-f]{40}")
VERSION_COMMENT_RE = re.compile(r"#\s*v?\d")
SHA1 = "11d5960a326750d5838078e36cf38b85af677262"


def structural_lines(yaml_text):
    """Lines that are YAML structure: not comments, not the body of a block scalar."""
    out, block_indent = [], None
    for line in yaml_text.replace("\r\n", "\n").split("\n"):
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
        m = BLOCK_SCALAR_RE.match(line)
        if m:
            block_indent = len(m.group(1))
    return out


def is_full_sha_pin(ref):
    """True only for `owner/repo[/path]@<40 hex>`: one `@`, and the whole version is the SHA."""
    name, sep, version = ref.partition("@")
    return bool(sep) and bool(name) and SHA_RE.fullmatch(version) is not None


def unpinned_uses(yaml_text):
    """Every step-level `uses:` that is not a full-SHA pin, or a pin with no version comment."""
    bad = []
    for line in structural_lines(yaml_text):
        m = USES_RE.match(line)
        flow = None if m else FLOW_USES_RE.match(line)
        if not (m or flow):
            continue
        ref = (m or flow).group(1)
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not is_full_sha_pin(ref):
            bad.append(ref)
        elif m and not VERSION_COMMENT_RE.search(m.group(2) or ""):
            bad.append("%s (no '# vX.Y.Z' comment)" % ref)
    return bad


def top_level_permissions(yaml_text):
    """True when `permissions:` is a top-level key, above `jobs:`, with a value."""
    lines = structural_lines(yaml_text)
    for i, line in enumerate(lines):
        if re.match(r"^jobs:", line):
            return False
        m = re.match(r"^permissions:[ \t]*(.*?)[ \t]*(?:#.*)?$", line)
        if not m:
            continue
        if m.group(1) not in ("", "~", "null"):
            return True  # read-all, write-all, {}, {contents: read}
        following = next((l for l in lines[i + 1:] if l.strip()), "")
        return bool(re.match(r"^[ \t]+\S", following))
    return False


def github_actions_update(text):
    """The uncommented `updates:` entry for github-actions in dependabot.yml, or None."""
    body = "\n".join(structural_lines(text))
    for entry in re.split(r"^\s*-\s+(?=package-ecosystem:)", body, flags=re.M)[1:]:
        if re.match(r"""package-ecosystem:\s*["']?github-actions["']?\s*$""", entry, re.M):
            return entry
    return None


def workflow_files(github=GITHUB):
    files = []
    for sub in ("workflows", "actions"):
        d = github / sub
        if d.is_dir():
            files += sorted(p for p in d.rglob("*") if p.suffix in (".yml", ".yaml"))
    return files


class ParserControls(unittest.TestCase):
    def test_flags_tag_short_sha_and_branch(self):
        text = (
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-node@49933ea\n"
            "        uses: 'owner/action@main'\n"
        )
        self.assertEqual(
            unpinned_uses(text),
            ["actions/checkout@v4", "actions/setup-node@49933ea", "owner/action@main"],
        )

    def test_flags_refs_that_only_end_in_a_sha(self):
        text = (
            "      - uses: actions/checkout@%s@%s # v4.4.0\n"
            "      - uses: actions/checkout@v1+%s # v4.4.0\n"
            "      - uses: actions/checkout@%sabcdef12 # v4.4.0\n"
            "      - uses: @%s # v4.4.0\n"
        ) % (SHA1, SHA1, SHA1, SHA1, SHA1)
        self.assertEqual(unpinned_uses(text), [
            "actions/checkout@%s@%s" % (SHA1, SHA1),
            "actions/checkout@v1+%s" % SHA1,
            "actions/checkout@%sabcdef12" % SHA1,
            "@%s" % SHA1,
        ])

    def test_flags_flow_mapping_steps(self):
        text = (
            "      - {uses: actions/checkout@v4}\n"
            "      - { name: x, uses: 'owner/action@main', with: {a: 1} }\n"
            "      - {uses: actions/checkout@%s}\n"
        ) % SHA1
        self.assertEqual(unpinned_uses(text), ["actions/checkout@v4", "owner/action@main"])

    def test_flags_a_pin_without_a_version_comment(self):
        self.assertEqual(unpinned_uses("      - uses: actions/checkout@%s\n" % SHA1),
                         ["actions/checkout@%s (no '# vX.Y.Z' comment)" % SHA1])

    def test_accepts_full_sha_local_and_docker(self):
        text = (
            "      - uses: actions/checkout@%s # v4.4.0\n"
            "        uses: ./\n"
            "        uses: ./.github/actions/setup\n"
            "        uses: docker://alpine:3.20\n"
            "      # uses: actions/checkout@v4 (a comment, not a step)\n"
        ) % SHA1
        self.assertEqual(unpinned_uses(text), [])

    def test_ignores_uses_inside_run_blocks(self):
        text = (
            "      - name: echo\n"
            "        run: |\n"
            "          echo 'uses: actions/checkout@v4'\n"
            "          uses: actions/checkout@v4\n"
            "      - name: next\n"
            "        uses: actions/setup-python@v5\n"
        )
        self.assertEqual(unpinned_uses(text), ["actions/setup-python@v5"])


class PermissionsControls(unittest.TestCase):
    def test_accepts_block_and_inline_values(self):
        self.assertTrue(top_level_permissions("on: push\npermissions:\n  contents: read\njobs:\n  a: {}\n"))
        self.assertTrue(top_level_permissions("permissions: read-all\njobs: {}\n"))
        self.assertTrue(top_level_permissions("permissions: {}  # no token scopes\njobs: {}\n"))

    def test_rejects_missing_commented_empty_and_lookalikes(self):
        self.assertFalse(top_level_permissions("on: push\njobs:\n  a:\n    permissions:\n      contents: read\n"))
        self.assertFalse(top_level_permissions("permissions:\njobs: {}\n"))
        self.assertFalse(top_level_permissions("# permissions:\n#   contents: read\njobs: {}\n"))
        self.assertFalse(top_level_permissions("permissions:\n#  contents: read\njobs: {}\n"))
        self.assertFalse(top_level_permissions(
            "on: push\nenv:\n  NOTE: |\n    permissions:\n      contents: read\njobs: {}\n"))


class DependabotControls(unittest.TestCase):
    def test_a_commented_out_entry_does_not_count(self):
        text = ("version: 2\nupdates:\n"
                "  # - package-ecosystem: github-actions\n"
                "  #   directory: \"/\"\n"
                "  - package-ecosystem: pip\n    directory: \"/\"\n    schedule:\n      interval: weekly\n")
        self.assertIsNone(github_actions_update(text))

    def test_another_entrys_schedule_does_not_count(self):
        text = ("version: 2\nupdates:\n"
                "  - package-ecosystem: github-actions\n    directory: \"/\"\n"
                "  - package-ecosystem: pip\n    directory: \"/\"\n    schedule:\n      interval: daily\n")
        self.assertNotRegex(github_actions_update(text), r"interval:")


class ScopeControls(unittest.TestCase):
    def test_composite_actions_are_scanned(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            action = tmp / "actions" / "setup" / "action.yml"
            action.parent.mkdir(parents=True)
            action.write_text("runs:\n  using: composite\n  steps:\n    - uses: actions/checkout@v4\n")
            files = workflow_files(tmp)
            self.assertEqual(files, [action])
            self.assertEqual(unpinned_uses(files[0].read_text()), ["actions/checkout@v4"])
        finally:
            shutil.rmtree(tmp)


class RepositoryWorkflows(unittest.TestCase):
    def test_workflows_exist(self):
        self.assertTrue(workflow_files(), "no workflow files found under .github/")

    def test_every_uses_is_sha_pinned(self):
        bad = {}
        for p in workflow_files():
            refs = unpinned_uses(p.read_text(encoding="utf-8"))
            if refs:
                bad[str(p.relative_to(ROOT))] = refs
        self.assertEqual(bad, {}, "pin these to a full commit SHA with a '# vX.Y.Z' comment")

    def test_every_workflow_sets_top_level_permissions(self):
        missing = [
            str(p.relative_to(ROOT))
            for p in sorted((GITHUB / "workflows").glob("*.y*ml"))
            if not top_level_permissions(p.read_text(encoding="utf-8"))
        ]
        self.assertEqual(missing, [])

    def test_dependabot_watches_github_actions(self):
        path = GITHUB / "dependabot.yml"
        self.assertTrue(path.is_file(), ".github/dependabot.yml is missing")
        entry = github_actions_update(path.read_text(encoding="utf-8"))
        self.assertIsNotNone(entry, "no uncommented github-actions entry in .github/dependabot.yml")
        self.assertRegex(entry, r"""(?m)^\s*directory:\s*["']?/["']?\s*$""")
        # Dependabot rejects an entry without a schedule, and then keeps no pin current.
        self.assertRegex(entry, r"(?m)^\s*schedule:\s*\n\s+interval:\s*\S+")


if __name__ == "__main__":
    unittest.main()
