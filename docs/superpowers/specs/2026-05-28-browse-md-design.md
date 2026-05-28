# browse-md — Markdown file structural browser

**Date:** 2026-05-28
**Status:** Draft (pre-implementation)
**Scope:** New recipe `recipes/browse-md`. No framework changes.

## Motivation

Markdown files are tree-shaped (headings nest sub-headings, lists nest
sub-lists), but the only ways to navigate them today are scrolling in
`$EDITOR` or grepping. A browse-tui recipe gives the same hierarchical
nav (collapse / expand, multi-select, preview, search) the rest of the
recipe family offers for sessions / files / processes.

## Goals

1. Positional argument is either a plain `FILE.md` or an item id
   `FILE.md#<anchor>`. Bare file → initial scope = root. Anchor →
   browser starts scoped INTO the matching item, drill-down style.
2. Tree contains all six heading levels and arbitrarily nested list
   items (ul + ol). Nothing else surfaces as a tree row.
3. Preview pane shows the source slice for the current item — the
   item itself plus every descendant. Root previews the whole file.
   Slices are exact source bytes — no reformatting.
4. `m` toggles md2ansi rendering of the preview, matching browse-claude
   / browse-fs conventions (gated on the md2ansi import).
5. `v` / `e` page / edit the preview text (framework defaults). `V` /
   `E` open the *source* — original file on root, temp-file extract
   for non-root.
6. `M` pages the preview through the external `$MD2ANSI` / `md2ansi`
   binary, again matching browse-claude.
7. Parser is **lifted from md2ansi.py's technique**: same regex
   fragments, same `_m2a_build_context` combined-regex approach, same
   precedence-by-rule-order masking of nested constructs. We re-use
   the actual constants (`_MD_H1`..`_MD_H6`, `_MD_HR`, `_MD_CODE_GEN`,
   `_MD_BLOCKQUOTE`, `_MD_TABLE`, `_MD_LIST`) and the
   `expandtabs(4) // 2` indent-to-level rule verbatim.

## Non-goals

- No setext headings (`===` / `---` under a line). md2ansi doesn't
  honour them; we mirror that.
- No tree rows for paragraphs, code blocks, blockquotes, tables, HRs,
  or YAML frontmatter. These are visible only in previews.
- No multi-file mode, no stdin mode, no recursive crawl. One file in,
  one tree out.
- No incremental reparse on disk change. Manual reload via Ctrl-R.
- No tests in this round — matches `browse-fs` / `browse-files` /
  `browse-find` / `browse-ls` (the only recipe with tests today is
  `browse-claude`).

## Argument parsing

```
browse-md FILE.md
browse-md FILE.md#<line_no>
browse-md FILE.md#<heading-name>
```

Argv parsing:

1. Split the positional once on the first `#`. The left half is the
   file path; the right half (if present) is the anchor.
2. `~` expansion + `os.path.abspath` on the path. Missing file →
   usage to stderr, exit 2.
3. Anchor resolution (after the file is parsed):
   - **Empty anchor** (`FILE.md` with no `#`) → initial scope = root.
   - **All-digit anchor** → treated as a line number (0-indexed,
     matches the `#<n>` id shape). Resolved via the lookup table
     (see below): exact line if a node starts there, else the
     closest node whose `line_offset` is ≤ the requested line (i.e.
     the deepest item that *contains* the requested line). Browser
     opens scoped into that item.
   - **Non-digit anchor** → matched against the *display title* of
     every heading node, defined as the source line with `#` markers
     AND leading/trailing whitespace stripped. The stored `title`
     keeps `#`s; the comparison only normalises the search key.
     Match is case-insensitive (both anchor and display title are
     lowered for comparison; the stored title is unchanged). Three-tier
     fallback — each tier scans all headings in source order before
     falling through:
     1. **Exact match**: `display_title == anchor`. First hit wins.
     2. **Prefix match**: `display_title.startswith(anchor)`. First
        hit wins.
     3. **Substring match**: `anchor in display_title`. First hit
        wins.
     No match in any tier → stderr warning, fall through to root
     scope so the user still gets a usable browser.
4. Anchor resolution happens after parse; the resolved id is passed
   as `initial_scope` to `BrowserConfig`. Pattern mirrors the
   `--item N` flow in browse-claude (where it positions the cursor
   on `<jsonl>#<n>` after the root has loaded).

## Tree model

Three node kinds: synthetic `root`, `heading`, `list-item`.

### Root

- `id = <abs_path>` (string).
- `title = os.path.basename(path)`.
- `tag = ''`, no tag_style.
- `has_children = True` whenever the parser found at least one node.
- Preview = the entire file.

### Heading nodes

- ATX only: `^\#{1..6}[ \t]+TEXT$` (re.VERBOSE shape from md2ansi:
  `_MD_H1` .. `_MD_H6`, re-used as-is via the combined-regex parser
  below).
- `id = '<path>#<line_no>'` where `line_no` is 0-indexed (matches
  browse-claude's message-id shape `<path>#<n>`).
- `title` = the source line verbatim, only `.rstrip('\n')` + leading
  whitespace stripped (browse-tui's tree pane already visualises
  indent). No `#`-stripping, no formatting-marker removal — the
  recipe never reformats. Example row: `## My **bold** heading`.
- `tag = 'h1'` … `'h6'`.
- `tag_style` per level (chosen to read close to md2ansi's palette
  using browse-tui's named colours):
  - h1: `red`
  - h2: `yellow`
  - h3: `green`
  - h4: `blue`
  - h5: `magenta`
  - h6: `gray`
- `has_children = True` iff there's at least one nested list item or
  sub-heading.

### List-item nodes

- Detection: matched per-line *inside* the multi-line list block the
  combined-regex parser emits (md2ansi's `_MD_LIST` matches a whole
  contiguous list, then `_m2a_fmt_list` walks its lines). We do the
  same: `re.match(r'^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$', ln)` over
  each line of the match. Exact regex shape copied from md2ansi
  `_m2a_fmt_list` (md2ansi.py:493).
- `level = len(indent.expandtabs(4)) // 2` — identical to md2ansi.
  Tab = 4 spaces; 2-space indent step = one level.
- `id = '<path>#<line_no>'`.
- `title` = the source marker line verbatim, only `.rstrip('\n')` and
  leading whitespace stripped (the tree pane visualises indent
  separately). No marker stripping, no bullet normalisation.
  Example row: `- foo bar` or `1. item one`. Continuation lines
  (deeper indent, no marker) belong to the item's source range but
  are not re-shown in the row title.
- `tag = 'ul'` for `-` `*` `+`; `tag = 'ol'` for `\d+.`.
- `tag_style = 'dim'`.
- `has_children = True` iff there's at least one deeper-indent list
  item before this item's section ends.

### Parser pipeline (lifted from md2ansi)

md2ansi's technique (md2ansi.py:113, `_m2a_build_context`):

> Build ONE combined regex by alternating each rule's pattern inside
> a named group: `(?P<h1>...) | (?P<h2>...) | ... | (?P<list>...)`.
> Compile with `re.VERBOSE | re.MULTILINE | re.DOTALL`. Run it as a
> single `re.sub` / `finditer` pass over the whole text. Precedence
> falls out of rule order — code-fence appears before list, so
> list-looking lines inside fences are masked.

#### Regex discipline (no rollback, greedy)

Every pattern in `_RULES` MUST follow md2ansi's linear-time regex
conventions verbatim. The rule, stated explicitly:

- **Every alternation has disjoint branches.** Each character in the
  input has exactly one matching branch — never two — so the regex
  engine never has to roll back a choice. Example from
  md2ansi.py:65, table cell:
  ```
  [^|\\\n] | \\.
  ```
  Every char is either "not a pipe/backslash/newline" OR "backslash
  + any char". Mutually exclusive. Linear time.

- **No nested unbounded quantifiers inside an alternation that can
  match the same input.** Forbids `(a+|b+)+` shapes that explode on
  backtracking.

- **Tempered-greedy bodies for cross-line spans.** Where a pattern
  needs to consume content up to a sentinel, use the
  `(?: (?! <sentinel> ) <single_char> )*` shape — md2ansi.py:70
  (`_M2A_STR_TDQ`), md2ansi.py:710 (fenced code body), and
  md2ansi.py:148 (`_M2A_TABLE_CELL_RE`) all use this. The negative
  lookahead consumes exactly one character per iteration, so the
  match runs in linear time regardless of how the sentinel is
  written.

- **No re-implementation of md2ansi patterns.** Patterns we lift
  (`_MD_H1`..`_MD_H6`, `_MD_HR`, `_MD_CODE_GEN`, `_MD_BLOCKQUOTE`,
  `_MD_TABLE`, `_MD_LIST`) are copied byte-for-byte from
  md2ansi.py — the only patterns we add are `_MD_FRONTMATTER`
  (offset-0 `\A---\n...\n---`) and the per-line list-item regex
  (already documented at md2ansi.py:493). Any new pattern follows
  the same discipline.

The combined regex is also compiled with the same flags as md2ansi:
`re.VERBOSE | re.MULTILINE | re.DOTALL`.

#### Linear, non-recursive

md2ansi's `_md2ansi` dispatcher (md2ansi.py:863) recurses — inline
rules carry an `actual_recurse` context so bold/italic *inside* a
heading or table cell can re-feed through the INLINE rule table.
**browse-md does not.** Our parser cares only about the block-level
matches that decide tree structure. Inline formatting stays in the
source bytes and reaches the user via md2ansi at preview time (if
`m` is on), not parse time. The parse is one `finditer` pass over
the combined regex, no recursive descent, no nested contexts.

#### Combined-regex enumerator

We adopt md2ansi's combined-regex shape, but use it for
**enumeration** instead of substitution:

```python
# Patterns copied verbatim from md2ansi.py.
_RULES = (
    ('h1',          _MD_H1),
    ('h2',          _MD_H2),
    ('h3',          _MD_H3),
    ('h4',          _MD_H4),
    ('h5',          _MD_H5),
    ('h6',          _MD_H6),
    ('frontmatter', _MD_FRONTMATTER),   # MUST precede `hr`: both match `---` at offset 0
    ('hr',          _MD_HR),
    ('code',        _MD_CODE_GEN),      # ``` and ~~~ both handled
    ('blockquote',  _MD_BLOCKQUOTE),
    ('table',       _MD_TABLE),
    ('list',        _MD_LIST),
)

_PARSER_RE = re.compile(
    '|'.join(f'(?P<{n}>{p})' for n, p in _RULES),
    re.VERBOSE | re.MULTILINE | re.DOTALL,
)

def _parse(text):
    nodes = []
    for m in _PARSER_RE.finditer(text):
        # md2ansi-style dispatch: walk _RULES and pick the first whose
        # outer named group has a non-None match. Robust against future
        # inner (?P<...>) groups — see md2ansi.py:863-882.
        groups = m.groupdict()
        kind = next(n for n, _ in _RULES if groups.get(n) is not None)
        if kind in _HEADING_KINDS:
            nodes.append(_make_heading(kind, m, text))
        elif kind == 'list':
            nodes.extend(_walk_list(m, text))
        # code / blockquote / table / hr / frontmatter: skip — they
        # exist only to mask their contents from later rules.
    return nodes
```

Note: we walk `_RULES` instead of using `m.lastgroup` because
`lastgroup` returns the *highest-numbered* matching named group, so
any future inner `(?P<...>)` group inside a rule pattern would
silently misroute dispatch.

Per-rule notes:

- **Headings** are line-bound (`^...$` with MULTILINE), one match per
  heading. `m.start()` gives the byte offset; line number is computed
  via a precomputed byte-to-line index. The index's exact construction
  is a ticket-level detail.
- **Fenced code blocks** are multi-line (md2ansi `_MD_CODE_GEN` with
  DOTALL). Match consumes the whole fenced block, so list-like
  content inside is invisible to the list rule. We emit nothing.
- **Blockquote / table / HR** — same masking purpose, no emission.
- **List blocks** are handled by md2ansi's `_MD_LIST` pattern. See
  the dedicated subsection below — that's the only rule of ours
  that fans out into multiple emitted nodes per match.
- **Frontmatter** is line-bound: pattern is
  `\A---\n.*?\n---(?:\n|$)` (DOTALL). Only matches at file start
  via `\A`. Emits nothing.

#### List parsing details

md2ansi's `_MD_LIST` (md2ansi.py:723-726) is line-oriented and
strict — it matches a contiguous run of lines that each start with
`[-*+]` or `\d+.`. Crucially:

```
^ [ \t]* (?: [-*+] | \d+\. ) [ \t]+ [^\n]*
(?: \n [ \t]* (?: [-*+] | \d+\. ) [ \t]+ [^\n]* )*
```

This means:

1. **Continuation lines are not captured.** A list like
   ```
   - foo
     more text for foo
   - bar
   ```
   produces TWO separate `_MD_LIST` matches — one for `- foo`, one
   for `- bar`. The `  more text for foo` line falls between
   matches, unstructured. We never see a `_MD_LIST` match that
   contains a non-marker line.

2. **Blank lines split list blocks.** `- foo\n\n- bar` is two
   `_MD_LIST` matches (the `\n[ \t]*(?:[-*+]|\d+\.)` continuation
   requires *exactly one* `\n` between marker lines). Same outcome
   as continuation: two matches, intervening line(s) unstructured.

3. **Paragraphs between list items also produce two matches.** Any
   non-marker line ends the current `_MD_LIST` match; the next
   marker line starts a new one. The intervening paragraph is
   unstructured.

For browse-md this is fine — we don't need a single match to cover
every line of a "logical" list. We emit one node per marker line
across all `_MD_LIST` matches, and the byte-size boundary rule
(below) absorbs all intervening content (continuations, blanks,
paragraphs) into the preceding item's source range. The user sees
exactly the source text under the item they navigated to.

Per-list-block walk (contract — implementation details are
ticket-level):

- Input: one `_MD_LIST` match (a contiguous run of marker-starting
  lines).
- For each line in the match: run the per-line regex
  `^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$` (the same shape md2ansi uses
  in `_m2a_fmt_list`, md2ansi.py:493). On match, emit one
  list-item node with `level = len(indent.expandtabs(4)) // 2`,
  `kind` = `'ol'` if marker is `\d+.` else `'ul'`, and
  `byte_offset` / `line_offset` derived from the line's position
  within the match.

`[-*+]|\d+\.` branches are disjoint (an unordered marker char
can't also be a digit), so the no-rollback rule holds.

#### Boundary rule (`byte_size` / `line_size`)

Computed in one pass over the flat node list after `_parse` returns.
For each node at index `i`:

- Find the next node `j > i` that is **sibling-or-shallower** of
  node `i`:
  - For a heading at level L → next heading at level ≤ L.
  - For a list-item at indent level L → next list-item at level ≤ L,
    OR next heading of any level.
- If such `j` exists: `byte_size = nodes[j].byte_offset - nodes[i].byte_offset`.
- Else (node `i` is the last in its scope): `byte_size = len(_FILE_TEXT) - nodes[i].byte_offset`.
- `line_size` computed identically over `line_offset` (with
  `len(line_starts)` as EOF sentinel).

This rule guarantees that every byte and every line of the file
that belongs "under" a node — including continuation lines, blank
lines, paragraphs, code blocks, blockquotes — is included in that
node's source slice. Nothing falls between cracks; the only price
is that an interleaving paragraph between two list items
attributes to the earlier list item, which is the right answer
visually (it's "below" `- foo`).

#### Tree linking

After `_parse` we have a flat ordered list of nodes. Two link passes:

1. **Heading tree** — stack of `(level, node)`. For each heading H at
   level L: pop entries with level ≥ L, attach H to stack top (or
   root if empty), push (L, H).
2. **List tree** — same stack mechanic on indent level. Reset on
   every heading boundary; list items attach to the nearest open
   list ancestor with strictly smaller indent, else to the
   surrounding heading (or root) if none.

## Lookup contract

Every parsed node carries these hidden fields (attached as plain
attributes on the `Item`, per the API convention):

| Field         | Type | Meaning |
| ------------- | ---- | ------- |
| `byte_offset` | int  | Start position in `_FILE_TEXT`. |
| `byte_size`   | int  | Length of the item's source slice, including all descendants. |
| `line_offset` | int  | 0-indexed line number where the item begins. |
| `line_size`   | int  | Number of lines covered, including all descendants. |
| `kind`        | str  | `'heading'` / `'list-item'` / `'root'`. |
| `level`       | int  | 1..6 for headings; indent level for lists. |

Sizes encompass the full subtree, so
`_FILE_TEXT[byte_offset:byte_offset+byte_size]` is exactly the
preview content for that item.

The recipe exposes one lookup primitive:

> **`_node_at_line(n) -> Item | None`** — return the deepest node
> whose source range contains line `n`. If `n` exactly matches a
> node's `line_offset`, that node wins. Otherwise return the node
> with the greatest `line_offset` ≤ `n` (the innermost containing
> node, since every range encompasses its descendants). Return
> `None` if `n` precedes every node (preamble territory) — the
> anchor flow falls back to root in that case.

How the lookup is implemented (data structure, build order,
`\r\n` handling, etc.) is a ticket-level concern, not part of the
contract.

## Preview

`get_preview(item_id)`:

- Root id (no `#`) → return `_FILE_TEXT`.
- `<path>#<n>` id → resolve to node via `_BY_LINE` (or
  `_node_at_line` fallback for anchor-style ids that don't sit
  exactly on a parsed node — defensive), return
  `_FILE_TEXT[node.byte_offset : node.byte_offset + node.byte_size]`.

The slice is the *original* bytes — no reformatting, no
re-indentation, no marker rewriting.

Render policy (display layer only — the underlying bytes don't
change):

- If `_MD_COLOR` is True (default when md2ansi importable) →
  pipe through `md2ansi(text, line_width=_BROWSER.preview_width or 80)`.
- Else → return `text` unchanged.

Matches the browse-fs `_MD_COLOR` / `_BROWSER` pattern verbatim.

## Actions

| Key | Source | Behavior |
| --- | ------ | -------- |
| `v` | framework default | Page the preview text in `$PAGER` (`less -R`). Honors `m` toggle. |
| `e` | framework default | Open preview text in `$EDITOR`. Edits discarded (preview-text semantics). |
| `V` | recipe | View source. See multi-select rules below. |
| `E` | recipe | Edit source. Same target rules as `V`; root edits persist, non-root edits go to a temp file and are discarded. |
| `m` | recipe (only bound if `md2ansi` importable) | Toggle md2ansi preview rendering. Drops preview cache. |
| `M` | recipe | View preview via external `$MD2ANSI` / `md2ansi` binary. Same plumbing as browse-claude `_action_md_preview`. |

### Multi-select semantics on `V` / `E`

Given a target set `T`:

1. **If any target in T is the root** → behave as if only root was
   selected: launch `$PAGER` / `$EDITOR` on the original file
   directly (no temp file, edits persist for `E`). The whole-file
   slice dominates any subset.
2. **All targets non-root** → build a temp `.md` file from their
   source ranges:
   - Collect `(byte_offset, byte_size)` for each target.
   - **Sort by `byte_offset` ascending.**
   - **Merge overlapping or adjacent ranges**: walk the sorted list,
     extend the current range whenever the next range's
     `byte_offset` is `≤` (current end). The resulting list is
     non-overlapping and in file order.
   - Concatenate `_FILE_TEXT[bo:bo+bs]` for each merged range,
     separating ranges with a single `\n` if the previous slice
     doesn't already end in one. No header chrome, no separators —
     the user asked for slices, they get slices.
   - Write to `tempfile.NamedTemporaryFile(suffix='.md', delete=False)`.
   - Launch `$PAGER` / `$EDITOR` on the temp path; unlink on return.

Pattern follows browse-claude's `_run_source_command` shape:
target-resolution → per-line-extract → temp-file → external command,
with the per-line extractor swapped for byte-range slicing.

## Reload

`get_children(item_id, *, reload=True)` with `item_id is None`
re-slurps the file, re-parses, rebuilds `_BY_LINE`. For non-root
reloads we return the cached children — single-file parse is cheap
enough that we don't bother with per-node invalidation.

## File layout

```
recipes/browse-md          ≈ 350 lines, single file, executable.
```

No new tests this round.

## Risks & open questions

- **Lazy continuation in list items**: md2ansi treats a deeper-indent
  line as a list-continuation (still part of the same item). We
  inherit that, so a `more text` line indented two spaces under
  `- foo` belongs to `foo`'s range, not the tree. ✔
- **Setext headings**: deliberately unsupported. Files that use
  `Heading\n===` get an unstructured tree. Documented as a non-goal.
- **Very large files**: parser does one `finditer` pass over the
  whole text; preview is O(slice). 100 KLOC tested in md2ansi's own
  rendering path — same regex, same budget.
- **Tab-indented lists at 2-space convention**: md2ansi's
  `expandtabs(4)` is non-configurable; we inherit. Documented.
- **Anchor name collision**: with two headings sharing a title (or
  matching the same prefix / substring tier), `FILE.md#<name>`
  resolves to the first in source order. The line-number form is
  the unambiguous escape hatch.
