# Changelog

All notable changes to ssot-check are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the project uses
[semantic versioning](https://semver.org/).

## [0.1.1] — 2026-08-03

No behavior change to `--fetch`. This release fixes documentation that
described the tool's write surface inaccurately, plus one ref-resolution bug.

### Fixed

- **Docs claimed a read-only guarantee the tool doesn't make.** `SKILL.md` said
  "All commands are read-only", `--fetch`'s own help said "read-only; never
  pulls", and the call site carried a `# read-only; safe` comment — while
  `--fetch` runs `git fetch` in the sibling repo, which makes a network call and
  updates that repo's remote-tracking refs and `FETCH_HEAD`. The flag's behavior
  is correct and off by default; the sentences describing it were wrong. They
  now state the boundary precisely: no file is ever written and no working tree,
  index, `HEAD`, or local branch is ever moved, and `--fetch` is the one opt-in
  exception. Same correction in the README, `.ssot.example.yaml`, and
  `action.yml`.
- **`origin/HEAD` was assumed to exist.** `git clone` writes
  `refs/remotes/origin/HEAD`, but `git remote add` + `git fetch` does not.
  Hardcoding it made `--fetch` fail against that repo shape and report
  UNVERIFIED even though a usable `origin/main` was present. The ref is now
  resolved (`branch@{upstream}`, else `origin/HEAD`) and verified to exist
  before use.

### Added

- Cross-repo values carry their ref and tip date — `remote origin/main @
  2026-07-28`. When the fetch itself fails (offline, auth, no remote), the value
  still comes from refs on disk and is labelled `fetch failed; local mirror may
  be stale`, so a stale mirror can't be mistaken for a fresh one.
- Tests pinning the write boundary in both directions: no `git fetch` runs
  without the flag (and no `FETCH_HEAD` appears), a `git fetch` does run with
  it, and in neither case does any pull/rebase/merge/checkout/reset run or the
  sibling's `HEAD` and working tree move. Plus: `--fetch` reads the remote ref
  rather than a drifted working tree, a sibling with no remote-tracking ref
  reports UNVERIFIED, and `--fetch`'s help text names the write.

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

[0.1.1]: https://github.com/conorbronsdon/ssot-check/releases/tag/v0.1.1
[0.1.0]: https://github.com/conorbronsdon/ssot-check/releases/tag/v0.1.0
