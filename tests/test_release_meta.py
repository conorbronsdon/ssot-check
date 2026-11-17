"""Unit tests for scripts/release_meta.py. Run: python3 -m unittest discover tests"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

import release_meta as rm  # noqa: E402

CHANGELOG = """# Changelog

## Unreleased

## [0.2.0] — 2026-12-01

### Added

- Something new.

## [0.1.4] — 2026-11-16

### Fixed

- Something old.

[0.2.0]: https://example.invalid/v0.2.0
[0.1.4]: https://example.invalid/v0.1.4
"""


class VersionTests(unittest.TestCase):
    def test_reads_exactly_one_version(self):
        self.assertEqual(rm.parse_version('x = 1\n__version__ = "0.2.0"\n'), '0.2.0')

    def test_refuses_missing_or_duplicate_version(self):
        for text in ('no version here\n', '__version__ = "0.2.0"\n__version__ = "0.2.1"\n',
                     '__version__ = "0.2"\n'):
            with self.subTest(text=text), self.assertRaises(rm.ReleaseError):
                rm.parse_version(text)


class ChangelogTests(unittest.TestCase):
    def test_dated_heading_is_required(self):
        self.assertEqual(rm.changelog_date(CHANGELOG, '0.2.0'), '2026-12-01')
        with self.assertRaises(rm.ReleaseError):
            rm.changelog_date(CHANGELOG, '0.3.0')
        undated = CHANGELOG.replace('## [0.2.0] — 2026-12-01', '## [0.2.0]')
        with self.assertRaises(rm.ReleaseError):
            rm.changelog_date(undated, '0.2.0')

    def test_notes_are_exactly_the_section_body(self):
        self.assertEqual(rm.changelog_notes(CHANGELOG, '0.2.0'), '### Added\n\n- Something new.\n')
        # the last section stops at the link references, not the end of the file
        self.assertEqual(rm.changelog_notes(CHANGELOG, '0.1.4'), '### Fixed\n\n- Something old.\n')

    def test_heading_dash_variants_are_accepted(self):
        for dash in ('\u2014', '\u2013', '-'):
            text = CHANGELOG.replace('## [0.2.0] \u2014 2026-12-01', '## [0.2.0] ' + dash + ' 2026-12-01')
            with self.subTest(dash=dash):
                self.assertEqual(rm.changelog_date(text, '0.2.0'), '2026-12-01')
                self.assertEqual(rm.changelog_notes(text, '0.2.0'), '### Added\n\n- Something new.\n')

    def test_notes_read_the_given_changelog_file(self):
        # the workflow passes the tagged commit's changelog, so later edits on main can't change the notes
        tmp = Path(tempfile.mkdtemp())
        tagged = tmp / 'tagged-CHANGELOG.md'
        tagged.write_text(CHANGELOG, encoding='utf-8')
        out = tmp / 'notes.md'
        r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'release_meta.py'), 'notes', '0.2.0',
                            '--file', str(tagged), '--out', str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_text(encoding='utf-8'), '### Added\n\n- Something new.\n')

    def test_empty_section_is_refused(self):
        empty = CHANGELOG.replace('### Added\n\n- Something new.\n\n', '')
        with self.assertRaises(rm.ReleaseError):
            rm.changelog_notes(empty, '0.2.0')


class DecideTests(unittest.TestCase):
    def test_existing_tag_is_done(self):
        # must-not-fire: re-running after a release, or a merge that did not bump, publishes nothing
        self.assertEqual(rm.decide('0.1.4', ['v0.1.2', 'v0.1.3', 'v0.1.4']), 'done')

    def test_new_highest_version_releases(self):
        # must-fire
        self.assertEqual(rm.decide('0.2.0', ['v0.1.3', 'v0.1.4']), 'release')
        self.assertEqual(rm.decide('0.1.10', ['v0.1.9']), 'release')    # numeric, not string, order
        self.assertEqual(rm.decide('1.0.0', []), 'release')

    def test_version_below_an_existing_tag_is_refused(self):
        with self.assertRaises(rm.ReleaseError):
            rm.decide('0.1.3', ['v0.1.4'])

    def test_unrelated_tags_are_ignored(self):
        self.assertEqual(rm.decide('0.2.0', ['v0.1.4', 'latest', 'v1', 'v0.3.0-rc1']), 'release')


class PlanTests(unittest.TestCase):
    def repo(self, version, changelog):
        tmp = Path(tempfile.mkdtemp())
        (tmp / 'ssot_check.py').write_text(f'__version__ = "{version}"\n', encoding='utf-8')
        (tmp / 'CHANGELOG.md').write_text(changelog, encoding='utf-8')
        return tmp

    def test_plan_combines_the_checks(self):
        root = self.repo('0.2.0', CHANGELOG)
        self.assertEqual(rm.plan(root, ['v0.1.4']),
                         {'version': '0.2.0', 'tag': 'v0.2.0', 'date': '2026-12-01', 'action': 'release'})

    def test_plan_refuses_a_bump_without_its_changelog_section(self):
        root = self.repo('0.3.0', CHANGELOG)
        with self.assertRaises(rm.ReleaseError):
            rm.plan(root, ['v0.2.0'])

    def test_this_repository_plans_cleanly(self):
        # the real manifest: the current version has a dated, non-empty section
        out = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'release_meta.py'), 'plan'],
                             capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('action=release', out.stdout)    # no tags passed: the current version is new to them


if __name__ == '__main__':
    unittest.main()
