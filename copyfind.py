#!/usr/bin/env python3
"""copyfind - find copy-pasted code, grouped by clone class.

One file, standard library only. Point it at a directory; it prints how much of
the code is duplicated, which blocks are duplicated, and every place each block
was copied to.

The unit of the report is a *clone class*: one block of code and all the files it
appears in. Ten copies of the same function are one entry with ten locations,
not forty-five pairs.

Usage:
    python copyfind.py .
    python copyfind.py src/ --min-tokens 60 --threshold 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from array import array
from collections import defaultdict

VERSION = "1.0.0"

# 64-bit rolling hash. Multiplication wraps by masking, which is exactly
# arithmetic modulo 2**64 and much cheaper than a prime modulus in Python.
_MASK = (1 << 64) - 1
_BASE = 1_000_003

DEFAULT_MIN_TOKENS = 40
DEFAULT_MIN_LINES = 5
DEFAULT_MAX_POSTINGS = 200
DEFAULT_BOILERPLATE_FILES = 3
DEFAULT_MAX_BYTES = 1_500_000

# Share of distinct tokens a window needs before it counts as evidence of a
# copy. A table of `"key": "value",` pairs scores about 0.10.
_MIN_DISTINCT = 0.20


# --------------------------------------------------------------------------
# Language table
# --------------------------------------------------------------------------
# A language is three things: how a line comment starts, how a block comment is
# delimited, and which quote characters open a string. Everything else about
# tokenizing is the same code for every language, which is why this table is
# twenty lines and not twenty parsers.
#
# Deliberately missing: 200+ niche formats, embedded-language extraction,
# JSX/TSX AST parsing. A clone that only shows up after parsing a template
# literal is not the clone that hurts.

LANGS = {
    "python": (("#",), (), ('"""', "'''", '"', "'")),
    "clike": (("//",), (("/*", "*/"),), ('"', "'")),
    "go": (("//",), (("/*", "*/"),), ("`", '"', "'")),
    "rust": (("//",), (("/*", "*/"),), ('"', "'")),
    "ruby": (("#",), (), ('"', "'")),
    "shell": (("#",), (), ('"', "'")),
    "sql": (("--",), (("/*", "*/"),), ("'", '"')),
    "lua": (("--",), (("--[[", "]]"),), ('"', "'")),
    "css": ((), (("/*", "*/"),), ('"', "'")),
    "html": ((), (("<!--", "-->"),), ('"', "'")),
    "lisp": ((";",), (), ('"',)),
}

EXT_LANG = {
    ".py": "python", ".pyi": "python", ".pyx": "python",
    ".js": "clike", ".mjs": "clike", ".cjs": "clike", ".jsx": "clike",
    ".ts": "clike", ".tsx": "clike", ".mts": "clike", ".cts": "clike",
    ".c": "clike", ".h": "clike", ".cc": "clike", ".cpp": "clike",
    ".cxx": "clike", ".hpp": "clike", ".hh": "clike",
    ".java": "clike", ".kt": "clike", ".kts": "clike", ".swift": "clike",
    ".cs": "clike", ".scala": "clike", ".php": "clike", ".dart": "clike",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql",
    ".lua": "lua",
    ".css": "css", ".scss": "css", ".less": "css",
    ".html": "html", ".htm": "html", ".vue": "html", ".svelte": "html",
    ".el": "lisp", ".clj": "lisp", ".cljs": "lisp",
}

# Words that must survive --names. Without this, `if` and `while` both become
# the same placeholder and a loop matches a branch.
KEYWORDS = frozenset("""
if else elif then for while do switch case default break continue return goto
function func fn def class struct enum impl trait interface extends implements
public private protected internal static final const let var val new delete
this self super nil null none true false True False None NULL nullptr
try catch except finally throw raises raise throws yield await async and or not
in is with as pass lambda match where import from export package use mod require
include sizeof typeof instanceof void int float double char long short bool
boolean string str bytes byte u8 u16 u32 u64 i8 i16 i32 i64 f32 f64 usize isize
""".split())

# Multi-character operators, longest first. Coalescing arbitrary runs would make
# `x=-1` tokenize as `=-` while `x = -1` gives `=` `-`, so only known operators
# are joined.
_OPERATORS = sorted(
    {
        "**=", "<<=", ">>=", ">>>", "===", "!==", "...", "->", "=>", "::",
        "==", "!=", "<=", ">=", "&&", "||", "++", "--", "+=", "-=", "*=", "/=",
        "%=", "&=", "|=", "^=", "<<", ">>", "**", "//", ":=", "?.", "??",
        "..", "<-", "<=>", "**",
    },
    key=len,
    reverse=True,
)
_PUNCT = frozenset("()[]{},;.:@#?$\\&|^~<>=+-*/%!")
_OP_START = frozenset("&|^~<>=+-*/%!")


class SourceFile:
    __slots__ = ("rel", "lang", "ids", "lines", "total_lines", "header")

    def __init__(self, rel, lang):
        self.rel = rel
        self.lang = lang
        self.ids = array("i")
        self.lines = array("i")
        self.total_lines = 0
        self.header = 0


# --------------------------------------------------------------------------
# .gitignore
# --------------------------------------------------------------------------
# Implemented from the gitignore specification rather than from a glob library:
# anchoring by a slash, `!` negation, directory-only patterns, `**`, and
# last-match-wins. A file that git ignores must not be scanned, and a file that
# git tracks must not be silently dropped.

class IgnoreRule:
    __slots__ = ("negate", "dir_only", "regex")

    def __init__(self, negate, dir_only, regex):
        self.negate = negate
        self.dir_only = dir_only
        self.regex = regex


def _glob_body(pat):
    out = []
    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                if i + 2 < n and pat[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
                i += 1
            else:
                cls = pat[i + 1:j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def parse_gitignore(text):
    rules = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negate = False
        if line.startswith("!"):
            negate = True
            line = line[1:]
        elif line.startswith("\\!") or line.startswith("\\#"):
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        if not line:
            continue
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        elif "/" in line:
            anchored = True
        body = _glob_body(line)
        pattern = ("^" + body + "$") if anchored else ("(?:^|/)" + body + "$")
        rules.append(IgnoreRule(negate, dir_only, re.compile(pattern)))
    return rules


class IgnoreSet:
    """Rules from every .gitignore above a path, evaluated last-match-wins."""

    def __init__(self):
        self.by_dir = {}
        self.has_negation = False

    def load(self, root):
        for dirpath, dirnames, filenames in os.walk(root):
            if ".git" in dirnames:
                dirnames.remove(".git")
            if ".gitignore" not in filenames:
                continue
            full = os.path.join(dirpath, ".gitignore")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    rules = parse_gitignore(fh.read())
            except OSError:
                continue
            if not rules:
                continue
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            if rel == ".":
                rel = ""
            self.by_dir[rel] = rules
            if any(r.negate for r in rules):
                self.has_negation = True

    def ignored(self, rel, is_dir):
        if not self.by_dir:
            return False
        parts = rel.split("/")
        verdict = False
        # Shallowest .gitignore first, so deeper rules win.
        for depth in range(len(parts)):
            key = "/".join(parts[:depth])
            rules = self.by_dir.get(key)
            if not rules:
                continue
            sub = "/".join(parts[depth:])
            for rule in rules:
                if rule.dir_only and not is_dir:
                    continue
                if rule.regex.search(sub):
                    verdict = not rule.negate
        return verdict


DEFAULT_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", "target", "vendor", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".idea", ".vscode", ".next", ".nuxt",
    ".gradle", ".cache", "coverage", "htmlcov", ".eggs", "site-packages",
}


def lang_for(path):
    base = os.path.basename(path)
    ext = os.path.splitext(base)[1].lower()
    return EXT_LANG.get(ext)


def discover(root, use_gitignore=True, skip_dirs=None, max_bytes=DEFAULT_MAX_BYTES):
    """Yield (abs_path, rel_path, lang) for every source file worth scanning."""
    skip = set(skip_dirs or DEFAULT_SKIP_DIRS)
    ig = IgnoreSet()
    if use_gitignore:
        ig.load(root)

    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        kept = []
        for d in sorted(dirnames):
            if d in skip:
                continue
            rel = (rel_dir + "/" + d) if rel_dir else d
            if use_gitignore and ig.ignored(rel, True):
                # A negation deeper down could re-include something, so only
                # prune when no .gitignore in the tree uses one.
                if not ig.has_negation:
                    continue
                if ig.ignored(rel + "/x", False):
                    continue
            kept.append(d)
        dirnames[:] = kept

        for name in sorted(filenames):
            lang = lang_for(name)
            if not lang:
                continue
            rel = (rel_dir + "/" + name) if rel_dir else name
            if use_gitignore and ig.ignored(rel, False):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > max_bytes:
                    continue
            except OSError:
                continue
            out.append((full, rel, lang))
    return out


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------
# Comments are dropped, whitespace is dropped, strings collapse to one token
# each. What is left is the shape of the code, which is what a copy preserves
# and what a rename does not change.

def _read_text(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


_IDENT_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)
_NUMBER_RE = re.compile(r"\d(?:\w|[.](?=\d))*", re.UNICODE)
_WS = frozenset(" \t\r\n\f\v")

_DISPATCH = {}


def _dispatch(lang):
    """First character -> the markers that could start there.

    A comment or string can only begin with one of a handful of characters, so
    a plain `startswith` chain over every marker at every character is the
    slowest possible way to tokenize. Dispatch on the first byte instead: for
    letters and digits the answer is "nothing", and that is most of the file.
    """
    d = _DISPATCH.get(lang)
    if d is not None:
        return d
    line_c, block_c, strings = LANGS[lang]
    table = {}
    # Blocks before line markers: in Lua `--[[` is a block and `--` a line
    # comment, so testing the shorter one first would swallow the longer.
    for op, cl in sorted(block_c, key=lambda p: -len(p[0])):
        table.setdefault(op[0], ([], [], []))[0].append((op, cl))
    for m in line_c:
        table.setdefault(m[0], ([], [], []))[1].append(m)
    for q in sorted(strings, key=len, reverse=True):
        table.setdefault(q[0], ([], [], []))[2].append(q)
    _DISPATCH[lang] = table
    return table


def tokenize(text, lang, vocab, keep_names=True, start_line=1, start_offset=0):
    table = _dispatch(lang)

    ids = array("i")
    lines = array("i")
    n = len(text)
    i = start_offset
    line = start_line
    get = vocab.get
    append_id = ids.append
    append_line = lines.append

    def intern(key):
        vid = get(key)
        if vid is None:
            vid = len(vocab)
            vocab[key] = vid
        return vid

    while i < n:
        c = text[i]

        if c in _WS:
            if c == "\n":
                line += 1
            i += 1
            continue

        entry = table.get(c)
        if entry is not None:
            blocks, line_marks, quotes = entry
            hit = False
            for op, cl in blocks:
                if text.startswith(op, i):
                    j = text.find(cl, i + len(op))
                    j = n if j < 0 else j + len(cl)
                    line += text.count("\n", i, j)
                    i = j
                    hit = True
                    break
            if hit:
                continue
            for m in line_marks:
                if text.startswith(m, i):
                    j = text.find("\n", i)
                    i = n if j < 0 else j
                    hit = True
                    break
            if hit:
                continue
            for q in quotes:
                if text.startswith(q, i):
                    qn = len(q)
                    j = i + qn
                    while j < n:
                        ch = text[j]
                        if ch == "\\":
                            j += 2
                            continue
                        if text.startswith(q, j):
                            j += qn
                            break
                        if ch == "\n" and qn == 1:
                            break
                        j += 1
                    else:
                        j = n
                    if j > n:
                        j = n
                    start = line
                    line += text.count("\n", i, j)
                    append_id(intern(("S", "")))
                    append_line(start)
                    i = j
                    hit = True
                    break
            if hit:
                continue

        if c.isalpha() or c == "_" or c == "$":
            m = _IDENT_RE.match(text, i)
            if m is None:
                j = i + 1
                word = text[i:j]
            else:
                j = m.end()
                word = m.group()
            if not keep_names and word not in KEYWORDS:
                append_id(intern(("I", "")))
            else:
                append_id(intern(("I", word)))
            append_line(line)
            i = j
            continue

        if c.isdigit():
            m = _NUMBER_RE.match(text, i)
            j = m.end() if m else i + 1
            append_id(intern(("N", text[i:j])))
            append_line(line)
            i = j
            continue

        op = c
        if c in _OP_START:
            for cand in _OPERATORS:
                if text.startswith(cand, i):
                    op = cand
                    break
        append_id(intern(("P", op)))
        append_line(line)
        i += len(op)

    return ids, lines


def header_extent(text, lang, max_lines=80):
    """Number of leading lines occupied by a comment block, 0 if none.

    License headers are identical in every file of a project, which is exactly
    what a clone detector is built to find. Counting them as duplication buries
    the clones that matter, so they are cut off before tokenizing and reported
    separately.
    """
    line_c = LANGS[lang][0]
    if not line_c:
        return 0
    lines = text.split("\n")
    count = 0
    last = 0
    for idx, ln in enumerate(lines[:max_lines]):
        s = ln.strip()
        if not s:
            continue
        if any(s.startswith(m) for m in line_c):
            count += 1
            last = idx + 1
            continue
        break
    return last if count >= 2 else 0


def header_signature(text, lang):
    n = header_extent(text, lang)
    if not n:
        return None
    body = []
    for ln in text.split("\n")[:n]:
        s = ln.strip()
        if s:
            body.append(re.sub(r"\d", "#", s))
    if len(body) < 2:
        return None
    return hashlib.sha1("\n".join(body).encode("utf-8", "replace")).hexdigest(), n


def offset_of_line(text, n_lines):
    if n_lines <= 0:
        return 0
    pos = 0
    for _ in range(n_lines):
        nxt = text.find("\n", pos)
        if nxt < 0:
            return len(text)
        pos = nxt + 1
    return pos


# --------------------------------------------------------------------------
# Rolling hash index
# --------------------------------------------------------------------------

def build_index(files, k, max_postings):
    """Map window hash -> posting list. A posting packs (file id, token index).

    Most windows are unique, so the first posting for a hash is stored as a bare
    int and only a second occurrence promotes it to a list. That keeps a repo
    with a million windows inside a few hundred megabytes instead of several
    gigabytes of one-element lists.
    """
    index = {}
    truncated = 0
    bp = pow(_BASE, k - 1, 1 << 64)
    base = _BASE
    mask = _MASK

    for fid, f in enumerate(files):
        ids = f.ids
        m = len(ids)
        if m < k:
            continue
        h = 0
        for t in ids[:k]:
            h = (h * base + t + 1) & mask
        limit = m - k + 1
        for start in range(limit):
            if start:
                h = ((h - (ids[start - 1] + 1) * bp) * base + ids[start + k - 1] + 1) & mask
            post = (fid << 32) | start
            cur = index.get(h)
            if cur is None:
                index[h] = post
            elif type(cur) is int:
                index[h] = [cur, post]
            else:
                if len(cur) < max_postings:
                    cur.append(post)
                else:
                    truncated += 1
    return index, truncated


class UnionFind:
    __slots__ = ("parent",)

    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent
        root = x
        while p.get(root, root) != root:
            root = p[root]
        while p.get(x, x) != x:
            nxt = p[x]
            p[x] = root
            x = nxt
        p.setdefault(root, root)
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _leading_line_run(lines, start, length):
    """How many of the region's first tokens sit on the region's first line."""
    first = lines[start]
    n = 0
    while n < length - 1 and lines[start + n] == first:
        n += 1
    return n


def detect(files, k, min_lines, max_postings):
    index, truncated = build_index(files, k, max_postings)

    groups = [v for v in index.values() if type(v) is list and len(v) > 1]
    # Left to right, so the first window of a clone is always processed before
    # the windows inside it. That is what makes coverage suppression work: the
    # first window extends over the whole clone and the rest are skipped.
    groups.sort(key=lambda g: g[0])

    covered = [bytearray(len(f.ids)) for f in files]
    uf = UnionFind()
    loc_len = {}
    loc_digest = {}
    one = b"\x01"
    flat = 0

    for postings in groups:
        # A window of `"key": "value",` pairs has four distinct tokens in forty
        # positions, and matches every other table in the project. Repetition is
        # not evidence of copying, so windows that are mostly the same few
        # tokens are dropped before any pair work happens.
        first = postings[0]
        probe = files[first >> 32].ids
        p0 = first & 0xFFFFFFFF
        window = probe[p0:p0 + k]
        if len(set(window)) < _MIN_DISTINCT * len(window):
            flat += 1
            continue

        alive = []
        for p in postings:
            fid = p >> 32
            st = p & 0xFFFFFFFF
            if covered[fid].find(one, st, st + k) < 0:
                alive.append(p)

        n = len(alive)
        for x in range(n):
            a = alive[x]
            fa = a >> 32
            sa = a & 0xFFFFFFFF
            if covered[fa].find(one, sa, sa + k) >= 0:
                continue
            for y in range(x + 1, n):
                b = alive[y]
                fb = b >> 32
                sb = b & 0xFFFFFFFF
                if covered[fb].find(one, sb, sb + k) >= 0:
                    continue
                if files[fa].ids[sa:sa + k] != files[fb].ids[sb:sb + k]:
                    continue

                ia = files[fa].ids
                ib = files[fb].ids
                la = len(ia)
                lb = len(ib)
                r = k
                while sa + r < la and sb + r < lb and ia[sa + r] == ib[sb + r]:
                    r += 1
                l = 0
                while sa - l - 1 >= 0 and sb - l - 1 >= 0 and ia[sa - l - 1] == ib[sb - l - 1]:
                    l += 1

                start_a = sa - l
                start_b = sb - l
                length = r + l
                raw_a = start_a
                raw_b = start_b
                raw_len = length

                # A maximal run of equal tokens often starts in the middle of a
                # statement, because the tail of the previous line happens to
                # match too (`= 0` before a `def`). Reporting `a.py:1-13` for a
                # block that starts on line 2 sends the reader to the wrong
                # place, so both sides slide forward to the next line break.
                # Both must start mid-line, and both must move by the same
                # amount, or the two sides stop corresponding.
                fa_lines = files[fa].lines
                fb_lines = files[fb].lines
                if length > k and fa_lines[start_a + length - 1] > fa_lines[start_a]:
                    skip_a = 0
                    if start_a > 0 and fa_lines[start_a - 1] == fa_lines[start_a]:
                        skip_a = _leading_line_run(fa_lines, start_a, length)
                    skip_b = 0
                    if start_b > 0 and fb_lines[start_b - 1] == fb_lines[start_b]:
                        skip_b = _leading_line_run(fb_lines, start_b, length)
                    shift = min(skip_a, skip_b, length - k)
                    if shift:
                        start_a += shift
                        start_b += shift
                        length -= shift

                span = fa_lines[start_a + length - 1] - fa_lines[start_a] + 1
                if span < min_lines:
                    continue

                # Coverage is marked over the untrimmed region. Marking the
                # trimmed one leaves the prefix open, and a later window
                # starting there reports the same block a second time.
                covered[fa][raw_a:raw_a + raw_len] = one * raw_len
                covered[fb][raw_b:raw_b + raw_len] = one * raw_len
                uf.union((fa, start_a), (fb, start_b))
                loc_len[(fa, start_a)] = length
                loc_len[(fb, start_b)] = length
                loc_digest[(fa, start_a)] = hash(ia[start_a:start_a + length].tobytes())
                loc_digest[(fb, start_b)] = hash(ib[start_b:start_b + length].tobytes())

    # A component says "these places are connected by matches", not "these
    # places hold the same block". Chaining can join a block to a shorter
    # overlap of it, and a shorter overlap to something else. Grouping by the
    # actual token content keeps a class honest: every location in it really is
    # the same block, not merely adjacent to one.
    buckets = defaultdict(list)
    for key in loc_len:
        buckets[(uf.find(key), loc_digest.get(key))].append(key)

    classes = []
    for locs in buckets.values():
        if len(locs) < 2:
            continue
        members = []
        seen = set()
        for fid, start in sorted(locs):
            length = loc_len[(fid, start)]
            first = files[fid].lines[start]
            last = files[fid].lines[start + length - 1]
            # Two anchors in the same block can trim to the same line range.
            # One place is one location, however many times it was found.
            key = (fid, first, last)
            if key in seen:
                continue
            seen.add(key)
            members.append({
                "file": files[fid].rel,
                "start": first,
                "end": last,
                "tokens": length,
            })
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m["file"], m["start"]))
        tokens = min(m["tokens"] for m in members)
        lines = max(m["end"] - m["start"] + 1 for m in members)
        classes.append({
            "tokens": tokens,
            "lines": lines,
            "locations": members,
            "removable": tokens * (len(members) - 1),
        })

    classes.sort(key=lambda c: (-c["removable"], -c["tokens"], c["locations"][0]["file"]))
    for i, c in enumerate(classes, 1):
        c["id"] = i

    return classes, covered, {"truncated": truncated, "flat": flat}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def build_report(root, files, classes, covered, elapsed, headers, stats, args):
    total_tokens = sum(len(f.ids) for f in files)
    total_lines = sum(f.total_lines for f in files)
    dup_tokens = 0
    per_file = []
    for i, f in enumerate(files):
        d = sum(covered[i])
        dup_tokens += d
        if d:
            per_file.append({
                "file": f.rel,
                "dup": d,
                "tokens": len(f.ids),
                "percent": round(_pct(d, len(f.ids)), 1),
            })
    per_file.sort(key=lambda r: (-r["dup"], r["file"]))

    return {
        "version": VERSION,
        "root": os.path.abspath(root),
        "elapsed": round(elapsed, 3),
        "files": len(files),
        "lines": total_lines,
        "tokens": total_tokens,
        "duplication": {
            "tokens": dup_tokens,
            "percent": round(_pct(dup_tokens, total_tokens), 2),
        },
        "classes": classes,
        "files_report": per_file,
        "boilerplate": headers,
        "skipped_windows": stats,
        "settings": {
            "min_tokens": args.min_tokens,
            "min_lines": args.min_lines,
            "ignore_names": args.names,
        },
    }


def _n(x):
    return f"{x:,}"


def render_console(rep, top):
    out = []
    add = out.append
    add(f"copyfind {rep['version']} - {_n(rep['files'])} files, "
        f"{_n(rep['lines'])} lines, {_n(rep['tokens'])} tokens, "
        f"{rep['elapsed']:.1f}s")
    add("")

    dup = rep["duplication"]
    add(f"duplication  {dup['percent']:.1f}%   {_n(dup['tokens'])} of "
        f"{_n(rep['tokens'])} tokens")
    locs = sum(len(c["locations"]) for c in rep["classes"])
    add(f"clone classes {len(rep['classes'])}   {locs} locations, "
        f"{_n(sum(c['removable'] for c in rep['classes']))} tokens removable")
    add("")

    if rep["boilerplate"]:
        total_files = sum(h["files"] for h in rep["boilerplate"])
        add(f"boilerplate  {len(rep['boilerplate'])} shared header block(s) in "
            f"{total_files} files - excluded from the count")
        for h in rep["boilerplate"][:2]:
            add(f"  {h['files']} files x {h['lines']} lines   e.g. {h['sample']}")
        add("")

    if rep["files_report"]:
        add("worst files")
        for r in rep["files_report"][:5]:
            add(f"  {r['percent']:5.1f}%  {_n(r['dup']):>9} / {_n(r['tokens']):>9}  {r['file']}")
        add("")

    if not rep["classes"]:
        add("no duplicated blocks found")
        return "\n".join(out)

    if top <= 0:
        return "\n".join(out).rstrip()

    shown = rep["classes"][:top]
    add(f"clone classes (top {len(shown)} of {len(rep['classes'])})")
    for c in shown:
        n = len(c["locations"])
        add(f"  #{c['id']}  {_n(c['tokens'])} tokens, {c['lines']} lines, "
            f"{n} copies, {_n(c['removable'])} removable")
        for m in c["locations"][:8]:
            add(f"      {m['file']}:{m['start']}-{m['end']}")
        if n > 8:
            add(f"      ... and {n - 8} more")
        add("")
    return "\n".join(out).rstrip()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run(argv=None):
    ap = argparse.ArgumentParser(
        prog="copyfind",
        description="Find copy-pasted code, grouped by clone class.",
        epilog="Exit code 1 when duplication exceeds --threshold.",
    )
    ap.add_argument("paths", nargs="*", default=["."],
                    help="directories or files to scan (default: .)")
    ap.add_argument("-k", "--min-tokens", type=int, default=DEFAULT_MIN_TOKENS,
                    help=f"shortest duplicated block in tokens (default {DEFAULT_MIN_TOKENS})")
    ap.add_argument("-l", "--min-lines", type=int, default=DEFAULT_MIN_LINES,
                    help=f"shortest duplicated block in lines (default {DEFAULT_MIN_LINES})")
    ap.add_argument("-t", "--threshold", type=float, default=None,
                    help="exit 1 when duplication percent is above this")
    ap.add_argument("--names", action="store_true",
                    help="ignore identifier names (finds renamed copies)")
    ap.add_argument("--top", type=int, default=10,
                    help="how many clone classes to print (default 10)")
    ap.add_argument("--boilerplate", type=int, default=DEFAULT_BOILERPLATE_FILES,
                    help="suppress headers shared by this many files (default 3, 0 disables)")
    ap.add_argument("--max-postings", type=int, default=DEFAULT_MAX_POSTINGS,
                    help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-gitignore", action="store_true",
                    help="do not read .gitignore files")
    ap.add_argument("--list-files", action="store_true",
                    help="print the files that would be scanned, then exit")
    ap.add_argument("--version", action="version", version=f"copyfind {VERSION}")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    roots = args.paths
    targets = []
    for root in roots:
        if os.path.isdir(root):
            targets.extend(discover(root, use_gitignore=not args.no_gitignore))
        elif os.path.isfile(root):
            lang = lang_for(root)
            if lang:
                targets.append((root, root, lang))
        else:
            sys.stderr.write(f"copyfind: no such path: {root}\n")
            return 2

    if args.list_files:
        for _, rel, _lang in targets:
            print(rel)
        return 0

    if not targets:
        sys.stderr.write(
            "copyfind: no source files found.\n"
            "  Nothing matched the extensions it knows (Python, JS/TS, Go, Rust,\n"
            "  Java, C/C++, Ruby, shell, SQL, CSS, HTML, Lua, Lisp).\n"
            "  Point it at a code directory, e.g. `python copyfind.py src`.\n"
        )
        return 2

    started = time.perf_counter()

    texts = []
    for full, rel, lang in targets:
        text = _read_text(full)
        texts.append(text)

    # Pass 1: which leading comment blocks are project boilerplate.
    sig_count = defaultdict(int)
    sig_lines = {}
    sig_sample = {}
    for (full, rel, lang), text in zip(targets, texts):
        if text is None:
            continue
        got = header_signature(text, lang)
        if got:
            sig, n = got
            sig_count[sig] += 1
            sig_lines[sig] = n
            if sig not in sig_sample:
                sig_sample[sig] = rel

    headers = []
    suppressed = set()
    if args.boilerplate > 0:
        for sig, cnt in sig_count.items():
            if cnt >= args.boilerplate:
                suppressed.add(sig)
                headers.append({"files": cnt, "lines": sig_lines[sig],
                                "sample": sig_sample[sig]})
        headers.sort(key=lambda h: (-h["files"], h["sample"]))

    vocab = {}
    files = []
    for (full, rel, lang), text in zip(targets, texts):
        if text is None:
            continue
        f = SourceFile(rel, lang)
        f.total_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        skip = 0
        if suppressed:
            got = header_signature(text, lang)
            if got and got[0] in suppressed:
                skip = got[1]
        f.header = skip
        off = offset_of_line(text, skip)
        ids, lines = tokenize(text, lang, vocab, keep_names=not args.names,
                              start_line=skip + 1, start_offset=off)
        f.ids = ids
        f.lines = lines
        files.append(f)
    texts = None

    if not files:
        sys.stderr.write("copyfind: no readable source files found.\n")
        return 2

    classes, covered, stats = detect(files, args.min_tokens, args.min_lines,
                                     args.max_postings)
    elapsed = time.perf_counter() - started

    rep = build_report(roots[0] if len(roots) == 1 else " ".join(roots),
                       files, classes, covered, elapsed, headers, stats, args)

    if args.json:
        json.dump(rep, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(render_console(rep, args.top))

    if args.threshold is not None:
        ok = rep["duplication"]["percent"] <= args.threshold
        if not args.json:
            verdict = "ok" if ok else "FAIL"
            print(f"\nthreshold {args.threshold:.1f}%: {verdict} "
                  f"({rep['duplication']['percent']:.2f}%)")
        return 0 if ok else 1
    return 0


def main():
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
