# browse-md: parse via `md2ansi_lib.md2ansi_scan`

**Date:** 2026-06-01
**Status:** Approved (design phase)

## Problem

`recipes/browse-md` carries a hand-copied duplicate of `md2ansi_lib`'s
block-level markdown grammar — `_MD_H1`..`_MD_H6`, `_MD_HR`,
`_MD_FRONTMATTER`, `_MD_CODE_GEN`, `_MD_BLOCKQUOTE`, `_MD_TABLE`,
`_MD_LIST`, stitched into `_RULES` / `_PARSER_RE` — and `_parse()` walks
those matches to emit heading and list events. The patterns were lifted
"verbatim from md2ansi_lib", so the two copies can silently drift: a fix
to the library grammar does not reach `browse-md`, and vice versa.

`md2ansi_lib` now exposes a structural-scan API (`md2ansi_scan`) that
yields the matches the renderer already produces, over the same compiled
grammar. `browse-md` should consume it instead of re-implementing the
grammar, so there is one source of truth.

Today `md2ansi_lib` is imported only *optionally* in `browse-md` — the
import gates the colored-preview render function (`_md2ansi_fn`) and
nothing else. Once parsing depends on the library, the library is
essential: `browse-md` cannot build its tree without it.

## Goal

1. `_parse()` derives heading / list structure from
   `md2ansi_lib.md2ansi_scan` rather than `browse-md`'s own grammar.
2. `md2ansi_lib` becomes a **hard dependency**: if it cannot be
   imported, `browse-md` prints a clear error and exits non-zero
   instead of running in a degraded state.
3. The now-redundant optional-import gating around the *library* render
   function is removed (full collapse).

## Non-goals

- **The external `md2ansi` binary stays untouched.** The `M` action and
  `_resolve_md_pager` shell out to a separate external `md2ansi`
  program (via `$MD2ANSI` / `PATH`). That is a different thing from the
  `md2ansi_lib` Python module; it keeps its own independent
  "renderer not found → graceful message" handling and is out of scope.
- No change to the tree shape, anchor resolution, multi-file handling,
  preview slicing, `V`/`E` source commands, or the `-l` list flag's
  semantics.
- No new public API on `md2ansi_lib`; we consume `md2ansi_scan` as-is.

## What `md2ansi_scan` provides

`md2ansi_scan(text, kinds=M2A_SPANS_BLOCK)` yields `M2A_Span` objects in
document order, each with `kind`, `subtype`, `is_block`, `start`, `end`,
`text`, where `text == text[start:end]` over the raw (unwrapped) source.
It runs the full combined MD grammar once and filters which *kinds* it
yields; constructs that are not requested (code, blockquote, table,
frontmatter, hr) are still matched internally, so their contents stay
masked from heading/list detection exactly as before.

For `browse-md` we request `kinds={'heading', 'list'}`:

- **heading** spans carry `subtype` in `{'h1'..'h6'}` and `text` is the
  heading line without its trailing newline (e.g. `'# H1'`).
- **list** spans are the *whole* top-level list block as one masked span
  (e.g. `'- foo\n- bar'`). `md2ansi_scan` is non-recursive and has no
  per-item breakdown (`recursive=` is unimplemented), so `browse-md`
  keeps fanning each list block into per-marker-line events itself.

### Verification

Running `md2ansi_scan(text, kinds={'heading','list'})` over every
`TestParse` fixture reproduces the current parser's output exactly:
six heading levels at identical offsets; frontmatter / fenced-code /
blockquote / table masking; mid-document `---` treated as HR (no event);
single list spans for `ul`/`ol`; the strict-continuation split into two
spans; and the mixed `-`/`1.` block as one span. This refactor is
therefore behavior-preserving on the tested surface, and the two
grammars can no longer drift.

## Design

### 1. Imports — hard dependency

Top of module, replacing the current optional `_md2ansi_fn` gate:

```python
try:
    from md2ansi_lib import md2ansi as _md2ansi_fn, md2ansi_scan as _md2ansi_scan
except ImportError:
    _md2ansi_fn = _md2ansi_scan = None
```

`_md2ansi_fn` is retained as the module-level render seam that
`get_preview` calls and tests monkeypatch. `_md2ansi_scan` is the new
parse entry point. Both resolve at runtime because `--run-py` prepends
the recipe's directory (`recipes/`) to `sys.path`, and `md2ansi_lib.py`
lives there.

### 2. Hard-dependency gate in `main()`

`main()` is the production entry point (`--run-py` runs the recipe with
`run_name='__main__'`, so `if __name__ == '__main__': main()` fires).
As the first action in `main()`, before any parsing:

```python
if _md2ansi_scan is None:
    _die('requires the md2ansi_lib module (expected alongside the recipe)',
         with_usage=False)
```

`_die` already prints `browse-md: <msg>` to stderr and `sys.exit(2)`.
This reuses the file's established error convention and is unit-testable
without subprocesses: set `_md2ansi_scan = None`, call `main()`, assert
`SystemExit(2)`. The gate sits in `main()` (not import time) so the
module stays importable for unit tests that exercise individual helpers.

### 3. `_parse()` rewrite

`_parse` becomes a thin adapter that preserves its current event-tuple
contract (so `_build_nodes` and everything downstream are untouched).
The requested kind set is chosen by `_INCLUDE_LISTS`, so the library
only yields spans we actually turn into tree rows — no `'list'`
callbacks when list rows are off:

```python
# Scan-kind sets requested from md2ansi_scan, chosen by _INCLUDE_LISTS so
# the library only yields spans we turn into rows. The full grammar runs
# internally either way (code/blockquote/table/frontmatter/hr stay
# masked), so heading detection is identical regardless of which set we
# pass — the only difference is whether 'list' spans are yielded.
_SCAN_KINDS_HEADINGS = frozenset(('heading',))
_SCAN_KINDS_WITH_LISTS = frozenset(('heading', 'list'))

def _parse(text):
    line_starts = _line_starts(text)
    kinds = _SCAN_KINDS_WITH_LISTS if _INCLUDE_LISTS else _SCAN_KINDS_HEADINGS
    events = []
    for span in _md2ansi_scan(text, kinds=kinds):
        if span.kind == 'heading':
            bo = span.start
            events.append((span.subtype, {        # subtype is 'h1'..'h6'
                'byte_offset': bo,
                'line_offset': _line_of(bo, line_starts),
                'source': span.text,
            }))
        else:  # 'list' — only yielded when _INCLUDE_LISTS is on
            events.extend(_walk_list(span.start, span.text, line_starts))
    return events
```

Emitted event kinds (`'h1'..'h6'`, `'ul'`/`'ol'`) and payload fields
(`byte_offset`, `line_offset`, `source`, `level`) are unchanged.
Gating the requested kinds (rather than filtering after the fact) means
list spans are never materialised when `-l` is off. Because
`md2ansi_scan` filters at yield time over the full combined grammar,
the heading spans are byte-for-byte identical whether or not `'list'`
is requested.

### 4. `_walk_list()` signature change

From `_walk_list(match, line_starts)` (reads `match.start()` /
`match.group()`) to `_walk_list(base, text, line_starts)`, fed
`span.start` and `span.text`. The body is otherwise unchanged: same
`_LIST_ITEM_RE` per-line scan, same `level = len(indent.expandtabs(4))
// 2`, same `ul`/`ol` classification, same `byte_offset = base +
offset` accumulation.

### 5. Full collapse of the optional render gate

With the library guaranteed present after the `main()` gate:

- `_MD_COLOR = _md2ansi_fn is not None` → `_MD_COLOR = True`
  (colored preview default-on).
- `get_preview`: `if _MD_COLOR and _md2ansi_fn is not None:` →
  `if _MD_COLOR:`. The broad `except Exception` raw-slice fallback
  stays (pathological markdown must not crash the preview pane).
- `main()`: the `m` action is bound unconditionally (drop the
  `if _md2ansi_fn is not None:` guard around its `Action`).
- Module docstring + the import-block comment are rewritten: the `m`
  line drops "only bound when md2ansi_lib is importable"; the gate
  comment describes the hard requirement.

### 6. Code removed

`_MD_H1`..`_MD_H6`, `_MD_HR`, `_MD_FRONTMATTER`, `_MD_CODE_GEN`,
`_MD_BLOCKQUOTE`, `_MD_TABLE`, `_MD_LIST`, the `_RULES` tuple,
`_PARSER_RE`, and the per-match outer-rule dispatch loop inside the old
`_parse`. The block-rule-table section header comment goes with them.

### Retained unchanged

`_line_starts`, `_line_of`, `_build_nodes`, `_build_line_index`,
`_node_at_line`, `_resolve_anchor`, `_reparse`, `get_children`,
`get_preview` slicing, `_HEADING_KINDS` / `_HEADING_EVENT_KINDS` (still
used by `_build_nodes`), `_HEADING_PREFIX_RE` / `_LIST_PREFIX_RE` title
stripping, `_LIST_ITEM_RE`, and the `V`/`E`/`M`/Ctrl-R/`→` actions.

## Error handling

- Missing `md2ansi_lib` → `browse-md: requires the md2ansi_lib module
  (expected alongside the recipe)` on stderr, exit 2 (via `_die`).
- Renderer blow-up on pathological markdown → unchanged: `get_preview`
  falls back to the raw slice under its `except Exception`.
- `md2ansi_scan` raising on bad input is not specially caught in
  `_parse` (it does not today, and a parse failure should surface, not
  silently yield an empty tree).

## Testing

`./run-tests.sh` (full suite) must stay green. Specific deltas:

1. **Test harness path.** Any test that loads the recipe must put
   `recipes/` on `sys.path` so the now-hard `from md2ansi_lib import …`
   resolves to the real library (today the optional import silently
   fails under test, leaving `_md2ansi_fn = None`). Add an idempotent
   `sys.path.insert(0, str(_REPO / 'recipes'))` in `_load_recipe`
   (and the equivalent in any other recipe-loading test, e.g.
   `test_main.py`, if affected).
2. **`TestWalkList`.** Stop calling `self.r._PARSER_RE.search(text)`;
   call `_walk_list(0, text.rstrip('\n'), line_starts)` (or feed it a
   `md2ansi_scan` list span's `start`/`text`) and assert the same
   per-line events.
3. **`TestParse`.** Assertions unchanged; they pass against the real
   library grammar (verified above). Add a check that with
   `_INCLUDE_LISTS` off the heading events are identical to lists-on
   (guards the flag-driven `_SCAN_KINDS` selection).
4. **New: missing-dependency gate.** Set `mod._md2ansi_scan = None`,
   call `mod.main()` with argv naming a real file, assert
   `SystemExit` code 2 and the stderr message.
5. **Render-path tests** (`TestGetPreview`, `TestToggleMd`) continue to
   monkeypatch `_md2ansi_fn`; the simplified `if _MD_COLOR:` guard
   keeps them valid.

## Risk

Low. Behavior is verified-equivalent on the existing fixtures; the
removed code is a duplicate of the consumed library; the new dependency
already ships alongside the recipe and is already imported (optionally)
today. The one behavioral hardening — exiting when the library is absent
— is the explicitly requested change.
