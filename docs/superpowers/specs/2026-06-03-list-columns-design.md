# browse-tui — Columnar Lists via Composable Row-Format Hooks Design

**Date:** 2026-06-03
**Status:** Draft (pre-implementation)
**Supersedes nothing.** Replaces the single `format_item` whole-row hook
with three composable row-format hooks, gives those hooks a real per-row
`ctx`, and adds measurement / cell-accurate formatting primitives so
recipes can assemble aligned columns themselves.

## Motivation

A few recipes want columnar lists:

- **`browse-fs`** — files with permissions, size, and mtime as columns
  beside the name.
- **`browse-git`** — commits with short-hash, author, and date columns
  beside the subject.

The *data* is already there: `Item` is non-slotted by design and recipes
already hang domain fields on it (`browse-fs` sets `item.size/mode/mtime`;
`browse-git` sets sha/author/date). What's missing is **alignment across
rows** — every row today is a flat left-to-right segment list, so
`browse-git`'s author/date ride after a variable-length subject and read
ragged, not columnar.

The one override point that exists, `format_item(item, ctx)`
(`050-render.py:182`), is too weak to fix this:

1. **It's blind to tree state.** The renderer calls it with
   `(item, None)` — no `depth`, `selected`, `expanded`, or
   `is_current_scope`. The override "wins entirely", so a recipe that
   supplies it loses the indentation, the `▼/▶` expander, and the `* `
   selection marker. `browse-fs` is a *tree*; using `format_item` there
   is a regression, not a feature.
2. **No content-auto width.** Sizing a column to the widest value on
   screen needs to see the sibling rows; the per-item hook can't.
3. **No pane width.** `ctx` is `None` and there is no list-width
   accessor (only `preview_width`), so a recipe can't flex a column to
   fit. Worse, when padded rows overflow, `_write_segments`
   (`050-render.py:874`) truncates from the *right* — chopping the very
   metadata columns the recipe aligned.
4. **Code-point padding.** `f"{s:>6}"` counts code points, not display
   cells, so CJK / emoji content misaligns.

**Decision: add capability, not an engine.** Rather than a declarative
`Column` spec with a width-resolution engine (~600–750 lines, see
"Alternatives"), we make the hook *capable* and hand recipes the
measurement + formatting primitives they need. Recipes assemble their
own columns (~25–30 lines each). This is roughly half the framework
code, keeps the framework un-opinionated about column layout, and the
engine can still be built *on top of* these primitives later if real
usage demands it.

## Goals

1. Replace the single `format_item` whole-row hook with **three
   composable, individually-overridable hooks**:
   - `format_row(item, ctx)` — the whole row (total control).
   - `format_row_chrome(item, ctx)` — selection marker + indent +
     expander (the structural prefix).
   - `format_row_content(item, ctx)` — the content region (id + tag +
     title + chips today; arbitrary columns when overridden).

   The default composition is `format_row = format_row_chrome +
   format_row_content`. Chrome stays framework-owned unless explicitly
   overridden, so a recipe overrides **only `format_row_content`** to get
   columns *while keeping the tree*.
2. Pass a real per-row **`ctx`** (a new `RowContext`) to all three hooks
   — today they get `None`. It exposes per-row state (`depth`,
   `selected`, `expanded`, `is_current_scope`, `kind`, `parent_id`) and
   dimensions (`list_width`, `content_width`).
3. Add **`ctx.max_col_width(field, parent_id=None)`** — the maximum
   display-cell width of a pre-formatted column string across a sibling
   group. Field-name based (recipes pre-store the *display* string on
   the Item); cached per parent; invalidated when that parent's child
   list is dropped or replaced. No per-item callback.
4. Add public **cell-accurate string helpers**: `cell_width`,
   `cell_ljust`, `cell_rjust`, `cell_center`, `cell_trim`
   (ellipsis at end / start / middle, optional word-boundary), and a
   `cell_fit` convenience that trims-or-pads to an exact width. Plus a
   **styles API** — `style(name)` to resolve a named style to its raw
   `(fg, bold)`, the `STYLE_NAMES` vocabulary, and the default chrome's
   palette constants — so recipes building their own segments colour them
   consistently with `tag_style` / chips.
5. Wire **`browse-fs`** and **`browse-git`** to render columnar lists
   with the new hooks.

## Non-goals

- **A declarative column engine / column header row.** Deferred. We add
  primitives instead; the engine can be layered on later. No header row
  (it would steal a list row and perturb scroll/`_layout_*` geometry).
- **Auto fit-truncation for "flex-in-the-middle" / right-pinned-column
  layouts** (e.g. a file manager's `name ……… size`). Recipes arrange
  the flexible column *last* so the existing left-to-right truncation
  trims it; the harder reserve-and-truncate layout is out of scope.
- **Global (whole-visible-list) alignment.** Alignment is **per-parent**
  (a sibling group sizes to itself). For flat lists (`git log`, all
  commits under one parent) per-parent equals global. For trees, each
  expanded directory becomes its own tidy table; the column may step
  between directories. Whole-visible alignment would require a per-frame
  scan that cache-churns on every expand / scroll — rejected for cost.
- **The `git --graph` branch graph.** Explicitly out of scope here. (It
  is a per-row pre-aligned prefix string, a separate recipe-only change
  that doesn't need this machinery.)
- **Changes to the children-grid pane or the preview pane.** This design
  touches the list pane only.
- **Any new `Item` field.** Recipes attach display-string extras via the
  existing non-slotted pattern (as `browse-fs` already does with
  `.size`); the `Item` dataclass identity is unchanged.

## Design

### A. The three hooks and the dispatcher

`BrowserConfig` (`040-state.py:2606`) gains three optional callables and
**drops `format_item`**:

```python
format_row:         Optional[Callable] = None   # (item, ctx) -> segments
format_row_chrome:  Optional[Callable] = None   # (item, ctx) -> segments
format_row_content: Optional[Callable] = None   # (item, ctx) -> segments
```

Each returns a list of `(text, fg, bold)` triples — the existing segment
shape. **Resolution is by config, not by return value:** a hook left
unset (`None` in the config) uses the framework default for that part; a
hook that *is* set owns its return completely. There is no magic
`None`-return sentinel — a set hook always returns real segments.

To build "the default, plus a tweak" — or to column-format the common
rows and fall back for the odd ones (an error row, a "working tree clean"
row) — a recipe calls the **public default handler**, edits the list it
returns, and returns that. Explicit composition beats a sentinel.

The hooks are **resolved once in `Browser.__init__`** (after the
`on_before_init` plugin hooks fire and config is read), so the per-row
render path never tests a hook against `None`:

```python
# Browser.__init__ — bind the row-format hooks once.
self.format_row_chrome  = config.format_row_chrome  or default_row_chrome
self.format_row_content = config.format_row_content or default_row_content
# Whole-row renderer: an explicit format_row override wins; otherwise the
# (resolved) chrome + content composer with the content_width hand-off.
self._row_segments = config.format_row or self._compose_row

def _compose_row(self, item, ctx):           # the default whole-row path
    chrome = self.format_row_chrome(item, ctx)
    ctx._set_content_width(_segments_cells(chrome))    # list_width − chrome
    return chrome + self.format_row_content(item, ctx)
```

`render_list` then paints a row with one resolved call —
`segments = browser._row_segments(item, ctx)` — no hook is `None` at
runtime. The old `format_item_segments` (`050-render.py:142`) is removed:
its default chrome / content bodies move into `default_row_chrome` /
`default_row_content`, and its `kind == 'pending'` branch moves to the
`render_list` placeholder path.

#### Public default handlers

The defaults are exported (module-level, via the `browse_tui` alias), so
a recipe hook can wrap them rather than reimplement them:

- **`default_row_chrome(item, ctx)`** — the selection marker
  (`'* '`/`'  '`), indentation (`'  ' * rel_depth`), and expander
  (`'▼ '`/`'▶ '`/`'  '`) segments, lifted verbatim from today's builder.
- **`default_row_content(item, ctx)`** — the id segment (gated by
  `show_ids` / `_id_visible`), the `tag` chip, the title (with the
  `is_current_scope` → `scope_title` override), and the trailing `chips`,
  lifted verbatim. Output is **byte-for-byte identical** to today's
  `kind='normal'` content.
- **`default_row(item, ctx)`** — returns `default_row_chrome(item, ctx) +
  default_row_content(item, ctx)` and sets `ctx.content_width` along the
  way, so a whole-row `format_row` override can call it, tweak, and
  return.

`_compose_row` calls the *resolved* `self.format_row_chrome` /
`self.format_row_content` (the module defaults when a hook is unset), in
that order, so it owns the chrome→content `content_width` hand-off. A
whole-row `format_row` override bypasses `_compose_row`, so its
`content_width` stays at `list_width`. The module-level `default_row`
composes the *framework* defaults (not any other resolved hook) — it is
the "give me a stock row to tweak" helper for a `format_row` override.

**Ordering matters:** chrome is built and measured *before*
`format_row_content` runs, so `ctx.content_width` (width left after the
chrome on *this* row) is correct when the content hook reads it. For a
whole-row `format_row` override, `content_width` is left equal to
`list_width` (the chrome split is unknown).

The `kind == 'pending'` placeholder keeps its own early return (indent +
dim `⧗ loading…`), and `insert_marker` rows are still handled inline in
`render_list` before the composer is reached. The three-hook split
applies to `normal` rows only.

`render_list` (`050-render.py:1295`) constructs a `RowContext` per row
and passes it plus the three hooks into `format_row_segments`. The three
existing paint paths (cursor reverse-video `:1308`, search-highlight
`:1324`, normal `:1340`) are **unchanged** — they collapse segments to
text or hand them to `_write_segments`, so padding baked into segment
text flows through all three for free.

### B. `RowContext`

A new lightweight class, **distinct from the action `Context`**
(`060-context.py:26`). The action Context is built once per dispatched
action and carries cursor/targets/confirm/run_external; a row ctx is
built per painted row and carries per-row geometry. Conflating them
would mutate per-row state onto a shared, longer-lived object. Keeping
them separate keeps each surface honest.

Per-row state (read-only):

| Field | Meaning |
|---|---|
| `depth` | tree depth of the row (absolute) |
| `selected` | `bool` — row is in `state.selected` |
| `expanded` | `bool` — row is in `state.expanded` |
| `is_current_scope` | `bool` — this item *is* the current scope root |
| `kind` | `VisibleEntry.kind` (`'normal'` for hook rows) |
| `parent_id` | `state._parent_of_id.get(item.id)` |

Dimensions (refreshed every paint, modelled on `preview_width`
`040-state.py:4088`):

| Field | Meaning |
|---|---|
| `list_width` | content width of the list pane in cells (`rect.width`) |
| `content_width` | cells left for `format_row_content` after chrome on this row |

Both return `0` before the first paint / in headless tests, matching the
`preview_width` contract; recipes wanting a fallback pick one explicitly.

Method:

- **`max_col_width(field, parent_id=None)`** — see §C.

Advanced escape hatch (mirrors `Context.browser`, marked unstable):

- **`browser`** — the underlying `Browser`, for capabilities not yet on
  `RowContext`.

`RowContext` is constructed cheaply per row (`height` objects per frame —
negligible, in line with the per-row work `render_list` already does).

### C. `max_col_width` — cached per-parent column measurement

```python
def max_col_width(self, field, parent_id=None) -> int:
    """Max display-cell width of ``str(getattr(child, field, ''))``
    over the children of ``parent_id`` (default: this row's parent)."""
```

- **Storage.** `browser._col_width_cache: dict[parent_id, dict[field,
  int]]`. First call for a `(parent_id, field)` iterates
  `state._children[parent_id].list`, computes `max(cell_width(
  str(getattr(child, field, ''))) ...)`, and memoises it. Later calls
  hit the cache. Missing attribute → `''` → contributes `0` (safe for
  synthetic / heterogeneous rows).
- **Invalidation.** Drop `_col_width_cache.pop(parent_id, None)` in
  `_index_drop_children` (`040-state.py:448`) — the single choke point
  where a parent's child list is dropped or replaced. `_index_add_children`
  (`:469`) installs the new list and the next render rebuilds the entry
  lazily. `cache_invalidate_subtree` / `cache_invalidate_all` clear their
  scope too. This makes the cache automatically correct across
  `refresh`, `update_data`, and re-fetch — no recipe action required.
- **It measures the *display* string, not the raw value.** The recipe
  stores the formatted string it intends to show (e.g.
  `item.col_size = human_size(st.st_size)`) and passes that field name.
  This is why §3 chose field-names over a `key=` callable: a callback
  fires per child per measure and per frame; a stored string is measured
  once per cache fill. Cheaper, and it keeps the "what you measure is
  what you render" invariant.
- **Per-parent semantics.** `parent_id` defaults to the current row's
  parent, so a row aligns to its siblings. Flat lists (a `git log` whose
  commits all share one parent) get global alignment for free; trees
  align per directory (see Non-goals).

### D. Cell-accurate string helpers (public, module-level)

Defined at module scope so the `sys.modules['browse_tui']` alias
(`080-cli.py:1134`) exports them automatically (the project has no
`__all__`); recipes do `from browse_tui import cell_fit, cell_ljust, …`.
All operate on **plain text** measured in **display cells** (wide-char
aware via `_char_width` `020-terminal.py:193`), consistent with
`_truncate_by_cells`. Recipes carry color via the segment `fg`/`bold`,
never embedded SGR, so width math stays exact.

```python
cell_width(s) -> int
    # display-cell width (public face of _visible_len)

cell_ljust(s, width, fill=' ') -> str   # pad right; s unchanged if wider
cell_rjust(s, width, fill=' ') -> str   # pad left
cell_center(s, width, fill=' ') -> str  # pad both sides

cell_trim(s, width, *, where='end', ellipsis='…', word_boundary=False) -> str
    # s unchanged if it already fits; else trim to `width` cells with the
    # ellipsis placed at the end ('abcd…'), start ('…wxyz'), or middle
    # ('ab…yz'). word_boundary (middle only) prefers a space near the cut.
    # ellipsis defaults to '…' (1 cell); pass '...' for three dots.

cell_fit(s, width, *, justify='left', trim='end', ellipsis='…',
         fill=' ', word_boundary=False) -> str
    # the one-call column formatter: cell_trim if too wide, else pad to
    # exactly `width` cells per `justify` (left/right/center). Always
    # returns exactly `width` cells.
```

`cell_ljust/rjust/center` and `cell_trim` are the primitives; `cell_fit`
is the ergonomic combinator recipes will reach for most. They build on
`_char_width` and the truncation logic already in `050-render.py`.

**Styles — named and raw.** A segment's colour *is* a raw style: a
`(fg, bold)` pair where `fg` is a 256-colour int (or `None` for the
terminal default) and `bold` is a bool. The `tag_style` / chip vocabulary
is the set of *named styles* mapping onto those raw pairs (`_TAG_STYLE`,
`050-render.py:49`), but that table is internal — a recipe assembling its
own segments can't reach it. Expose both layers:

```python
# Named styles
style(name) -> (fg, bold)
    # 'green'|'red'|'yellow'|'gray'|'cyan'|'blue'|'magenta'|'dim'|''
    # → the raw (fg, bold) pair tag_style / chips resolve to.
    # Unknown / '' name → (None, False) (plain), matching tag rendering.
STYLE_NAMES: frozenset[str]      # the valid named-style keys

# Raw styles — the semantic palette the default chrome uses, exposed as
# constants so columns can match it without magic numbers:
MARKER_FG = 4      # blue ▼/▶ expander
ID_FG     = 3      # yellow #id segment
DIM_FG    = 242    # dim — the 'dim' named style's fg
```

A segment author writes either a named style (`fg, bold = style('dim')`)
or a raw value directly (`(text, DIM_FG, False)`, or any 256-colour int).
The named vocabulary is the recommended, stable colour API; raw ints are
the escape hatch for colours outside the palette. `STYLE_NAMES` lets a
recipe validate / enumerate (it mirrors the list documented on
`Item.tag_style`).

### E. The "flexible column last" truncation contract

Recipes put the *flexible* column (the name / subject) **last** in the
segment list. Then the existing left-to-right truncation in
`_write_segments` trims that column when the pane is narrow, leaving the
fixed metadata columns intact — no reserve math needed:

- `browse-fs`: `[perms][size][mtime][▼ name…]` → narrow trims the name.
- `git log`:   `[sha][author][date][subject…]` → narrow trims the subject.

This is documented as the supported layout. The opposite arrangement
(metadata pinned to the right edge, flex in the middle) is the case left
to a future engine (Non-goals).

## Worked examples

### `browse-fs`

`get_children` stores the display strings it wants as columns:

```python
item.col_perms = stat.filemode(st.st_mode)        # '-rw-r--r--'
item.col_size  = '' if is_dir else human_size(st.st_size)
item.col_mtime = time.strftime('%b %d %H:%M', time.localtime(st.st_mtime))
```

```python
def fs_row_content(item, ctx):
    if getattr(item, 'col_perms', None) is None:
        return default_row_content(item, ctx)    # error/synthetic row
    w = ctx.max_col_width
    dfg, dbold = style('dim')
    return [
        (cell_ljust(item.col_perms, w('col_perms')) + '  ', dfg, dbold),
        (cell_rjust(item.col_size,  w('col_size'))  + '  ', dfg, dbold),
        (cell_ljust(item.col_mtime, w('col_mtime')) + '  ', dfg, dbold),
        (item.title, None, False),        # flexible column, last
    ]
```

`BrowserConfig(..., format_row_content=fs_row_content)`. Chrome (indent +
`▼/▶`) is untouched, so the directory tree still works. Size moves out of
the `tag` chip.

### `browse-git`

Commits are flat under the root, so `max_col_width('col_author')` is
global. `get_children` stores `item.col_sha`, `item.col_author`,
`item.col_date`; `format_row_content` emits `[sha][author][date][subject]`
with the subject last. The `%D` decoration chips render between the date
column and the subject, so the subject stays the flexible last segment (a
narrow pane truncates the subject, not the metadata). Reflog rows keep the
default chip layout (out of scope here). ~30 lines.

## Rollout

Landed as separate commits (house style), each green before the next.

**Stage 1 — `cell_*` helpers.** Pure functions + unit tests. No behavior
change anywhere. Independent.

**Stage 2 — `RowContext` + hook binding.** Extract the public
`default_row_chrome` / `default_row_content` / `default_row` handlers and
the `_compose_row` composer; **resolve the three hooks in
`Browser.__init__`** (`config.X or default_X`); build the ctx and call
`browser._row_segments(item, ctx)` in `render_list`; drop `format_item` in
favour of `format_row` / `format_row_chrome` / `format_row_content`.
Default rendering is unchanged.

**Stage 3 — `max_col_width` + cache + invalidation.** Add the cache,
wire the drop into `_index_drop_children` and friends.

**Stage 4 — `browse-fs` columns.**

**Stage 5 — `browse-git` columns.**

## Test coverage

Stage 1:
- `cell_width` counts wide chars as 2.
- `cell_ljust/rjust/center` pad to exact cells; return input unchanged
  when already wider; honour a wide `fill` guard (fill must be 1 cell).
- `cell_trim` end/start/middle placement; no-op when it fits; ellipsis
  width is accounted; `word_boundary` snaps to a space; wide chars near
  the cut don't overshoot.
- `cell_fit` always returns exactly `width` cells across justify × trim.

Stage 2:
- Default `format_row_segments` output is identical to the pre-change
  `format_item_segments` for normal / pending rows (golden comparison).
- Overriding `format_row_content` keeps chrome; overriding
  `format_row_chrome` keeps the default content; overriding `format_row`
  replaces everything.
- A hook left unset (config `None`) uses the default for that part
  (golden); a set hook's return is used verbatim (no `None`-return path).
- `default_row` / `default_row_chrome` / `default_row_content` are
  importable from `browse_tui` and equal the composer's internal
  defaults; a hook that calls one, edits the result, and returns it
  composes correctly.
- The three hooks are bound in `Browser.__init__` —
  `browser.format_row_chrome is default_row_chrome` when unset — and the
  render path calls `browser._row_segments` with no `None` test.
- `ctx.content_width == list_width - cells(chrome)` for varied depths;
  `== list_width` under a whole-row `format_row` override.
- `ctx` carries correct `depth/selected/expanded/is_current_scope/
  parent_id`.
- Cursor-row and search-highlight paths still render correctly over a
  column-formatted row (padding survives the collapse-to-text).

Stage 3:
- `max_col_width` returns the per-parent max in display cells; missing
  field contributes 0.
- Second call hits the cache (no re-scan — assert via a spy / counter).
- Invalidated on `refresh`, `update_data` upsert/mod, and
  `cache_invalidate_subtree`.
- Flat list (single parent) → alignment matches global max.

Stages 4–5:
- `browse-fs`: a directory with mixed names/sizes renders aligned
  perms/size/mtime columns; the tree (indent + `▼/▶`) is intact; an
  error row falls back to the default content (via `default_row_content`).
- `browse-git`: a `git log` renders aligned sha/author/date columns with
  the subject flexing/truncating last.

## Compatibility

- **`format_item` is removed**, replaced by `format_row`. No recipe in
  the tree assigns `format_item` (verified via grep); the internal
  caller in `render_list` is migrated. The internal
  `format_item_segments` is renamed `format_row_segments` (no external
  consumers — it's a render-layer helper).
- **Hooks now receive a real `ctx`** where they previously received
  `None`. Since `format_item` was unused, nothing observes the old
  `None`.
- **`cell_*` helpers and `RowContext` are additive.** No `Item` field is
  added or changed; recipes use the existing extra-attribute pattern for
  column display strings.
- **Per-parent alignment** is a deliberate, documented semantic, not an
  accident.

## Performance

- `max_col_width` is `O(siblings)` on a cache miss and `O(1)` on a hit,
  invalidated only when a child list changes — no per-frame scan over
  the visible set.
- `RowContext` is a small per-row object (`height` per frame), in line
  with the per-row work `render_list` already does.
- `cell_*` are `O(len)`. Consistent with the repo's perf posture
  (PaneCache, render caches).

## Alternatives considered

- **Declarative `Column` engine** (config-level `columns=[Column(...)]`,
  framework width-resolution, optional header row): ~600–750 lines,
  uniform and auto-width across recipes, but more code and an opinionated
  layout model, and the header row perturbs scroll/`_layout_*` geometry.
  Rejected for v1; can be built on top of these primitives later.
- **Keep `format_item`, just forward `depth`/`selected`/width to it**:
  fixes the tree-blindness but still leaves every recipe to reimplement
  chrome and lacks the measurement/formatting primitives. The
  chrome/content split + `cell_*` + `max_col_width` is barely more code
  and far more ergonomic.

## Decisions (resolved in review)

1. **The content hook is `format_row_content`** — it owns the full content
   region (id + tag + title + chips), not only the title text.
2. **Hook resolution is by config, never by return value, and bound
   once.** Unset (`None`) → framework default; a set hook owns its return.
   The hooks are resolved in `Browser.__init__` (`config.X or default_X`),
   so the per-row render path makes no `None` comparison. The default
   handlers are public (`default_row`, `default_row_chrome`,
   `default_row_content`) so recipes compose by call-edit-return rather
   than via a `None`-return sentinel.
3. **Styles are exposed in two layers** — named (`style(name)`,
   `STYLE_NAMES`) and raw (`(fg, bold)` pairs and the palette constants
   `MARKER_FG` / `ID_FG` / `DIM_FG`).
4. **`cell_trim` / `cell_fit` ellipsis defaults to `'…'`** (1 cell);
   `'...'` is available via the `ellipsis=` parameter.
5. **`RowContext.browser` escape hatch is included** now, mirroring the
   action `Context`.

## Open questions for review

- **Raw-style palette constants** (`MARKER_FG` / `ID_FG` / `DIM_FG`) — is
  this the right set and naming? They are the chrome colours a column is
  most likely to want to match; more can be added later if needed.
