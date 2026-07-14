# Changelog

All notable changes to ssot-check are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] — 2026-07-13

Initial release. The tool-backed graduation of the `ssot-check` skill: a
deterministic, stdlib-only CLI with a skill wrapper, a pre-commit hook, and a
GitHub Action.

### Added

- `ssot_check.py` — single-file CLI (Python 3.9+, standard library only):
  - `check` (default) — extract each fact's canonical value via a one-capture-
    group regex and compare every copy. Exit `0` in sync, `1` drift/staleness,
    `2` manifest/config error. `--json` for machine-readable output. `--fetch`
    to compare cross-repo copies against their remote ref (read-only).
  - `validate` — structural manifest check with line-context errors.
  - `discover` — heuristic proposal mode that scans prose for drift-prone values
    (repeated distinctive numbers, `$` amounts, percentages, x.y.z versions,
    "as of/currently/over N" phrasing). Never writes the manifest.
  - `explain NAME` — show a fact's canonical value and all copies.
  - Per-fact `type` (string|integer|currency|semver|date) with normalization,
    `monotonic` canonical-suspect direction hint, `freshness` (git last-edit
    age), `ignore_paths` globs, globbed copy paths, per-copy `rounding`
    transforms, and read-only cross-repo copy handling.
  - A small, tested YAML-subset parser (no third-party dependencies).
- `schema/ssot.schema.json` — formal JSON Schema for the manifest.
- `SKILL.md` — thin skill wrapper for AI-assisted discovery and check
  interpretation (model-invocable; the CLI is read-only, manifest writes are
  human-gated).
- `.ssot.example.yaml` — annotated manifest template.
- `hooks/pre-commit` — sample hook (soft-fail by default, hard-fail with
  `SSOT_STRICT=1`).
- `action.yml` — composite GitHub Action that fails a build on drift.
- `tests/` — stdlib `unittest` suite (parser, normalization, extraction, drift
  detection, exit codes, discover heuristics) with fixtures.
- `.github/workflows/test.yml` — CI running the suite on push and PR.

[0.1.0]: https://github.com/conorbronsdon/ssot-check/releases/tag/v0.1.0
