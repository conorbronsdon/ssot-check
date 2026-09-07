---
name: ssot-check
license: MIT
description: Find duplicated facts and report documentation drift. Use when asked to check documentation drift or curate an SSOT manifest.
---

# SSOT Check

Read [the canonical skill](../../SKILL.md) completely before acting.
Resolve its resource links from the plugin root (two directories above this
file), not from the user's working directory. Keep the user's target repository
as the working directory; do not modify the installed plugin.

In those instructions, `${CLAUDE_SKILL_DIR}` means the plugin root in Codex.
Use the absolute path to its `ssot_check.py`, with a Python 3 interpreter.
Do not assume a Claude environment variable exists. Preserve the approval
requirement for manifest edits and the opt-in boundary for `--fetch`.
