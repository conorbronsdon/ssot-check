"""Seeded property and fuzz tests for the manifest parser, the typed value
normalizers, extraction and the --json output.

No dependency: each test draws its examples from random.Random(SEED). A
failure message names the seed and the example, so it reproduces with
`python3 -m unittest tests.test_properties -v`.

Scalars here are always quoted, or plain scalars that avoid the leading
characters YAML reads as structure ([ { & * ! - and friends), so these
properties do not depend on how unquoted flow-like values are treated.
"""

import contextlib
import io
import json
import os
import random
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssot_check as sc  # noqa: E402

SEED = 20261117

KEY_HEAD = "abcdefghijklmnopqrstuvwxyzé"
KEY_TAIL = KEY_HEAD + "0123456789_.-ü"
# Text inside quotes or block scalars: YAML punctuation, comment markers,
# quotes, backslashes and non-ASCII are all allowed.
QUOTED_CHARS = ("abcxyzABC019 :#-[]{}&*!|>%@`,?'\"\\/$€£¥+.=()^~"
                "éßüñ漢字✓🙂")
# Plain scalars: no quotes, no leading structure characters, no ' #' or ': '.
PLAIN_HEAD = "abcxyzABC019$€(/.=+~éß漢"
PLAIN_TAIL = PLAIN_HEAD + " -:,?&*!%@[]{}^"


def _rand_key(rng):
    return rng.choice(KEY_HEAD) + "".join(
        rng.choice(KEY_TAIL) for _ in range(rng.randint(0, 8)))


def _rand_text(rng, alphabet, lo=0, hi=12):
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(lo, hi)))


def _rand_plain(rng):
    while True:
        s = rng.choice(PLAIN_HEAD) + _rand_text(rng, PLAIN_TAIL, 0, 12)
        s = s.rstrip()
        if ": " in s or s.endswith(":") or " #" in s:
            continue
        return s


# Excluded until claude/fix-strip-comment-quotes lands: _strip_comment treats
# an apostrophe inside a plain scalar (or an escaped \" inside a double-quoted
# one) as a quote boundary, so a trailing comment is kept or the value is cut.
# Plain values here carry no quotes, and double-quoted values no '"' or '\'.
def _scalar_value(rng):
    """Return (value, yaml_text) for a single-line scalar."""
    kind = rng.random()
    if kind < 0.45:
        v = _rand_text(rng, QUOTED_CHARS)
        return v, "'" + v.replace("'", "''") + "'"
    if kind < 0.65:
        v = _rand_text(rng, QUOTED_CHARS.replace('"', "").replace("\\", ""))
        return v, '"' + v + '"'
    v = _rand_plain(rng)
    return v, v


def _block_lines(rng):
    lines = []
    for i in range(rng.randint(1, 4)):
        text = rng.choice("abcxyzé漢#:'\"-[{") + _rand_text(
            rng, QUOTED_CHARS.replace("\\", ""), 0, 10).rstrip()
        if i and rng.random() < 0.2:
            text = "  " + text  # deeper indentation is kept by '|'
        lines.append(text)
    return lines


def _gen_node(rng, depth):
    """Return a random value tree of str / None / dict / list."""
    r = rng.random()
    if depth >= 3 or r < 0.45:
        return ("scalar",) + _scalar_value(rng)
    if r < 0.52:
        return ("none",)
    if r < 0.6:
        return ("block", rng.choice("|>"), _block_lines(rng))
    if r < 0.8:
        return ("map", _gen_map(rng, depth + 1))
    items = []
    for _ in range(rng.randint(1, 4)):
        if rng.random() < 0.4:
            items.append(("map", _gen_map(rng, depth + 1)))
        elif rng.random() < 0.1:
            items.append(("none",))
        else:
            items.append(("scalar",) + _scalar_value(rng))
    return ("seq", items)


def _gen_map(rng, depth):
    out = {}
    for _ in range(rng.randint(1, 4)):
        out[_rand_key(rng)] = _gen_node(rng, depth)
    return out


def _expected(node):
    kind = node[0]
    if kind == "scalar":
        return node[1]
    if kind == "none":
        return None
    if kind == "block":
        _, style, lines = node
        return "\n".join(lines) if style == "|" else " ".join(
            ln.strip() for ln in lines)
    if kind == "map":
        return {k: _expected(v) for k, v in node[1].items()}
    return [_expected(i) for i in node[1]]


class _Writer:
    def __init__(self, rng):
        self.rng = rng
        self.lines = []

    def deco(self, text):
        """Trailing whitespace and comments after a non-block line."""
        rng = self.rng
        if rng.random() < 0.2:
            text += " " * rng.randint(1, 3)
        if rng.random() < 0.2:
            text += " " * rng.randint(1, 2) + "# " + _rand_text(
                rng, QUOTED_CHARS, 0, 8)
        return text

    def noise(self, indent):
        rng = self.rng
        if rng.random() < 0.15:
            self.lines.append("")
        if rng.random() < 0.15:
            self.lines.append(" " * rng.choice([0, indent]) + "# " +
                              _rand_text(rng, QUOTED_CHARS, 0, 8))

    def value(self, prefix, node, indent):
        kind = node[0]
        if kind == "scalar":
            self.lines.append(self.deco(prefix + " " + node[2]))
        elif kind == "none":
            self.lines.append(self.deco(prefix))
        elif kind == "block":
            self.lines.append(prefix + " " + node[1])
            for ln in node[2]:
                self.lines.append(" " * (indent + 2) + ln)
        elif kind == "map":
            self.lines.append(self.deco(prefix))
            self.mapping(node[1], indent + 2)
        else:
            self.lines.append(self.deco(prefix))
            self.seq(node[1], indent + 2)

    def mapping(self, mapping, indent, first_prefix=None):
        for i, (key, node) in enumerate(mapping.items()):
            if i or first_prefix is None:
                self.noise(indent)
                lead = " " * indent
            else:
                lead = first_prefix
            self.value(lead + key + ":", node, indent)

    def seq(self, items, indent):
        for node in items:
            self.noise(indent)
            if node[0] == "map":
                self.mapping(node[1], indent + 2, " " * indent + "- ")
            elif node[0] == "none":
                self.lines.append(self.deco(" " * indent + "-"))
            else:
                self.lines.append(self.deco(" " * indent + "- " + node[2]))


def _serialize(rng, tree):
    w = _Writer(rng)
    w.mapping(tree, 0)
    return "\n".join(w.lines) + ("\n" if rng.random() < 0.8 else "")


def _has_block(tree):
    if isinstance(tree, dict):
        return any(_has_block(v) for v in tree.values())
    if isinstance(tree, tuple):
        if tree[0] == "block":
            return True
        if tree[0] == "map":
            return _has_block(tree[1])
        if tree[0] == "seq":
            return any(_has_block(i) for i in tree[1])
    return False


def _check_lines(test, node, lines, ctx):
    """Every key's recorded line holds that key."""
    if isinstance(node, dict):
        test.assertIsInstance(node, sc.LineDict, ctx)
        for key, child in node.items():
            ln = node.lines[key]
            test.assertRegex(lines[ln - 1],
                             r"^\s*(?:-\s+)?" + re.escape(key) + ":", ctx)
            _check_lines(test, child, lines, ctx)
    elif isinstance(node, list):
        for child in node:
            _check_lines(test, child, lines, ctx)


class ManifestRoundTripProperties(unittest.TestCase):
    N = 300

    def test_parse_of_serialized_tree_round_trips(self):
        rng = random.Random(SEED)
        for i in range(self.N):
            tree = _gen_map(rng, 0)
            text = _serialize(rng, tree)
            ctx = f"seed={SEED} example={i}\n{text}"
            parsed = sc.parse_manifest(text)
            self.assertEqual(parsed, _expected(("map", tree)), ctx)
            _check_lines(self, parsed, text.split("\n"), ctx)

    def test_crlf_and_trailing_blank_lines_parse_the_same(self):
        rng = random.Random(SEED + 1)
        for i in range(self.N):
            tree = _gen_map(rng, 0)
            text = _serialize(rng, tree)
            want = _expected(("map", tree))
            ctx = f"seed={SEED + 1} example={i}\n{text}"
            # Trailing blank and whitespace-only lines change nothing.
            self.assertEqual(sc.parse_manifest(text + "\n  \n\n"), want, ctx)
            # A CRLF manifest read through load_manifest (the CLI path).
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, ".ssot.yaml")
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text.replace("\n", "\r\n"))
                self.assertEqual(sc.load_manifest(path), want, ctx)
            # Passed straight to parse_manifest, CRLF parses the same unless
            # a literal block keeps its '\r' (see the report: only
            # load_manifest calls parse_manifest, and it reads in text mode).
            if not _has_block(tree):
                self.assertEqual(
                    sc.parse_manifest(text.replace("\n", "\r\n")), want, ctx)

    def test_quoted_scalar_round_trip(self):
        rng = random.Random(SEED + 2)
        for i in range(2000):
            v, text = _scalar_value(rng)
            ctx = f"seed={SEED + 2} example={i} text={text!r}"
            self.assertEqual(sc.parse_manifest("k: " + text + "\n")["k"],
                             v, ctx)
            self.assertEqual(sc.parse_manifest("s:\n  - " + text)["s"],
                             [v], ctx)

    def test_strip_comment_keeps_quoted_hash_and_indentation(self):
        rng = random.Random(SEED + 3)
        for i in range(2000):
            v, text = _scalar_value(rng)
            indent = " " * rng.randint(0, 6)
            line = indent + "k: " + text
            ctx = f"seed={SEED + 3} example={i} line={line!r}"
            self.assertEqual(sc._strip_comment(line + "  # c # d"), line, ctx)
            self.assertEqual(sc._strip_comment(line + " \t "), line, ctx)
            once = sc._strip_comment(line)
            self.assertEqual(sc._strip_comment(once), once, ctx)


FUZZ_CHARS = ("abk: -#'\"|>\t\r\n\ufeff[]{}&*!%@`\\ é漢\x00\u2028")


class ManifestFuzzProperties(unittest.TestCase):
    def assert_clean(self, text, ctx):
        try:
            result = sc.parse_manifest(text)
        except sc.ManifestError:
            return
        self.assertIsInstance(result, dict, ctx)
        # validate never raises on anything the parser returns.
        self.assertIsInstance(sc.validate_manifest(result), list, ctx)

    def test_empty_and_blank_text_is_a_manifest_error(self):
        for text in ("", "\n", "   ", "\r\n\r\n", " \t \n", "# only\n",
                     "\n# a\n  # b\n"):
            with self.subTest(text=text):
                with self.assertRaises(sc.ManifestError):
                    sc.parse_manifest(text)

    def test_random_text_never_raises_another_exception(self):
        rng = random.Random(SEED + 4)
        for i in range(3000):
            text = _rand_text(rng, FUZZ_CHARS, 0, 40)
            self.assert_clean(text, f"seed={SEED + 4} example={i} {text!r}")

    def test_mutated_manifests_never_raise_another_exception(self):
        rng = random.Random(SEED + 5)
        for i in range(600):
            lines = _serialize(rng, _gen_map(rng, 0)).split("\n")
            for _ in range(rng.randint(1, 3)):
                j = rng.randrange(len(lines))
                op = rng.randrange(6)
                if op == 0:
                    del lines[j]
                elif op == 1:
                    lines.insert(j, lines[j])
                elif op == 2:
                    lines[j] = " " * rng.randint(1, 3) + lines[j]
                elif op == 3:
                    lines[j] = lines[j].lstrip(" ")
                elif op == 4:
                    lines[j] = "\t" + lines[j]
                else:
                    k = rng.randint(0, len(lines[j]))
                    lines[j] = lines[j][:k] + rng.choice(FUZZ_CHARS) + \
                        lines[j][k:]
                if not lines:
                    lines = [""]
            text = "\n".join(lines)
            if rng.random() < 0.3:
                text = text.replace("\n", "\r\n")
            if rng.random() < 0.2:
                text = "\ufeff" + text
            self.assert_clean(text, f"seed={SEED + 5} example={i}\n{text}")

    # Tab indentation is not covered here: _cur's tab check never fires, so a
    # tab-indented line parses at the wrong level (claude/fix-tab-indentation).


# --------------------------------------------------------------------------- #
# Typed normalizers
# --------------------------------------------------------------------------- #
def _sep_digits(rng, digits, sep):
    out = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return sep.join(out)


def _spell_int(rng, n):
    body = _sep_digits(rng, str(abs(n)), rng.choice(["", ",", "_", " "]))
    s = ("-" if n < 0 else "") + body
    if rng.random() < 0.3:
        s += rng.choice(["+", " +", "+ "])
    return rng.choice(["", " ", "\t"]) + s + rng.choice(["", " "])


def _spell_currency(rng, cents):
    whole, frac = divmod(cents, 100)
    body = _sep_digits(rng, str(whole), rng.choice(["", ",", "_"]))
    style = rng.randrange(3)
    if style == 1 and frac == 0:
        body += ".00"
    elif style != 0 or frac:
        body += f".{frac:02d}" if frac % 10 or rng.random() < 0.5 \
            else f".{frac // 10}"
    sym = rng.choice(["", "$", "€", "£", "¥", "$ "])
    return sym + body + ("+" if rng.random() < 0.2 else "")


class NormalizerProperties(unittest.TestCase):
    TYPES = ("string", "integer", "currency", "semver", "date")

    def test_integer_spellings_normalize_equal_and_idempotent(self):
        rng = random.Random(SEED + 10)
        for i in range(2000):
            n = rng.choice([0, rng.randint(-10**4, 10**4),
                            rng.randint(-10**15, 10**15)])
            a, b = _spell_int(rng, n), _spell_int(rng, n)
            ctx = f"seed={SEED + 10} example={i} {a!r} {b!r}"
            na = sc.normalize_value(a, "integer")
            self.assertEqual(na, str(n), ctx)
            self.assertEqual(sc.normalize_value(na, "integer"), na, ctx)
            self.assertEqual(sc.values_match(a, b, "integer"), (True, ""), ctx)
            if n:
                self.assertFalse(
                    sc.values_match(a, _spell_int(rng, n + 1),
                                    "integer")[0], ctx)

    def test_currency_spellings_normalize_equal_and_idempotent(self):
        # Kept below 2**53 cents: amounts are compared as floats (report).
        rng = random.Random(SEED + 11)
        for i in range(2000):
            cents = rng.choice([0, rng.randint(1, 10**6),
                                rng.randint(1, 10**13)])
            a, b = _spell_currency(rng, cents), _spell_currency(rng, cents)
            ctx = f"seed={SEED + 11} example={i} {a!r} {b!r}"
            na = sc.normalize_value(a, "currency")
            self.assertEqual(sc.normalize_value(na, "currency"), na, ctx)
            self.assertEqual(na, sc.normalize_value(b, "currency"), ctx)
            self.assertEqual(float(na), cents / 100, ctx)
            self.assertFalse(sc.values_match(
                a, _spell_currency(rng, cents + 1), "currency")[0], ctx)

    def test_semver_normalize_idempotent_and_prefix_insensitive(self):
        rng = random.Random(SEED + 12)
        for i in range(1000):
            core = ".".join(str(rng.randint(0, 120))
                            for _ in range(rng.randint(1, 4)))
            ctx = f"seed={SEED + 12} example={i} {core!r}"
            n = sc.normalize_value(rng.choice(["", "v", "V"]) + core,
                                   "semver")
            self.assertEqual(n, core, ctx)
            self.assertEqual(sc.normalize_value(n, "semver"), n, ctx)

    def test_date_formats_normalize_to_iso_and_idempotent(self):
        rng = random.Random(SEED + 13)
        start = date(1900, 1, 1)
        for i in range(1500):
            d = start + timedelta(days=rng.randint(0, 200 * 366))
            fmt = rng.choice(sc._DATE_FORMATS)
            if fmt == "%d/%m/%Y" and d.day <= 12:
                fmt = "%m/%d/%Y"  # ambiguous: m/d is tried first, by design
            text = d.strftime(fmt)
            ctx = f"seed={SEED + 13} example={i} {fmt} {text!r}"
            n = sc.normalize_value(text, "date")
            self.assertEqual(n, d.isoformat(), ctx)
            self.assertEqual(sc.normalize_value(n, "date"), n, ctx)

    def test_normalize_raises_only_value_error_on_junk(self):
        rng = random.Random(SEED + 14)
        alphabet = "0123456789,._ +-$€vVxX*/:aZ\t\u00a0é"
        for i in range(3000):
            s = _rand_text(rng, alphabet, 0, 14)
            for t in self.TYPES:
                ctx = f"seed={SEED + 14} example={i} {t} {s!r}"
                try:
                    n = sc.normalize_value(s, t)
                except ValueError:
                    continue
                self.assertIsInstance(n, str, ctx)
                if t != "currency" and not (t == "string" and
                                            n.endswith("+")):
                    # Currency is excluded: very small or large amounts come
                    # back in float exponent form, which does not re-parse
                    # (report). Generated amounts above stay idempotent.
                    # A string with two trailing '+' loses one per pass.
                    self.assertEqual(sc.normalize_value(n, t), n, ctx)
                self.assertEqual(sc.values_match(s, s, t), (True, ""), ctx)

    def test_format_like_writes_a_matching_value_in_the_copy_style(self):
        rng = random.Random(SEED + 15)
        for i in range(1500):
            t = rng.choice(["integer", "currency", "semver", "string"])
            if t == "integer":
                canon = _spell_int(rng, rng.randint(-10**9, 10**9))
                copy = _spell_int(rng, rng.randint(0, 10**9))
            elif t == "currency":
                canon = _spell_currency(rng, rng.randint(0, 10**10))
                copy = _spell_currency(rng, rng.randint(0, 10**10))
            elif t == "semver":
                canon = rng.choice(["", "v"]) + "1.%d.%d" % (
                    rng.randint(0, 99), rng.randint(0, 99))
                copy = rng.choice(["", "v", "V"]) + "0.9" + rng.choice(
                    ["", "+"])
            else:
                canon = _rand_text(rng, QUOTED_CHARS, 1, 10)
                copy = _rand_text(rng, QUOTED_CHARS, 1, 10)
            ctx = f"seed={SEED + 15} example={i} {t} {canon!r} {copy!r}"
            out = sc.format_like(canon, copy, t)
            self.assertIsNotNone(out, ctx)
            if t != "string":
                self.assertEqual(sc.values_match(canon, out, t), (True, ""),
                                 ctx)
            # Applying the copy's style twice changes nothing.
            self.assertEqual(sc.format_like(canon, out, t), out, ctx)

    def test_floor_accepts_the_canonical_itself(self):
        rng = random.Random(SEED + 16)
        for i in range(1000):
            n = rng.randint(0, 10**9)
            a, b = _spell_int(rng, n), _spell_int(rng, n)
            ctx = f"seed={SEED + 16} example={i} {a!r} {b!r}"
            self.assertEqual(sc.compare_floor(a, b), (True, None, ""), ctx)
            if n:
                self.assertEqual(sc.compare_floor(a, str(n + 1))[1],
                                 "overstated", ctx)


# --------------------------------------------------------------------------- #
# Extraction, json paths and globs
# --------------------------------------------------------------------------- #
JSON_KEY_CHARS = "abcxyz_é漢 -"


class ExtractionProperties(unittest.TestCase):
    def test_regex_extract_finds_value_and_line(self):
        rng = random.Random(SEED + 20)
        for i in range(800):
            lines = [_rand_text(rng, "abc xyz:é漢.,#", 0, 20)
                     for _ in range(rng.randint(0, 8))]
            value = str(rng.randint(0, 10**9))
            at = rng.randint(0, len(lines))
            lines.insert(at, "Price: " + value + " " + _rand_text(rng, "ab ."))
            nl = rng.choice(["\n", "\r\n"])
            content = nl.join(lines)
            ctx = f"seed={SEED + 20} example={i}"
            self.assertEqual(sc.extract(content, r"Price: (\d+)"),
                             (value, at + 1, 1), ctx)

    def test_json_extract_reads_every_generated_path(self):
        rng = random.Random(SEED + 21)

        def gen(depth):
            r = rng.random()
            if depth > 3 or r < 0.4:
                return rng.choice([rng.randint(-10**6, 10**6),
                                   rng.random() * 1000, True, False,
                                   _rand_text(rng, QUOTED_CHARS)])
            if r < 0.7:
                return {_rand_text(rng, JSON_KEY_CHARS, 1, 6): gen(depth + 1)
                        for _ in range(rng.randint(1, 4))}
            return [gen(depth + 1) for _ in range(rng.randint(0, 4))]

        def paths(node, prefix):
            if isinstance(node, dict):
                yield prefix + ["#"], len(node)
                for k, v in node.items():
                    yield from paths(v, prefix + [k])
            elif isinstance(node, list):
                yield prefix + ["#"], len(node)
                for j, v in enumerate(node):
                    yield from paths(v, prefix + [str(j)])
            else:
                yield prefix, node

        for i in range(300):
            data = {"root": gen(0)}  # a path cannot be '#' alone
            content = json.dumps(data, indent=rng.choice([None, 2]),
                                 ensure_ascii=rng.random() < 0.5)
            for segs, want in paths(data["root"], ["root"]):
                path = ".".join(segs)
                ctx = f"seed={SEED + 21} example={i} path={path!r}"
                value, line, err = sc.extract_json(content, path)
                self.assertIsNone(err, ctx)
                self.assertEqual(value, sc._json_scalar(want), ctx)
                if line is not None:
                    self.assertTrue(1 <= line <= content.count("\n") + 1, ctx)

    def test_parse_json_path_splits_or_raises_value_error(self):
        rng = random.Random(SEED + 22)
        for i in range(3000):
            path = _rand_text(rng, "ab.#0 é", 0, 10)
            try:
                segs = sc.parse_json_path(path)
            except ValueError:
                continue
            ctx = f"seed={SEED + 22} example={i} {path!r}"
            self.assertEqual(".".join(segs), path, ctx)
            self.assertTrue(all(segs), ctx)

    def test_value_regex_matches_whole_values_only(self):
        rng = random.Random(SEED + 23)
        for i in range(1500):
            n = rng.randint(0, 10**7)
            value = _sep_digits(rng, str(n), rng.choice(["", ","])) + \
                rng.choice(["", "K", "+", "%"])
            rx = sc.value_regex(value)
            ctx = f"seed={SEED + 23} example={i} {value!r}"
            for ok in (value, f"about {value}.", f"(${value}),",
                       f"{value}, then"):
                self.assertTrue(rx.search(ok), ctx + " " + ok)
            self.assertIsNone(rx.search("1" + value), ctx)
            if value[-1].isdigit():
                self.assertIsNone(rx.search(value + "1"), ctx)
                self.assertIsNone(rx.search(value + ".5"), ctx)

    def test_path_glob_double_star_also_matches_root(self):
        rng = random.Random(SEED + 24)
        for i in range(1000):
            name = _rand_text(rng, "abé-_", 1, 6) + rng.choice(
                [".md", ".txt", ".rst"])
            dirs = [_rand_text(rng, "abé", 1, 4)
                    for _ in range(rng.randint(0, 3))]
            rel = "/".join(dirs + [name])
            ext = name.rsplit(".", 1)[1]
            ctx = f"seed={SEED + 24} example={i} {rel!r}"
            self.assertTrue(sc._path_glob(rel, "**/*." + ext), ctx)
            self.assertFalse(sc._path_glob(rel, "**/*.nope"), ctx)
            if dirs:
                self.assertTrue(sc._path_glob(rel, dirs[0] + "/"), ctx)
            self.assertEqual(sc._matches_any_glob(rel, ["*." + ext]), True,
                             ctx)


# --------------------------------------------------------------------------- #
# --json output
# --------------------------------------------------------------------------- #
class JsonOutputProperties(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = sc.main(argv)
        return code, out.getvalue()

    def test_check_explain_discover_json_round_trip(self):
        rng = random.Random(SEED + 30)
        for i in range(25):
            with tempfile.TemporaryDirectory() as tmp:
                facts, names = [], []
                for f in range(rng.randint(1, 4)):
                    name = f"fact-{f}"
                    names.append(name)
                    label = "Zählung" + str(f) if rng.random() < 0.5 \
                        else "count" + str(f)
                    canon = rng.randint(0, 10**6)
                    with open(os.path.join(tmp, f"c{f}.md"), "w",
                              encoding="utf-8") as fh:
                        fh.write(f"# Notes ✓\n{label}: {canon:,} users\n")
                    copies = []
                    for c in range(rng.randint(1, 3)):
                        val = canon if rng.random() < 0.6 else \
                            rng.randint(0, 10**6)
                        body = f"{label}: {val} users\n" \
                            if rng.random() < 0.85 else "nothing here\n"
                        with open(os.path.join(tmp, f"k{f}_{c}.md"), "w",
                                  encoding="utf-8") as fh:
                            fh.write("intro é\n" + body)
                        copies.append(
                            f"      - file: k{f}_{c}.md\n"
                            f"        pattern: '{label}: ([\\d,]+) users'\n")
                    facts.append(
                        f"  - name: {name}\n    type: integer\n"
                        f"    note: 'it''s fact {f} ✓'\n    canonical:\n"
                        f"      file: c{f}.md\n"
                        f"      pattern: '{label}: ([\\d,]+) users'\n"
                        f"    copies:\n" + "".join(copies))
                path = os.path.join(tmp, ".ssot.yaml")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("facts:\n" + "".join(facts))
                ctx = f"seed={SEED + 30} example={i}"
                code, out = self.run_cli(["check", "-f", path, "--json"])
                data = json.loads(out)
                self.assertEqual(code, data["exit_code"], ctx)
                self.assertIn(code, (0, 1), ctx)
                # Re-serializing the parsed output is stable.
                self.assertEqual(json.loads(json.dumps(data)), data, ctx)
                direct = sc.check(tmp, sc.load_manifest(path),
                                  manifest_path=path)
                self.assertEqual(json.loads(json.dumps(direct)), data, ctx)
                for name in names:
                    code, out = self.run_cli(
                        ["explain", name, "-f", path, "--json"])
                    self.assertIsInstance(json.loads(out), dict, ctx)
                code, out = self.run_cli(["discover", "--root", tmp,
                                          "--json"])
                self.assertEqual(code, 0, ctx)
                self.assertIsInstance(json.loads(out), dict, ctx)


if __name__ == "__main__":
    unittest.main()
