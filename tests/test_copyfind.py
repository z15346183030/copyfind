import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import copyfind as cf  # noqa: E402


def keys(text, lang="python", names=True):
    """The token stream as (kind, text) pairs, comparable across tokenizations."""
    vocab = {}
    ids, _ = cf.tokenize(text, lang, vocab, keep_names=names)
    back = {v: k for k, v in vocab.items()}
    return [back[i] for i in ids]


def tokens(text, lang="python", names=True):
    return keys(text, lang, names)


def words(text, lang="python", names=True):
    """Token stream as readable strings, for readable assertions."""
    out = []
    for kind, text_ in keys(text, lang, names):
        out.append("<str>" if kind == "S" else text_)
    return out


def build(files, min_tokens=10, min_lines=3, max_postings=200, names=True):
    """Tokenize a {name: text} mapping and run detection over it."""
    vocab = {}
    sources = []
    for name, text in files.items():
        f = cf.SourceFile(name, cf.lang_for(name))
        f.ids, f.lines = cf.tokenize(text, "python", vocab, keep_names=names)
        f.total_lines = text.count("\n")
        sources.append(f)
    classes, covered, truncated = cf.detect(sources, min_tokens, min_lines, max_postings)
    return sources, classes, covered, truncated


BODY = """def merge_intervals(intervals):
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [tuple(pair) for pair in merged]
"""

# Same shape, every identifier renamed. An exact detector must not see it; a
# detector that ignores names must.
RENAMED = """def collapse(spans):
    if not spans:
        return []
    ranked = sorted(spans, key=lambda item: item[0])
    output = [list(ranked[0])]
    for lo, hi in ranked[1:]:
        tail = output[-1]
        if lo <= tail[1]:
            tail[1] = max(tail[1], hi)
        else:
            output.append([lo, hi])
    return [tuple(item) for item in output]
"""


class GitignoreTests(unittest.TestCase):
    def rules(self, text):
        return cf.parse_gitignore(text)

    def test_simple_name_matches_at_any_depth(self):
        rules = self.rules("*.log\n")
        self.assertTrue(rules[0].regex.search("a/b/c.log"))
        self.assertTrue(rules[0].regex.search("c.log"))
        self.assertFalse(rules[0].regex.search("c.logx"))

    def test_leading_slash_anchors_to_root(self):
        rules = self.rules("/build\n")
        self.assertTrue(rules[0].regex.search("build"))
        self.assertFalse(rules[0].regex.search("src/build"))

    def test_slash_inside_pattern_anchors(self):
        rules = self.rules("docs/*.md\n")
        self.assertTrue(rules[0].regex.search("docs/a.md"))
        self.assertFalse(rules[0].regex.search("src/docs/a.md"))

    def test_trailing_slash_is_directory_only(self):
        rules = self.rules("cache/\n")
        self.assertTrue(rules[0].dir_only)
        self.assertTrue(rules[0].regex.search("cache"))

    def test_negation_is_recorded(self):
        rules = self.rules("*.log\n!keep.log\n")
        self.assertFalse(rules[0].negate)
        self.assertTrue(rules[1].negate)

    def test_double_star_crosses_directories(self):
        rules = self.rules("a/**/b\n")
        self.assertTrue(rules[0].regex.search("a/b"))
        self.assertTrue(rules[0].regex.search("a/x/b"))
        self.assertTrue(rules[0].regex.search("a/x/y/b"))

    def test_trailing_double_star_matches_everything_below(self):
        rules = self.rules("vendor/**\n")
        self.assertTrue(rules[0].regex.search("vendor/a.py"))
        self.assertTrue(rules[0].regex.search("vendor/x/y.py"))

    def test_comment_and_blank_lines_skipped(self):
        self.assertEqual(self.rules("# a comment\n\n   \n"), [])

    def test_question_mark_is_one_character(self):
        rules = self.rules("a?.py\n")
        self.assertTrue(rules[0].regex.search("ab.py"))
        self.assertFalse(rules[0].regex.search("abc.py"))

    def test_character_class(self):
        rules = self.rules("x[0-9].py\n")
        self.assertTrue(rules[0].regex.search("x5.py"))
        self.assertFalse(rules[0].regex.search("xa.py"))


class IgnoreSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def write(self, rel, text):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_nested_gitignore_applies_below_its_directory(self):
        self.write(".gitignore", "*.log\n")
        self.write("sub/.gitignore", "!important.log\n")
        ig = cf.IgnoreSet()
        ig.load(self.tmp)
        self.assertTrue(ig.ignored("a/b/x.log", False))
        self.assertFalse(ig.ignored("sub/important.log", False))
        self.assertTrue(ig.ignored("other/important.log", False))

    def test_last_matching_rule_wins(self):
        self.write(".gitignore", "*.py\n!keep.py\n")
        ig = cf.IgnoreSet()
        ig.load(self.tmp)
        self.assertTrue(ig.ignored("drop.py", False))
        self.assertFalse(ig.ignored("keep.py", False))

    def test_discover_skips_ignored_files(self):
        self.write(".gitignore", "generated/\n*.min.js\n")
        self.write("a.py", "x = 1\n")
        self.write("generated/b.py", "y = 2\n")
        self.write("c.min.js", "z=3\n")
        names = [rel for _, rel, _ in cf.discover(self.tmp)]
        self.assertIn("a.py", names)
        self.assertNotIn("generated/b.py", names)
        self.assertNotIn("c.min.js", names)


class TokenizerTests(unittest.TestCase):
    def test_comment_marker_inside_string_is_not_a_comment(self):
        self.assertEqual(words('s = "// not a comment"'), ["s", "=", "<str>"])

    def test_hash_inside_string_is_not_a_comment(self):
        self.assertEqual(words('s = "a # b"'), ["s", "=", "<str>"])

    def test_line_comment_is_dropped(self):
        self.assertEqual(words("a = 1  # trailing\nb = 2"), ["a", "=", "1", "b", "=", "2"])

    def test_block_comment_is_dropped(self):
        self.assertEqual(words("a /* one\ntwo */ b", "clike"), ["a", "b"])

    def test_whitespace_never_changes_the_token_stream(self):
        self.assertEqual(tokens("x=-1"), tokens("x = -1"))
        self.assertEqual(tokens("f(a,b)"), tokens("f( a , b )"))

    def test_known_multi_character_operator_is_one_token(self):
        self.assertEqual(words("a == b"), ["a", "==", "b"])
        self.assertEqual(words("a === b", "clike"), ["a", "===", "b"])

    def test_triple_quoted_string_is_one_token_and_keeps_line_count(self):
        vocab = {}
        ids, lines = cf.tokenize('x = """one\ntwo"""\ny = 1', "python", vocab)
        self.assertEqual(len(ids), 6)
        self.assertEqual(list(lines), [1, 1, 1, 3, 3, 3])

    def test_escaped_quote_does_not_end_the_string(self):
        self.assertEqual(words('s = "a\\"b"'), ["s", "=", "<str>"])

    def test_unterminated_string_stops_at_newline(self):
        vocab = {}
        ids, lines = cf.tokenize("s = 'abc\ny = 2", "python", vocab)
        self.assertEqual(list(lines), [1, 1, 1, 2, 2, 2])

    def test_names_off_collapses_identifiers(self):
        self.assertEqual(tokens("total = first + second", names=False),
                         tokens("amount = left + right", names=False))

    def test_names_off_keeps_keywords_distinct(self):
        self.assertNotEqual(tokens("if flag:\n    go()", names=False),
                            tokens("while flag:\n    go()", names=False))

    def test_names_off_keeps_literals_distinct(self):
        self.assertNotEqual(tokens("x = 1", names=False), tokens("x = 2", names=False))

    def test_strings_compare_equal_regardless_of_content(self):
        self.assertEqual(tokens('x = "hello"'), tokens('x = "goodbye"'))

    def test_shebang_line_is_a_comment(self):
        self.assertEqual(words("#!/usr/bin/env python\nx = 1"), ["x", "=", "1"])


class CommentHeaderTests(unittest.TestCase):
    """A licence header is not a clone, and that is not a feature.

    Comments never reach the token stream, so twenty identical lines at the top
    of every file contribute nothing to compare. This is worth a test because it
    is the one thing jscpd users complain about most, and the answer here is
    structural rather than a flag that switches the header off.
    """

    HEADER = (
        "# Copyright 2026 Example Ltd.\n"
        "# Licensed under the MIT licence.\n"
        "# See LICENSE for details.\n"
        "#\n"
        "# This file is part of a demo.\n"
    )

    def test_a_comment_header_contributes_no_tokens(self):
        self.assertEqual(tokens(self.HEADER), [])

    def test_identical_headers_over_different_code_are_not_a_clone(self):
        files = {
            "a.py": self.HEADER + "\ndef alpha(count):\n    total = 0\n"
                    "    for i in range(count):\n        total += i * 3\n    return total\n",
            "b.py": self.HEADER + "\ndef beta(items):\n    out = []\n"
                    "    for item in items://n        out.append(item.strip())\n    return out\n",
            "c.py": self.HEADER + "\ndef gamma(table):\n    keys = sorted(table)\n"
                    "    return {k: table[k] for k in keys}\n",
        }
        _, classes, _, _ = build(files, min_tokens=8, min_lines=3)
        self.assertEqual(classes, [])

    def test_line_numbers_after_a_header_are_still_correct(self):
        files = {
            "a.py": self.HEADER + "\n" + BODY,
            "b.py": self.HEADER + "\n" + BODY,
        }
        _, classes, _, _ = build(files)
        self.assertEqual(classes[0]["locations"][0]["start"], 7)


class DetectionTests(unittest.TestCase):
    def test_n_way_duplicate_is_one_class_not_n_squared_pairs(self):
        files = {f"m{i:02d}.py": BODY + f"\ndef only_{i}(x):\n    return x + {i}\n"
                 for i in range(12)}
        _, classes, _, _ = build(files)
        self.assertEqual(len(classes), 1)
        self.assertEqual(len(classes[0]["locations"]), 12)
        self.assertEqual(classes[0]["removable"], classes[0]["tokens"] * 11)

    def test_locations_report_real_line_numbers(self):
        files = {
            "a.py": "x = 0\n" + BODY,
            "b.py": "y = 0\nz = 0\n" + BODY,
        }
        _, classes, _, _ = build(files)
        locs = {m["file"]: (m["start"], m["end"]) for m in classes[0]["locations"]}
        self.assertEqual(locs["a.py"], (2, 13))
        self.assertEqual(locs["b.py"], (3, 14))

    def test_two_unrelated_clones_are_two_classes(self):
        other = """def render_row(cells):
    parts = []
    for cell in cells:
        parts.append(str(cell).strip())
    joined = "|".join(parts)
    return "|" + joined + "|"
"""
        files = {
            "a.py": BODY, "b.py": BODY,
            "c.py": other, "d.py": other,
        }
        _, classes, _, _ = build(files)
        self.assertEqual(len(classes), 2)

    def test_block_below_min_lines_is_not_reported(self):
        short = "a = 1\nb = 2\nc = 3\n"
        files = {"a.py": short, "b.py": short}
        _, classes, _, _ = build(files, min_tokens=5, min_lines=6)
        self.assertEqual(classes, [])

    def test_block_above_min_lines_is_reported(self):
        files = {"a.py": BODY, "b.py": BODY}
        _, classes, _, _ = build(files, min_tokens=5, min_lines=4)
        self.assertEqual(len(classes), 1)

    def test_duplicate_inside_one_file_is_reported(self):
        files = {"a.py": BODY + "\n" + BODY}
        _, classes, _, _ = build(files)
        self.assertEqual(len(classes), 1)
        starts = sorted(m["start"] for m in classes[0]["locations"])
        self.assertEqual(len(starts), 2)
        self.assertGreater(starts[1], starts[0])

    def test_coverage_counts_each_token_once(self):
        files = {"a.py": BODY + "\ndef only_a():\n    return 1\n",
                 "b.py": BODY + "\ndef only_b():\n    return 2\n",
                 "c.py": BODY + "\ndef only_c():\n    return 3\n"}
        sources, classes, covered, _ = build(files)
        total = sum(len(f.ids) for f in sources)
        dup = sum(sum(c) for c in covered)
        self.assertLess(dup, total)
        self.assertGreater(dup, 0)
        self.assertEqual(len(classes[0]["locations"]), 3)

    def test_identical_files_are_fully_duplicated(self):
        files = {"a.py": BODY, "b.py": BODY}
        sources, _, covered, _ = build(files)
        self.assertEqual(sum(covered[0]), len(sources[0].ids))
        self.assertEqual(sum(covered[1]), len(sources[1].ids))

    def test_renamed_copy_is_missed_without_names_flag(self):
        files = {"a.py": BODY, "b.py": RENAMED}
        _, classes, _, _ = build(files)
        self.assertEqual(classes, [])

    def test_renamed_copy_is_found_with_names_flag(self):
        files = {"a.py": BODY, "b.py": RENAMED}
        _, classes, _, _ = build(files, names=False)
        self.assertEqual(len(classes), 1)

    def test_edited_copy_is_not_reported_as_exact(self):
        edited = BODY.replace("last[1] = max(last[1], end)", "last[1] = end")
        files = {"a.py": BODY, "b.py": edited}
        _, classes, _, _ = build(files)
        for c in classes:
            self.assertLess(c["tokens"], 100)

    def test_postings_are_capped_for_boilerplate(self):
        files = {f"f{i}.py": BODY for i in range(40)}
        _, classes, _, stats = build(files, max_postings=8)
        self.assertGreater(stats["truncated"], 0)
        self.assertEqual(len(classes), 1)
        self.assertLessEqual(len(classes[0]["locations"]), 9)

    def test_no_location_is_reported_twice(self):
        # Regression: coverage used to be marked on the trimmed region, so the
        # trimmed-off prefix stayed open and the same block was found again
        # from a second anchor, showing up as two identical locations.
        files = {"a.py": "x = 0\n" + BODY + "\n" + BODY}
        _, classes, _, _ = build(files)
        for c in classes:
            keys = [(m["file"], m["start"], m["end"]) for m in c["locations"]]
            self.assertEqual(len(keys), len(set(keys)), keys)

    def test_table_of_string_pairs_is_not_a_clone(self):
        # Two dictionaries with different keys but the same shape tokenize to
        # `S : S ,` repeated. That is a table, not a copy, and must not be
        # reported - it was, before windows with too few distinct tokens were
        # dropped.
        left = "CONFIG = {\n" + "".join(
            f'    "alpha_{i}": "value_{i}",\n' for i in range(14)) + "}\n"
        right = "SETTINGS = {\n" + "".join(
            f'    "beta_{i}": "other_{i}",\n' for i in range(14)) + "}\n"
        _, classes, _, _ = build({"a.py": left, "b.py": right},
                                 min_tokens=40, min_lines=5)
        self.assertEqual(classes, [])

    def test_classes_are_ordered_by_how_much_they_would_save(self):
        big = BODY
        small = """def clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v
"""
        files = {
            "a.py": big, "b.py": big, "c.py": big,
            "d.py": small, "e.py": small,
        }
        _, classes, _, _ = build(files, min_tokens=5, min_lines=4)
        self.assertGreater(len(classes), 1)
        self.assertGreaterEqual(classes[0]["removable"], classes[-1]["removable"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "copyfind.py")] + list(args),
            capture_output=True, text=True,
        )

    def test_json_report_shape(self):
        self.write("a.py", BODY)
        self.write("b.py", BODY)
        rc = self.run_cli(self.tmp, "--json")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        rep = json.loads(rc.stdout)
        self.assertEqual(rep["files"], 2)
        self.assertEqual(len(rep["classes"]), 1)
        self.assertEqual(rep["duplication"]["percent"], 100.0)
        self.assertIn("locations", rep["classes"][0])

    def test_threshold_fails_the_run(self):
        self.write("a.py", BODY)
        self.write("b.py", BODY)
        rc = self.run_cli(self.tmp, "--threshold", "10")
        self.assertEqual(rc.returncode, 1)
        self.assertIn("FAIL", rc.stdout)

    def test_threshold_passes_the_run(self):
        self.write("a.py", "a = 1\n")
        self.write("b.py", "b = 2\n")
        rc = self.run_cli(self.tmp, "--threshold", "10")
        self.assertEqual(rc.returncode, 0)
        self.assertIn("ok", rc.stdout)

    def test_empty_directory_explains_itself(self):
        rc = self.run_cli(self.tmp)
        self.assertEqual(rc.returncode, 2)
        self.assertIn("no source files found", rc.stderr)

    def test_missing_path_explains_itself(self):
        rc = self.run_cli(os.path.join(self.tmp, "nope"))
        self.assertEqual(rc.returncode, 2)
        self.assertIn("no such path", rc.stderr)

    def test_list_files_shows_what_would_be_scanned(self):
        self.write("a.py", "x = 1\n")
        self.write("b.txt", "ignore me\n")
        rc = self.run_cli(self.tmp, "--list-files")
        self.assertEqual(rc.returncode, 0)
        self.assertEqual(rc.stdout.strip(), "a.py")

    def test_single_file_argument(self):
        self.write("a.py", BODY)
        rc = self.run_cli(os.path.join(self.tmp, "a.py"), "--json")
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(json.loads(rc.stdout)["files"], 1)

    def test_console_output_names_the_places(self):
        self.write("a.py", BODY)
        self.write("b.py", BODY)
        rc = self.run_cli(self.tmp)
        self.assertIn("a.py:", rc.stdout)
        self.assertIn("b.py:", rc.stdout)
        self.assertIn("clone classes", rc.stdout)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def write(self, rel, text):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full) or self.tmp, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_node_modules_is_skipped_by_default(self):
        self.write("node_modules/x.js", "a\n")
        self.write("src/a.js", "b\n")
        names = [r for _, r, _ in cf.discover(self.tmp)]
        self.assertEqual(names, ["src/a.js"])

    def test_unknown_extension_is_skipped(self):
        self.write("notes.txt", "hello\n")
        self.assertEqual(cf.discover(self.tmp), [])

    def test_binary_file_is_skipped(self):
        self.write("a.py", "x = 1\n")
        with open(os.path.join(self.tmp, "bin.py"), "wb") as fh:
            fh.write(b"\x00\x01\x02binary")
        self.assertIsNone(cf._read_text(os.path.join(self.tmp, "bin.py")))
        self.assertIsNotNone(cf._read_text(os.path.join(self.tmp, "a.py")))

    def test_language_is_guessed_from_extension(self):
        self.assertEqual(cf.lang_for("a.py"), "python")
        self.assertEqual(cf.lang_for("a.tsx"), "clike")
        self.assertEqual(cf.lang_for("a.rs"), "rust")
        self.assertIsNone(cf.lang_for("a.bin"))


if __name__ == "__main__":
    unittest.main()
