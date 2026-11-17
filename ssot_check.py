#!/usr/bin/env python3
"""ssot-check — single-source-of-truth drift auditor.

Deterministic, stdlib-only CLI. Reads a `.ssot.yaml` manifest that names one
canonical location per fact and every place the value is hand-copied, extracts
each value via a one-capture-group regex, and reports copies that have drifted.

Subcommands:
  check      (default) verify every copy matches its canonical value
  validate   structural check of the manifest against the documented schema
  discover   heuristic proposal mode — scan prose for drift-prone facts
  explain    show one fact's canonical value and all copies

Exit codes (check): 0 in sync, 1 drift/staleness found, 2 config/manifest error.

No third-party dependencies. The manifest is parsed by a small YAML-subset
parser (see parse_manifest); the supported subset is documented in README.md.
"""

import argparse
import difflib
import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import PurePosixPath, PureWindowsPath

__version__ = "0.1.4"

VALID_TYPES = {"string", "integer", "currency", "semver", "date"}
VALID_ROUNDING = {"floor-10", "floor-100", "floor-1000", "floor-1000-as-K"}
VALID_RELATIONS = {"equal", "floor"}
FLOOR_TYPES = {"integer", "currency"}

# The keys the manifest accepts at each level, as in schema/ssot.schema.json
# (every object there sets additionalProperties: false). A misspelled key is
# otherwise silently ignored, which turns off what it was meant to enable.
MANIFEST_KEYS = {"facts", "guard_skip_marker", "ignore_paths", "repos"}
REPO_KEYS = {"path"}
FACT_KEYS = {"name", "type", "monotonic", "note", "freshness", "canonical",
             "copies", "guard"}
FRESHNESS_KEYS = {"owner", "max_age_days"}
LOCATOR_KEYS = {"file", "pattern", "json", "repo"}
COPY_KEYS = LOCATOR_KEYS | {"rounding", "relation", "max_gap_pct", "note"}
GUARD_KEYS = {"anchors", "superseded", "restated", "also_current", "paths",
              "exempt"}
CURRENCY_SYMBOLS = "$€£¥"

# File extensions discover scans (prose only — code/data are out of scope here).
DISCOVER_EXTS = {".md", ".html", ".htm", ".txt", ".rst"}
# Directories never worth scanning.
DISCOVER_SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv",
                      "__pycache__", "dist", "build", ".mypy_cache"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ManifestError(Exception):
    """Raised for a malformed manifest. Carries an optional 1-based line no."""

    def __init__(self, message, lineno=None):
        self.lineno = lineno
        self.message = message
        super().__init__(f"line {lineno}: {message}" if lineno else message)


# --------------------------------------------------------------------------- #
# YAML-subset parser
#
# Supported subset (documented in README):
#   - block mappings  (key: value)
#   - block sequences (- item), items may be scalars or mappings
#   - nested blocks must be indented MORE than their key (strictly greater)
#   - scalars: bare, single-quoted ('' escapes a quote), double-quoted
#   - folded (>) and literal (|) block scalars
#   - full-line and trailing (# ...) comments, quote-aware
# Not supported: anchors/aliases, flow collections ({}, []), multi-doc (---),
# explicit tags, same-line-as-key sequences, complex keys.
# --------------------------------------------------------------------------- #
class LineDict(dict):
    """A dict that remembers the source line number of each key."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lines = {}


def _strip_comment(line):
    """Remove a trailing `# ...` comment, respecting quoted strings.

    A `#` only starts a comment when it is at line start or preceded by
    whitespace and not inside quotes. Leading indentation is preserved.
    """
    out = []
    quote = None
    prev = ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        else:
            if ch in "'\"":
                quote = ch
                out.append(ch)
            elif ch == "#" and (prev == "" or prev in " \t"):
                break
            else:
                out.append(ch)
        prev = ch
    return "".join(out).rstrip()


def _unquote(scalar):
    s = scalar.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


class _Parser:
    def __init__(self, text):
        # Keep raw physical lines; sequence parsing rewrites dashes in place.
        self.raw = text.split("\n")
        self.n = len(self.raw)
        self.pos = 0

    def _skip_blanks(self):
        while self.pos < self.n:
            if _strip_comment(self.raw[self.pos]).strip() == "":
                self.pos += 1
            else:
                return

    def _cur(self):
        """Return (lineno, indent, content) for the current significant line."""
        self._skip_blanks()
        if self.pos >= self.n:
            return None
        s = _strip_comment(self.raw[self.pos])
        indent = len(s) - len(s.lstrip(" "))
        if "\t" in s[:indent]:
            raise ManifestError("tab in indentation (use spaces)", self.pos + 1)
        return (self.pos + 1, indent, s.strip())

    def parse(self):
        node = self.parse_node(0)
        self._skip_blanks()
        if self.pos < self.n:
            lineno = self.pos + 1
            raise ManifestError("unexpected content after top-level block",
                                lineno)
        return node

    def parse_node(self, min_indent):
        cur = self._cur()
        if cur is None:
            return None
        _, indent, content = cur
        if indent < min_indent:
            return None
        if content == "-" or content.startswith("- "):
            return self.parse_seq(indent)
        return self.parse_map(indent)

    def parse_map(self, indent):
        d = LineDict()
        while True:
            cur = self._cur()
            if cur is None:
                break
            lineno, ind, content = cur
            if ind < indent:
                break
            if ind > indent:
                raise ManifestError("unexpected indentation", lineno)
            if content.startswith("- "):
                raise ManifestError("sequence item where a mapping key was "
                                    "expected", lineno)
            m = re.match(r"^([^:\s][^:]*?):(?:\s+(.*))?$", content)
            if not m:
                raise ManifestError(f"expected 'key: value', got {content!r}",
                                    lineno)
            key = m.group(1).strip()
            valpart = (m.group(2) or "").strip()
            if key in d:
                raise ManifestError(f"duplicate key {key!r}", lineno)
            self.pos += 1  # consume the key line
            d.lines[key] = lineno
            if valpart in (">", "|", ">-", "|-", ">+", "|+"):
                d[key] = self.parse_block_scalar(indent, valpart[0])
            elif valpart == "":
                nxt = self._cur()
                if nxt and nxt[1] > indent:
                    d[key] = self.parse_node(indent + 1)
                else:
                    d[key] = None
            else:
                d[key] = _unquote(valpart)
        return d

    def parse_seq(self, indent):
        items = []
        while True:
            cur = self._cur()
            if cur is None:
                break
            lineno, ind, content = cur
            if ind < indent or not (content == "-" or content.startswith("- ")):
                break
            if ind > indent:
                raise ManifestError("unexpected indentation in sequence",
                                    lineno)
            after = content[1:].strip()
            if after == "":
                self.pos += 1
                nxt = self._cur()
                if nxt and nxt[1] > indent:
                    items.append(self.parse_node(indent + 1))
                else:
                    items.append(None)
            elif re.match(r"^[\w.\-]+:(\s|$)", after):
                # Inline mapping start: blank out the dash and reparse the line
                # as a mapping whose first key sits just past the dash.
                line = self.raw[self.pos]
                dash_col = len(line) - len(line.lstrip(" "))
                self.raw[self.pos] = line[:dash_col] + " " + line[dash_col + 1:]
                map_indent = len(self.raw[self.pos]) - \
                    len(self.raw[self.pos].lstrip(" "))
                items.append(self.parse_map(map_indent))
            else:
                self.pos += 1
                items.append(_unquote(after))
        return items

    def parse_block_scalar(self, key_indent, style):
        """Collect lines indented past key_indent. `>` folds, `|` keeps breaks."""
        lines = []
        block_indent = None
        while self.pos < self.n:
            raw = self.raw[self.pos]
            if raw.strip() == "":
                lines.append("")
                self.pos += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= key_indent:
                break
            if block_indent is None:
                block_indent = ind
            lines.append(raw[block_indent:])
            self.pos += 1
        while lines and lines[-1] == "":
            lines.pop()
        if style == "|":
            return "\n".join(lines)
        # folded: blank lines become breaks, runs of text join with a space
        out, buf = [], []
        for ln in lines:
            if ln == "":
                if buf:
                    out.append(" ".join(buf))
                    buf = []
                out.append("")
            else:
                buf.append(ln.strip())
        if buf:
            out.append(" ".join(buf))
        return " ".join(p for p in out if p != "").strip()


def parse_manifest(text):
    """Parse manifest text into nested LineDict/list/str. Raises ManifestError."""
    # A UTF-8 byte order mark (written by some Windows editors) would
    # otherwise become part of the first key.
    if text.startswith("\ufeff"):
        text = text[1:]
    parser = _Parser(text)
    result = parser.parse()
    if result is None:
        raise ManifestError("empty manifest")
    if not isinstance(result, dict):
        raise ManifestError("top level must be a mapping with a 'facts:' key")
    return result


def load_manifest(path):
    if not os.path.isfile(path):
        raise ManifestError(f"manifest not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except UnicodeDecodeError as exc:
        raise ManifestError(f"manifest is not valid UTF-8: {exc.reason} "
                            f"at byte {exc.start}") from None
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc.strerror or exc}") \
            from None
    return parse_manifest(text)


# --------------------------------------------------------------------------- #
# Schema validation (stdlib re-implementation of schema/ssot.schema.json)
# --------------------------------------------------------------------------- #
def _non_string_values(manifest, line_of):
    """Report a mapping or list where the manifest requires a string."""
    errors = []

    def need_str(node, key, where):
        value = node.get(key)
        if isinstance(value, (dict, list)):
            ln = line_of(node, key)
            msg = f"{where}.{key}: must be a string, not a nested block"
            errors.append(f"line {ln}: {msg}" if ln else msg)

    ignore = manifest.get("ignore_paths")
    if isinstance(ignore, list) and not all(
            isinstance(g, str) for g in ignore):
        ln = line_of(manifest, "ignore_paths")
        msg = "ignore_paths: every entry must be a glob string"
        errors.append(f"line {ln}: {msg}" if ln else msg)

    need_str(manifest, "guard_skip_marker", "manifest")
    repos = manifest.get("repos")
    if isinstance(repos, dict):
        for rname, rdef in repos.items():
            if isinstance(rdef, dict):
                need_str(rdef, "path", f"repos.{rname}")

    facts = manifest.get("facts")
    for i, fact in enumerate(facts if isinstance(facts, list) else []):
        if not isinstance(fact, dict):
            continue
        where = f"facts[{i}]"
        need_str(fact, "name", where)
        need_str(fact, "type", where)
        locators = [("canonical", fact.get("canonical"))]
        copies = fact.get("copies")
        locators += [(f"copies[{j}]", cp) for j, cp in
                     enumerate(copies if isinstance(copies, list) else [])]
        for label, loc in locators:
            if isinstance(loc, dict):
                for key in ("file", "pattern", "rounding", "repo", "json",
                            "relation"):
                    need_str(loc, key, f"{where}.{label}")
    return errors


def _unknown_keys(manifest, line_of):
    """Report keys the schema does not define, naming the closest valid key."""
    errors = []

    def check(node, allowed, where):
        if not isinstance(node, dict):
            return
        for key in node:
            if key in allowed:
                continue
            close = difflib.get_close_matches(str(key), sorted(allowed), n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            ln = line_of(node, key)
            msg = (f"{where}: unknown key {key!r}{hint}; allowed: "
                   f"{', '.join(sorted(allowed))}")
            errors.append(f"line {ln}: {msg}" if ln else msg)

    check(manifest, MANIFEST_KEYS, "manifest")
    repos = manifest.get("repos")
    if isinstance(repos, dict):
        for rname, rdef in repos.items():
            check(rdef, REPO_KEYS, f"repos.{rname}")
    facts = manifest.get("facts")
    for i, fact in enumerate(facts if isinstance(facts, list) else []):
        if not isinstance(fact, dict):
            continue
        name = fact.get("name") if isinstance(fact.get("name"), str) and \
            fact.get("name") else f"facts[{i}]"
        check(fact, FACT_KEYS, name)
        check(fact.get("freshness"), FRESHNESS_KEYS, f"{name}.freshness")
        check(fact.get("canonical"), LOCATOR_KEYS, f"{name}.canonical")
        check(fact.get("guard"), GUARD_KEYS, f"{name}.guard")
        copies = fact.get("copies")
        for j, cp in enumerate(copies if isinstance(copies, list) else []):
            check(cp, COPY_KEYS, f"{name}.copies[{j}]")
    return errors


def validate_manifest(manifest):
    """Return a list of human-readable error strings ([] means valid)."""
    errors = []

    def line_of(d, key):
        return getattr(d, "lines", {}).get(key)

    def err(msg, node=None, key=None):
        ln = line_of(node, key) if node is not None and key is not None else None
        errors.append(f"line {ln}: {msg}" if ln else msg)

    if not isinstance(manifest, dict):
        return ["top level must be a mapping"]

    # The checks below (and check itself) treat these values as strings; a
    # nested block in their place would raise TypeError instead of failing
    # validation with exit 2.
    shape_errors = _non_string_values(manifest, line_of)
    if shape_errors:
        return shape_errors

    errors.extend(_unknown_keys(manifest, line_of))

    ignore = manifest.get("ignore_paths")
    if ignore is not None and not isinstance(ignore, list):
        err("ignore_paths must be a list of glob strings", manifest,
            "ignore_paths")

    marker = manifest.get("guard_skip_marker")
    if marker is not None:
        try:
            re.compile(str(marker))
        except re.error as exc:
            err(f"guard_skip_marker: invalid regex ({exc})", manifest,
                "guard_skip_marker")

    repos = manifest.get("repos")
    repo_names = set()
    if repos is not None:
        if not isinstance(repos, dict):
            err("repos must be a mapping of repo name to {path}", manifest,
                "repos")
        else:
            for rname, rdef in repos.items():
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", rname):
                    err(f"repos: name {rname!r} must be letters, digits, "
                        "'.', '_' or '-'", repos, rname)
                if rdef is not None and not isinstance(rdef, dict):
                    err(f"repos.{rname}: must be a mapping (e.g. "
                        "{path: ../name}) or empty", repos, rname)
                repo_names.add(rname)

    facts = manifest.get("facts")
    if facts is None:
        err("missing required key 'facts'", manifest, "facts")
        return errors
    if not isinstance(facts, list):
        err("'facts' must be a list", manifest, "facts")
        return errors
    if not facts:
        err("'facts' is empty — nothing to check", manifest, "facts")

    seen_names = {}
    for i, fact in enumerate(facts):
        where = f"facts[{i}]"
        if not isinstance(fact, dict):
            errors.append(f"{where}: each fact must be a mapping")
            continue
        name = fact.get("name")
        if not name:
            err(f"{where}: missing 'name'", fact, "name")
            name = where
        else:
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                err(f"{where}: name {name!r} should be kebab-case", fact, "name")
            if name in seen_names:
                err(f"duplicate fact name {name!r} (first at line "
                    f"{seen_names[name]})", fact, "name")
            seen_names[name] = line_of(fact, "name")

        ftype = fact.get("type", "string")
        if ftype not in VALID_TYPES:
            err(f"{name}: type {ftype!r} not one of {sorted(VALID_TYPES)}",
                fact, "type")

        mono = fact.get("monotonic")
        if mono is not None and str(mono).lower() not in ("true", "false"):
            err(f"{name}: monotonic must be true/false", fact, "monotonic")

        fresh = fact.get("freshness")
        if fresh is not None:
            if not isinstance(fresh, dict):
                err(f"{name}: freshness must be a mapping {{owner, max_age_days}}",
                    fact, "freshness")
            else:
                if not fresh.get("owner"):
                    err(f"{name}: freshness.owner is required", fresh, "owner")
                mad = fresh.get("max_age_days")
                if mad is None or not re.fullmatch(r"\d+", str(mad)):
                    err(f"{name}: freshness.max_age_days must be an integer",
                        fresh, "max_age_days")

        canon = fact.get("canonical")
        if not isinstance(canon, dict):
            err(f"{name}: missing 'canonical' mapping", fact, "canonical")
        else:
            _check_locator(canon, f"{name}.canonical", errors, line_of,
                           repo_names)

        guard = fact.get("guard")
        if guard is not None:
            _check_guard(guard, name, errors, line_of)

        copies = fact.get("copies")
        if copies is None and guard is None:
            err(f"{name}: missing 'copies' list (or a 'guard' block)", fact,
                "copies")
        elif copies is None:
            pass  # a guarded fact with no copies: pointer-only
        elif not isinstance(copies, list):
            err(f"{name}: 'copies' must be a list", fact, "copies")
        elif not copies and guard is None:
            err(f"{name}: 'copies' is empty — a fact with no copies cannot "
                "drift; remove it, add copies, or add a 'guard'", fact,
                "copies")
        else:
            for j, cp in enumerate(copies):
                if not isinstance(cp, dict):
                    errors.append(f"{name}.copies[{j}]: must be a mapping")
                    continue
                _check_locator(cp, f"{name}.copies[{j}]", errors, line_of,
                               repo_names)
                rnd = cp.get("rounding")
                if rnd is not None and rnd not in VALID_ROUNDING:
                    err(f"{name}.copies[{j}]: rounding {rnd!r} not one of "
                        f"{sorted(VALID_ROUNDING)}", cp, "rounding")
                rel = cp.get("relation")
                if rel is not None and rel not in VALID_RELATIONS:
                    err(f"{name}.copies[{j}]: relation {rel!r} not one of "
                        f"{sorted(VALID_RELATIONS)}", cp, "relation")
                if rel == "floor":
                    if ftype not in FLOOR_TYPES:
                        err(f"{name}.copies[{j}]: relation floor needs type "
                            f"integer or currency (fact is {ftype!r})", cp,
                            "relation")
                    if rnd is not None:
                        err(f"{name}.copies[{j}]: relation floor and "
                            "rounding cannot be combined; a floor already "
                            "allows a lower value", cp, "rounding")
                gap = cp.get("max_gap_pct")
                if gap is not None:
                    if rel != "floor":
                        err(f"{name}.copies[{j}]: max_gap_pct needs "
                            "relation: floor", cp, "max_gap_pct")
                    elif not re.fullmatch(r"\d+(?:\.\d*)?|\.\d+",
                                          str(gap)) or \
                            not 0 < float(gap) < 100:
                        err(f"{name}.copies[{j}]: max_gap_pct must be a "
                            "number between 0 and 100", cp, "max_gap_pct")
    return errors


def _check_guard(guard, name, errors, line_of):
    """Validate a fact's `guard` block (superseded / restated scans)."""
    def err(msg, node, key):
        ln = line_of(node, key)
        errors.append(f"line {ln}: {msg}" if ln else msg)

    if not isinstance(guard, dict):
        errors.append(f"{name}.guard: must be a mapping")
        return
    anchors = guard.get("anchors")
    if not isinstance(anchors, list) or not anchors or \
            not all(isinstance(a, str) and a.strip() for a in anchors):
        err(f"{name}.guard: 'anchors' must be a non-empty list of words — "
            "a value is only reported on a line that also names the fact",
            guard, "anchors")
    for key in ("superseded", "also_current", "paths", "exempt"):
        val = guard.get(key)
        if val is not None and (not isinstance(val, list) or
                                not all(isinstance(v, str) for v in val)):
            err(f"{name}.guard.{key}: must be a list of strings", guard, key)
    restated = guard.get("restated")
    if restated is not None and str(restated).lower() not in ("true",
                                                              "false"):
        err(f"{name}.guard.restated: must be true/false", guard, "restated")
    restated_on = str(restated).lower() == "true"
    if not guard.get("superseded") and not restated_on:
        err(f"{name}.guard: nothing to scan for — give 'superseded' values, "
            "'restated: true', or both", guard, "superseded")
    if guard.get("also_current") and not restated_on:
        err(f"{name}.guard.also_current: only used with 'restated: true'",
            guard, "also_current")


def _is_absolute_any_os(path):
    """True if a manifest path is absolute or drive-anchored on ANY OS.

    Manifest paths are portable input, so this must not depend on the host:
    os.path.isabs('/x') is False on Windows from Python 3.13, and
    os.path.isabs('C:/x') is False on POSIX. 'C:x' (drive-relative) and
    '\\\\server\\share' are rejected too.
    """
    return (PurePosixPath(path).is_absolute() or
            bool(PureWindowsPath(path).anchor))


def _check_locator(loc, where, errors, line_of, repo_names=()):
    """Validate a {file, pattern[, repo]} block."""
    def err(msg, key):
        ln = line_of(loc, key)
        errors.append(f"line {ln}: {msg}" if ln else msg)

    repo = loc.get("repo")
    if repo is not None:
        if repo not in repo_names:
            err(f"{where}: repo {repo!r} is not declared under top-level "
                "'repos'", "repo")
        f = str(loc.get("file") or "")
        if _is_absolute_any_os(f) or ".." in f.replace("\\", "/").split("/"):
            err(f"{where}: with 'repo', 'file' must be a path inside that "
                "repo (no absolute path, no '..')", "file")
        elif any(ch in f for ch in "*?["):
            err(f"{where}: globs are not supported in another repo", "file")

    if not loc.get("file"):
        err(f"{where}: missing 'file'", "file")
    jpath = loc.get("json")
    pat = loc.get("pattern")
    if jpath is not None and pat is not None:
        err(f"{where}: give 'pattern' or 'json', not both", "json")
        return
    if jpath is not None:
        try:
            parse_json_path(jpath)
        except ValueError as exc:
            err(f"{where}: {exc}", "json")
        return
    if not pat:
        err(f"{where}: missing 'pattern' (or 'json')", "pattern")
        return
    try:
        compiled = re.compile(pat)
    except re.error as exc:
        err(f"{where}: invalid regex ({exc})", "pattern")
        return
    if compiled.groups != 1:
        err(f"{where}: pattern must have exactly one capture group "
            f"(found {compiled.groups}) — wrap the value in parentheses",
            "pattern")


# --------------------------------------------------------------------------- #
# Value normalization & comparison
# --------------------------------------------------------------------------- #
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y", "%Y.%m.%d",
)


def _strip_trailing_plus(value):
    v = value.strip()
    return v[:-1].strip() if v.endswith("+") else v


def normalize_value(value, ftype):
    """Normalize a captured string for comparison. Raises ValueError if the
    value cannot be interpreted under the declared type."""
    v = _strip_trailing_plus(value)
    if ftype == "string":
        return v
    if ftype == "integer":
        n = re.sub(r"[,_\s]", "", v)
        if not re.fullmatch(r"-?\d+", n):
            raise ValueError(f"not an integer: {value!r}")
        return str(int(n))
    if ftype == "currency":
        n = v.lstrip(CURRENCY_SYMBOLS).strip()
        n = re.sub(r"[,_\s]", "", n)
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", n):
            raise ValueError(f"not a currency amount: {value!r}")
        f = float(n)
        return str(int(f)) if f == int(f) else repr(f)
    if ftype == "semver":
        s = v.lstrip("vV")
        if not re.fullmatch(r"\d+(?:\.\d+)*(?:\.[0-9xX*]+)?", s):
            raise ValueError(f"not a semver: {value!r}")
        return s
    if ftype == "date":
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(v, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        raise ValueError(f"unrecognized date format: {value!r}")
    raise ValueError(f"unknown type: {ftype!r}")


def _as_int(value):
    n = re.sub(r"[,_\s+]", "", value.strip().lstrip(CURRENCY_SYMBOLS))
    if not re.fullmatch(r"-?\d+", n):
        raise ValueError(f"not an integer for rounding: {value!r}")
    return int(n)


def values_match(canonical, copy, ftype="string", rounding=None):
    """Return (matched: bool, detail: str). detail describes an unparseable
    side when matched is False for a reason other than a plain mismatch."""
    if rounding:
        try:
            ci, co = _as_int(canonical), _as_int(copy)
        except ValueError as exc:
            return False, str(exc)
        if rounding == "floor-10":
            return ci - ci % 10 == co, ""
        if rounding == "floor-100":
            return ci - ci % 100 == co, ""
        if rounding == "floor-1000":
            return ci - ci % 1000 == co, ""
        if rounding == "floor-1000-as-K":
            return ci // 1000 == co, ""
        return False, f"unknown rounding {rounding!r}"
    try:
        cn = normalize_value(canonical, ftype)
        co = normalize_value(copy, ftype)
    except ValueError as exc:
        return False, str(exc)
    return cn == co, ""


_MAGNITUDE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _floor_number(value):
    """Parse a floor's number: separators, a currency symbol, a trailing '+'
    and a K/M/B magnitude suffix ('13.4K+' -> 13400). Floors are written for
    readers, so the suffix is allowed here and nowhere else."""
    v = _strip_trailing_plus(value).lstrip(CURRENCY_SYMBOLS).strip()
    v = re.sub(r"[,_\s]", "", v)
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([kKmMbB]?)", v)
    if not m:
        raise ValueError(f"not a number: {value!r}")
    n = float(m.group(1)) * _MAGNITUDE.get(m.group(2).lower(), 1)
    if n < 0:
        raise ValueError(f"floor values must be zero or positive, got "
                         f"{value!r}")
    return n


def compare_floor(canonical, copy, max_gap_pct=None):
    """Check a floor copy ('80+ episodes') against the exact canonical.

    A floor may never exceed the canonical (overstated), and, when
    max_gap_pct is set, may not trail it by more than that percentage
    (lagging). Returns (ok, direction, detail).
    """
    try:
        cn, co = _floor_number(canonical), _floor_number(copy)
    except ValueError as exc:
        return False, None, str(exc)
    if co > cn:
        return False, "overstated", (f"floor {copy.strip()!r} is above the "
                                     f"canonical {canonical.strip()!r}")
    # Both values are >= 0 here. With a zero canonical the only floor that
    # is not above it is 0, which has no gap, so the gap check needs cn > 0.
    if max_gap_pct is not None and cn > 0:
        gap = (cn - co) / cn * 100
        if gap > float(max_gap_pct):
            return False, "floor_lagging", (
                f"floor {copy.strip()!r} is {gap:.1f}% below the canonical "
                f"{canonical.strip()!r} (allowed {max_gap_pct}%)")
    return True, None, ""


# --------------------------------------------------------------------------- #
# JSON locators. `json: a.b.0.c` reads a scalar; a final `#` segment reads the
# length of the list or object it follows (`episodes.#`).
# --------------------------------------------------------------------------- #
def parse_json_path(path):
    """Split a json path into segments. Raises ValueError when malformed."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("json path is empty")
    segs = path.split(".")
    for i, seg in enumerate(segs):
        if not seg:
            raise ValueError(f"empty segment in json path {path!r}")
        if seg == "#" and i != len(segs) - 1:
            raise ValueError(f"'#' (length) must be the last segment in "
                             f"{path!r}")
        if seg != "#" and "#" in seg:
            raise ValueError(f"'#' must be its own segment in {path!r}")
    if segs == ["#"]:
        raise ValueError(f"'#' (length) needs a list or object before it, "
                         f"e.g. 'episodes.#', not {path!r}")
    return segs


def _json_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value
    return None


def extract_json(content, path):
    """Return (value, line, error). The line is approximate: where the last
    named key first appears."""
    try:
        node = json.loads(content)
    except ValueError as exc:
        return None, None, f"not valid JSON ({exc.__class__.__name__})"
    segs = parse_json_path(path)
    walked = []
    for seg in segs:
        if seg == "#":
            if not isinstance(node, (list, dict)):
                return None, None, f"'{'.'.join(walked)}' has no length"
            node = len(node)
            break
        if isinstance(node, dict) and seg in node:
            node = node[seg]
        elif isinstance(node, list) and re.fullmatch(r"\d+", seg) and \
                int(seg) < len(node):
            node = node[int(seg)]
        else:
            return None, None, f"json path not found: {path} (at {seg!r})"
        walked.append(seg)
    value = _json_scalar(node)
    if value is None:
        return None, None, f"json path {path} is a list or object, not a " \
                           "value (end it with '#' for its length)"
    line = None
    keys = [w for w in walked if not w.isdigit()]
    if keys:
        m = re.search(r'"' + re.escape(keys[-1]) + r'"\s*:', content)
        if m:
            line = content.count("\n", 0, m.start()) + 1
    return value, line, None


def extract_locator(content, loc):
    """Return (value, line, match_count, error) for a pattern or json
    locator. value is None when nothing was extracted."""
    if loc.get("json") is not None:
        value, line, error = extract_json(content, loc["json"])
        return value, line, (1 if value is not None else 0), error
    value, line, count = extract(content, loc["pattern"])
    return value, line, count, None


# --------------------------------------------------------------------------- #
# Capture-group extraction
# --------------------------------------------------------------------------- #
def _extract_details(content, pattern):
    """Return the first captured value, its location, and the match count."""
    compiled = re.compile(pattern)
    matches = list(compiled.finditer(content))
    if not matches:
        return None, None, 0, None, None
    first = matches[0]
    start, end = first.span(1)
    lineno = content.count("\n", 0, start) + 1
    return first.group(1), lineno, len(matches), start, end


def extract(content, pattern):
    """Return (value, lineno, match_count). value/lineno are None if no match.

    Uses the FIRST match; match_count > 1 signals an ambiguous pattern.
    Patterns are matched against the whole file content (not line by line).
    The line identifies the captured value, which may be below the beginning
    of a multiline match.
    """
    value, line, count, _, _ = _extract_details(content, pattern)
    return value, line, count


# --------------------------------------------------------------------------- #
# Path handling
# --------------------------------------------------------------------------- #
def _resolve(root, relpath):
    return os.path.normpath(os.path.join(root, relpath))


def is_cross_repo(root, relpath):
    """True when the path escapes the repo root (sibling clone or absolute)."""
    if os.path.isabs(relpath):
        return True
    target = os.path.abspath(_resolve(root, relpath))
    root_abs = os.path.abspath(root)
    return os.path.commonpath([target, root_abs]) != root_abs


def _matches_any_glob(relpath, globs):
    rp = relpath.replace(os.sep, "/")
    for g in globs or []:
        if fnmatch.fnmatch(rp, g) or fnmatch.fnmatch(os.path.basename(rp), g):
            return True
    return False


def _expand_copy_files(root, relpath, ignore_paths):
    """Expand a possibly-globbed copy path into concrete relative paths.

    A glob that matches no checkable file yields the glob itself, so the
    copy is reported as a missing file (stale entry) instead of vanishing
    from the check and letting the fact pass as in sync.
    """
    if any(ch in relpath for ch in "*?[") and not is_cross_repo(root, relpath):
        import glob as _glob
        hits = sorted(_glob.glob(_resolve(root, relpath), recursive=True))
        rels = [os.path.relpath(h, root).replace(os.sep, "/")
                for h in hits if os.path.isfile(h)]
        rels = [r for r in rels if not _matches_any_glob(r, ignore_paths)]
        return rels or [relpath]
    return [relpath]


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Cross-repo sibling access. Never edits a sibling; `--fetch` updates its
# remote-tracking refs and nothing else.
# --------------------------------------------------------------------------- #
def _git(args, cwd):
    try:
        out = subprocess.run(["git"] + args, cwd=cwd,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _sibling_repo_root(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    top = _git(["rev-parse", "--show-toplevel"], d)
    return top.strip() if top else None


def _remote_tracking_ref(repo_root):
    """Name of the sibling's remote-tracking ref, or None if it has none.

    Tries the checked-out branch's upstream first, then `origin/HEAD`. Don't
    assume the latter exists: `git clone` writes refs/remotes/origin/HEAD, but
    `git remote add` + `git fetch` does not, and hardcoding it made a repo with
    a perfectly good origin/main look unverifiable.
    """
    head = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo_root)
    if head:
        up = _git(["rev-parse", "--abbrev-ref",
                   f"{head.strip()}@{{upstream}}"], repo_root)
        if up and up.strip():
            return up.strip()
    origin_head = _git(["symbolic-ref", "--quiet",
                        "refs/remotes/origin/HEAD"], repo_root)
    if origin_head and origin_head.strip():
        return origin_head.strip().split("refs/remotes/")[-1]
    return None


def _working_tree_source(abspath):
    """Describe the exact sibling checkout used for a local cross-repo read.

    Cross-repo checks deliberately read the working tree unless ``--fetch`` is
    requested. Make that choice auditable: a parked feature branch or dirty
    checkout must never be presented as an unlabeled statement about the repo.
    """
    repo_root = _sibling_repo_root(abspath)
    if not repo_root:
        return "local (not a git repo)"

    branch_out = _git(["symbolic-ref", "--quiet", "--short", "HEAD"],
                      repo_root)
    branch = branch_out.strip() if branch_out and branch_out.strip() \
        else "detached HEAD"
    sha_out = _git(["rev-parse", "--short", "HEAD"], repo_root)
    sha = sha_out.strip() if sha_out and sha_out.strip() else "unknown SHA"

    warnings = []
    origin_head = _git(["symbolic-ref", "--quiet",
                        "refs/remotes/origin/HEAD"], repo_root)
    if origin_head and origin_head.strip():
        default_ref = origin_head.strip().split("refs/remotes/")[-1]
        default_branch = default_ref.split("/", 1)[-1]
        if branch != default_branch:
            warnings.append(f"non-default branch; default is {default_ref}")

    # --no-optional-locks: plain `status` refreshes stale stat data and
    # rewrites .git/index (taking index.lock); a label must not write.
    status = _git(["--no-optional-locks", "status", "--porcelain",
                   "--untracked-files=normal"], repo_root)
    if status and status.strip():
        warnings.append("dirty working tree")

    warning = f"; WARNING: {'; '.join(warnings)}" if warnings else ""
    return f"local {branch} ({sha}{warning})"


def _read_cross_repo(abspath, fetch):
    """Read a sibling file. Never modifies its working tree, index, or HEAD.

    Default (fetch=False): read the sibling's working tree as it is on disk.
    Nothing in the sibling repo is touched at all.

    With fetch=True: run `git fetch` in the sibling, then read the file out of
    its remote-tracking ref via `git show`. Be precise about what that costs —
    `git fetch` makes a network call and updates that repo's remote-tracking
    refs, FETCH_HEAD, and object store. It does not pull, rebase, merge, or
    move the working tree, index, HEAD, or any local branch. That is the whole
    of this tool's write surface, and it is opt-in.

    Either way, if the value can't be established the fact is reported
    UNVERIFIED rather than guessed.
    """
    if not fetch:
        return _read_file(abspath), _working_tree_source(abspath)
    repo_root = _sibling_repo_root(abspath)
    if not repo_root:
        return _read_file(abspath), "local (not a git repo)"
    # A failed fetch (offline, auth, no remote) leaves whatever was already on
    # disk. Track it so a stale mirror can't be labelled as freshly fetched.
    fetched = _git(["fetch", "--quiet"], repo_root) is not None
    ref = _remote_tracking_ref(repo_root)
    if not ref:
        return None, "unverified (sibling has no remote-tracking ref)"
    if _git(["rev-parse", "--verify", "--quiet", ref], repo_root) is None:
        return None, f"unverified ({ref} not present locally)"
    rel = os.path.relpath(abspath, repo_root).replace(os.sep, "/")
    content = _git(["show", f"{ref}:{rel}"], repo_root)
    if content is None:
        return None, f"unverified (could not read {ref}:{rel})"
    tip = _git(["log", "-1", "--format=%cs", ref], repo_root)
    asof = f" @ {tip.strip()}" if tip and tip.strip() else ""
    stale = "" if fetched else " (fetch failed; local mirror may be stale)"
    return content, f"remote {ref}{asof}{stale}"


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #
def _git_last_edit(abspath):
    d = os.path.dirname(abspath) or "."
    out = _git(["log", "-1", "--format=%cs", "--", abspath], d)
    if not out or not out.strip():
        return None
    try:
        return datetime.strptime(out.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def check_freshness(abspath, fresh, today=None):
    """Return a dict describing freshness, or None to skip (not in git)."""
    today = today or date.today()
    last = _git_last_edit(abspath)
    if last is None:
        return {"skipped": True,
                "note": "no git history for canonical file (skipped)"}
    age = (today - last).days
    max_age = int(fresh["max_age_days"])
    return {
        "skipped": False,
        "owner": fresh.get("owner"),
        "last_edit": last.isoformat(),
        "age_days": age,
        "max_age_days": max_age,
        "stale": age > max_age,
    }


# --------------------------------------------------------------------------- #
# Named repos. A locator may say `repo: NAME` instead of a `../NAME` path, so
# a manifest does not hardcode one machine's folder layout. Each name resolves
# per run, first match wins:
#   1. --repo NAME=PATH            explicit; used even if missing (no fallback)
#   2. $SSOT_REPO_<NAME>           explicit; used even if missing (no fallback)
#   3. $SSOT_REPOS_DIR/NAME        if that directory exists
#   4. the manifest's repos.NAME.path (default ../NAME), relative to the root
# Explicit choices never fall through: a typo in an override must not quietly
# read a different checkout. A repo found nowhere is reported REPO NOT FOUND,
# with every place that was tried.
# --------------------------------------------------------------------------- #
def _repo_env_var(name):
    return "SSOT_REPO_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def resolve_repos(root, manifest, overrides=None, env=None):
    """Return {name: {"path": abs-or-None, "via": str, "looked": [paths]}}."""
    env = os.environ if env is None else env
    overrides = overrides or {}
    out = {}
    for name, rdef in (manifest.get("repos") or {}).items():
        rdef = rdef if isinstance(rdef, dict) else {}
        default = rdef.get("path") or f"../{name}"
        default_abs = os.path.abspath(
            default if os.path.isabs(default) else _resolve(root, default))
        explicit = None
        if name in overrides:
            explicit = (overrides[name], f"--repo {name}=")
        elif env.get(_repo_env_var(name)):
            explicit = (env[_repo_env_var(name)], f"${_repo_env_var(name)}")
        if explicit:
            path = os.path.abspath(os.path.expanduser(explicit[0]))
            ok = os.path.isdir(path)
            out[name] = {"path": path if ok else None, "via": explicit[1],
                         "looked": [path]}
            continue
        looked = []
        found = None
        via = None
        base = env.get("SSOT_REPOS_DIR")
        if base:
            cand = os.path.abspath(os.path.join(os.path.expanduser(base),
                                                name))
            looked.append(cand)
            if os.path.isdir(cand):
                found, via = cand, "$SSOT_REPOS_DIR"
        if not found:
            looked.append(default_abs)
            if os.path.isdir(default_abs):
                found, via = default_abs, "manifest default"
        out[name] = {"path": found, "via": via, "looked": looked}
    return out


def _rel_under_root(root, abspath):
    """Root-relative '/' path of a resolved file inside root, else None."""
    if not abspath:
        return None
    try:
        rel = os.path.relpath(os.path.realpath(abspath),
                              os.path.realpath(root))
    except ValueError:  # another Windows drive
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or \
            os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _locate(root, loc_file, repo, repos):
    """Resolve a locator. Returns (abspath, display, cross, error)."""
    if repo:
        info = (repos or {}).get(repo)
        display = f"{repo}:{loc_file}"
        if not info or not info.get("path"):
            looked = ", ".join(info["looked"]) if info else "(undeclared)"
            return None, display, True, f"repo {repo!r} not found " \
                                         f"(looked in: {looked})"
        return (os.path.normpath(os.path.join(info["path"], loc_file)),
                display, True, None)
    abspath = (os.path.abspath(loc_file) if os.path.isabs(loc_file)
               else _resolve(root, loc_file))
    return abspath, loc_file, is_cross_repo(root, loc_file), None


# --------------------------------------------------------------------------- #
# Guards. A `guard` block scans this repo's prose for a fact's value on the
# same line as one of its anchor words:
#   STALE VALUE  a `superseded` value is still asserted somewhere
#   RESTATED     (restated: true) the current value — the canonical's value or
#                an `also_current` spelling — appears outside the canonical
#                and the declared copies. The fix is a pointer to the owner,
#                or, if the copy is legitimate, declaring it under `copies`.
# Without `restated`, a guard only polices superseded values ("mirror" use: the
# copies may agree freely, but an old value left behind by a partial update is
# caught). Guards scan the local repo only; other repos run their own manifest.
# --------------------------------------------------------------------------- #
GUARD_DEFAULT_PATHS = ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.rst"]


def value_regex(value):
    """Match a value without matching inside a longer number.

    '77K' must not match '177K'; '9/19' must not match '19/19'. A leading '$'
    must not block a match, and a trailing '.' or ',' ends a sentence rather
    than continuing the number — only '.<digit>' or ',<digit>' does. A value
    that starts or ends with a letter ('11K') must not match inside a longer
    word either ('11Kbps').
    """
    lead = r"(?<![\d,/.])"
    trail = r"(?![\d/]|[,.]\d)"
    if value[:1].isalpha():
        lead = r"(?<![\d,/.A-Za-z])"
    if value[-1:].isalpha():
        trail = r"(?![\d/A-Za-z]|[,.]\d)"
    return re.compile(lead + re.escape(value) + trail, re.IGNORECASE)


def _anchor_regexes(anchors):
    # Whole words only: a bare substring test let 'UI' match 'rebuilt'.
    return [re.compile(r"\b" + re.escape(a) + r"\b", re.IGNORECASE)
            for a in anchors]


def _path_glob(rel, pattern):
    """Glob match for guard scope. A trailing '/' means a directory prefix;
    '**/x' also matches 'x' at the root. As in fnmatch, '*' crosses '/'."""
    rel = rel.replace(os.sep, "/")
    if pattern.endswith("/"):
        return rel.startswith(pattern)
    if fnmatch.fnmatch(rel, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:])


def _repo_files(root):
    """Tracked files when root is in a git work tree, else a filtered walk.

    Run from a subdirectory of a work tree (a manifest that is not at the
    repo top, or --root pointing inside one), `git ls-files` lists only the
    tracked files under it, relative to it, so ignored build output is still
    skipped. A root with nothing tracked (an untracked or ignored folder inside
    another repo) falls back to the walk rather than scanning nothing.
    """
    out = _git(["ls-files", "-z"], root)
    top = _git(["rev-parse", "--show-toplevel"], root)
    if out is not None and top:
        tracked = [p for p in out.split("\0") if p]
        if tracked or \
                os.path.realpath(top.strip()) == os.path.realpath(root):
            return tracked
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DISCOVER_SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            files.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(files)


def scan_guard(root, fact, guard, current_values, allowed, ignore_paths,
               skip_marker=None, manifest_rel=None, files=None,
               canonical_rel=None):
    """Return a list of guard findings for one fact.

    The canonical file is skipped entirely: its value is policed by its own
    pattern, and an owner often records history ("raised from 1,152").
    """
    anchors = _anchor_regexes(guard.get("anchors") or [])
    superseded = [(v, value_regex(v)) for v in guard.get("superseded") or []]
    restated_on = str(guard.get("restated", "")).lower() == "true"
    current = [(v, value_regex(v)) for v in current_values] \
        if restated_on else []
    paths = guard.get("paths") or GUARD_DEFAULT_PATHS
    exempt = guard.get("exempt") or []
    marker = re.compile(skip_marker, re.MULTILINE) if skip_marker else None
    findings = []
    for rel in (files if files is not None else _repo_files(root)):
        if rel == manifest_rel or rel == canonical_rel:
            continue
        if not any(_path_glob(rel, p) for p in paths):
            continue
        if any(_path_glob(rel, g) for g in list(ignore_paths) + exempt):
            continue
        text = _read_file(_resolve(root, rel))
        if text is None:
            continue
        if marker and marker.search(text):
            continue
        check_restated = current and rel not in allowed
        for lineno, line in enumerate(text.splitlines(), 1):
            if not any(a.search(line) for a in anchors):
                continue
            for value, rx in superseded:
                if rx.search(line):
                    findings.append({"kind": "stale_value", "file": rel,
                                     "line": lineno, "value": value,
                                     "text": line.strip()[:160]})
                    break
            if check_restated:
                for value, rx in current:
                    if rx.search(line):
                        findings.append({"kind": "restated", "file": rel,
                                         "line": lineno, "value": value,
                                         "text": line.strip()[:160]})
                        break
    return findings


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def check(root, manifest, fetch=False, today=None, repo_overrides=None,
          env=None, manifest_path=None):
    """Run a full check. Returns a result dict (also drives --json)."""
    ignore_paths = manifest.get("ignore_paths") or []
    repos = resolve_repos(root, manifest, repo_overrides, env)
    skip_marker = manifest.get("guard_skip_marker")
    manifest_rel = None
    if manifest_path:
        try:
            manifest_rel = os.path.relpath(os.path.abspath(manifest_path),
                                           os.path.abspath(root)).replace(
                                               os.sep, "/")
        except ValueError:
            # Different Windows drives: the manifest cannot be inside root,
            # so it is never among the files a guard scans.
            manifest_rel = None
    files_cache = []
    fact_results = []
    counts = {"in_sync": 0, "drifted": 0, "canonical_moved": 0,
              "stale_entry": 0, "unverified": 0, "warnings": 0,
              "freshness_stale": 0, "repo_missing": 0, "stale_value": 0,
              "restated": 0}

    def run_guard(fact, fr, cval, allowed, canonical_rel):
        guard = fact.get("guard")
        if not guard:
            return
        if not files_cache:
            files_cache.append(_repo_files(root))
        current = []
        if cval is not None:
            current.append(cval.strip())
        current += [v for v in guard.get("also_current") or []
                    if v not in current]
        found = scan_guard(root, fact, guard, current, allowed, ignore_paths,
                           skip_marker, manifest_rel, files_cache[0],
                           canonical_rel)
        fr["guard"] = {"findings": found,
                       "restated": str(guard.get("restated", "")).lower()
                       == "true"}
        for f in found:
            counts[f["kind"]] += 1
        if found and fr["status"] == "in_sync":
            fr["status"] = found[0]["kind"]

    for fact in manifest["facts"]:
        name = fact["name"]
        ftype = fact.get("type", "string")
        monotonic = str(fact.get("monotonic", "")).lower() == "true"
        canon = fact["canonical"]
        cfile = canon["file"]
        cabs, cdisplay, cross_canon, rerr = _locate(root, cfile,
                                                    canon.get("repo"), repos)
        fr = {"name": name, "type": ftype, "copies": [],
              "canonical": {"file": cdisplay}}

        # Files a guard may find the current value in without it being a
        # stray restatement: the canonical itself and every declared copy.
        # Membership is by resolved location, so a named repo checked out
        # under the root is recognised too.
        canonical_rel = _rel_under_root(root, cabs)
        allowed = set()
        if canonical_rel:
            allowed.add(canonical_rel)

        if rerr:
            fr["status"] = "repo_missing"
            fr["canonical"]["error"] = rerr
            counts["repo_missing"] += 1
            run_guard(fact, fr, None, allowed, canonical_rel)
            fact_results.append(fr)
            continue
        if cross_canon:
            ccontent, csrc = _read_cross_repo(cabs, fetch)
        else:
            ccontent, csrc = _read_file(cabs), "local"
        fr["canonical"]["source"] = csrc

        if ccontent is None:
            fr["status"] = "canonical_moved"
            fr["canonical"]["error"] = "file missing or unreadable"
            counts["canonical_moved"] += 1
            run_guard(fact, fr, None, allowed, canonical_rel)
            fact_results.append(fr)
            continue

        cval, cline, cmulti, cerr = extract_locator(ccontent, canon)
        if cval is None:
            fr["status"] = "canonical_moved"
            fr["canonical"]["error"] = cerr or "pattern did not match"
            counts["canonical_moved"] += 1
            run_guard(fact, fr, None, allowed, canonical_rel)
            fact_results.append(fr)
            continue

        fr["canonical"].update({"value": cval, "line": cline})
        if cmulti > 1:
            fr["canonical"]["warning"] = f"pattern matched {cmulti}x; used first"
            counts["warnings"] += 1

        # Freshness (advisory + drives exit 1 when stale).
        if fact.get("freshness") and not cross_canon:
            frsh = check_freshness(cabs, fact["freshness"], today=today)
            fr["freshness"] = frsh
            if frsh and frsh.get("stale"):
                counts["freshness_stale"] += 1

        fact_status = "in_sync"
        for cp in fact.get("copies") or []:
            concretes = ([cp["file"]] if cp.get("repo") else
                         _expand_copy_files(root, cp["file"], ignore_paths))
            for concrete in concretes:
                copy_rel = _rel_under_root(
                    root, _locate(root, concrete, cp.get("repo"), repos)[0])
                if copy_rel:
                    allowed.add(copy_rel)
                res = _check_one_copy(root, concrete, cp, cval, ftype,
                                      monotonic, fetch, repos)
                fr["copies"].append(res)
                st = res["status"]
                counts[st] = counts.get(st, 0) + 1
                if st != "in_sync":
                    if st == "drifted" and fact_status == "in_sync":
                        fact_status = "drifted"
                    elif st in ("canonical_moved",):
                        pass
                    elif fact_status == "in_sync":
                        fact_status = st
        if fr.get("freshness", {}).get("stale") and fact_status == "in_sync":
            fact_status = "freshness_stale"
        fr["status"] = fact_status
        run_guard(fact, fr, cval, allowed, canonical_rel)
        fact_results.append(fr)

    problems = (counts["drifted"] + counts["canonical_moved"] +
                counts["stale_entry"] + counts["unverified"] +
                counts["freshness_stale"] + counts["repo_missing"] +
                counts["stale_value"] + counts["restated"])
    exit_code = 1 if problems else 0
    return {
        "version": __version__,
        "generated": (today or date.today()).isoformat(),
        "root": os.path.abspath(root),
        "repos": {n: {"path": r["path"], "via": r["via"],
                      "looked": r["looked"]} for n, r in repos.items()},
        "facts": fact_results,
        "summary": {
            "facts_checked": len(fact_results),
            **counts,
            "problems": problems,
        },
        "exit_code": exit_code,
    }


def _check_one_copy(root, relpath, cp, cval, ftype, monotonic, fetch,
                    repos=None):
    cabs, display, cross, rerr = _locate(root, relpath, cp.get("repo"),
                                         repos)
    res = {"file": display, "cross_repo": cross}
    if rerr:
        res["status"] = "repo_missing"
        res["note"] = rerr
        return res
    if cross:
        content, src = _read_cross_repo(cabs, fetch)
        res["source"] = src
        if content is None:
            res["status"] = "unverified"
            res["note"] = src
            return res
    else:
        content = _read_file(cabs)
        res["source"] = "local"
        if content is None:
            res["status"] = "stale_entry"
            res["note"] = "copy file missing or unreadable"
            return res

    val, line, multi, xerr = extract_locator(content, cp)
    if val is None:
        res["status"] = "stale_entry"
        res["note"] = xerr or \
            "pattern did not match (copy reworded or removed)"
        return res
    res.update({"value": val, "line": line})
    if multi > 1:
        res["warning"] = f"pattern matched {multi}x; used first"

    if cp.get("relation") == "floor":
        ok, direction, detail = compare_floor(cval, val,
                                              cp.get("max_gap_pct"))
        res["relation"] = "floor"
        if ok:
            res["status"] = "in_sync"
        else:
            res["status"] = "drifted"
            res["canonical_value"] = cval
            if direction:
                res["direction"] = direction
            if detail:
                res["note"] = detail
        return res

    rounding = cp.get("rounding")
    matched, detail = values_match(cval, val, ftype, rounding)
    if matched:
        res["status"] = "in_sync"
    else:
        res["status"] = "drifted"
        res["canonical_value"] = cval
        if detail:
            res["note"] = detail
        if monotonic and not rounding:
            try:
                if _as_int(val) > _as_int(cval):
                    res["direction"] = "canonical_suspect"
            except ValueError:
                pass
    return res


# --------------------------------------------------------------------------- #
# explain
# --------------------------------------------------------------------------- #
def explain(root, manifest, name, fetch=False, repo_overrides=None,
            env=None, manifest_path=None):
    for fact in manifest["facts"]:
        if fact["name"] == name:
            single = dict(manifest)
            single_fact = dict(fact)
            single["facts"] = [single_fact]
            return check(root, single, fetch=fetch,
                         repo_overrides=repo_overrides, env=env,
                         manifest_path=manifest_path)
    return None


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #
UNIT_NOUNS = (
    "episodes", "episode", "subscribers", "subscriber", "downloads", "download",
    "users", "user", "stars", "star", "customers", "customer", "listeners",
    "listener", "followers", "follower", "views", "view", "members", "member",
    "developers", "developer", "companies", "teams", "team", "readers",
    "reader", "installs", "install", "contributors", "contributor",
    "repositories", "repos", "commits", "requests", "queries", "sessions",
    "integrations", "integration", "seats", "seat", "organizations",
    "organizations", "projects", "project", "deployments", "deployment",
)
_UNIT_RE = "|".join(sorted(set(UNIT_NOUNS), key=len, reverse=True))

# Counted values carry their own unit noun in the match, so the unit is
# read from the capture — never from a wide context window (which would
# misattribute a nearby unrelated noun).
_COUNTED_AFTER = re.compile(
    r"\b(\d[\d,]{1,})\s?\+?\s*(" + _UNIT_RE + r")\b", re.I)
_COUNTED_BEFORE = re.compile(
    r"\b(" + _UNIT_RE + r")\b\s*[:=]?\s*(\d[\d,]{1,})\+?", re.I)

# Generic value-bearing patterns. Unit is not reliably inferable; left None.
# Version is restricted to x.y.z style to avoid catching "99.9" from a percent.
_GENERIC_PATTERNS = [
    (re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?[kKmM]?\b"), "currency"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s?%"), "percentage"),
    (re.compile(r"\bv?(\d+\.\d+\.\d+(?:\.[0-9xX]+)?)\b"), "version"),
]
_AGING_RE = re.compile(
    r"\b(?:as of|currently|to date|over|more than|at least|total of|now)\b"
    r"[^.\n]{0,60}?(\d[\d,]*(?:\.\d+)?)",
    re.I)


def _is_loopback_example(content, match):
    """Do not propose the conventional localhost address as a copied version.

    Four-component versions remain valid candidates. Even this address-shaped
    value remains discoverable when prefixed with v or explicitly labelled as
    a version, release or build immediately before the value on the same line.
    This heuristic affects discovery only; manifest checks still compare it.
    """
    if match.group(1) != "127.0.0.1" or match.group(0).startswith("v"):
        return False
    line_start = content.rfind("\n", 0, match.start()) + 1
    prefix = content[line_start:match.start()]
    return not re.search(r"(?<![a-z0-9])(?:version|release|build)[\s*_`'\":=]*$",
                         prefix, re.I)


def _norm_number(raw):
    return re.sub(r"[,\s]", "", raw).rstrip("+")


def _context(text, start, end, width=60):
    a = max(0, start - width)
    b = min(len(text), end + width)
    snippet = text[a:b].replace("\n", " ")
    return re.sub(r"\s+", " ", snippet).strip()


def _iter_prose_files(root, ignore_paths):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DISCOVER_SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in DISCOVER_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if _matches_any_glob(rel, ignore_paths):
                continue
            yield rel, full


def discover(root, ignore_paths=None):
    """Scan prose files and propose drift-prone facts. Returns a structured
    proposal dict. Never writes anything."""
    ignore_paths = ignore_paths or []
    occurrences = []  # each: dict(value, raw, kind, file, line, context, unit)
    for rel, full in _iter_prose_files(root, ignore_paths):
        content = _read_file(full)
        if content is None:
            continue
        def add(value, raw, kind, m, unit, capture=1):
            start, end = m.span(capture)
            line = content.count("\n", 0, start) + 1
            line_start = content.rfind("\n", 0, start) + 1
            occurrences.append({
                "value": value, "raw": raw, "kind": kind, "file": rel,
                "line": line, "context": _context(content, m.start(), m.end()),
                "unit": unit, "_start": start, "_end": end,
                "_col": start - line_start + 1,
                "_end_column": end - line_start + 1,
            })

        for regex, kind in _GENERIC_PATTERNS:
            for m in regex.finditer(content):
                raw = m.group(1)
                if kind == "version":
                    if _is_loopback_example(content, m):
                        continue
                    value = raw
                elif kind == "percentage":
                    value = raw + "%"
                else:  # currency
                    value = _norm_number(raw)
                add(value, raw, kind, m, None)
        for m in _COUNTED_AFTER.finditer(content):
            value = _norm_number(m.group(1))
            if len(re.sub(r"\D", "", value)) < 2:
                continue
            add(value, m.group(1), "counted", m, m.group(2).lower().rstrip("s"))
        for m in _COUNTED_BEFORE.finditer(content):
            value = _norm_number(m.group(2))
            if len(re.sub(r"\D", "", value)) < 2:
                continue
            add(value, m.group(2), "counted", m,
                m.group(1).lower().rstrip("s"), capture=2)
        for m in _AGING_RE.finditer(content):
            value = _norm_number(m.group(1))
            if len(re.sub(r"\D", "", value)) < 2:
                continue
            add(value, m.group(1), "aging", m, None)

    proposals, drift, discarded = _cluster(occurrences)
    return {"root": os.path.abspath(root), "proposals": proposals,
            "drift": drift, "discarded": discarded,
            "files_scanned": len({o["file"] for o in occurrences})}


def _manifest_occurrences(root, manifest):
    """Return the concrete local occurrences already curated in a manifest.

    Discovery is heuristic, but coverage is not: a candidate is covered only
    when its physical value capture falls inside the value capture of a
    locator that the manifest actually extracts from the same file.

    Coverage is first-match-per-locator, because `extract` is: when a manifest
    pattern matches a file more than once, only the first match's capture counts
    as covered. A later repetition of the same value is reported as a genuine
    untracked occurrence — `check` reports the same file as an ambiguous
    pattern. Cross-repo locators are skipped; discovery only scans `root`.
    """
    tracked = []
    ignore_paths = manifest.get("ignore_paths") or []
    for fact in manifest["facts"]:
        ftype = fact.get("type", "string")
        locators = [(fact["canonical"]["file"], fact["canonical"])]
        for cp in fact.get("copies") or []:
            if cp.get("repo"):
                continue
            for concrete in _expand_copy_files(root, cp["file"], ignore_paths):
                locators.append((concrete, cp))

        for relpath, locator in locators:
            if locator.get("repo") or locator.get("json") is not None or \
                    is_cross_repo(root, relpath):
                continue
            content = _read_file(_resolve(root, relpath))
            if content is None:
                continue
            value, line, _, start, end = _extract_details(
                content, locator["pattern"])
            if value is None:
                continue
            try:
                normalized = normalize_value(value, ftype)
            except ValueError:
                continue
            tracked.append({
                "fact": fact["name"],
                "file": os.path.normpath(relpath).replace(os.sep, "/"),
                "line": line,
                "value": normalized,
                "type": ftype,
                "start": start,
                "end": end,
            })
    return tracked


def _occurrence_identity(occurrence):
    """Identify a discovery occurrence without conflating same-line values."""
    return (occurrence["file"], occurrence["_start"], occurrence["_end"])


def _span_contains(container, contained):
    """Return whether one half-open capture span fully contains another."""
    outer_start, outer_end = container
    inner_start, inner_end = contained
    return outer_start <= inner_start and inner_end <= outer_end


def add_manifest_coverage(root, result, manifest):
    """Mark discovered occurrences that an existing manifest already covers."""
    tracked = _manifest_occurrences(root, manifest)
    by_file = {}
    for item in tracked:
        by_file.setdefault(item["file"], []).append(item)

    # _cluster shares occurrence dicts between proposals and drift. Resolve
    # each physical capture once, then copy the answer everywhere that shared
    # occurrence appears. Capture spans distinguish repeated values on one
    # line and survive discovery's intentionally lossy display normalization.
    resolved = {}
    for group in result["proposals"] + result["drift"]:
        for occurrence in group["occurrences"]:
            identity = _occurrence_identity(occurrence)
            if identity not in resolved:
                facts = []
                candidates = by_file.get(occurrence["file"], [])
                for candidate in candidates:
                    if "_start" in occurrence and _span_contains(
                            (candidate["start"], candidate["end"]),
                            (occurrence["_start"], occurrence["_end"])):
                        facts.append(candidate["fact"])
                resolved[identity] = facts
            occurrence["covered_by"] = list(resolved[identity])

    result["manifest_facts"] = len(manifest["facts"])
    result["manifest_locators"] = len(tracked)
    return result


def filter_uncovered_discovery(result):
    """Keep only candidate occurrences not represented in the manifest.

    `proposals` and `drift` are filtered. `files_scanned` and `discarded`
    are carried through from the full scan unchanged: they describe what was
    read, not what the manifest covers.
    """
    filtered = dict(result)
    filtered["proposals"] = []
    filtered["drift"] = []

    for key in ("proposals", "drift"):
        for group in result[key]:
            occurrences = []
            occurrence_keys = set()
            for occurrence in group["occurrences"]:
                if occurrence.get("covered_by"):
                    continue
                identity = _occurrence_identity(occurrence)
                if identity in occurrence_keys:
                    continue
                occurrence_keys.add(identity)
                occurrences.append(occurrence)
            if not occurrences:
                continue
            item = dict(group)
            item["occurrences"] = occurrences
            item["files"] = sorted({o["file"] for o in occurrences})
            filtered[key].append(item)

    filtered["uncovered_occurrences"] = len({
        _occurrence_identity(o)
        for key in ("proposals", "drift")
        for group in filtered[key]
        for o in group["occurrences"]
    })
    return filtered


def _cluster(occurrences):
    # Value appearing in >= 2 distinct files -> proposed fact (likely a copy).
    by_value = {}
    for o in occurrences:
        by_value.setdefault(o["value"], []).append(o)

    proposals, discarded = [], []
    for value, occs in sorted(by_value.items()):
        files = sorted({o["file"] for o in occs})
        if len(files) >= 2:
            unit = next((o["unit"] for o in occs if o["unit"]), None)
            name = _suggest_name(unit, occs[0]["kind"], value)
            proposals.append({
                "name": name, "value": value, "kind": occs[0]["kind"],
                "unit": unit, "files": files,
                "canonical_guess": files[0],
                "reason": "appears verbatim in %d files; confirm canonical"
                          % len(files),
                "occurrences": occs,
            })
        else:
            discarded.append({
                "value": value, "reason": "single file — nothing to drift",
                "file": files[0],
            })

    # Same unit noun with DIFFERENT values across files -> live-drift candidate.
    drift = []
    by_unit = {}
    for o in occurrences:
        if o["unit"] and o["kind"] == "counted":
            by_unit.setdefault(o["unit"], []).append(o)
    for unit, occs in sorted(by_unit.items()):
        values = {o["value"] for o in occs}
        files = {o["file"] for o in occs}
        if len(values) > 1 and len(files) > 1:
            drift.append({
                "unit": unit,
                "values": sorted(values),
                "occurrences": sorted(
                    occs, key=lambda o: (o["file"], o["line"])),
            })
    return proposals, drift, discarded


def _suggest_name(unit, kind, value):
    if unit:
        return re.sub(r"[^a-z0-9]+", "-", unit.lower()).strip("-") + "-count"
    if kind == "currency":
        return "price-" + re.sub(r"\D", "", value)[:6]
    if kind == "percentage":
        return "pct-" + re.sub(r"\D", "", value)[:4]
    if kind == "version":
        return "version-" + re.sub(r"[^0-9]", "", value)[:4]
    return "fact-" + re.sub(r"\D", "", value)[:6]


# --------------------------------------------------------------------------- #
# pointers. A pointer replaces a copied value with a reference to its owner:
#   ⏳ <what>: `<file>`          or          ⏳ <what>: [label](<file>)
# `pointers` finds every pointer marker in the repo's prose and checks that
# its target exists. A target resolves, first match wins, from the repo root,
# then the pointing file's directory, then another repo: `~/repo/path`,
# `repo:path`, or `repo/path` whose first segment is a repo found the same way
# `check` finds named repos. Fragments (`#heading`) and `:line` suffixes are
# stripped and not checked.
#
#   MISSING          the target does not exist anywhere it could resolve
#   REPO NOT FOUND   the target names another repo that is not checked out
#   MALFORMED        a marker with no target; fails only with --strict
#
# Markers inside inline code or fenced blocks are documentation about the
# convention, not pointers, and are skipped, as are placeholder targets
# (`<owner>`) and URLs.
# --------------------------------------------------------------------------- #
POINTER_MARKER = "⏳"
_PTR_TARGET = (r"(?:`(?P<code>[^`\n]+)`|\[[^\]\n]*\]\((?P<link>[^)\s]+)\)"
               r"|(?P<url>https?://[^\s)]+))")
_PTR_REPO = r"(?:(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)[ \t]+)?"
# What may follow the marker. Each form puts the target straight after the
# marker, the colon, or an opening parenthesis; "⏳ Open: the `x` branch" is
# not a pointer, and neither is "⏳ pointer." with no target at all.
_PTR_FORMS = [
    # ⏳ `file`   (a bare target, common in table cells)
    re.compile(r"^[ \t]*[*_]*[ \t]*" + _PTR_TARGET),
    # ⏳ <what>: `file`   ⏳ **<what>:** `file`   ⏳ <what>: repo `file`
    re.compile(r"^(?P<what>[^:`\n]*?):[*_]*[ \t]*" + _PTR_REPO + _PTR_TARGET),
    # ⏳ <what> (`file`)
    re.compile(r"^(?P<what>[^:`(\n]*?)\([ \t]*" + _PTR_TARGET),
]


def _match_pointer(text):
    for form in _PTR_FORMS:
        m = form.match(text)
        if m:
            target = m.group("code") or m.group("link") or m.group("url")
            repo = m.groupdict().get("repo")
            return target, repo
    return None


_FENCE = re.compile(r"^\s*(```|~~~)")


def _code_spans(line):
    """Return [(start, end)] of inline code spans on one line."""
    spans, i = [], 0
    while True:
        a = line.find("`", i)
        if a < 0:
            return spans
        run = len(line[a:]) - len(line[a:].lstrip("`"))
        b = line.find("`" * run, a + run)
        if b < 0:
            return spans
        spans.append((a, b + run))
        i = b + run


def _clean_target(raw):
    t = raw.strip()
    if " " in t:
        t = t.split(" ", 1)[0]
    t = t.split("#", 1)[0]
    t = re.sub(r":\d+(?:-\d+)?$", "", t)
    return t.rstrip(".,;")


def find_pointers(text, marker=POINTER_MARKER):
    """Yield (line, raw_target_or_None, repo_hint_or_None) per marker."""
    lines = text.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        spans = _code_spans(line)
        start = 0
        while True:
            pos = line.find(marker, start)
            if pos < 0:
                break
            start = pos + len(marker)
            if any(a <= pos < b for a, b in spans):
                continue
            nxt = line.find(marker, start)
            rest = line[start:nxt] if nxt >= 0 else line[start:]
            found = _match_pointer(rest)
            if not found and nxt < 0 and i + 1 < len(lines) and \
                    not _FENCE.match(lines[i + 1]) and \
                    marker not in lines[i + 1]:
                # A pointer wrapped onto the next line, anywhere in it.
                found = _match_pointer(rest.rstrip() + " " +
                                       lines[i + 1].strip())
            if not found:
                yield i + 1, None, None
                continue
            yield i + 1, found[0], found[1]


def _pointer_repo(name, root, repos_cache, manifest, overrides, env):
    if name not in repos_cache:
        declared = dict(manifest.get("repos") or {}) if manifest else {}
        synthetic = {"repos": {name: declared.get(name)}}
        repos_cache[name] = resolve_repos(root, synthetic, overrides,
                                          env)[name]
    return repos_cache[name]


def resolve_pointer(root, rel, target, manifest=None, overrides=None,
                    env=None, repos_cache=None, repo_hint=None):
    """Return (status, resolved_path_or_note).

    repo_hint is a repo name written just before the target, as in
    "cot-production `path`": the target is looked up in that repo. A hint
    that is neither declared in the manifest's repos nor found on disk is
    ordinary prose ("Open: the `branch`"), so the line is not a pointer.
    """
    repos_cache = {} if repos_cache is None else repos_cache
    t = target.replace("\\", "/")
    if repo_hint and not t.startswith("~/"):
        info = _pointer_repo(repo_hint, root, repos_cache, manifest,
                             overrides, env)
        declared = repo_hint in ((manifest or {}).get("repos") or {}) or \
            repo_hint in (overrides or {})
        if not info.get("path") and not declared:
            return "malformed", (f"{repo_hint!r} before the target is not a "
                                 "known repo; the target must follow the "
                                 "colon")
        if not info.get("path"):
            return "repo_missing", f"repo {repo_hint!r} not found (looked " \
                                   f"in: {', '.join(info['looked'])})"
        if os.path.exists(os.path.join(info["path"], t.lstrip("/"))):
            return "ok", f"{repo_hint}:{t.lstrip('/')}"
        return "missing", f"not found in repo {repo_hint!r} ({info['path']})"
    candidates = []
    repo_ref = None
    if t.startswith("~/"):
        name, _, sub = t[2:].partition("/")
        repo_ref = (name, sub)
    elif re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*:[^/\\]", t) and \
            not re.match(r"^[A-Za-z]:", t):
        name, _, sub = t.partition(":")
        repo_ref = (name, sub)
    else:
        candidates.append(_resolve(root, t.lstrip("/")))
        candidates.append(_resolve(os.path.join(root, os.path.dirname(rel)),
                                   t))
    for c in candidates:
        if os.path.exists(c):
            return "ok", os.path.relpath(c, root).replace(os.sep, "/")
    if repo_ref is None and "/" in t:
        # `repo/path`: try the first segment as another repo.
        name, _, sub = t.partition("/")
        info = _pointer_repo(name, root, repos_cache, manifest, overrides,
                             env)
        if info.get("path") and os.path.exists(os.path.join(info["path"],
                                                            sub)):
            return "ok", f"{name}:{sub}"
        return "missing", "not found from the repo root or the file's " \
                          "directory"
    if repo_ref is None:
        return "missing", "not found from the repo root or the file's " \
                          "directory"
    name, sub = repo_ref
    info = _pointer_repo(name, root, repos_cache, manifest, overrides, env)
    if not info.get("path"):
        return "repo_missing", f"repo {name!r} not found (looked in: " \
                               f"{', '.join(info['looked'])})"
    if os.path.exists(os.path.join(info["path"], sub)):
        return "ok", f"{name}:{sub}"
    return "missing", f"not found in repo {name!r} ({info['path']})"


def check_pointers(root, manifest=None, paths=None, ignore=None,
                   marker=POINTER_MARKER, overrides=None, env=None,
                   skip_marker=None):
    paths = paths or GUARD_DEFAULT_PATHS
    ignore = list(ignore or []) + list((manifest or {}).get("ignore_paths")
                                       or [])
    skip_marker = skip_marker or (manifest or {}).get("guard_skip_marker")
    skip = re.compile(skip_marker, re.MULTILINE) if skip_marker else None
    repos_cache = {}
    results = []
    counts = {"ok": 0, "missing": 0, "repo_missing": 0, "malformed": 0,
              "skipped": 0}
    files = 0
    for rel in _repo_files(root):
        if not any(_path_glob(rel, p) for p in paths):
            continue
        if any(_path_glob(rel, g) for g in ignore):
            continue
        text = _read_file(_resolve(root, rel))
        if text is None or marker not in text:
            continue
        if skip and skip.search(text):
            continue
        files += 1
        for line, raw, hint in find_pointers(text, marker):
            entry = {"file": rel, "line": line, "target": raw}
            if raw is None:
                entry["status"] = "malformed"
                entry["note"] = (f"not '{marker} <what>: `file`' (or "
                                 "[label](file)); the target must follow "
                                 "the colon")
            else:
                target = _clean_target(raw)
                if not target or "<" in raw or ">" in raw or \
                        "://" in target or target.startswith("mailto:"):
                    entry["status"] = "skipped"
                else:
                    if hint:
                        entry["repo"] = hint
                    status, note = resolve_pointer(
                        root, rel, target, manifest, overrides, env,
                        repos_cache, hint)
                    entry["status"] = status
                    entry["resolved" if status == "ok" else "note"] = note
            counts[entry["status"]] += 1
            results.append(entry)
    return {"version": __version__, "root": os.path.abspath(root),
            "marker": marker, "files_with_pointers": files,
            "pointers": results, "summary": counts}


def render_pointers(result, strict=False):
    s = result["summary"]
    total = sum(v for k, v in s.items())
    _print(f"SSOT POINTERS — {total} {result['marker']} markers in "
           f"{result['files_with_pointers']} files")
    _print("")
    for status, title in (("missing", "MISSING"),
                          ("repo_missing", "REPO NOT FOUND"),
                          ("malformed", "MALFORMED" +
                           ("" if strict else " (advisory; --strict fails)"))):
        rows = [p for p in result["pointers"] if p["status"] == status]
        if not rows:
            continue
        _print(f"{title} ({len(rows)}):")
        for p in rows:
            tgt = f" -> {p['target']}" if p.get("target") else ""
            _print(f"  - {p['file']}:{p['line']}{tgt}: {p.get('note')}")
        _print("")
    _print(f"{s['ok']} resolve, {s['missing']} missing, "
           f"{s['repo_missing']} repo not found, {s['malformed']} malformed, "
           f"{s['skipped']} skipped (placeholders, URLs).")


def pointers_exit_code(result, strict=False):
    s = result["summary"]
    bad = s["missing"] + s["repo_missing"] + (s["malformed"] if strict else 0)
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# Reporting (human-readable)
# --------------------------------------------------------------------------- #
def _print(*a):
    print(*a)


def render_check(result):
    s = result["summary"]
    in_sync = [f for f in result["facts"] if f["status"] == "in_sync"]
    _print(f"SSOT CHECK — {result['generated']}")
    _print("")
    if in_sync:
        _print(f"IN SYNC ({len(in_sync)}): " +
               ", ".join(f["name"] for f in in_sync))
        _print("")

    drifted = [f for f in result["facts"] if f["status"] == "drifted"]
    if drifted:
        _print(f"DRIFTED ({len(drifted)}):")
        for f in drifted:
            for cp in f["copies"]:
                if cp["status"] != "drifted":
                    continue
                tag = {"canonical_suspect": " (canonical suspect)",
                       "overstated": " (floor overstates)",
                       "floor_lagging": " (floor lagging)"}.get(
                           cp.get("direction"), "")
                _print(f"  - {f['name']}{tag} — canonical says "
                       f"{cp.get('canonical_value')!r}, copy says "
                       f"{cp.get('value')!r}")
                _print(f"      canonical: {f['canonical']['file']}:"
                       f"{f['canonical'].get('line')}")
                _print(f"      copy:      {cp['file']}:{cp.get('line')}")
                if cp.get("cross_repo"):
                    _print(f"      source:    {cp.get('source')}")
                if cp.get("note"):
                    _print(f"      note: {cp['note']}")
        _print("")

    moved = [f for f in result["facts"] if f["status"] == "canonical_moved"]
    if moved:
        _print(f"CANONICAL MOVED ({len(moved)}):")
        for f in moved:
            _print(f"  - {f['name']} — {f['canonical'].get('error')} "
                   f"in {f['canonical']['file']}")
        _print("")

    stale = [f for f in result["facts"]
             if any(c["status"] == "stale_entry" for c in f["copies"])]
    if stale:
        _print(f"STALE MANIFEST ENTRY ({len(stale)}):")
        for f in stale:
            for cp in f["copies"]:
                if cp["status"] == "stale_entry":
                    _print(f"  - {f['name']} — {cp.get('note')} "
                           f"({cp['file']})")
        _print("")

    unver = [f for f in result["facts"]
             if any(c["status"] == "unverified" for c in f["copies"])]
    if unver:
        _print(f"UNVERIFIED cross-repo ({len(unver)}):")
        for f in unver:
            for cp in f["copies"]:
                if cp["status"] == "unverified":
                    _print(f"  - {f['name']} — {cp.get('note')} ({cp['file']})")
        _print("")

    missing = []
    for f in result["facts"]:
        if f["status"] == "repo_missing" and "error" in f["canonical"]:
            missing.append((f["name"], f["canonical"]["file"],
                            f["canonical"]["error"]))
        for cp in f["copies"]:
            if cp["status"] == "repo_missing":
                missing.append((f["name"], cp["file"], cp.get("note")))
    if missing:
        _print(f"REPO NOT FOUND ({len(missing)}):")
        for name, where, note in missing:
            _print(f"  - {name} — {where}: {note}")
        _print("      fix: clone it, or point at it with --repo NAME=PATH, "
               "$SSOT_REPO_<NAME>, or $SSOT_REPOS_DIR")
        _print("")

    for kind, title, hint in (
            ("stale_value", "STALE VALUE",
             "a superseded value is still asserted"),
            ("restated", "RESTATED",
             "the current value is written outside the canonical and its "
             "declared copies")):
        rows = [(f, g) for f in result["facts"]
                for g in f.get("guard", {}).get("findings", [])
                if g["kind"] == kind]
        if not rows:
            continue
        _print(f"{title} ({len(rows)}) — {hint}:")
        for f, g in rows:
            _print(f"  - {f['name']} — {g['file']}:{g['line']} has "
                   f"{g['value']!r}; owner: {f['canonical']['file']}")
            _print(f"      {g['text']}")
        _print("")

    fresh_stale = [f for f in result["facts"]
                   if f.get("freshness", {}).get("stale")]
    if fresh_stale:
        _print(f"STALE CANONICAL — freshness ({len(fresh_stale)}):")
        for f in fresh_stale:
            fr = f["freshness"]
            _print(f"  - {f['name']} — {f['canonical']['file']} last edited "
                   f"{fr['last_edit']} ({fr['age_days']}d ago, max "
                   f"{fr['max_age_days']}d); owner: {fr.get('owner')}")
        _print("")

    parts = [f"{s['facts_checked']} facts checked"]
    if s["problems"] == 0:
        parts.append("all in sync")
    else:
        if s["drifted"]:
            parts.append(f"{s['drifted']} drifted")
        if s["canonical_moved"]:
            parts.append(f"{s['canonical_moved']} canonical moved")
        if s["stale_entry"]:
            parts.append(f"{s['stale_entry']} stale entries")
        if s["unverified"]:
            parts.append(f"{s['unverified']} unverified")
        if s["freshness_stale"]:
            parts.append(f"{s['freshness_stale']} stale canonical")
        if s.get("repo_missing"):
            parts.append(f"{s['repo_missing']} repo not found")
        if s.get("stale_value"):
            n = s["stale_value"]
            parts.append(f"{n} stale value{'' if n == 1 else 's'}")
        if s.get("restated"):
            parts.append(f"{s['restated']} restated")
    _print(", ".join(parts) + ".")


def render_explain(result, name):
    f = result["facts"][0]
    _print(f"FACT: {f['name']}  (type: {f['type']})")
    c = f["canonical"]
    if "value" in c:
        _print(f"  canonical: {c['file']}:{c.get('line')} = {c['value']!r} "
               f"[{c.get('source')}]")
    else:
        _print(f"  canonical: {c['file']} — {c.get('error')}")
    if f.get("freshness"):
        fr = f["freshness"]
        if fr.get("skipped"):
            _print(f"  freshness: {fr['note']}")
        else:
            flag = "STALE" if fr["stale"] else "ok"
            _print(f"  freshness: {flag} — last edit {fr['last_edit']} "
                   f"({fr['age_days']}d, max {fr['max_age_days']}d), "
                   f"owner {fr.get('owner')}")
    _print(f"  copies ({len(f['copies'])}):")
    for cp in f["copies"]:
        val = cp.get("value", "—")
        _print(f"    [{cp['status']}] {cp['file']}:{cp.get('line')} = {val!r}"
               + (f"  ({cp['note']})" if cp.get("note") else ""))
    if "guard" in f:
        g = f["guard"]
        _print(f"  guard ({'superseded + restated' if g['restated'] else 'superseded'}"
               f"): {len(g['findings'])} finding(s)")
        for item in g["findings"]:
            _print(f"    [{item['kind']}] {item['file']}:{item['line']} = "
                   f"{item['value']!r}")


def render_discover(result):
    _print(f"SSOT DISCOVER — {date.today().isoformat()}  "
           f"({result['files_scanned']} files scanned)")
    _print("")
    if result["drift"]:
        _print("LIVE DRIFT CANDIDATES (same unit, different values):")
        for d in result["drift"]:
            vals = " vs ".join(d["values"])
            _print(f"  - {d['unit']}: {vals}")
            for o in d["occurrences"]:
                _print(f"      {o['file']}:{o['line']}  {o['value']}  "
                       f"\"{o['context'][:70]}\"")
        _print("")
    _print(f"PROPOSED FACTS ({len(result['proposals'])}):")
    for p in result["proposals"]:
        _print(f"  - {p['name']}: {p['value']} — canonical guess: "
               f"{p['canonical_guess']} ({p['reason']})")
        _print(f"      copies: " +
               ", ".join(sorted(set(f for f in p['files']
                                    if f != p['canonical_guess']))))
    _print("")
    _print(f"DISCARDED (low confidence): {len(result['discarded'])} "
           "single-file values")
    _print("")
    _print("Draft manifest is NOT written. Curate these entries, assign "
           "canonicals, then create .ssot.yaml by hand or with AI assistance.")


def _gha_escape_data(value):
    return (str(value).replace("%", "%25").replace("\r", "%0D")
            .replace("\n", "%0A"))


def _gha_escape_property(value):
    return (_gha_escape_data(value).replace(":", "%3A")
            .replace(",", "%2C"))


def _github_annotation_file(result, occurrence):
    """Return a path relative to the GitHub workspace/current directory."""
    root = result.get("root")
    if not root:
        return occurrence["file"]
    absolute = os.path.join(root, occurrence["file"])
    base = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    try:
        relative = os.path.relpath(absolute, base)
    except ValueError:  # Different Windows drives cannot be relativized.
        return occurrence["file"]
    return relative.replace(os.sep, "/")


def render_discover_github(result):
    """Emit one GitHub warning per uncovered occurrence, never a failure."""
    seen = set()
    warnings = 0

    # A live-drift candidate is more useful than the repeated-value proposal
    # for the same occurrence, so render drift first and de-duplicate afterward.
    for drift in result["drift"]:
        values = " vs ".join(drift["values"])
        for occurrence in drift["occurrences"]:
            identity = _occurrence_identity(occurrence)
            if identity in seen:
                continue
            seen.add(identity)
            message = (f"Potential untracked drift for {drift['unit']}: "
                       f"{values}. This occurrence is not covered by the "
                       "SSOT manifest.")
            _print("::warning file=%s,line=%s,col=%s,endColumn=%s,title=%s::%s" % (
                _gha_escape_property(
                    _github_annotation_file(result, occurrence)),
                occurrence["line"],
                occurrence["_col"], occurrence["_end_column"],
                _gha_escape_property("Untracked SSOT drift"),
                _gha_escape_data(message)))
            warnings += 1

    for proposal in result["proposals"]:
        for occurrence in proposal["occurrences"]:
            identity = _occurrence_identity(occurrence)
            if identity in seen:
                continue
            seen.add(identity)
            message = (f"{proposal['value']!r} appears in multiple files, "
                       "but this occurrence is not covered by the SSOT "
                       "manifest. Add it as a copy or ignore the path.")
            _print("::warning file=%s,line=%s,col=%s,endColumn=%s,title=%s::%s" % (
                _gha_escape_property(
                    _github_annotation_file(result, occurrence)),
                occurrence["line"],
                occurrence["_col"], occurrence["_end_column"],
                _gha_escape_property("Untracked SSOT candidate"),
                _gha_escape_data(message)))
            warnings += 1

    _print(f"ssot-check discover: {warnings} uncovered candidate "
           f"occurrence(s); advisory only.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fail_config(msg):
    print(f"ssot-check: {msg}", file=sys.stderr)
    return 2


def _public_discovery_result(value):
    """Remove private capture offsets before producing machine-readable JSON."""
    if isinstance(value, dict):
        return {key: _public_discovery_result(item)
                for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_public_discovery_result(item) for item in value]
    return value


def cmd_validate(args):
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        return _fail_config(f"{args.manifest}: {exc}")
    errors = validate_manifest(manifest)
    if errors:
        print(f"INVALID — {args.manifest} ({len(errors)} error(s)):")
        for e in errors:
            print(f"  {e}")
        return 2
    n = len(manifest.get("facts", []))
    print(f"VALID — {args.manifest}: {n} fact(s), schema OK.")
    return 0


def _repo_overrides(args, manifest=None):
    out = {}
    for item in getattr(args, "repo", None) or []:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ManifestError(f"--repo expects NAME=PATH, got {item!r}")
        out[name] = path
    if manifest is not None:
        # An override for a name the manifest never declares would be
        # silently unused — the same quiet misread a typo must not cause.
        declared = manifest.get("repos") or {}
        unknown = sorted(n for n in out if n not in declared)
        if unknown:
            names = ", ".join(repr(n) for n in unknown)
            known = ", ".join(sorted(declared)) or "(none)"
            raise ManifestError(f"--repo names a repo the manifest does not "
                                f"declare: {names}; declared repos: {known}")
    return out


def cmd_check(args):
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        return _fail_config(f"{args.manifest}: {exc}")
    errors = validate_manifest(manifest)
    if errors:
        print(f"ssot-check: manifest invalid ({len(errors)} error(s)); "
              "run `ssot_check.py validate` for details.", file=sys.stderr)
        for e in errors[:5]:
            print(f"  {e}", file=sys.stderr)
        return 2
    root = args.root or os.path.dirname(os.path.abspath(args.manifest))
    try:
        overrides = _repo_overrides(args, manifest)
    except ManifestError as exc:
        return _fail_config(str(exc))
    result = check(root, manifest, fetch=args.fetch,
                   repo_overrides=overrides, manifest_path=args.manifest)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render_check(result)
    return result["exit_code"]


# --------------------------------------------------------------------------- #
# fix: rewrite drifted copies from their canonical value. The only command
# that edits files, so it is narrow on purpose: a dry run (a diff) unless
# --write; local copies with an exact relation and a regex locator only; and
# it refuses anything where the canonical may be the stale side.
# --------------------------------------------------------------------------- #
FIXABLE_TYPES = {"string", "integer", "currency", "semver"}


def _group_thousands(digits, sep):
    neg = digits.startswith("-")
    d = digits.lstrip("-")
    out = []
    while len(d) > 3:
        out.insert(0, d[-3:])
        d = d[:-3]
    out.insert(0, d)
    return ("-" if neg else "") + sep.join(out)


def format_like(canonical, copy, ftype):
    """Write the canonical value the way the copy writes its values.

    '1268' into a copy written '1,100+' gives '1,268+'; '$1500' into '1,200'
    gives '1,500'. Returns None when the value cannot be carried over.
    """
    plus = "+" if copy.strip().endswith("+") else ""
    if ftype == "string":
        return canonical.strip()
    if ftype == "semver":
        core = canonical.strip().lstrip("vV")
        prefix = copy.strip()[:1] if copy.strip()[:1] in "vV" else ""
        return prefix + core + plus
    try:
        norm = normalize_value(canonical, ftype)
    except ValueError:
        return None
    body = _strip_trailing_plus(copy)
    sep = ","
    for cand in (",", "_", " "):
        if re.search(r"\d" + re.escape(cand) + r"\d{3}", body):
            sep = cand
            break
    else:
        sep = ""
    if ftype == "integer":
        return (_group_thousands(norm, sep) if sep else norm) + plus
    # currency
    symbol = body[:1] if body[:1] in CURRENCY_SYMBOLS else ""
    whole, _, frac = norm.partition(".")
    m = re.search(r"\.(\d+)$", body)
    if m and not frac:
        frac = "0" * len(m.group(1))
    text = (_group_thousands(whole, sep) if sep else whole)
    return symbol + text + ("." + frac if frac else "") + plus


def _read_raw(path):
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def plan_fixes(root, manifest, result):
    """Return (edits, refusals). edits: {relpath: [(start, end, old, new,
    fact)]}; refusals: [(fact, file, reason)] for drift fix will not touch."""
    ignore_paths = manifest.get("ignore_paths") or []
    by_name = {f["name"]: f for f in result["facts"]}
    edits, refusals = {}, []
    for fact in manifest["facts"]:
        fr = by_name.get(fact["name"])
        if not fr:
            continue
        ftype = fact.get("type", "string")
        cval = fr["canonical"].get("value")
        drifted = {c["file"]: c for c in fr["copies"]
                   if c["status"] == "drifted"}
        if not drifted:
            continue
        for cp in fact.get("copies") or []:
            files = ([cp["file"]] if cp.get("repo") else
                     _expand_copy_files(root, cp["file"], ignore_paths))
            for rel in files:
                res = drifted.get(f"{cp['repo']}:{rel}" if cp.get("repo")
                                  else rel)
                if res is None:
                    continue
                name, where = fact["name"], res["file"]
                reason = None
                if res.get("cross_repo"):
                    reason = "in another repo; fix it there"
                elif cp.get("relation") == "floor":
                    reason = "a floor is written by a person; pick the " \
                             "round number by hand"
                elif cp.get("rounding"):
                    reason = "a rounded copy is written by its own script"
                elif cp.get("json") is not None:
                    reason = "JSON copies are not rewritten"
                elif res.get("direction") == "canonical_suspect":
                    reason = "canonical suspect: the copy is higher, so " \
                             "the canonical may be the stale side"
                elif ftype not in FIXABLE_TYPES:
                    reason = f"type {ftype} is not rewritten"
                elif cval is None:
                    reason = "no canonical value"
                if reason:
                    refusals.append((name, where, reason))
                    continue
                content = _read_raw(_resolve(root, rel))
                if content is None:
                    refusals.append((name, where, "unreadable"))
                    continue
                old, _, count, start, end = _extract_details(content,
                                                             cp["pattern"])
                if old is None:
                    refusals.append((name, where, "pattern no longer "
                                     "matches"))
                    continue
                if count > 1:
                    refusals.append((name, where, f"pattern matches "
                                     f"{count}x; fix by hand or tighten it"))
                    continue
                new = format_like(cval, old, ftype)
                ok = new is not None and values_match(cval, new, ftype)[0]
                if not ok:
                    refusals.append((name, where, "could not write the "
                                     "canonical in the copy's format"))
                    continue
                edits.setdefault(rel, []).append((start, end, old, new,
                                                  name))
    # Two facts editing overlapping text in one file cannot both be right.
    for rel, items in list(edits.items()):
        items.sort()
        if any(a[1] > b[0] for a, b in zip(items, items[1:])):
            for item in items:
                refusals.append((item[4], rel, "overlaps another fact's "
                                 "edit in the same file"))
            del edits[rel]
    return edits, refusals


def apply_edits(content, items):
    for start, end, _, new, _ in sorted(items, reverse=True):
        content = content[:start] + new + content[end:]
    return content


def cmd_fix(args):
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        return _fail_config(f"{args.manifest}: {exc}")
    errors = validate_manifest(manifest)
    if errors:
        return _fail_config(f"manifest invalid; run validate ({errors[0]})")
    root = args.root or os.path.dirname(os.path.abspath(args.manifest))
    try:
        overrides = _repo_overrides(args, manifest)
    except ManifestError as exc:
        return _fail_config(str(exc))
    result = check(root, manifest, repo_overrides=overrides,
                   manifest_path=args.manifest)
    edits, refusals = plan_fixes(root, manifest, result)
    import difflib
    for rel in sorted(edits):
        before = _read_raw(_resolve(root, rel))
        after = apply_edits(before, edits[rel])
        sys.stdout.writelines(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        if args.write:
            with open(_resolve(root, rel), "w", encoding="utf-8",
                      newline="") as fh:
                fh.write(after)
    n = sum(len(v) for v in edits.values())
    if refusals:
        _print("")
        _print(f"NOT FIXED ({len(refusals)}):")
        for name, where, reason in refusals:
            _print(f"  - {name} — {where}: {reason}")
    _print("")
    if not n:
        _print("Nothing to fix." if not refusals else
               "Nothing fix can rewrite safely.")
        return 1 if refusals else 0
    if not args.write:
        _print(f"{n} cop{'y' if n == 1 else 'ies'} in {len(edits)} "
               f"file{'' if len(edits) == 1 else 's'} would change. "
               "Re-run with --write to apply.")
        return 1
    after = check(root, manifest, repo_overrides=overrides,
                  manifest_path=args.manifest)
    still, _ = plan_fixes(root, manifest, after)
    fixed_files = set(edits)
    remaining = [c for f in after["facts"] for c in f["copies"]
                 if c["status"] == "drifted" and c["file"] in fixed_files]
    _print(f"Rewrote {n} cop{'y' if n == 1 else 'ies'} in {len(edits)} "
           f"file{'' if len(edits) == 1 else 's'}; re-check: "
           + ("all rewritten copies in sync." if not remaining and not still
              else f"{len(remaining)} still drifted."))
    return 1 if (remaining or refusals) else 0


def cmd_discover(args):
    # The annotation text states that an occurrence is not covered by the
    # manifest, which is only established by the --untracked-only pass.
    if args.github_annotations and not args.untracked_only:
        return _fail_config(
            "discover --github-annotations requires --untracked-only")

    manifest = None
    manifest_ignore = []
    mpath = args.manifest
    explicit_manifest = mpath is not None
    if mpath is None:
        candidate = os.path.join(args.root or ".", ".ssot.yaml")
        if os.path.isfile(candidate):
            mpath = candidate
    if mpath is not None:
        try:
            manifest = load_manifest(mpath)
        except ManifestError as exc:
            if explicit_manifest or args.untracked_only:
                return _fail_config(f"{mpath}: {exc}")
        if manifest is not None:
            # Plain discovery historically honored ignore_paths from an
            # auto-loaded draft before the manifest had a complete facts
            # section. Preserve that useful behavior, but only for a valid
            # list of strings; explicit/coverage manifests remain strict.
            ignore_paths = manifest.get("ignore_paths")
            if isinstance(ignore_paths, list) and all(
                    isinstance(path, str) for path in ignore_paths):
                manifest_ignore = ignore_paths
            errors = validate_manifest(manifest)
            if errors:
                if explicit_manifest or args.untracked_only:
                    return _fail_config(
                        f"manifest invalid; run validate ({errors[0]})")
                manifest = None

    if args.untracked_only and manifest is None:
        return _fail_config(
            "discover --untracked-only requires a valid SSOT manifest")

    # Match check/explain: a manifest's relative paths are resolved against its
    # own directory unless --root says otherwise. Resolving them against the
    # working directory instead silently matches nothing, which reads as "the
    # manifest covers none of this" rather than as the misconfiguration it is.
    root = args.root
    if root is None:
        root = (os.path.dirname(os.path.abspath(mpath)) if mpath is not None
                else ".")

    result = discover(root, ignore_paths=args.ignore or manifest_ignore)
    if args.untracked_only:
        add_manifest_coverage(root, result, manifest)
        result = filter_uncovered_discovery(result)
        result["manifest"] = os.path.abspath(mpath)
        if not result["manifest_locators"]:
            print(f"ssot-check: no manifest locator resolved under {root!r}; "
                  "every occurrence will be reported as uncovered. Check "
                  "--root and the manifest's file paths.", file=sys.stderr)
    if args.json:
        print(json.dumps(_public_discovery_result(result), indent=2))
    elif args.github_annotations:
        render_discover_github(result)
    else:
        render_discover(result)
    return 0


def cmd_explain(args):
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        return _fail_config(f"{args.manifest}: {exc}")
    errors = validate_manifest(manifest)
    if errors:
        return _fail_config(f"manifest invalid; run validate ({errors[0]})")
    root = args.root or os.path.dirname(os.path.abspath(args.manifest))
    try:
        overrides = _repo_overrides(args, manifest)
    except ManifestError as exc:
        return _fail_config(str(exc))
    result = explain(root, manifest, args.name, fetch=args.fetch,
                     repo_overrides=overrides, manifest_path=args.manifest)
    if result is None:
        return _fail_config(f"no fact named {args.name!r} in {args.manifest}")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render_explain(result, args.name)
    return 0


def cmd_pointers(args):
    manifest = None
    mpath = args.manifest
    if mpath is None and os.path.isfile(os.path.join(args.root or ".",
                                                     ".ssot.yaml")):
        mpath = os.path.join(args.root or ".", ".ssot.yaml")
    if mpath is not None:
        try:
            manifest = load_manifest(mpath)
        except ManifestError as exc:
            return _fail_config(f"{mpath}: {exc}")
        errors = validate_manifest(manifest)
        if errors:
            return _fail_config(f"manifest invalid; run validate "
                                f"({errors[0]})")
    root = args.root or (os.path.dirname(os.path.abspath(mpath)) if mpath
                         else ".")
    overrides = {}
    for item in args.repo or []:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            return _fail_config(f"--repo expects NAME=PATH, got {item!r}")
        overrides[name] = path
    try:
        if args.skip_marker:
            re.compile(args.skip_marker)
    except re.error as exc:
        return _fail_config(f"--skip-marker: invalid regex ({exc})")
    result = check_pointers(root, manifest, paths=args.path,
                            ignore=args.ignore, marker=args.marker,
                            overrides=overrides,
                            skip_marker=args.skip_marker)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        render_pointers(result, strict=args.strict)
    return pointers_exit_code(result, strict=args.strict)


def _add_repo(sp):
    sp.add_argument("--repo", action="append", metavar="NAME=PATH",
                    help="where a named repo (manifest 'repos:') is checked "
                         "out on this machine; repeatable. Overrides "
                         "$SSOT_REPO_<NAME>, $SSOT_REPOS_DIR and the "
                         "manifest default")


def _add_fetch(sp):
    sp.add_argument("--fetch", action="store_true",
                    help="for cross-repo copies, run `git fetch` in the "
                         "sibling repo and compare against its remote-tracking "
                         "ref. This updates that repo's remote-tracking refs "
                         "and FETCH_HEAD and makes a network call; it never "
                         "pulls, rebases, or touches its working tree. Off by "
                         "default")


def build_parser():
    p = argparse.ArgumentParser(
        prog="ssot_check.py",
        description="Single-source-of-truth drift auditor (stdlib-only).")
    p.add_argument("--version", action="version",
                   version=f"ssot-check {__version__}")
    sub = p.add_subparsers(dest="command")

    def add_common(sp):
        sp.add_argument("-f", "--manifest", default=".ssot.yaml",
                        help="path to the manifest (default: .ssot.yaml)")
        sp.add_argument("--root", default=None,
                        help="repo root for relative paths "
                             "(default: manifest's directory)")

    c = sub.add_parser("check", help="verify copies against canonicals")
    add_common(c)
    c.add_argument("--json", action="store_true", help="machine-readable output")
    _add_fetch(c)
    _add_repo(c)
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("validate", help="check manifest structure")
    add_common(v)
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("discover", help="propose drift-prone facts from prose")
    d.add_argument("--root", default=None,
                   help="tree to scan (default: the manifest's directory, "
                        "or . when there is no manifest)")
    d.add_argument("-f", "--manifest", default=None,
                   help="manifest used to identify already-covered occurrences")
    d.add_argument("--ignore", action="append",
                   help="glob to skip (repeatable)")
    d.add_argument("--untracked-only", action="store_true",
                   help="with a manifest, report only uncovered occurrences")
    d.add_argument("--github-annotations", action="store_true",
                   help="emit advisory GitHub Actions warning annotations "
                        "(requires --untracked-only; --json takes precedence)")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.set_defaults(func=cmd_discover)

    e = sub.add_parser("explain", help="show one fact's values")
    add_common(e)
    e.add_argument("name", help="fact name")
    e.add_argument("--json", action="store_true", help="machine-readable output")
    _add_fetch(e)
    _add_repo(e)
    e.set_defaults(func=cmd_explain)

    fx = sub.add_parser("fix", help="rewrite drifted local copies from the "
                                    "canonical (dry run unless --write)")
    add_common(fx)
    fx.add_argument("--write", action="store_true",
                    help="apply the changes; without it, only print a diff")
    _add_repo(fx)
    fx.set_defaults(func=cmd_fix)
    pt = sub.add_parser("pointers",
                        help="check that every pointer marker's target "
                             "exists")
    pt.add_argument("--root", default=None,
                    help="tree to scan (default: the manifest's directory, "
                         "or .)")
    pt.add_argument("-f", "--manifest", default=None,
                    help="manifest whose ignore_paths and repos apply "
                         "(default: <root>/.ssot.yaml when present)")
    pt.add_argument("--marker", default=POINTER_MARKER,
                    help=f"pointer marker (default: {POINTER_MARKER})")
    pt.add_argument("--path", action="append",
                    help="glob of files to scan; repeatable (default: "
                         "Markdown, text and reST)")
    pt.add_argument("--ignore", action="append",
                    help="glob or dir/ prefix to skip; repeatable")
    pt.add_argument("--repo", action="append", metavar="NAME=PATH",
                    help="where another repo named by a pointer is checked "
                         "out; repeatable")
    pt.add_argument("--skip-marker", default=None,
                    help="regex; files matching it are skipped (default: "
                         "the manifest's guard_skip_marker)")
    pt.add_argument("--strict", action="store_true",
                    help="also fail on markers with no target")
    pt.add_argument("--json", action="store_true",
                    help="machine-readable output")
    pt.set_defaults(func=cmd_pointers)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # Default subcommand is `check`. Global options (--version, -h/--help) are
    # owned by the top-level parser and must not be swept into the default
    # subcommand — doing so routed `--version` to `check`, which rejected it.
    known = {"check", "validate", "discover", "explain", "fix", "pointers"}
    global_opts = {"-h", "--help", "--version"}
    if argv and argv[0] in global_opts:
        pass
    elif not argv or (argv[0] not in known and not argv[0].startswith("-")):
        argv = ["check"] + argv
    elif argv and argv[0].startswith("-") and \
            not any(a in known for a in argv):
        argv = ["check"] + argv
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
