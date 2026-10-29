# copyfind

**Find copy-pasted code, and report each block once — with every file it was pasted into. One file, no dependencies, no install.**

```bash
python copyfind.py examples/
```

```
copyfind 1.0.0 - 4 files, 81 lines, 364 tokens, 0.0s

duplication  71.4%   260 of 364 tokens
clone classes 1   4 locations, 195 tokens removable

worst files
   74.7%         65 /        87  orders/invoice.py
   66.3%         65 /        98  orders/order.py
   71.4%         65 /        91  orders/refund.py
   73.9%         65 /        88  orders/shipment.py

clone classes (top 1 of 1)
  #1  65 tokens, 13 lines, 4 copies, 195 removable
      orders/invoice.py:6-18
      orders/order.py:6-18
      orders/refund.py:6-18
      orders/shipment.py:6-18
```

Four files copy the same `validate_line`. That is **one** line of the report, listing
four places. Most clone detectors print one row per pair — eleven rows for twelve
copies — and leave you to work out that they are all the same block.

## Why this exists

`jscpd` is the tool everyone uses for this, and it is good at finding clones. What it
will not do is tell you that a block appears in ten files: it reports
`file1 ↔ file2`, `file1 ↔ file3`, … and you reconstruct the group by hand. The
feature request for this has been open since April 2019
([kucherenko/jscpd#221](https://github.com/kucherenko/jscpd/issues/221)).

The same tool also has a long-standing complaint about licence headers being
counted as clones
([#788](https://github.com/kucherenko/jscpd/issues/788)). Twenty identical lines at
the top of every file are, technically, a twenty-line clone. Here they cannot be
one: comments never become tokens, so there is nothing to compare. That is a
property of the tokenizer, not a flag you have to remember to pass.

`copyfind` is the same idea with the reporting decision made the other way round.

## Install

Nothing to install. Python 3.9 or newer, standard library only.

```bash
curl -O https://raw.githubusercontent.com/z15346183030/copyfind/main/copyfind.py
python copyfind.py --help
```

## Use

```bash
# scan the current directory
python copyfind.py .

# scan one tree, fail CI above 3% duplication
python copyfind.py src/ --threshold 3

# find renamed copies too (a = b + c  matches  x = y + z)
python copyfind.py src/ --names

# shorter blocks, more results
python copyfind.py src/ -k 25 -l 4

# what would be scanned, and why some files are not
python copyfind.py . --list-files

# machine-readable
python copyfind.py . --json
```

Exit code is `0` normally, `1` when `--threshold` is exceeded, `2` when there is
nothing to scan. A CI step is one line:

```bash
python copyfind.py src/ --threshold 5 || exit 1
```

| Flag | Default | Meaning |
|---|---|---|
| `-k, --min-tokens` | 40 | shortest duplicated block, in tokens |
| `-l, --min-lines` | 5 | shortest duplicated block, in lines |
| `-t, --threshold` | – | exit 1 above this duplication percentage |
| `--names` | off | ignore identifier names, finds renamed copies |
| `--top` | 10 | how many clone classes to print |
| `--json` | off | machine-readable report |
| `--no-gitignore` | off | do not read `.gitignore` files |
| `--list-files` | off | print the files that would be scanned, then exit |

Zero configuration: every flag has a default, and `python copyfind.py .` on a real
project produces a usable answer.

## What counts as a copy

Source is tokenized the way the language defines it — comments and whitespace are
dropped, a string literal is one token, and `x=-1` and `x = -1` produce the same
stream. Two runs of 40 tokens that match exactly are a clone.

Languages: Python, JavaScript / TypeScript / JSX / TSX, Go, Rust, Java, Kotlin,
Swift, C#, Scala, PHP, Dart, C / C++ / Objective-C, Ruby, shell, SQL, Lua, CSS /
SCSS / LESS, HTML / Vue / Svelte, Lisp / Clojure.

That is 11 comment-and-string grammars covering 25 extensions, not 224 hand-tuned
tokenizers. A clone that only appears after parsing a template literal is not the
clone that costs you a week.

## How it works

1. **Walk.** `.gitignore` is read as git reads it — anchoring, `!` negation,
   directory-only patterns, `**`, last match wins. `node_modules`, `vendor`,
   `dist`, `target`, `site-packages` and friends are skipped by default.
2. **Tokenize.** One scanner, driven by a per-language table of comment markers
   and quote characters. Comments never enter the stream, so a `#` inside a
   string is not a comment — and a twenty-line licence header contributes
   nothing to compare, which is why it never shows up as a clone.
3. **Hash.** A rolling hash over every window of `-k` tokens. The first position
   for a hash is stored as a bare integer; only a second occurrence promotes it to
   a list. Windows whose 40 tokens are mostly the same few values — a table of
   `"key": "value",` pairs — are dropped before any comparison, because repetition
   is not evidence of copying.
4. **Extend and group.** Matches are extended left and right to their maximal run,
   grouped into classes by token content, and each class is reported once with all
   of its locations.

The block boundaries are snapped forward to the next line break, so a clone never
gets reported as starting mid-statement at `= 0`.

## How it compares

On a corpus of 109 files with 6 blocks planted at known line ranges (`A` copied
into 12 files, `F` into 40, `D` renamed, the rest 2–3 files each):

| | copyfind | copyfind `--names` | jscpd 5.4.2 |
|---|---|---|---|
| locations found | 59 / 61 | 61 / 61 | 59 / 61 |
| blocks reported as one entry | 5 / 6 | 6 / 6 | 2 / 6 |
| rows to read | **5** | 7 | **54** |
| rows for the 12-copy block | 1 | 1 | 11 |
| rows for the 40-copy block | 1 | 1 | 39 |
| time | 0.43 s | 0.44 s | 0.59 s |

On the CPython 3.13 standard library (537 files, 270,680 lines, 967,604 tokens),
both tools with `-k 40 -l 5`:

| | copyfind | jscpd 5.4.2 |
|---|---|---|
| time | 3.1 s | 1.9 s |
| peak memory | 156 MB | 12 MB |
| duplication | 9.8% | 8.7% |
| rows to read | 363 | 907 |

jscpd is a compiled Rust binary and it shows: twice as fast, and thirteen times less
memory.
The token definitions differ, so the two percentages are not the same measurement.
What is comparable is the shape of the output — 363 blocks against 907 pairs.

## Limits

- **Exact clones only, by default.** A block with one edited line is two shorter
  blocks. `--names` covers renamed copies, not edited ones. There is no
  `--max-gap-lines` and no syntax-tree pass.
- **`--names` over-reports.** Collapsing every identifier makes any two functions
  with the same shape identical, including ones that were never copied. That is
  what Type-2 detection means; it is not what you want for a refactoring list.
- **Memory grows with the number of windows.** About 160 bytes per window, so a
  million-token project needs about 150 MB. A ten-million-token monorepo will
  want a different tool.
- **No baselines, no SARIF, no HTML, no badge, no language server.** One report,
  three formats: console, JSON, exit code.

## Tests

```bash
python -m unittest discover -s tests
```

57 tests: gitignore semantics, tokenizer edge cases, class grouping, coverage
accounting, comment headers, and the CLI end to end.

## Licence

MIT.
