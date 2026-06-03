# Markdown document subtrees — browse-claude & browse-md

**Date:** 2026-06-03
**Status:** Draft (pre-implementation)
**Scope:** New shared module `recipes/md_doc.py`; one new framework `Item`
field (`boundary`) plus boundary-aware recursive expansion in `src-tui/`;
`recipes/browse-claude` integration; `recipes/browse-md` adoption. A
contained bug fix to `_decode_project_path`. Worktree development.

## Motivation

Claude Code transcripts are full of markdown: assistant messages with
headings, and — increasingly — references to markdown files an agent
produced (`I wrote the report to docs/report.md`, a `Write report.md`
tool call, a subagent's `.md` deliverable). Today a message row in
`browse-claude` is always a leaf: to see a report's structure you scroll
the preview or open `$EDITOR`. `browse-md` already turns a markdown file
into a navigable heading tree; this work brings that same structural
navigation *inside* `browse-claude` — and, symmetrically, teaches
`browse-md` to follow markdown references between files.

When `md_doc` (and through it `md2ansi_lib`) is importable, a message
that contains markdown headings or references existing `.md` files
becomes expandable into a **document subtree**: an inline `markdown`
document plus one node per referenced file, each drillable into its own
heading tree, recursively.

## Goals

1. A message row gains children when its content has markdown headings
   **or** it references existing `.md` files on disk. Detection is by
   cheap regex at item-build time; the real tree is built lazily on
   expand.
2. Every markdown document — the message's own inline content *and* each
   referenced file — is represented by a synthetic **document node**
   whose children are its nested headings (h1 > h2 > …). This mirrors
   `browse-md`'s per-file model.
3. References are followed **recursively**: a referenced file may itself
   reference further `.md` files, with no depth cap and no cycle
   detection — under lazy materialisation a subtree only deepens as far
   as the user manually drills in, so a self-referential chain never
   expands on its own.
4. A new framework `Item.boundary` flag marks self-contained foreign
   subtrees (referenced files, subagent transcripts, bare sessions) so
   recursive expansion stops at them and their descendants never fold
   into an ancestor's preview cascade.
5. The markdown-structure logic lives in one shared, framework-agnostic
   module `recipes/md_doc.py`, consumed by both recipes — a hard
   dependency for `browse-md`, an optional one (graceful degradation)
   for `browse-claude`.
6. `browse-md` adopts the shared builder and gains the same
   reference-following behaviour.
7. Works identically in `browse-claude`'s tree and flat modes.

## Non-goals

- **No alias / dedup of repeated files, and no cycle detection.** If the
  same file is reachable via two branches — or a file references one of
  its own ancestors — each occurrence simply expands in place. Lazy
  materialisation makes this safe: a subtree only deepens as the user
  drills in, so nothing expands infinitely on its own. A process-wide
  `abspath → parsed-tree` cache keeps repeated files from
  re-reading/re-parsing.
- **No directory tree.** Referenced files are a flat list with relative
  labels — we do not synthesise folder nodes (it breaks down across
  multiple roots).
- **No list-item rows in `browse-claude` subtrees.** Headings only.
  (The shared builder still supports lists because `browse-md` needs
  them; `browse-claude` requests headings-only.)
- **No global id-encoding refactor.** Existing `browse-claude` id shapes
  (`<path>#<n>`, `#prompt:`, `#tool:`, `#agent:`, `#span:`) stay as-is
  and stay human-readable. Only the new `#md:` segments are
  URL-encoded. A blanket migration to URL-encoded ids is explicitly out
  of scope (separable future ticket if ever wanted).
- **No new preview-ready / cursor-scan hooks.** Detection rides the
  existing item-build path.

## Architecture overview

Three pieces:

1. **`recipes/md_doc.py`** — new importable module (sibling of
   `md2ansi_lib.py`; self-registers as a plugin the same way). Pure
   markdown-structure logic, no `browse_tui`/`Item` coupling, so it is
   unit-testable in isolation:
   - `build_doc_tree(text, *, include_lists=False)` → a structural tree
     of heading (and optionally list) nodes carrying `level`, `title`,
     `line_offset`, `byte_offset`, `byte_size`, and `children`. This is
     `browse-md`'s current `_build_nodes` generalised and lifted out.
   - `find_md_refs(text)` → ordered list of captured `.md` reference
     strings (regex `\b[^\s"\\$*]+\.(?:md|MD)\b` — excludes whitespace
     and `"`/`\`/`$`/`*` so JSON quotes/escapes, shell vars, and globs
     don't pollute a match).
   - `resolve_md_ref(captured, *, doc_dir, cwd, project_root)` →
     absolute path or `None` (multi-base discovery, see below).
   - Id codec: `compose_md_id(base, abspaths, line_offset=None)` and
     `parse_md_id(item_id)` → `(base, abspaths, line_offset)` using
     `urllib.parse`.
   - `md_heading_trigger(text)` / the ref regex, for the cheap detection
     gate.
   - A process-wide `abspath → (text, doc_tree)` cache with an explicit
     `clear_cache()` for the recipes' reload paths.

2. **Framework** (`src-tui/030-data.py`, `src-tui/040-state.py`) — add
   `Item.boundary: bool = False` and make recursive/multi-expansion
   treat a `boundary` node as a leaf (expand *to* it, never *through*).

3. **Recipe integration** — `browse-claude` (primary) and `browse-md`
   (adoption) map `md_doc` structural nodes into their own `Item`/id
   spaces and wire preview + boundary semantics.

## The uniform node model

A *document* is either the message's inline markdown or a referenced
file. References are collected at a document's top level, never under a
specific heading (headings have only sub-heading children — matching
`browse-md`).

Two levels with a deliberate asymmetry:

- **Message (level 0)** is the container. Its children are its inline
  `markdown` document node (headings only) **plus** one node per `.md`
  file referenced *anywhere in the record* — the ref scan covers the
  whole record's string content (see Detection for the cheap traversal),
  so it catches `Write`/`Read`/`Edit` tool paths, not just prose. A ref
  that appears in the inline text therefore surfaces as a message-level
  sibling of the `markdown` node, not under it; the `markdown` node stays
  a pure heading index of the inline text. This is deliberate — it avoids
  listing a prose reference twice, and it yields one unified list of
  every file the message references (prose or tool call) directly under
  the message, rather than scattering refs between the `markdown` node
  and the message level. (Separating them — scan only non-markdown fields
  at the message level, defer prose refs to the expanded `markdown` node
  — is possible but couples the message-level walker to the exact leaves
  the markdown-text extractor consumes, and scatters the refs; not worth
  it.)
- **A referenced-file document** has children = its top-level headings
  **plus** one node per `.md` file referenced *in that file's own text*.

Either way each child file is itself a document, recursively.

Example (cursor expanded down into a report that links onward):

```
▾ assistant: Here's the plan…            [assistant]   ← message leaf, now expandable
  ▾ markdown                              [md]   inline content (boundary=False)
    ▾ Summary                             [h1]
      ▸ Risks                             [h2]
  ▾ docs/report.md                        [md]   referenced file (boundary=True)
    ▾ Findings                            [h2]
    ▸ docs/appendix.md                    [md]   report.md → appendix.md (recursion)
  ▸ NOTES.md                              [md]
```

## ID scheme

The existing message base id is unchanged: `<session_path>#<n>`. A
markdown selector is appended using the existing `#keyword:` convention,
so routing stays a substring test (`'#md:' in item_id`).

```
inline document   <base>#md:
inline heading    <base>#md:#<lineoffset>
file document      <base>#md:<enc>
file heading       <base>#md:<enc>#<lineoffset>
nested file doc    <base>#md:<enc1>#md:<enc2>
nested file heading<base>#md:<enc1>#md:<enc2>#<lineoffset>
```

where
- `<base>` = the untouched message id `<session_path>#<n>`,
- `<enc>` = `urllib.parse.quote('file://' + abspath, safe='')` (a
  referenced file; `safe=''` percent-encodes every delimiter that
  matters — notably `#`→`%23` — so a segment never contains a raw `#`),
- empty segment (`#md:` with nothing after) = the inline content of
  `<base>`,
- `<lineoffset>` = decimal digits, a heading's 0-based line within the
  *last* document of the chain.

**Why this parses unambiguously.** `parse_md_id` finds the first
`#md:`; everything before is `<base>` (left untouched and still
readable), everything after is the chain. Splitting the chain on `#md:`
yields the encoded segments; encoded segments contain no raw `#`
(it becomes `%23`), so a trailing `#<digits>` on the final segment can
only be the line offset — which is why a numeric `#<lineoffset>` suffix
is safe and consistent with the existing numeric `#<n>` message suffix.
`file://` is cosmetic-but-consistent flavour; resolution decodes the
last segment back to the abspath directly — **no lookup map is needed**.
The encoded chain also makes dedup-within-a-document a plain abspath
comparison.

## Detection (item-build) and lazy build (expand)

**Detection — cheap, regex-only, at item delivery.** In the tree-row
builder `_tree_item` (and the flat-mode builders), where the raw record
`rec` is in hand, set `has_children` when either:

- `md_doc.md_heading_trigger(text)` matches — regex `(?:^|\n)[ \t]*#`
  over the message's extracted markdown text (`_message_md_text(rec)`,
  the joined `text` parts plus any `tool_result` text), **or**
- the `.md` reference regex matches the record's own string content.
  We do **not** serialise the record to JSON. At item delivery the
  builder already holds the parsed `rec`, so the gate does a
  short-circuiting depth-first walk over its string-valued leaves
  (text, tool inputs, paths), stopping at the first `.md` hit — far
  cheaper than `json.dumps(rec)`, and it still sees `Write`/`Read`/`Edit`
  paths and prose alike. (When the raw JSONL source line is in hand —
  e.g. during the expand build — regexing it directly is equivalent and
  simpler; the improved regex excludes `"`/`\` so JSON escaping can't
  leak into a match.)

No `md2ansi_scan` and no `os.stat` at detection time — it is a pure
regex gate. This can over-trigger (a `#` that lives only inside a fenced
code block; a `.md` token whose file does not exist), so it is
*optimistic*.

**Build — lazy, authoritative, on expand.** `get_children(<base>#…)`
builds the real subtree:

- **message id `<base>`** → `[inline doc node if md2ansi_scan finds ≥1
  heading]` + `[file doc node per existing ref found in the record,
  deduped by abspath, sorted by label]`. The record is read on demand
  here, so the ref scan runs over its raw JSONL line / string leaves
  (not `json.dumps`).
- **inline document `<base>#md:`** → its top-level heading nodes only
  (its references already live at the message level above).
- **file document `<base>#md:<segs>`** → `[top-level heading nodes]` +
  `[file doc node per existing ref in *that file's text*]`, deduped by
  abspath and sorted by label.
- **heading node `…#<lineoffset>`** → the sub-headings nested under that
  heading (no reference children).

**Self-heal.** If a build returns `[]` (the optimistic gate fired but
there was no real heading and no existing ref), the already-wired
`on_children_loaded` hook issues `mod(id, has_children=False)` to retract
the stale expansion arrow. Cheap, and it keeps detection regex-only.

`has_children` on freshly built document/file nodes: the inline node is
`True` (we just confirmed headings); file nodes are optimistic `True`
(we have not read the file yet) and self-heal on their own expand. The
inline node is same-file content (`boundary=False`); file nodes are
foreign (`boundary=True`).

## Heading-tree building

`md_doc.build_doc_tree` runs `md2ansi_scan(text, kinds={'heading'})`
(adds `'list'` only when `include_lists=True`), then applies `browse-md`'s
existing logic — lifted verbatim and generalised — to compute
`line_offset`, `byte_offset`, the boundary-rule `byte_size`, and the
nesting. The result is a structural tree (no `Item`s); each recipe maps
nodes to `Item`s with its own id scheme, tag, and styling.

**`browse-claude` never requests lists** — it always calls
`build_doc_tree` headings-only. `include_lists=True` is used solely by
`browse-md` (its `-l` flag); list support lives in the shared module only
so `browse-md` can adopt it.

## Reference detection, resolution, labels

- **Detection:** `find_md_refs(text)` returns captured strings matching
  `\b[^\s"\\$*]+\.(?:md|MD)\b` (excludes whitespace, `"`, `\`, `$`, `*`),
  in document order.
- **Resolution:** `resolve_md_ref(captured, *, doc_dir, cwd,
  project_root)` returns the first existing candidate, tried in order:
  1. absolute path, or `~`-prefixed → `expanduser`, used directly;
  2. relative to `doc_dir` — the referencing document's directory (the
     CommonMark norm; for the inline document `doc_dir == cwd`);
  3. relative to `cwd` — the record's working directory;
  4. relative to `project_root` — git root (walk up from `cwd` for
     `.git`), else the session's real project directory.

  Returns the `realpath`/`abspath`, or `None` if nothing exists. Within
  a document, results are deduped by abspath.
- **Label:** computed once and stored on the `Item` (not in the id —
  the id is abspath-canonical). Rendered relative to a single common
  anchor so a flat list reads cleanly without `../` noise:
  - relative to `project_root` if the file is inside it, else
  - relative to `cwd` if inside it, else
  - a `~`-collapsed absolute path.

`cwd` / `project_root` come from the session's records (see the
`_decode_project_path` fix below), not from the lossy directory-name
decode.

## Recursion: expand-in-place + cache

References are followed to arbitrary depth (no cap), with **no cycle or
alias detection**:

- **Expand-in-place:** every reference — including one that points back
  at an ancestor (a cycle) or duplicates a file reached via another
  branch — simply expands in place as its own document node. There is no
  canonical/alias bookkeeping (explicit non-goal).
- **Why that's safe:** the tree is lazy — a node's children are built
  only when the user expands it, never pre-cached. So a cyclic chain
  (`a → b → a → …`) only deepens as far as the user manually drills; it
  never expands on its own, and each level is a finite, distinct node id.
- **Cache:** a process-wide `abspath → (text, doc_tree)` cache in
  `md_doc` means a file is read and scanned once regardless of how many
  places (or how many times along a cycle) reference it. Cleared on the
  recipes' reload (`Ctrl-R` / `_bust_caches_for`).

A `boundary` node also halts recursive/multi-expansion explicitly, so
even a deliberate "expand everything" stops at each referenced file
rather than walking the reference graph.

## `boundary` — new framework Item flag

Add to the framework `Item` (`src-tui/030-data.py`):

```
boundary: bool = False
```

**Semantics (to document in the dataclass):** marks a node that heads a
*self-contained foreign subtree* — content sourced from outside the
current document. The framework must (a) **not auto-expand or
recursively walk into it** — recursive/multi-expand (`expand_subtree`
and the Alt-Right path in `src-tui/040-state.py`) treats it as a leaf,
expanding *to* it but never *through* it, **even when its children are
already cached** from a prior manual expand. (Lazy expansion already
skips *uncached* subtrees, so this is a no-op in the common case;
`boundary` covers the cached case and makes the contract explicit.) The
node stays manually expandable. Behaviour (b) — **not folding its
descendants into an ancestor's preview cascade** — is honoured by
recipes that build such cascades (the framework has no cross-item
preview concept).

**`browse-claude` consumers + migration (per the agreed cleanup):**

- Set `boundary=True` on md **file** document nodes (not the inline
  `markdown` node — that is same-file content), on subagent-group rows,
  and on bare `.jsonl` session rows.
- Replace the id-shape `'#agent:' in cid` skip in `_walk_umbrella` with
  `child.boundary`.
- Reimplement `_is_cross_file_id` to consult the Item's `boundary`
  (looked up via `_BROWSER.get_item(item_id)`), preserving today's
  behaviour for `#agent:` / bare-`.jsonl` rows. Note the two `if`s serve
  different purposes (cascade-skip vs. metadata→cascade preview upgrade);
  tests assert behaviour parity for the existing rows. This is **not** a
  behaviour change for subagents — recursive/multi-expansion is lazy and
  already does not open an unopened subagent transcript; `boundary` only
  makes that explicit and extends it to the previously-cached case.

## Preview

New routing in `get_preview`, checked **before** the generic `'#' in
item_id` message path:

- `'#md:' in item_id`:
  - **document node** → that document's full text (inline message text,
    or the file's text via the `md_doc` cache), rendered through
    `_md2ansi_fn` honouring the existing `_MD_COLOR` toggle;
  - **heading node** (`…#<lineoffset>`) → the heading's byte-range
    section (`byte_offset : byte_offset + byte_size` from the cached
    `doc_tree`), same rendering.

Existing message-leaf previews are unchanged. md-node previews read only
the node's own document — never a cross-item cascade.

## Tags & styling

- Document nodes: tag `[md]` for both inline and file (uniform). Inline
  styled `gray`/`dim` (same-file index); file docs styled `blue` (the
  recipe's cross-file / attachment hue) so the two read apart while
  sharing the tag.
- Heading nodes: `[h1]`…`[h6]` with the level→colour map (red, yellow,
  green, blue, magenta, gray), matching `browse-md`.

## Flat mode

The feature is mode-agnostic: detection rides the shared item builders,
and `get_children` returns the document nodes regardless of `_TREE_MODE`.
Flat-mode message rows therefore gain the same expandable subtree.

## `browse-md` adoption

- Refactor `browse-md`'s `_build_nodes` onto `md_doc.build_doc_tree`
  (`include_lists` honours its `-l` flag). `browse-md` keeps its
  Item-construction, anchor resolution, per-file roots, and `V`/`E`
  actions.
- Add reference-following: a file root / heading gains `[md]` children
  for the `.md` files it references (resolved via `resolve_md_ref`),
  using the same `#md:` chain codec composed onto `browse-md`'s base id
  (`<file_path>` / `<file_path>#<lineoffset>`). Referenced-file nodes are
  `boundary=True`. `browse-md`'s existing heading id `<path>#<lineoffset>`
  is already consistent with the `#<lineoffset>` suffix.
- `md_doc` is a **hard dependency** for `browse-md` (it already
  hard-depends on `md2ansi_lib`): `main()` dies with a clear message if
  the import fails.

## Graceful degradation

`browse-claude` imports `md_doc` in a `try/except` (as it already does
for `md2ansi`). When the import fails, the detection gate is a no-op,
no message row gains markdown children, and every other behaviour is
unchanged. No hard failure.

## Bug fix: `_decode_project_path`

`_decode_project_path` (`recipes/browse-claude:209`) reverses Claude's
`/`-and-`.`→`-` encoding by replacing **every** `-` with `/`, so a real
hyphen is mangled (`browse-tui` → `browse/tui`). It is used only for the
project display title (`:521`) and project preview (`:2687`), and its
docstring already admits the lossiness.

**Fix:** derive the true path from a session record's stored `cwd`
(`_session_cwds` already reads it). Add `_real_project_path(project_dir)`
that returns the real `cwd` (HOME→`~` collapsed for display) from the
first readable session, falling back to the lossy `_decode_project_path`
only when no record/cwd is available. This both fixes the display and
supplies the accurate `cwd` / `project_root` the reference resolver
needs.

## File layout

- `recipes/md_doc.py` — **new** shared module (~270–400 lines incl. the
  dense house-style docstrings; much of it relocated from `browse-md`).
- `src-tui/030-data.py` — add `Item.boundary` field + docstring.
- `src-tui/040-state.py` — boundary-aware recursive/multi-expansion.
- `recipes/browse-claude` — detection in row builders; `#md:` routing in
  `get_children`/`get_preview`; `md_doc` import + graceful degrade;
  `boundary` on file/subagent/session rows + `_walk_umbrella` /
  `_is_cross_file_id` migration; `_real_project_path` fix; self-heal in
  `on_children_loaded`.
- `recipes/browse-md` — adopt `md_doc.build_doc_tree`; add
  reference-following; hard-dep gate.
- Tests under `test/unit/` (and `test/ui/` smoke where natural).

## Testing strategy

- **`md_doc` (isolated, no `browse_tui` stub):** `build_doc_tree`
  nesting + byte-range slicing on fixtures; heading/ref trigger regexes
  (true/false: real heading, code-fence-only `#`, ref-only, none; ref
  regex excludes `"`/`\`/`$`/`*`); `resolve_md_ref` base-precedence +
  non-existent → `None`, dedup; `compose_md_id`/`parse_md_id` round-trip
  incl. paths containing `#`/`~`/`?`/spaces and the `#<lineoffset>`
  suffix; a self-referential chain still expands (no cycle special-case);
  cache hit/`clear_cache`.
- **`browse-claude` (existing stub-import pattern in
  `test_browse_claude_render.py`):** detection sets `has_children`;
  `get_children` builds inline + file docs and headings; `get_preview`
  routes `#md:` to slices; self-heal flips `has_children` off on empty;
  `boundary` set correctly and `_walk_umbrella` / `_is_cross_file_id`
  parity; `_real_project_path` with and without a readable `cwd`;
  graceful degradation when `md_doc` is absent.
- **framework:** `Item.boundary` default; recursive/multi-expand stops
  at a `boundary` node.
- **`browse-md`:** parity after the `_build_nodes` refactor; new
  reference children resolve and expand.

## Sequencing (each phase lands independently)

1. **`md_doc.py`** (structural builder lifted from `browse-md`, ref
   detection/resolution, id codec, cache) + framework `Item.boundary`
   and boundary-aware expansion.
2. **`browse-claude` core:** detection + `#md:` ids + inline & file
   documents with headings (one or more levels), preview routing,
   resolution + labels, `boundary` migration, `_decode_project_path`
   fix, self-heal. (Recursion falls out of the uniform model; the
   `md_doc` cache is included.)
3. **`browse-md` adoption:** refactor onto `md_doc`; add
   reference-following.

## Risks & open questions

- **Self-heal flicker:** an optimistic arrow that retracts on first
  expand of a false-positive (code-fence-only `#`, dead ref) is rare and
  visually minor; accepted over scanning at item-build time.
- **`boundary` migration parity:** `_is_cross_file_id` and the
  `_walk_umbrella` skip serve subtly different purposes; the migration
  must preserve existing `#agent:` / `.jsonl` behaviour (asserted by
  tests).
- **Resolution I/O:** multi-base discovery does a few `os.stat`s per
  referenced token, but only on expand of a markdown message and only
  for tokens that matched the regex — bounded and lazy.
- **`md_doc` structural-vs-Item boundary:** the module returns plain
  structural nodes (no `Item`), each recipe maps to its own `Item`s.
  This decouples and eases isolated testing at the cost of a small
  mapping layer per recipe.
