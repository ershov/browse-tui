# Meta rows — non-content rows the cursor skips

**Status:** Draft for review
**Date:** 2026-06-05
**Branch:** `worktree-meta-rows`

## Problem

Recipes need rows that are *not content* — dividers, section headers, and
structural connector lines. Today the framework has no such concept: every row
in the visible list is a landable, selectable content row. The row-kind
discriminator that exists (`VisibleEntry.kind`, `040-state.py:297`) is
`'normal'` / `'pending'`, both framework-injected and both landable — the cursor
moves by raw index (`_nav_down`/`_nav_up`, `070-actions.py:101-115`) and never
skips anything.

The gap is already being paid for. `browse-git`'s `--graph` mode emits
non-commit connector lines (`|\`, `|/`) that map to no commit. With no
primitive, it fakes them as inert `Item`s (`id='filler:<ns>:<n>'`,
`recipes/browse-git:909`) and **reimplements non-selectability by hand**:

- `_skip_fillers` (`recipes/browse-git:1391`) — an `on_cursor_change` hook that
  *bounces* the cursor off a filler **after** it lands. Infers travel direction
  from index deltas, scans a module-global ordered list (`_graph_rows_by_ns`) by
  id-position, handles top/bottom runs by reversing, and carries loop-guard
  logic because `cursor_to` re-fires the hook.
- `_on_selection_change` (`recipes/browse-git:1466`) — a *second* hook, needed
  because space / Ctrl-A / invert mutate `state.cursor` directly without firing
  `on_cursor_change`. It strips `filler:` ids out of the selection on every path
  and re-runs the bounce.
- Module globals `_graph_rows_by_ns` + `_prev_cursor_index` as scaffolding.

Three properties make this workaround a poor substitute for a primitive:

1. **Reactive, not preventive.** The cursor lands on the filler, then bounces.
   The docstring itself calls it "jarring."
2. **Leaks across choke points.** Cursor motion happens through two paths —
   `mark_cursor_changed` (arrows, mouse-click, `cursor_to`) and direct
   `state.cursor` mutation (selection-toggle steps). That fragmentation is *why*
   two hooks are needed. Any new move path is a new leak.
3. **Non-reusable.** `browse-claude` (orphaned-subagent dividers) and any future
   recipe would each reinvent it.

The need was already diagnosed in
`docs/superpowers/specs/2026-06-01-browse-git-improvements-design.md:95`:
connector rows "map to no `Item` — they'd need a **new synthetic,
non-selectable row kind**."

## Goals

- A recipe can mark a row as a **meta row**: a single line of display text
  (ANSI/SGR allowed) that the cursor **skips**, that is **never selectable**,
  and that carries **no chrome** (no selection marker, no `▼/▶`, no expansion).
- Cursor-skip is **preventive** — the cursor never comes to rest on a meta row,
  through any move path (arrows, page, home/end, mouse, selection-toggle steps).
- Meta content can be **width-aware** (columns aligned via the existing `cell_*`
  helpers) by giving the render path a `RowContext`.
- Migrate `browse-git` filler rows onto the primitive and **delete** the
  `_skip_fillers` / `_on_selection_change` / globals workaround.
- Add `browse-claude` orphaned-subagent section dividers as meta rows.
- A defined behavior for a list with **no landable row** (all-meta / empty).

## Non-goals

- **A standalone `selectable=False` flag on *content* rows.** This is a genuinely
  *orthogonal* axis (cursor still lands; the row is just excluded from the
  selection set — e.g. a `load more…` sentinel). It shares no machinery with
  meta rows (selection-set guards vs. cursor-skip) and is deferred to its own
  change.
- **A grouping / column engine.** Recipes inject meta rows positionally into
  their own `get_children` list; the framework does not own "groups." Consistent
  with the "primitives, not an engine" stance of the list-columns design
  (`...2026-06-03-list-columns-design.md:90`).
- **A `browse-fs` column header.** Dropped per review: the columns are
  self-evident and a header steals a list row.
- **Meta rows with children / expansion.** Meta rows are always leaves;
  `has_children` is ignored on them.

## Naming

The one naming decision to confirm. This spec uses **`meta`** throughout:
`Item.meta` (the flag) → `VisibleEntry.kind == 'meta'` → `format_meta` (the
render hook). Rationale: short, sits cleanly beside `'normal'`/`'pending'`, and
reads as "about the list, not in it" — covering both a divider and a header.
Alternatives considered: `aside` (evocative but less conventional); `filler`
(too narrow — undersells a labeled header; stays as browse-git's local id
prefix); `out-of-band` (accurate, clunky as an identifier). **If you prefer one
of these, say so and I'll global-replace before planning.**

## Design

### 1. Data model — `Item.meta` (`030-data.py`)

Add one structural flag, alongside `hidden` / `boundary`:

```python
meta: bool = False
```

A meta row is a display-only line. `meta=True` means: render as a single
(optionally ANSI) line, skip the cursor over it, never select it, draw no
chrome. `has_children` is ignored. Add `'meta'` to the two `known`-field sets in
`to_item` (`030-data.py:197`, `030-data.py:240`) so dict/`Item` coercion accepts
it. Docstring paragraph in the `Item` class matching the `hidden`/`boundary`
style.

A recipe declares a static divider with nothing more than:

```python
Item(id='sep:subagents', title='── Subagents ──', meta=True)
```

### 2. Visible tree — emit `kind='meta'` (`040-state.py`)

`_emit_children` (`040-state.py:1721`) sets the kind when emitting:

```python
kind = 'meta' if getattr(child, 'meta', False) else 'normal'
out.append(VisibleEntry(item=child, depth=d, kind=kind))
```

Meta rows occupy a visible-list slot (they take vertical space and a scroll
position) but are never recursed into (treated as leaves regardless of
`has_children`). Update the `VisibleEntry.kind` docstring to document `'meta'`.

### 3. Cursor navigation — preventive skip (`070-actions.py`, `040-state.py`)

This is the core of the change and the part that replaces browse-git's
workaround.

**Landability.** A row is *landable* when `kind != 'meta'`. (`'normal'` and the
`'pending'` placeholder both stay landable — current behavior is unchanged for
them. `'insert_marker'` is render-only and never appears in the real
`visible_items` list.)

**One helper, used by every move site:**

```python
def _next_landable(vis, start, direction):
    """Nearest landable index from `start` (inclusive) moving `direction`
    (+1/-1). Returns None when there is no landable row that way."""
```

Rewire the move sites so they compute the next landable index instead of
`cursor ± 1`:

- `_nav_down` / `_nav_up` (`070-actions.py:101`) — step to the next landable in
  the travel direction.
- `_nav_pgdn` / `_nav_pgup` (`070-actions.py:144`) — jump a page, then snap to
  the nearest landable (preferring the travel direction, falling back the other
  way so a page that lands in a meta run still resolves).
- `_nav_home` / `_nav_end` (`070-actions.py:118`) — first / last **landable**
  row (not raw index 0 / `len-1`).
- `_click_list_row` (`070-actions.py:1770`) — a click on a meta row snaps to the
  nearest landable (or no-ops if none).
- Selection-toggle steps (`_select_toggle_down` / `_select_toggle_up`, the paths
  `browse-git`'s second hook exists for) — route their cursor step through
  `_next_landable` too, so meta is skipped without a separate hook.

Because the skip is centralized and applied *during* the move, the cursor never
rests on a meta row — no bounce, no `on_cursor_change` round-trip.

**Clamp / anchor / init.** The cursor-position invariants must also respect
landability:

- Initial cursor and post-refresh clamps (`040-state.py` cursor-assignment
  sites, e.g. `:4130`, `:5259`, `:5971`) snap to the nearest landable.
- `_apply_cursor_anchor` (`040-state.py:7097`) and `_reanchor_cursor`
  (`040-state.py:7005`) — when the anchored id resolves onto a meta row (or the
  index it re-snaps to is meta), move to the nearest landable.

**No-landable case.** When `_next_landable` finds nothing (empty or all-meta
list), the cursor "parks" — it does not move onto a meta row, and the existing
`0 <= cursor < len(vis)` guards in row actions make them no-op. See §7.

### 4. Render — the meta branch (`050-render.py`)

Add a `kind == 'meta'` branch in `render_list` (beside the `'pending'` /
`'insert_marker'` branches, `050-render.py:1544-1565`):

- Resolve the row's text via the render hook (§5): a single `str` that may
  contain SGR.
- Truncate with `_truncate_visible` (`050-render.py:866`) — the existing
  one-pass, ANSI-aware truncator — to the list-pane width, then pad to width.
- Emit it with **no chrome**: no selection `*`, no `▼/▶`, no depth indent
  imposed by the framework, no search highlight, no cursor reverse-video (the
  cursor cannot land here, so the `is_cursor_line` path is never taken for meta).
  The hook owns the whole row width — this is the "occupy the whole row" model.

A meta row is cheaper to render than a normal row (it skips the segment /
RowContext-content / search-overlay pipeline).

### 5. Render hook — `format_meta(item, ctx) -> str` (`BrowserConfig`, `050-render.py`)

Meta content is produced by an **optional** hook so the simple case stays
trivial and the width-aware case is possible:

- **Default** (no override): returns `item.title`. A static divider needs only
  `meta=True` + a `title`.
- **Override** `format_meta(item, ctx)`: returns a single `str` (ANSI allowed).
  Receives a `RowContext` (`050-render.py:229`) — so the recipe has `depth`,
  `list_width`, `max_col_width`, `is_current_scope`, and `kind='meta'`. This is
  what lets `browse-git` align its graph art under the commit columns at render
  time (it reads `ctx.max_col_width`, exactly as `git_row_content` does today —
  `recipes/browse-git:948-956`).

Wired in `Browser.__init__` like the other row hooks (bound once; never `None`
at the call site).

**Why a string, not segments.** The user requirement is a single ANSI line, and
it unlocks the deferred colored-graph-spine future (a string *can* carry ANSI;
normal-row titles cannot — `...browse-git-improvements-design.md:93`).
`browse-git` fillers are monochrome today, so as strings they are literally
`' ' * pad + graph`. Columnar/colored meta content composes with the existing
`cell_*` helpers, which are already ANSI-aware (they strip escapes when
measuring width — `050-render.py:983`): justify the plain text, then paint.

### 6. Minor primitive — `paint(text, name_or_fg, bold=False) -> str`

`style()` returns a `(fg, bold)` tuple consumed by the segment writer; there is
no string-emitting analogue, so a colored meta *string* would otherwise
hand-roll `\033[…m`. Add a small `paint` helper (the string analogue of
`style()` + the segment writer) so colored dividers are ergonomic:
`paint('── Subagents ──', 'dim')`. Monochrome cases (browse-git fillers) need
nothing. Exported to recipes alongside `style` / `cell_*`.

### 7. Empty / all-meta mode (`BrowserConfig`)

fzf-style, minimal. A config option `on_empty` (or similar) with values
`'wait'` (default) and `'exit'`:

- `'wait'`: the cursor parks (no landable row), all row-actions no-op, the user
  exits manually. Matches today's genuinely-empty-list behavior.
- `'exit'`: when the list has no landable row, the browser quits (cancel code),
  like `fzf --exit-0`.

The hard requirement is only that `_next_landable` returns `None` cleanly and
nothing crashes; the auto-exit is a thin layer on top.

### 8. Already-handled axes (verify, don't rebuild)

- **Selection exclusion** is free: `select_all_visible` (`070-actions.py:562`),
  `is_selected` (`050-render.py:1538`), and the toggle/invert paths already gate
  on `kind == 'normal'`, so a meta row can never enter `state.selected`. This is
  the ~45 lines of `filler:`-stripping browse-git deletes outright.
- **Search exclusion** is free: `_search_find` already skips non-`'normal'`
  entries (`040-state.py:1858`).

## Migrations

### browse-git (the validation)

- In `_commit_graph_items` (`recipes/browse-git:893`): set `meta=True` on filler
  `Item`s; drop the `filler_n` counter's role as skip-scaffolding, the
  `row_ids`/`(id, is_filler)` tracking, and the `_graph_rows_by_ns[ns] = …`
  write. (Filler ids may stay unique for cache hygiene but no longer need
  position-tracking.)
- Convert the filler branch of `git_row_content` (`recipes/browse-git:950-956`)
  into the `format_meta` override: return the string `' ' * pad + graph` using
  `ctx.max_col_width`.
- **Delete:** `_skip_fillers` (`:1391`), `_next_non_filler` (`:1441`),
  `_on_selection_change` (`:1466`), the globals `_graph_rows_by_ns` /
  `_prev_cursor_index`, the doc-comment block (`:109`), and the
  `on_cursor_change=` / `on_selection_change=` wiring (`:1628`). Remove their
  tests.

The clean deletion is the proof the primitive is correctly shaped.

### browse-claude (orphaned-subagent dividers)

In `_list_session_children` (`recipes/browse-claude:975`), when the session has
subagents, bracket the subagent block with two meta divider rows:

```
── Subagents ──        (meta)
<subagent group rows>
── Session ──          (meta)
<message rows>
```

These give visual separation **and** double as **insertion sentinels**: the
recipe can locate them to position new rows via the `update_data` positioning
API. Emit them only when subagents are present (a session with none renders
exactly as before).

## Testing

Following the repo convention of testing through a real headless `Browser`
rather than a fabricated ctx (per prior hook-arity surprises):

- **Nav skip:** down/up across a single meta row and a meta *run*; meta at top
  (Home / first-down) and bottom (End / last-up); page jumps that land in a meta
  run; mouse-click on a meta row; selection-toggle steps over meta.
- **No-landable:** all-meta and empty lists — cursor parks, no crash; `on_empty`
  `'exit'` quits, `'wait'` stays.
- **Render:** meta branch emits the hook string; `_truncate_visible` trims
  ANSI-bearing content to width; no chrome / no selection marker / no reverse
  video; default hook returns `title`.
- **Data:** `to_item` accepts `meta` via dict and `Item`; `meta` excluded from
  nothing it shouldn't be.
- **Selection / search:** a meta row never enters `selected` (space, Ctrl-A,
  invert) and is never a search match.
- **Migrations:** browse-git fillers are skipped *preventively* (no
  `on_cursor_change` bounce) and unselectable; browse-claude dividers appear
  around subagents and not otherwise.
- Remove the obsolete browse-git filler-skip tests.

Baseline before work: **2989 tests, 0 failures, 4 skipped** (`main@43ea321`).

## Risks / edge cases

- **Scroll geometry.** The cursor's landable row and the viewport math must stay
  consistent when meta rows sit between content rows — `_snap_list_scroll_to_row`
  (`040-state.py:6916`), `_active_list_row` (`040-state.py:6937`), and page-size
  math. This is exactly the "perturbs scroll/`_layout_*` geometry" cost the
  list-columns design flagged when deferring a header row
  (`...2026-06-03-list-columns-design.md:92`); it is where the real care goes.
- **Filtering.** With an active filter, a meta divider whose whole section
  filters away would be left orphaned. v1 stance: meta rows are not subject to
  `_filter_hidden` (they aren't content), and keeping a divider visible when its
  section is empty is the recipe's concern (it chose to emit it). Revisit only
  if it looks bad in practice.
- **Scope row / insert mode.** The depth-0 scope row is always `'normal'`
  (a real content row); meta interacts with neither it nor the render-only
  insert marker beyond occupying ordinary list slots.

## Estimated impact

Production code is roughly **net-flat** — the framework absorbs what browse-git
sheds:

| Area | Added | Removed/moved | Net |
|---|---|---|---|
| `030-data.py` (flag + coercion + doc) | ~18 | 0 | +18 |
| `040-state.py` (emit kind, `_next_landable`, clamp/anchor snap) | ~30 | mod ~8 | +28 |
| `070-actions.py` (rewire ~7 move sites) | ~20 | mod ~20 | +20 |
| `050-render.py` (meta branch + `paint`) | ~22 | 0 | +22 |
| config (`format_meta` wiring, `on_empty`) | ~12 | 0 | +12 |
| `browse-git` (delete workaround; set flag; `format_meta`) | ~6 | ~114 | −108 |
| `browse-claude` (dividers) | ~8 | 0 | +8 |
| **Production total** | **~116** | **~122** | **≈ flat** |

Tests: roughly **+150–220** added, **~50–80** removed (obsolete filler-skip).

The cost is not volume; it is the cursor/scroll-geometry correctness work in
`040-state.py` / `070-actions.py`.
