#!/usr/bin/env python3
"""Release metadata for the automatic release workflow (.github/workflows/release.yml).

Merging a version bump to main is the release. This script holds every decision the
workflow makes, so each one is unit-tested (tests/test_release_meta.py):

    python3 scripts/release_meta.py plan [--tags "v0.1.3 v0.1.4"] [--github-output FILE]
    python3 scripts/release_meta.py notes VERSION [--file CHANGELOG] [--out FILE]

`plan` reads the canonical version from ssot_check.py, requires a dated
`## [X.Y.Z] — YYYY-MM-DD` changelog heading for it, and compares it with the existing
`vX.Y.Z` tags:

- tag already exists                 -> action=done (idempotent: nothing to tag)
- version is above every tag         -> action=release
- version is at or below a later tag -> refuses (exit 1)

It never decides about the GitHub release itself: the workflow checks that target
separately, so a run that tagged but failed before the release can be re-run.
Standard library only; runs on Python 3.9+.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r'^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', re.M)
HEADING_RE = re.compile(r'^## \[([0-9]+\.[0-9]+\.[0-9]+)\] [\u2014\u2013-] ([0-9]{4}-[0-9]{2}-[0-9]{2})\s*$', re.M)
TAG_RE = re.compile(r'^v([0-9]+)\.([0-9]+)\.([0-9]+)$')


class ReleaseError(Exception):
    pass


def parse_version(text):
    found = VERSION_RE.findall(text)
    if len(found) != 1:
        raise ReleaseError(f'expected exactly one __version__ = "X.Y.Z" in ssot_check.py, found {len(found)}')
    return found[0]


def key(version):
    return tuple(int(part) for part in version.split('.'))


def changelog_date(changelog, version):
    """The release date of `version`'s dated heading; refuses when it is missing."""
    for found, date in HEADING_RE.findall(changelog):
        if found == version:
            return date
    raise ReleaseError(f'CHANGELOG.md has no dated "## [{version}] — YYYY-MM-DD" heading')


def changelog_notes(changelog, version):
    """The body of `version`'s section, without its heading, trimmed."""
    lines = changelog.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match and match.group(1) == version:
            start = i + 1
            break
    if start is None:
        raise ReleaseError(f'CHANGELOG.md has no dated "## [{version}]" section')
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith('## ') or re.match(r'^\[[^\]]+\]:\s', lines[j]):
            end = j
            break
    body = '\n'.join(lines[start:end]).strip()
    if not body:
        raise ReleaseError(f'CHANGELOG.md section [{version}] is empty')
    return body + '\n'


def decide(version, tags):
    """'done' when the tag exists, 'release' when the version is new and highest; refuses otherwise."""
    existing = []
    for tag in tags:
        match = TAG_RE.match(tag.strip())
        if match:
            existing.append(tuple(int(g) for g in match.groups()))
    if key(version) in existing:
        return 'done'
    newer = [t for t in existing if t >= key(version)]
    if newer:
        highest = 'v' + '.'.join(map(str, max(newer)))
        raise ReleaseError(f'version {version} is not above existing tag {highest}; bump the version')
    return 'release'


def plan(root, tags):
    version = parse_version((root / 'ssot_check.py').read_text(encoding='utf-8'))
    changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    date = changelog_date(changelog, version)
    changelog_notes(changelog, version)    # refuses an empty section before anything is published
    return {'version': version, 'tag': f'v{version}', 'date': date, 'action': decide(version, tags)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p_plan = sub.add_parser('plan')
    p_plan.add_argument('--tags', default='', help='space- or newline-separated existing tags')
    p_plan.add_argument('--github-output', type=Path)
    p_notes = sub.add_parser('notes')
    p_notes.add_argument('version')
    p_notes.add_argument('--file', type=Path, help='changelog to read (default: CHANGELOG.md); '
                         'the workflow passes the tagged commit\'s copy')
    p_notes.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            result = plan(ROOT, args.tags.split())
            lines = [f'{k}={v}' for k, v in result.items()]
            print('\n'.join(lines))
            if args.github_output:
                with open(args.github_output, 'a', encoding='utf-8') as fh:
                    fh.write('\n'.join(lines) + '\n')
        else:
            source = args.file or (ROOT / 'CHANGELOG.md')
            text = changelog_notes(source.read_text(encoding='utf-8'), args.version)
            if args.out:
                args.out.write_text(text, encoding='utf-8')
            else:
                sys.stdout.write(text)
    except ReleaseError as exc:
        print(f'release: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
