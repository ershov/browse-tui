# browse-md edit/insert + md_doc reduction — design

Date: 2026-08-06. Status: implemented (v1); cross-document addendum of
2026-08-07 in progress — see the final section.

## Motivation

`md2ansi_lib` now ships a chapter-level document-ops layer: a document model
(`md2ansi_doc` → `M2A_Doc` / `M2A_Node`), a query DSL with hash addressing
(`md2ansi_resolve`), and extract/splice operations with a content-hash
safeguard (`md2ansi_extract` / `md2ansi_splice`). Two consequences for this
repo:

1. `md_doc.py`'s structural half (its own heading-tree builder) duplicates
   `md2ansi_doc` and can be deleted once the recipes consume `M2A_Doc`
   directly.
2. `browse-md` can gain real **edit** and **insert** operations on sections,
   with the splice layer guaranteeing we never corrupt a document that
   changed underneath us.

Guiding principle throughout: **simplicity** — reuse the library's public
API, no bespoke re-implementations, smallest recipe-side logic that works.

**Hard constraint: `recipes/md2ansi_lib.py` is synced verbatim from an
external project and MUST NOT be modified here.** Everything below is
recipe-/`md_doc`-side only.

## Part 1 — adopt the `md2ansi_lib` document model

### What `md_doc.py` keeps

The reference/launcher half, unchanged in behavior: `find_md_refs`,
`resolve_md_ref`, `find_git_root`, `ref_label`, `resolve_refs`,
`md_heading_trigger`, the launcher-row builders (`references_umbrella`,
`launcher_row`, `launch`), and the process-wide parse cache
(`get_doc` / `clear_cache`). `node_at_line` also stays (it is
shape-agnostic: `M2A_Node` carries the same `line_offset` / `children`
fields).

### What changes

* `get_doc(abspath)` now returns `(text, M2A_Doc)` — the cached tree is
  `md2ansi_doc(text)` instead of `build_doc_tree(text)`.
* The whole structural section is **deleted**: `MdNode`, `build_doc_tree`,
  `_scan_events`, `_walk_list`, `_line_starts` / `_line_of`, the
  title-stripping regexes, and the `_SCAN_KINDS_*` / event plumbing.
* `browse-md` and `browse-claude` build their `Item` trees from
  `M2A_Doc.tree` (`M2A_Node`s) instead of `MdNode`s.

### Behavioral deltas (all agreed)

* **Lists are dropped.** `browse-md` loses the `-l` / `--list` / `--lists`
  flag and every `[ul]` / `[ol]` code path (`_INCLUDE_LISTS`, the list
  branches of parsing/boundary/tagging, list-related docs and tests).
  Headings only.
* **Titles switch to the library's stripped display form**
  (`M2A_Node.title`): heading sigil gone (as before) and inline markup
  stripped (new — no more literal `**` / backticks in row titles). Anchor
  matching (`_resolve_anchor` / `_display_title`) and slug generation
  (`_slug`) operate on the stripped titles; GitHub-style slugs over stripped
  text are *more* correct, but review those paths for consistency.
* **`#text` row-set rule.** `md2ansi_doc` gives *every* heading a `#text`
  first child (zero-width when the body is blank) plus a preamble node at
  `tree[0]`. To preserve today's visible rows, a recipe shows a text run
  only when **`byte_size > 0` and it has at least one heading sibling**
  (i.e. it is the intro run of a scope that also has sub-headings). Empty
  runs and leaf-chapter bodies are filtered out — a leaf heading must not
  sprout a text child it never had.

### `-h` / `--help`

`browse-md -h` currently dies `unrecognised option: -h`. Fix: pop
`-h` / `--help` before the leftover-option scan; when present, print the
full help text (the existing usage/keys block) to **stdout** and exit 0.

## Part 2 — edit and insert in `browse-md`

### Addressing: capture at action time

No query is needed to *read* a section — the recipe already holds the
parsed text and node offsets. When an edit/insert action fires on a node we
capture:

* the extent slice `text[byte_offset : byte_offset + byte_size]` (editor
  payload, for edit),
* the extent's **full sha256** (`hashlib.sha256(extent.encode()).hexdigest()`),
* its 1-based inclusive **line range** `(a, b)`,
* the node kind (`heading` vs `text`) and, for headings, its level.

### Apply chain (the single shared helper)

On editor save, re-read the document (from disk; from the in-memory copy
for stdin docs) and apply, trying in order — each step a
`try/except ValueError` around public `md2ansi_lib` calls, no error-message
parsing:

1. **Hash-primary** (heading nodes only): query `#hash:<full sha256>`.
   * edit: `md2ansi_splice(text, q, content, where='replace',
     hash_prefix=<full hash>)`.
   * insert: `md2ansi_splice(text, q, content, where=<relation>)` — the
     hash query itself verifies the anchor.
   This transparently survives *other* regions of the file changing
   underneath the editor. The full 64-hex hash means ambiguity can only be
   true byte-identical duplicate sections.
2. **Range fallback** (also the *primary* for `text`-run nodes, which hash
   addressing cannot name): verify first — `t = md2ansi_resolve(text,
   f"#{a}-{b}")`; require `t.hash == <full hash>[:12]`. Then:
   * edit: `md2ansi_splice(text, f"#{a}-{b}", content, where='replace',
     hash_prefix=<full hash>)`.
   * insert `before`/`after`: splice on the same range query.
   * insert `first` (child position; heading anchors only): the range
     verification proved the extent is byte-identical at lines `a..b`, so
     splice with the unique query `f"#h{level}:{a}"`, `where='first'`.
   This covers duplicate sections sitting at their original position.
3. **Both failed** → the retry dialog (below). Duplicates *and* a shift is
   genuinely ambiguous; rare-squared.

After a successful splice: write the file back (or store the in-memory
copy), bust caches (`md_doc.clear_cache` + the recipe's per-file state),
reparse, preserve cursor/expansion where the ids allow, and log the
mutation to the recipe log (that is what tests assert).

### Edit (`E`)

* Acts on the **cursor node only — selection is ignored** (replaces the
  current multi-select/throwaway-temp behavior of `E`).
* File-root row: unchanged — `$EDITOR` on the file in place, then reparse.
* Heading / text-run row: extent → temp `.md` → `$EDITOR` → apply chain
  (`where='replace'`). If the editor exits non-zero or content is
  unchanged, no write happens.
* **Retry loop**: when the apply chain fails (target vanished), present a
  modal choice — **"Cancel edit"** / **"Return to editor"** — and loop:
  return re-opens the same temp file (the user's work is never lost; they
  can save it elsewhere from the editor), then re-read + re-apply. Cancel
  discards. Mind the framework's async-modal conventions
  (`docs/superpowers/specs/2026-06-16-async-modal-dialogs-design.md`).

### Insert (`a` / `i`)

* Both keys bound to the same action ("add"/"insert").
* Uses the framework's `ctx.insert(label, on_confirm)` marker mode
  (browse-plan's technique): the user places the marker, confirm yields
  `(relation, dest_id)` with `relation ∈ 'before' | 'after' | 'first'` —
  mapping 1:1 onto `md2ansi_splice`'s `where`.
* On confirm: `$EDITOR` on a temp file seeded with a small template
  (e.g. `## New section`), then the apply chain anchored on `dest_id`'s
  node with `where=relation`. Empty/unchanged-template content cancels.
* Valid anchors (v1): a primary file's root row (`relation` maps onto the
  root query `/`, e.g. `first` → after the preamble) and its
  heading / text-run rows. Reference-subtree rows, launcher rows and the
  refs umbrella are rejected with a `ctx.flash` explaining v1 scope.
* Same retry dialog on apply failure.

### Stdin documents (`browse-md -`)

Edit/insert stay **enabled** — the apply pipeline is identical except the
document is re-read from, and written back to, the recipe's in-memory copy
(no disk I/O). After a successful apply, flash a clear notice that the
change is **in-memory only** (e.g. `applied to in-memory copy — not saved
to disk`). This also keeps `E` useful as "export a section via the editor".

### Context menu

Add the new rows to the content/file context menus, reusing the same
handlers as the keybindings (single-sourced, hotkey shown in parens, per
the existing convention): `Edit section (E)`, `Insert here (a)`.

## Non-goals (v1)

* **Cross-file edit/insert** on `[md]` reference-subtree rows — future
  ticket (they resolve to a real file via their chain, so the apply chain
  extends naturally later).
* Heading releveling on insert/move; the splice layer never massages
  content beyond the library's trailing-newline rule.
* A frontmatter row / frontmatter editing.
* Any change to `md2ansi_lib.py` itself.

## Testing

* Unit: apply-chain cases (hash hit; shifted file → hash hit; duplicate
  sections → range fallback; duplicate + shift → dialog), stdin in-memory
  apply, insert relation→where mapping, anchor rejection, `-h`/`--help`,
  dropped `-l`, title/text-run row-set expectations.
* Context-menu builders via `test/unit/test_browse_md_context_menu.py`
  (the open-by-key path is not pty-triggerable).
* Full `./run-tests-parallel.sh` before merge (UI tests spawn the real
  binary; scoped runs hide rendering regressions).

## Addendum (2026-08-07): cross-document edit / insert / move

v1 scoped `E` / `a` / `x` (the `x` move landed post-v1, same machinery) to
primary-file rows. This addendum extends all three to the `[md]`
reference-subtree rows — and, for move, across documents. The apply chain
is UNCHANGED; what changes is row routing and, for move only, a two-file
transaction.

### Row → document resolution (one helper)

A single helper maps any file-backed row id to `(path, text, node)`:

* `('file', path)` → the primary file's root (`fs.file_root`, `fs.text`).
* `('content', path, line)` → `fs.by_id`, as today.
* `('md', anchor, chain, None)` → the referenced doc's root:
  `path = chain[-1]`, `(text, doc) = _md_chain_doc(anchor, chain)`.
* `('md', anchor, chain, line)` → the heading / text run:
  `md_doc.node_at_line(doc.tree, line)` over the same `_md_chain_doc`.

The References umbrella (`('refs', …)`), launcher rows and stale ids stay
rejected. Capture text for md rows comes from the `get_doc` cache —
staleness is SAFE because the apply chain re-reads the file fresh and
hash-verifies before any write (that is the chain's whole purpose).

### Edit / insert on reference rows

* `E` on an md heading / text row: the same temp-file pipeline, persisted
  to `chain[-1]`. `E` on the md-doc ROOT row: in-place `$EDITOR` on the
  referenced file + refresh (parity with primary file roots; aliases the
  context menu's "Edit referenced file").
* `a` anchored on md rows: same pipeline, `where=relation`; the md-doc
  root row takes the root-`/` arm. Cursor landing constructs the
  `('md', anchor, chain, line)` id (best-effort, as today).
* The md context menu gains the section rows (`Edit section (E)` /
  `Insert here (a)` / `Move section (x)`) for heading / text rows,
  single-sourced with the keybindings.

### Cross-document move

`x`'s source and destination may now be rows of DIFFERENT documents
(primary or referenced, stdin included via the path-sentinel
read/persist):

* Same document → the existing single-surgery path, no-op rule intact.
* Different documents → resolve BOTH endpoints against fresh reads of the
  two documents FIRST (any failure → flash, nothing written); then write
  the DESTINATION insert, then the SOURCE cut. An interruption between
  the two writes leaves a duplicate, never a loss. The block keeps the
  trailing-newline guarantee; the within-extent no-op rule cannot apply
  across documents by construction.

### Non-goals (unchanged)

Releveling; frontmatter rows; any `md2ansi_lib.py` change.
