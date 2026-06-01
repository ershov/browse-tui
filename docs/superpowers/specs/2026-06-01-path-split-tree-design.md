# browse-tui — `--path-sep` Path-Split Tree Mode Design

**Date:** 2026-06-01
**Status:** Draft (pre-implementation)
**Supersedes nothing.** Adds a fourth hierarchy-detection mode to
`Browser.from_flat_tree` and a `--path-sep` CLI flag.

## Motivation

`browse-tui` builds a tree from eager (`--root-cmd`) input via
`Browser.from_flat_tree`, which detects hierarchy three ways:

- **parent-pointer** — any row carries a `parent` field naming its
  parent's id.
- **depth-coded** — any row carries a `depth` integer; a row at depth
  `d+1` is a child of the most recent row at depth `d`.
- **flat** — no hint; every row is a direct child of `root_id`.

A very common shape isn't covered: a flat list of *path-like* strings
(`docs/api/auth.md`, `a.b.c`, `/etc/passwd`) that already encode their
hierarchy in a delimiter. Today such a list renders flat — the user
must pre-compute `parent` or `depth` columns themselves. The column
parsers (`tsv`, `ifs:CHARS`, `split:REGEX`, …) split a *record into
columns*; none of them build a *hierarchy*. There is no
delimiter-to-tree path.

This design adds that path: split each entry's `id` on a separator and
synthesize the intervening nodes.

## Goals

1. Add a `path_sep: str | None = None` keyword to
   `Browser.from_flat_tree`. When set (and no more-explicit hint is
   present — see Precedence), the rows are expanded into a tree by
   splitting each row's `id` on `path_sep`.
2. Add a standalone `--path-sep CHARS` CLI flag (eager mode only) that
   threads through to that keyword.
3. Synthesize intermediate nodes for every prefix of every entry, so a
   bare list like `docs/api/auth.md` produces real, expandable `docs`
   and `docs/api` nodes.
4. Keep node ids as valid, reconstructable paths (full prefix paths),
   so `$TUI_ID`, preview, and actions operate on real paths.

## Non-Goals

- **Lazy mode.** `--path-sep` applies only to eager `--root-cmd` input.
  Combining it with `--children-cmd` is a CLI error (see CLI surface).
  Lazy listing is inherently per-parent and already expresses `/`
  nesting through the source command.
- **Splitting a field other than `id`.** The separator always splits
  `id`. Metadata columns ride along onto the leaf unchanged.
- **CSV-style quoting / escaping of the separator.** A separator inside
  an entry is always a boundary; there is no escape syntax. (Choose a
  separator that does not occur within a segment.)

## Data model

Given a separator `sep` and a flat list of rows, each row's `id` is
split into segments and a tree is built. Node identity is the **full
prefix path**.

### Node kinds

- **Leaf node** — corresponds to an input row. `id` = the row's full
  path; `title` = the last segment, *unless the row carried an explicit
  `title`* (then that wins); every other field on the row (`tag`,
  `tag_style`, arbitrary extras like `size`) attaches here.
- **Intermediate node** — a prefix that is not itself an input row.
  `id` = the prefix path; `title` = its own segment; carries no
  metadata. Created the first time a descendant references it.
- **Merged node** — a prefix that *is also* an explicit input row
  (e.g. the input contains both `docs/api` and `docs/api/auth.md`). The
  explicit row's `title`/metadata win; the node is still expandable
  because it has children.

### `has_children`

Derived from tree structure: a node is expandable iff it has ≥1 child.
In path-split mode the incoming `has_children` column is ignored
(the tree is fully known and eager, so there is nothing to lazily
expand into).

### Ordering

Siblings appear in **first-seen input order**. An intermediate node is
created at the position where its first child appears. This matches the
stable, input-order behavior of flat mode.

## Empty-segment handling (path-aware)

Splitting on `sep` can yield empty segments from a leading separator,
a trailing separator, or a doubled separator. Rules:

- **Leading separator preserved.** `/etc/passwd` → top-level node
  `/etc` (title `etc`), child `/etc/passwd` (title `passwd`). Absolute
  and relative paths stay distinct: `/etc/x` ≠ `etc/x`.
- **Doubled separators collapse.** `a//b` → `a` › `a/b`.
- **Trailing separator ignored.** `a/b/` → `a` › `a/b` (no empty leaf).
- **Empty or all-separator entries are skipped** (no node produced).

Concretely: let `lead = sep` if `id` starts with `sep` else `''`, and
`segs = [s for s in id.split(sep) if s]` (the non-empty pieces). The
node at prefix depth `k` (1-indexed) has `id = lead + sep.join(segs[:k])`
and `title = segs[k-1]`. So a single leading `sep` is retained on every
id while interior runs and a trailing `sep` collapse away:
`/etc/passwd` → `/etc` › `/etc/passwd`; `a//b` and `a/b/` → `a` › `a/b`.

### Worked example

```
input ids:                       resulting tree (id shown):
  docs/api/auth.md                 docs              ▼  id=docs
  docs/api/users.md                 ├ api            ▼  id=docs/api
  docs/README.md                    │  ├ auth.md        id=docs/api/auth.md
  /etc/passwd                       │  └ users.md       id=docs/api/users.md
                                    └ README.md         id=docs/README.md
                                  /etc                ▼  id=/etc
                                    └ passwd             id=/etc/passwd
```

`docs` and `docs/api` and `/etc` are synthesized intermediate nodes;
the rest are leaves.

## Precedence

Path-split is a *derivation rule*, less explicit than a per-row
structural column. It slots into the existing "most-explicit-wins"
chain just above flat:

```
parent-pointer  >  depth-coded  >  path-split  >  flat
```

- If any row has an explicit `parent`, parent-pointer mode wins and
  `path_sep` is ignored.
- Else if any row has an explicit `depth`, depth-coded mode wins and
  `path_sep` is ignored.
- Else if `path_sep` is set, path-split mode runs.
- Else flat.

Because the user typed `--path-sep` deliberately, silently ignoring it
would be confusing. When `path_sep` is set **and** the rows carry a
`parent`/`depth` column, the CLI emits a single stderr warning and
proceeds with the more-explicit mode:

```
browse-tui: --path-sep ignored: rows carry explicit parent/depth
```

(The warning lives in the CLI layer; `from_flat_tree` itself just
applies the precedence silently, so recipes calling the API don't get
stderr noise.)

## Architecture / insertion point

A pure helper does the expansion; `from_flat_tree` reuses its existing,
tested parent-pointer machinery.

### `expand_path_rows(rows, sep)` (new helper, data layer)

- Input: the raw rows as handed to `from_flat_tree` (each may be
  `Item`, `str`, `tuple`, or `dict`), and the separator string.
- Output: a new list of rows (dicts) with explicit `id`, `parent`, and
  `title` set, including one row per synthesized intermediate node, in a
  deterministic order that yields first-seen sibling ordering.
- It runs on rows **before `to_item` coercion** so it can distinguish an
  explicit `title` from the `str(id)` default: for a `dict` row, `title`
  is explicit iff the `'title'` key is present; for a `str`/`tuple`/
  `Item` row the rules degrade gracefully (a `str` row has only an id;
  a `tuple`/`Item` with a title equal to its id is treated as default).
- Top-level nodes get `parent = None` (→ `root_id` downstream).
- For each entry it walks the prefixes, emitting a node row the first
  time each prefix id is seen; a later explicit row for an
  already-synthesized prefix merges its fields (explicit wins) onto the
  emitted row.
- `has_children` is **not** set here; it is derived after grouping
  (any node that appears as a `parent` is expandable).

### `Browser.from_flat_tree(..., path_sep=None)`

- Existing `has_parent` / `has_depth` detection is unchanged.
- New branch: when `not has_parent and not has_depth and path_sep`,
  call `expand_path_rows(rows, path_sep)`, then run the **existing
  parent-pointer grouping** on the expanded rows. Set `has_children =
  True` on every node that is a key in `children_by_parent`.
- Everything downstream (cache pre-population via `upsert`/`complete`,
  the synthesized eager `get_children`) is unchanged.

### CLI wiring (`080-cli.py`)

- New argument `--path-sep CHARS` (default `None`). Stored on `args`.
- `_build_eager_browser` passes `path_sep=args.path_sep` into
  `from_flat_tree`.
- `run_tui` rejects `--path-sep` combined with `--children-cmd`:
  `error: --path-sep requires --root-cmd (eager mode)` → exit 2.
- After parsing rows, if `args.path_sep` is set and any parsed row has a
  `parent`/`depth` key, emit the stderr warning above (then let
  `from_flat_tree`'s precedence drop `path_sep`).

## CLI surface

```bash
# plain path list → tree
find . | browse-tui --root-cmd cat --path-sep /

# metadata columns ride along onto leaves
find . -printf '%p\t%s\n' \
  | browse-tui --root-cmd cat --fields id,size --path-sep /

# non-/ separator (qualified names)
printf 'os.path.join\nos.path.split\nsys.argv\n' \
  | browse-tui --root-cmd cat --path-sep .
```

`--path-sep` accepts any non-empty string (multi-character separators
like `::` are allowed). It is orthogonal to `--input` and `--fields`:
the chosen parser produces rows as usual, then the `id` column is split.

## Testing

Unit tests for `expand_path_rows`:

- basic nesting + intermediate synthesis (`docs/api/auth.md` family)
- explicit-prefix merge (input has both `docs/api` and a child)
- absolute vs relative distinction (`/etc/x` ≠ `etc/x`)
- doubled separator collapse (`a//b`), trailing separator (`a/b/`)
- empty / all-separator entries skipped
- single-segment entry → top-level leaf
- multi-character separator (`::`, `.`)
- first-seen sibling ordering
- explicit `title` on a leaf row overrides the segment

`Browser.from_flat_tree(..., path_sep=...)` integration:

- cache pre-population: `visible_items` shows the synthesized tree,
  `has_children` markers correct, no runtime fetch
- precedence: a `parent` column beats `path_sep`; a `depth` column
  beats `path_sep`; `path_sep` beats flat

CLI end-to-end (`test/`):

- `--path-sep /` through `parse_input` → Browser produces the tree
- metadata column rides onto leaf
- `--path-sep` + `--children-cmd` → exit 2 with the error message
- `--path-sep` + a `parent`/`depth` column → stderr warning, explicit
  mode used

Docs:

- a `--path-sep` subsection in `docs/cli.md` (flag reference + the
  hierarchy-detection list updated to mention path-split and its
  precedence).
```
