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
import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

__version__ = "0.1.1"

VALID_TYPES = {"string", "integer", "currency", "semver", "date"}
VALID_ROUNDING = {"floor-10", "floor-100", "floor-1000", "floor-1000-as-K"}
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
    with open(path, encoding="utf-8") as fh:
        return parse_manifest(fh.read())


# --------------------------------------------------------------------------- #
# Schema validation (stdlib re-implementation of schema/ssot.schema.json)
# --------------------------------------------------------------------------- #
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

    ignore = manifest.get("ignore_paths")
    if ignore is not None and not isinstance(ignore, list):
        err("ignore_paths must be a list of glob strings", manifest,
            "ignore_paths")

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
            _check_locator(canon, f"{name}.canonical", errors, line_of)

        copies = fact.get("copies")
        if copies is None:
            err(f"{name}: missing 'copies' list", fact, "copies")
        elif not isinstance(copies, list):
            err(f"{name}: 'copies' must be a list", fact, "copies")
        elif not copies:
            err(f"{name}: 'copies' is empty — a fact with no copies cannot "
                "drift; remove it or add copies", fact, "copies")
        else:
            for j, cp in enumerate(copies):
                if not isinstance(cp, dict):
                    errors.append(f"{name}.copies[{j}]: must be a mapping")
                    continue
                _check_locator(cp, f"{name}.copies[{j}]", errors, line_of)
                rnd = cp.get("rounding")
                if rnd is not None and rnd not in VALID_ROUNDING:
                    err(f"{name}.copies[{j}]: rounding {rnd!r} not one of "
                        f"{sorted(VALID_ROUNDING)}", cp, "rounding")
    return errors


def _check_locator(loc, where, errors, line_of):
    """Validate a {file, pattern} block."""
    def err(msg, key):
        ln = line_of(loc, key)
        errors.append(f"line {ln}: {msg}" if ln else msg)

    if not loc.get("file"):
        err(f"{where}: missing 'file'", "file")
    pat = loc.get("pattern")
    if not pat:
        err(f"{where}: missing 'pattern'", "pattern")
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


# --------------------------------------------------------------------------- #
# Capture-group extraction
# --------------------------------------------------------------------------- #
def extract(content, pattern):
    """Return (value, lineno, match_count). value/lineno are None if no match.

    Uses the FIRST match; match_count > 1 signals an ambiguous pattern.
    Patterns are matched against the whole file content (not line by line).
    """
    compiled = re.compile(pattern)
    matches = list(compiled.finditer(content))
    if not matches:
        return None, None, 0
    first = matches[0]
    lineno = content.count("\n", 0, first.start()) + 1
    return first.group(1), lineno, len(matches)


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
    """Expand a possibly-globbed copy path into concrete relative paths."""
    if any(ch in relpath for ch in "*?[") and not is_cross_repo(root, relpath):
        import glob as _glob
        hits = sorted(_glob.glob(_resolve(root, relpath), recursive=True))
        rels = [os.path.relpath(h, root).replace(os.sep, "/")
                for h in hits if os.path.isfile(h)]
        return [r for r in rels if not _matches_any_glob(r, ignore_paths)]
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
        return _read_file(abspath), "local"
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
# check
# --------------------------------------------------------------------------- #
def check(root, manifest, fetch=False, today=None):
    """Run a full check. Returns a result dict (also drives --json)."""
    ignore_paths = manifest.get("ignore_paths") or []
    fact_results = []
    counts = {"in_sync": 0, "drifted": 0, "canonical_moved": 0,
              "stale_entry": 0, "unverified": 0, "warnings": 0,
              "freshness_stale": 0}

    for fact in manifest["facts"]:
        name = fact["name"]
        ftype = fact.get("type", "string")
        monotonic = str(fact.get("monotonic", "")).lower() == "true"
        canon = fact["canonical"]
        cfile = canon["file"]
        cabs = (os.path.abspath(cfile) if os.path.isabs(cfile)
                else _resolve(root, cfile))
        cross_canon = is_cross_repo(root, cfile)
        if cross_canon:
            ccontent, csrc = _read_cross_repo(cabs, fetch)
        else:
            ccontent, csrc = _read_file(cabs), "local"

        fr = {"name": name, "type": ftype, "copies": [],
              "canonical": {"file": cfile, "source": csrc}}

        if ccontent is None:
            fr["status"] = "canonical_moved"
            fr["canonical"]["error"] = "file missing or unreadable"
            counts["canonical_moved"] += 1
            fact_results.append(fr)
            continue

        cval, cline, cmulti = extract(ccontent, canon["pattern"])
        if cval is None:
            fr["status"] = "canonical_moved"
            fr["canonical"]["error"] = "pattern did not match"
            counts["canonical_moved"] += 1
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
        for cp in fact["copies"]:
            for concrete in _expand_copy_files(root, cp["file"], ignore_paths):
                res = _check_one_copy(root, concrete, cp, cval, ftype,
                                      monotonic, fetch)
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
        fact_results.append(fr)

    problems = (counts["drifted"] + counts["canonical_moved"] +
                counts["stale_entry"] + counts["unverified"] +
                counts["freshness_stale"])
    exit_code = 1 if problems else 0
    return {
        "version": __version__,
        "generated": (today or date.today()).isoformat(),
        "root": os.path.abspath(root),
        "facts": fact_results,
        "summary": {
            "facts_checked": len(fact_results),
            **counts,
            "problems": problems,
        },
        "exit_code": exit_code,
    }


def _check_one_copy(root, relpath, cp, cval, ftype, monotonic, fetch):
    cabs = (os.path.abspath(relpath) if os.path.isabs(relpath)
            else _resolve(root, relpath))
    cross = is_cross_repo(root, relpath)
    res = {"file": relpath, "cross_repo": cross}
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

    val, line, multi = extract(content, cp["pattern"])
    if val is None:
        res["status"] = "stale_entry"
        res["note"] = "pattern did not match (copy reworded or removed)"
        return res
    res.update({"value": val, "line": line})
    if multi > 1:
        res["warning"] = f"pattern matched {multi}x; used first"

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
def explain(root, manifest, name, fetch=False):
    for fact in manifest["facts"]:
        if fact["name"] == name:
            single = dict(manifest)
            single_fact = dict(fact)
            single["facts"] = [single_fact]
            return check(root, single, fetch=fetch)
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
        def add(value, raw, kind, m, unit):
            line = content.count("\n", 0, m.start()) + 1
            occurrences.append({
                "value": value, "raw": raw, "kind": kind, "file": rel,
                "line": line, "context": _context(content, m.start(), m.end()),
                "unit": unit,
            })

        for regex, kind in _GENERIC_PATTERNS:
            for m in regex.finditer(content):
                raw = m.group(1)
                if kind == "version":
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
            add(value, m.group(2), "counted", m, m.group(1).lower().rstrip("s"))
        for m in _AGING_RE.finditer(content):
            value = _norm_number(m.group(1))
            if len(re.sub(r"\D", "", value)) < 2:
                continue
            add(value, m.group(1), "aging", m, None)

    proposals, drift, discarded = _cluster(occurrences)
    return {"root": os.path.abspath(root), "proposals": proposals,
            "drift": drift, "discarded": discarded,
            "files_scanned": len({o["file"] for o in occurrences})}


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
                tag = " (canonical suspect)" if cp.get("direction") == \
                    "canonical_suspect" else ""
                _print(f"  - {f['name']}{tag} — canonical says "
                       f"{cp.get('canonical_value')!r}, copy says "
                       f"{cp.get('value')!r}")
                _print(f"      canonical: {f['canonical']['file']}:"
                       f"{f['canonical'].get('line')}")
                _print(f"      copy:      {cp['file']}:{cp.get('line')}")
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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _fail_config(msg):
    print(f"ssot-check: {msg}", file=sys.stderr)
    return 2


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
    result = check(root, manifest, fetch=args.fetch)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render_check(result)
    return result["exit_code"]


def cmd_discover(args):
    root = args.root or "."
    manifest_ignore = []
    mpath = os.path.join(root, ".ssot.yaml")
    if os.path.isfile(mpath):
        try:
            manifest_ignore = load_manifest(mpath).get("ignore_paths") or []
        except ManifestError:
            manifest_ignore = []
    result = discover(root, ignore_paths=args.ignore or manifest_ignore)
    if args.json:
        print(json.dumps(result, indent=2))
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
    result = explain(root, manifest, args.name, fetch=args.fetch)
    if result is None:
        return _fail_config(f"no fact named {args.name!r} in {args.manifest}")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render_explain(result, args.name)
    return 0


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
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("validate", help="check manifest structure")
    add_common(v)
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("discover", help="propose drift-prone facts from prose")
    d.add_argument("--root", default=".", help="tree to scan (default: .)")
    d.add_argument("--ignore", action="append",
                   help="glob to skip (repeatable)")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.set_defaults(func=cmd_discover)

    e = sub.add_parser("explain", help="show one fact's values")
    add_common(e)
    e.add_argument("name", help="fact name")
    e.add_argument("--json", action="store_true", help="machine-readable output")
    _add_fetch(e)
    e.set_defaults(func=cmd_explain)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # Default subcommand is `check`. Global options (--version, -h/--help) are
    # owned by the top-level parser and must not be swept into the default
    # subcommand — doing so routed `--version` to `check`, which rejected it.
    known = {"check", "validate", "discover", "explain"}
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
