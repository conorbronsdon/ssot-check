---
name: ssot-check
license: MIT
description: Audit copied facts, missing SSOT pointers, and disconnected setup guides. Use for documentation drift, cross-repo ownership, orphaned handoffs, or an SSOT manifest.
---

# SSOT Check

Read [the canonical skill](../../SKILL.md) completely before acting.
Resolve its resource links from the plugin root (two directories above this
file), not from the user's working directory. Keep the user's target repository
as the working directory; do not modify the installed plugin.

Where those instructions refer to the skill's installed directory, use the
plugin root in both Codex and Claude Code; a skill-directory variable set for
this adapter points here, not at the root. Use the absolute path to the root's
`ssot_check.py`, with a Python 3 interpreter. Preserve the approval
boundary for edits and the opt-in boundary for `--fetch`. The `pointers` mode is
an agent workflow, not a CLI subcommand; read its reference from the plugin root.
