# recipes/browse-md

Markdown document browser — one or more files as a navigable heading tree.

**One-line summary:** parses each `.md` file into a tree of headings
(h1..h6), with the file body (or a per-row byte-slice) shown in the
preview pane, optionally rendered through md2ansi. Positionals may be
files or directories; `FILE.md#section` deep-links straight to a heading.

**Demonstrates:**

- A file format parsed into a lazy tree via a shared library —
  `md2ansi_lib.md2ansi_doc` builds the document model from md2ansi's own
  scanner, so the tree and the colored render share one grammar. Row
  titles use the library's display form: heading sigil and inline
  markup stripped.
- Structured tuple ids — `('file', abspath)` for the per-file roots and
  `('content', abspath, line)` for every heading / text row; hashable,
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
- Dim `[text]` rows synthesised for a non-empty body run that precedes a
  scope's first sub-heading (a leaf section's body stays in its preview).
- Cross-file multi-select viewing — `V` groups selected rows by file,
  merges their byte ranges, and concatenates the slices with a per-file
  separator.
- Conflict-safe section editing — `E` opens the cursor row's section in
  `$EDITOR` (a file row edits the file in place) and re-applies the
  result through `md2ansi_lib`'s hash-addressed splice: the section is
  re-found by content hash (or a hash-verified line range) even if the
  file changed underneath the editor, and a genuinely ambiguous apply
  offers a Cancel / Return-to-editor retry dialog instead of guessing.
- Section insert via marker mode — `a` / `i` enter the framework's
  placement marker; the confirmed `(relation, dest_id)` (before / after /
  first) maps 1:1 onto the splice position, and `$EDITOR` opens a small
  new-section template. Same conflict-safe apply and retry dialog as `E`.
- Stdin documents stay editable — `E` / `a` apply to the in-memory copy
  (flagged "not saved to disk"), so a piped document can still be
  restructured or used to export a section through the editor.
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
./recipes/browse-md README.md#install    # deep-link straight to a section
./recipes/browse-md --help               # print usage + keys and exit
cat NOTES.md | ./recipes/browse-md -     # one document from stdin (row titled '-')
git show HEAD:README.md | ./recipes/browse-md - --root "$(git rev-parse --show-toplevel)"
```

Keys: `m` toggle md2ansi coloring, `M` page the preview through
`$MDCAT` / `mdcat`, `V` page the source in `$PAGER`, `E` edit the
section under the cursor in `$EDITOR` (conflict-safe splice on save),
`a` / `i` insert a new section at a marker-chosen position, `→` expand
(auto-expands a single-heading file), `Ctrl-R` re-slurp every file
from disk.

**Source:** [`recipes/browse-md`](../../recipes/browse-md)

---

*[← All recipes](../recipes.md)*
