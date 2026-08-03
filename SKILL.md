---
name: ssot-check
description: Single-source-of-truth drift auditor for docs-heavy repos; wraps a deterministic CLI. Use when asked to "check for drift," "find copies of this number," "audit docs for stale facts," or "set up an SSOT manifest."
argument-hint: "[discover|check|explain <fact>]"
---

# ssot-check — Fact-Copy Drift Auditor

Documentation-heavy repos repeat facts. A price lives in `pricing.md`, then gets
hand-copied into a media kit, a landing page, and a README. Someone updates the
price. The copies drift. This skill wraps `ssot_check.py`, a stdlib-only CLI that
records canonical locations in a `.ssot.yaml` manifest and verifies every copy on
each run.

The CLI does the deterministic work (regex extraction, comparison, exit codes).
This skill adds the judgment: helping a human curate a manifest from `discover`
output, and interpreting `check` results.

**Invocation:** model-invocable. The CLI never edits docs or the manifest, and
never modifies any working tree — the audited repo's or a sibling's. Its one
piece of state-changing behavior is the opt-in `--fetch`, which runs `git fetch`
in a sibling clone; see Check Mode. Writing `.ssot.yaml` is human-gated: propose,
wait for approval, then write. Content fixes for drift are proposed as diffs,
never auto-applied.

## When to Use

- **Discover**: first run on a repo, or after adding a doc surface (a media kit,
  a landing page, a pricing page).
- **Check**: before commits that touch docs, as a pre-commit habit, or any time a
  canonical value changed.
- **Explain**: to see one fact's canonical value and every copy at a glance.

## When NOT to Use

- A repo with no duplicated facts — nothing to drift.
- Values that legitimately differ per file (dated snapshot series, goal targets
  vs. current numbers). Those are not copies; tracking them produces false
  positives.

## Arguments

`$ARGUMENTS` selects the mode:

- `discover` → run discovery and help curate a manifest (below).
- `check` (or empty) → run `check` and interpret the report.
- `explain <fact>` → run `explain` for one fact.

Find the CLI at `${CLAUDE_SKILL_DIR}/ssot_check.py`. No command writes a file
anywhere, or modifies any working tree. The single exception to "reads only" is
`check --fetch` / `explain --fetch`, which updates a sibling repo's
remote-tracking refs; it is off unless you pass it.

## Discover Mode (proposing a manifest)

1. **Confirm there is no `.ssot.yaml` yet.** If one exists, ask whether to extend
   it or just run check mode.
2. **Run the CLI to get candidates:**
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/ssot_check.py discover --root .
   ```
   It scans prose files (md/html/txt/rst) for repeated distinctive numbers, `$`
   amounts, percentages, x.y.z versions, and "as of/currently/over N" phrasing,
   then prints LIVE DRIFT CANDIDATES, PROPOSED FACTS, and a DISCARDED count. It
   never writes the manifest.
3. **Curate with judgment the CLI can't apply.** For each proposed fact, decide
   the canonical: prefer a file the repo's `CLAUDE.md`/`README` names as source of
   truth, then an auto-generated data file, then an analytics file, then an index
   README. Marketing copies (media kits, landing pages) are almost never
   canonical unless the repo's docs explicitly delegate the surface to them. For a
   count that only grows (followers, downloads), the lowest value is the suspect —
   don't let the canonical-file heuristic override that.
4. **Draft `.ssot.yaml`.** Give each fact a kebab-case `name`, a `canonical`
   `{file, pattern}`, a `copies[]` list, and a `type` (string|integer|currency|
   semver|date). Add a `note` for counting conventions. See README.md for the
   manifest reference and `.ssot.example.yaml` for an annotated template.
5. **Lead with live drift, then present the draft. Write the manifest only after
   explicit approval.** Then offer to run check mode.

## Check Mode (every subsequent run)

1. **Run:**
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/ssot_check.py check
   ```
   Exit codes: `0` all in sync, `1` drift or staleness found, `2` manifest/config
   error. Add `--json` for machine-readable output. Add `--fetch` to compare
   cross-repo copies against the sibling's remote instead of its working tree —
   this runs `git fetch` in that repo, so it makes a network call and updates
   that repo's remote-tracking refs and `FETCH_HEAD`. It never pulls, rebases,
   or touches the sibling's working tree, and it is off by default. Without it,
   the sibling's working tree is read as-is. Either way, a value that can't be
   established is reported UNVERIFIED, not guessed.
2. **Interpret the report:**
   - **DRIFTED** — a copy no longer matches its canonical. Report the canonical
     value, the copy value, and `file:line`. A `(canonical suspect)` tag means the
     copy is numerically higher on a monotonic count — confirm the live value
     before proposing an edit that would regress the copy.
   - **CANONICAL MOVED** — the canonical pattern no longer matches. The manifest
     is stale; propose an updated pattern or path.
   - **STALE MANIFEST ENTRY** — a copy pattern no longer matches (reworded or
     removed). Propose a manifest update.
   - **STALE CANONICAL (freshness)** — the canonical file hasn't been edited
     within `max_age_days`. Ping the owner.
   - **UNVERIFIED** — a cross-repo copy couldn't be read (missing file, or
     `--fetch` against a sibling with no remote-tracking ref). Report, don't
     guess. A value labelled `fetch failed; local mirror may be stale` came
     from refs already on disk because the fetch didn't succeed — treat its
     age as unknown.
3. **Propose fixes, one fact at a time, with the exact diff. Apply only after
   approval.** Drifted copies get content edits; CANONICAL MOVED / STALE ENTRY get
   manifest edits. Never edit a sibling tree — flag the edit as landing in the
   sibling repo with its own commit and deploy path.

## Design Principles

- **One canonical, everything else is a copy.** The manifest encodes that.
- **Propose, never auto-apply.** Drift is reported with exact diffs; edits wait
  for approval.
- **Deterministic.** No fuzzy matching — a mismatch always means something is
  wrong. Ambiguity is surfaced, not silently resolved.
- **Fast enough for a pre-commit habit.** File reads and regex matches; seconds.

## Cross-references

- `README.md` — problem statement, quickstart, manifest and CLI reference, the
  YAML-subset limits, hook and Action setup.
- `.ssot.example.yaml` — annotated manifest template.
- `schema/ssot.schema.json` — formal manifest schema for editors and CI.
- `hooks/pre-commit` — sample pre-commit hook.
- `action.yml` — GitHub Action that fails a build on drift.
