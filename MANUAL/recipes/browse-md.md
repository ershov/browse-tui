# recipes/browse-md

Markdown document browser — one or more files as a navigable heading tree.

**One-line summary:** parses each `.md` file into a tree of headings
(h1..h6), with the file body (or a per-row byte-slice) shown in the
preview pane, optionally rendered through md2ansi. Positionals may be
files or directories; `FILE.md#section` deep-links straight to a heading.

**Demonstrates:**

- A file format parsed into a lazy tree via a shared library —
  `md_doc.build_doc_tree` derives the structure from md2ansi's own
  scanner, so the tree and the colored render share one grammar.
- Structured tuple ids — `('file', abspath)` for the per-file roots and
  `('content', abspath, line)` for every heading / list row; hashable,
  no string parsing.
- Multi-file roots with no synthetic parent — `get_children(None)`
  returns the per-file roots in argv order, each labelled relative to the
  project root (the git root, else cwd) so same-named files in different
  directories stay distinct.
- Anchor deep-links — `FILE.md#name` / `FILE.md#<line>` seed the initial
  scope; with several anchored files the first in argv order wins.
- A preview that re-renders to the pane width — md2ansi word-wraps to
  `preview_width`, refetched via `on_resize` → `drop_preview_cache` when
  the layout changes.
- Optional list-item rows (`-l` / `--list` / `--lists`) plus dim `[text]`
  nodes synthesised for loose body text between headings.
- Cross-file multi-select actions — `V` / `E` group selected rows by
  file, merge their byte ranges, and concatenate the slices with a
  per-file separator.
- Repeatable `--root DIR` — extra base directories for resolving a
  document's relative cross-file references (tried after the file's own
  directory), and the only way references resolve for a stdin (`-`)
  document.
- An `on_enter` / `→` override that flips expand/collapse and
  auto-expands a single-heading document in one keystroke.

**Usage:**

```bash
./recipes/browse-md                      # every .md in the current directory
./recipes/browse-md README.md            # one file (opens scoped into its headings)
./recipes/browse-md docs/                # every .md directly inside docs/
./recipes/browse-md -l NOTES.md          # also surface list items as rows
./recipes/browse-md README.md#install    # deep-link straight to a section
cat NOTES.md | ./recipes/browse-md -     # one document from stdin (row titled '-')
git show HEAD:README.md | ./recipes/browse-md - --root "$(git rev-parse --show-toplevel)"
```

Keys: `m` toggle md2ansi coloring, `M` page the preview through
`$MDCAT` / `mdcat`, `V` page the source in `$PAGER`, `E` edit the
source in `$EDITOR`, `→` expand (auto-expands a single-heading file),
`Ctrl-R` re-slurp every file from disk.

**Source:** [`recipes/browse-md`](../../recipes/browse-md)

---

*[← All recipes](../recipes.md)*
