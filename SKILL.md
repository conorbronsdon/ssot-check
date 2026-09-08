---
name: ssot-check
description: Audit documentation for copied-fact drift, missing SSOT pointers, and disconnected setup guides across repos. Use for stale facts, orphaned handoffs, unclear ownership, or an SSOT manifest.
argument-hint: "[discover|check|explain <fact>|pointers]"
---

# SSOT Check

Audit both ways documentation loses its source of truth: copied values drift,
and useful source documents become disconnected from the places readers start.
Use the mode that matches the request. Do not invent a numeric manifest for a
routing problem.

## Choose a mode

- **check** (default for a fact-drift request): compare the existing manifest's
  canonical values and copies with the CLI.
- **discover**: propose drift-prone facts for a new or expanded manifest.
- **explain NAME**: inspect one fact's canonical value and copies.
- **pointers**: trace documentation ownership and access routes using
  [the pointer audit](references/pointer-audit.md). This is an agent workflow,
  not a `ssot_check.py` subcommand. Use it even when there are no copied numbers.

The CLI performs deterministic extraction and comparison. It does not check
whether a reader can find a setup guide, whether a linked service works, or
whether an integration is configured in another checkout. A green fact check
establishes none of those things. A mixed request can need both modes.

## Execution and authorization

The CLI never edits docs, manifests, or working trees. Its only opt-in mutation
is `check --fetch` / `explain --fetch`, which runs `git fetch` in sibling repos
and updates their remote-tracking refs, `FETCH_HEAD`, and object store. It does
not pull, rebase, reset, or move their working trees or local branches.

For an audit-only request, report proposed fixes. When the user explicitly asks
to fix the docs or manifest, apply reviewable edits within that authorized scope
using the normal editing workflow; do not ask again for the same permission.
Keep changes in each owning repo, preserve unrelated work, and verify the diff.
Authorization to repair docs does not authorize credential rotation, permission
changes, messages, deployments, or changes in additional repos outside the task.

Resolve `ssot_check.py` relative to this skill's installed directory. In Claude,
`${CLAUDE_SKILL_DIR}` can provide that directory; other runtimes should use the
actual skill path. Run against the user's target repo, not the installation.
Do not edit an installed skill when its maintained source repo is available.
For the shell examples, set `SKILL_DIR` and `TARGET_REPO` to those resolved
absolute paths. Use the requested manifest path if it differs from `.ssot.yaml`.

## Discover

1. Read an existing `.ssot.yaml` before proposing additions. Preserve its curated
   scope; do not replace it with discovery output. If there is no manifest, start
   with the user's requested surfaces.
2. Run `python3 "$SKILL_DIR/ssot_check.py" discover --root "$TARGET_REPO"`.
   It scans prose for repeated numbers, amounts, percentages, versions and
   freshness wording. It writes no manifest.
3. Curate the findings. Use the source named by the repo's contract or owner
   documentation, then inspect the actual source. A marketing copy is not
   canonical unless ownership was explicitly delegated to it. Do not treat
   historical snapshots, goals, rounded summaries, or intentionally simplified
   downstream material as exact copies.
4. Draft a fact with `name`, `canonical` (`file`, one-capture-group `pattern`),
   `copies`, `type`, and a useful `note`. See the [manifest reference](README.md)
   and [example](.ssot.example.yaml). Explain any intentional rounding.
5. Apply the authorization rule above, then validate and check the manifest.

## Check and explain

```sh
python3 "$SKILL_DIR/ssot_check.py" check --manifest "$TARGET_REPO/.ssot.yaml" --root "$TARGET_REPO"
python3 "$SKILL_DIR/ssot_check.py" explain FACT --manifest "$TARGET_REPO/.ssot.yaml" --root "$TARGET_REPO"
```

`--json` produces machine-readable output. For `check`, exit `0` means tracked
facts match, `1` means drift or staleness, and `2` means a manifest/configuration
error. `explain` returns `0` for an existing valid fact even when its copies
drift; use `check` as a gate. `--root` resolves file paths; `--manifest` selects
the manifest independently of the shell working directory.
Without `--fetch`, cross-repo reads describe the local working trees. With it,
reads use the selected remote-tracking ref. Neither is automatically proof of a
fresh default branch: inspect the reported checkout/ref and fetch result.

Interpret the result before editing:

- **DRIFTED:** show the canonical and copy locations. For a monotonic count,
  `canonical suspect` means the higher copy may be fresher; verify before
  regressing it.
- **CANONICAL MOVED / STALE MANIFEST ENTRY:** inspect whether the source moved
  or the extraction rule became stale. Repair the locator, not a guessed value.
- **STALE CANONICAL:** report the freshness threshold and owner; a date alone
  does not establish that the fact is wrong.
- **UNVERIFIED:** state what could not be read or established. A failed fetch
  can leave stale refs on disk. Do not silently treat them as fresh evidence.

Read actual files, make authorized fixes in their owning repos, and re-run the
relevant checks. State what the check covers and what remains unverified.

## Resources

- [Pointer audit](references/pointer-audit.md): entry points, owners, worktrees,
  credential-location metadata, redirects and verification boundaries.
- [README](README.md): CLI, YAML subset, install and hook/Action usage.
- [Schema](schema/ssot.schema.json) and [example manifest](.ssot.example.yaml).
