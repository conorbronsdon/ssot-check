# demo.tape — canonical vhs recording script for docs/demo.gif
#
# Regenerate with vhs (https://github.com/charmbracelet/vhs):
#     vhs demo.tape
#
# vhs runs the commands in a real shell, so the IN SYNC / DRIFTED output is
# captured live. If vhs/ttyd/ffmpeg are unavailable, use the Pillow fallback
# renderer, which bakes in the same captured output:
#     python3 scripts/render_demo.py      # -> docs/demo.gif

Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1200
Set Height 440
Set Theme "Dracula"
Set Padding 20
Set TypingSpeed 45ms

# Build a tiny two-file demo: pricing.md is canonical, landing.md copies it.
Hide
Type "d=$(mktemp -d); cd $d" Enter
Type `printf '# Pricing\n\nThe Pro plan is **$49/month** billed annually.\n' > pricing.md` Enter
Type `printf '# Landing Page\n\nGet Pro for just $49/month — cancel anytime.\n' > landing.md` Enter
Type `printf 'facts:\n  - name: pro-price\n    type: currency\n    canonical:\n      file: pricing.md\n      pattern: %sPro plan is \\*\\*\\$([\\d,]+)/month%s\n    copies:\n      - file: landing.md\n        pattern: %sPro for just \\$([\\d,]+)/month%s\n' "'" "'" "'" "'" > .ssot.yaml` Enter
Type "alias ssot_check.py='python3 $OLDPWD/ssot_check.py'" Enter
Show

# --- Beat 1: everything in sync ----------------------------------------------
Type "python3 $OLDPWD/ssot_check.py check"
Enter
Sleep 500ms
Type "echo $?"
Enter
Sleep 2s

# --- Beat 2: edit the copy, forget the canonical -> drift --------------------
Type "sed -i 's/\\$49/\\$39/' landing.md"
Enter
Sleep 800ms
Type "python3 $OLDPWD/ssot_check.py check"
Enter
Sleep 500ms
Type "echo $?"
Enter
Sleep 3s
