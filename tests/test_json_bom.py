"""A json locator reads a file that starts with a UTF-8 byte order mark.

PowerShell 5 (`Out-File -Encoding utf8`, `Set-Content -Encoding UTF8`) and
some Windows editors write one. json.loads rejects a leading BOM, so the
copy was reported as "not valid JSON" instead of being checked.
Run: python3 -m unittest tests.test_json_bom -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402

DOC = '{\n  "stats": {\n    "episodes": 82\n  }\n}\n'


class JsonBomTests(unittest.TestCase):
    def test_bom_json_is_read(self):
        # Before the fix: (None, None, 'not valid JSON (JSONDecodeError)').
        self.assertEqual(sc.extract_json("﻿" + DOC, "stats.episodes"),
                         ("82", 3, None))
        self.assertEqual(sc.extract_json("﻿" + DOC, "stats.#"),
                         ("1", 2, None))

    def test_without_bom_unchanged(self):
        self.assertEqual(sc.extract_json(DOC, "stats.episodes"),
                         ("82", 3, None))

    def test_invalid_json_still_reported(self):
        self.assertEqual(sc.extract_json("﻿{", "a"),
                         (None, None, "not valid JSON (JSONDecodeError)"))
        self.assertEqual(sc.extract_json("{﻿}", "a"),
                         (None, None, "not valid JSON (JSONDecodeError)"))

    def test_check_reads_bom_json_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".ssot.yaml"), "w",
                      encoding="utf-8") as fh:
                fh.write("facts:\n  - name: episodes\n    type: integer\n"
                         "    canonical:\n      file: data.json\n"
                         "      json: stats.episodes\n"
                         "    copies:\n      - file: README.md\n"
                         "        pattern: '(\\d+) episodes'\n")
            with open(os.path.join(tmp, "data.json"), "w",
                      encoding="utf-8-sig") as fh:
                fh.write(DOC)
            with open(os.path.join(tmp, "README.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("We have 82 episodes.\n")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = sc.main(["check", "-f",
                                os.path.join(tmp, ".ssot.yaml")])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
