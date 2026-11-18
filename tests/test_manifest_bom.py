"""A UTF-8 byte order mark at the start of a manifest is not part of the
first key. Windows editors and PowerShell's `Out-File -Encoding utf8` write
one; before the fix `facts` was read as '\\ufefffacts' and validation failed
with "unknown key" and "missing required key 'facts'".
Run: python3 -m unittest tests.test_manifest_bom -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402

MANIFEST = ("facts:\n"
            "  - name: price\n"
            "    type: integer\n"
            "    canonical:\n"
            "      file: a.md\n"
            "      pattern: 'Price: (\\d+)'\n"
            "    copies:\n"
            "      - file: b.md\n"
            "        pattern: 'Price: (\\d+)'\n")


class ManifestBomTests(unittest.TestCase):
    def test_bom_is_not_part_of_the_first_key(self):
        m = sc.parse_manifest("﻿" + MANIFEST)
        self.assertEqual(list(m), ["facts"])
        self.assertEqual(m.lines["facts"], 1)
        self.assertEqual(sc.validate_manifest(m), [])

    def test_without_bom_unchanged(self):
        m = sc.parse_manifest(MANIFEST)
        self.assertEqual(list(m), ["facts"])
        self.assertEqual(m["facts"][0]["canonical"]["pattern"],
                         "Price: (\\d+)")
        self.assertEqual(sc.validate_manifest(m), [])

    def test_bom_manifest_file_checks_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".ssot.yaml"), "w",
                      encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write(MANIFEST)
            for name in ("a.md", "b.md"):
                with open(os.path.join(tmp, name), "w",
                          encoding="utf-8") as fh:
                    fh.write("Price: 42\n")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = sc.main(["check", "-f",
                                os.path.join(tmp, ".ssot.yaml")])
            self.assertEqual(code, 0)

    def test_bom_alone_is_an_empty_manifest(self):
        with self.assertRaises(sc.ManifestError) as ctx:
            sc.parse_manifest("﻿\n")
        self.assertEqual(ctx.exception.message, "empty manifest")

    def test_bom_later_in_the_text_is_left_alone(self):
        m = sc.parse_manifest("note: '﻿x'\nfacts:\n")
        self.assertEqual(m["note"], "﻿x")


if __name__ == "__main__":
    unittest.main()
