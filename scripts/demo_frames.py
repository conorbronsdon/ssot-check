# Frame spec for the ssot-check demo GIF.
# Rendered by scripts/render_demo.py (canonical path: demo.tape via vhs).
#
# Output shown is REAL: captured from `python3 ssot_check.py check` run against a
# tiny two-file demo (pricing.md canonical, landing.md a copy) built in a temp
# dir. The demo files are not committed — the arc is: in sync (exit 0), edit the
# copy, re-run showing DRIFTED with file:line (exit 1).

TITLE = "ssot-check — drift auditor"

FRAMES = [
    ("out", [
        [("# One price fact, two files. pricing.md is canonical; landing.md copies it.", "dim")],
        [("# The .ssot.yaml manifest records that relationship.", "dim")],
    ], 1700),

    ("cmd", "python3 ssot_check.py check"),
    ("out", [
        "",
        [("SSOT CHECK — 2026-07-14", "cyan")],
        "",
        [("IN SYNC (1): ", "green"), ("pro-price", "fg")],
        "",
        [("1 facts checked, all in sync.", "fg")],
    ], 900),
    ("out", [
        "",
        [("$ echo $?  ", "dim"), ("0", "green")],
    ], 2300),

    ("clear",),
    ("out", [
        [("# Marketing drops the price on the landing page — and forgets pricing.md.", "dim")],
    ], 1400),
    ("cmd", "sed -i 's/\\$49/\\$39/' landing.md"),
    ("hold", 800),
    ("cmd", "python3 ssot_check.py check"),
    ("out", [
        "",
        [("SSOT CHECK — 2026-07-14", "cyan")],
        "",
        [("DRIFTED (1):", "red")],
        [("  - pro-price — canonical says '", "fg"), ("49", "green"),
         ("', copy says '", "fg"), ("39", "red"), ("'", "fg")],
        [("      canonical: ", "dim"), ("pricing.md:3", "cyan")],
        [("      copy:      ", "dim"), ("landing.md:3", "cyan")],
        "",
        [("1 facts checked, ", "fg"), ("1 drifted.", "red")],
    ], 900),
    ("out", [
        "",
        [("$ echo $?  ", "dim"), ("1", "red"), ("   # non-zero fails the pre-commit "
         "hook / CI before the drift ships", "dim")],
    ], 2600),
]
