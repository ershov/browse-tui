# Meta rows — non-content rows the cursor skips

**Status:** Draft for review (rev 2)
**Date:** 2026-06-05
**Branch:** `worktree-meta-rows`

## Problem

Recipes need rows that are *not content* — dividers, section headers, and
structural connector lines. Today every row in the visible list is a landable,
selectable content row. The row-kind discriminator that exists
(`VisibleEntry.kind`, `040-state.py:297`) is `'normal'` / `'pending'`, both
framework-injected and both landable — the cursor moves by raw index
(`_nav_down`/`_nav_up`, `070-actions.py:101-115`) and never skips anything.

The gap is already being paid for. `browse-git`'s `--graph` mode emits
non-commit connector lines (`|\`, `|/`) that map to no commit. With no
primitive, it fakes them as inert `Item`s (`id='filler:<ns>:<n>'`,
`recipes/browse-git:909`) and **reimplements non-selectability by hand**:

- `_skip_fillers` (`recipes/browse-git:1391`) — an `on_cursor_change` hook that
  *bounces* the cursor off a filler **after** it lands (its own docstring calls
  this "jarring"). Infers direction from index deltas, scans a module-global
  ordered list (`_graph_rows_by_ns`) by id-position, handles end-runs by
  reversing, and carries loop-guard logic because `cursor_to` re-fires it.
- `_on_selection_change` (`recipes/browse-git:1466`) — a *second* hook, needed
  because space / Ctrl-A / invert mutate `state.cursor` directly without firing
  `on_cursor_change`. Strips `filler:` ids out of the selection on every path
  and re-runs the bounce.
- Module globals `_graph_rows_by_ns` + `_prev_cursor_index`.

The workaround is reactive (not preventive), leaks across the two cursor-move
choke points (`mark_cursor_changed` vs. direct `state.cursor` writes), and is
non-reusable. The need was already diagnosed in
`docs/superpowers/specs/2026-06-01-browse-git-improvements-design.md:95`:
connector rows "map to no `Item` — they'd need a **new synthetic,
non-selectable row kind**."

## Goals

- A recipe marks a row `meta=True`: a non-content row the cursor **skips**
  (best-effort, see below), that is **never selectable**, and **does not
  participate in search/filter by default**.
- Cursor-skip is **preventive** for sequential navigation (arrows, page) — no
  bounce, no `on_cursor_change` round-trip.
- Meta rows **reuse the normal chrome + content rendering pipeline**, so
  indentation and alignment come for free and there is no second hook.
- Row content may be **segments** (default; color via segment `fg`) **or a raw
  ANSI string** (passthrough of pre-colored text), with defined ANSI handling
  shared with the preview pane.
- Per-Browser control over how meta rows behave under search and filter.
- Migrate `browse-git` filler rows onto the primitive and **delete** the
  `_skip_fillers` / `_on_selection_change` / globals workaround.
- Add `browse-claude` orphaned-subagent section dividers as meta rows.

## Non-goals

- **A navigable meta header** (cursor *lands* on a header, Enter collapses its
  section). The design stays forward-compatible (landing is tolerated, see
  §3.1), but skip-by-default ships and the opt-out is deferred.
- **A standalone `selectable=False` on *content* rows** (the load-more
  sentinel). Orthogonal axis, different machinery, deferred.
- **Arbitrary non-hashable IDs (`==`-only).** Rejected: ids are dict keys / set
  members on the hot path; `==`-only forces O(n) lookups. Structured *hashable*
  ids (tuples / namedtuples) already work and are the recommendation (§6).
- **A grouping / column engine, and a `browse-fs` column header.** Recipes
  inject meta rows positionally; the framework owns no "groups." (browse-fs
  header dropped per review — columns are self-evident, don't steal a row.)
- **Meta rows with children / expansion.** Meta rows are always leaves.

## Naming

Confirmed: **`meta`** — `Item.meta` (flag) → `VisibleEntry.kind == 'meta'`.
No render hook is named (the pipeline is reused, §4).

## Design

### 1. Data model — `Item.meta` (`030-data.py`)

One structural flag, alongside `hidden` / `boundary`:

```python
meta: bool = False
```

`meta=True` means: render via the normal pipeline with chrome reduced to
indentation, skip the cursor over it by default, never select it, and exclude it
from search/filter (per §5). `has_children` is ignored (always a leaf). Add
`'meta'` to the two `known`-field sets in `to_item` (`030-data.py:197,240`).
Docstring paragraph matching the `hidden`/`boundary` style.

A static divider needs nothing more than:

```python
Item(id=('sep', 'subagents'), title='── Subagents ──', meta=True)
```

**Why a single flag (not separate `meta`/`selectable`/`landable`).** The three
are conceptually distinct, but only *landable* has independent value (a
navigable header). Selecting a non-content row is meaningless, so non-selectable
stays coupled to `meta`. Because landing is *tolerated* (§3.1), "non-landable"
reduces to "nav-skips-by-default"; a future navigable header is `meta` +
opt-out-of-skip — purely additive. Ship one flag.

### 2. Visible tree — emit `kind='meta'` (`040-state.py`)

`_emit_children` (`040-state.py:1721`) sets the kind:

```python
kind = 'meta' if getattr(child, 'meta', False) else 'normal'
out.append(VisibleEntry(item=child, depth=d, kind=kind))
```

Meta rows occupy a visible-list slot (vertical space + scroll position) and are
never recursed into. Update the `VisibleEntry.kind` docstring.

### 3. Cursor navigation

#### 3.1 Best-effort, not an invariant

The framework makes its **best effort** to never rest the cursor on a meta row,
but it **can** happen (explicit `cursor_to`, a click policy edge, an all-meta
list) and **this is not an error**. Recipes must tolerate it: `ctx.cursor`
already returns `None` for non-`'normal'` rows (`050-render.py:1996`), so
cursor-item actions no-op there today. The renderer must also paint a meta row
*correctly even under the cursor* (no crash; see §4).

#### 3.2 Move policy

A row is *landable* when `kind != 'meta'` (`'normal'` and `'pending'` stay
landable — unchanged). Sites:

- **Arrows / page** (`_nav_down/up/pgdn/pgup`, `070-actions.py:101-167`): step in
  the travel direction to the next landable row (page = jump then snap toward
  travel, falling back the other way).
- **Home / End** (`_nav_home/end`, `:118`): first / last **landable** row.
- **Mouse click** (`_click_list_row`, `:1770`): snap to the **closest** landable
  row; tie → down. (A click is spatial intent; a divider isn't actionable.)
- **Selection-toggle steps** (`_select_toggle_down/up`, `:502,526`): route the
  cursor step through the same skip helper — no separate hook (this is what
  browse-git's second hook existed for).
- **API `cursor_to(id)`**: honored **exactly** — explicit recipe intent; landing
  tolerated per §3.1.

One shared helper does the work:

```python
def _next_landable(vis, start, direction):
    """Nearest landable index from `start` moving `direction` (+1/-1);
    None if there is none that way."""   # closest-variant for clicks
```

#### 3.3 Complexity assumption (#4)

**We expect few meta rows and no long runs of them.** The skip is a plain linear
scan from the cursor; in practice it advances 1–2 rows. No precomputed
landable-index structure (browse-git's `_graph_rows_by_ns` exists only because
its reactive hook couldn't scan the live list — the preventive design doesn't
need it). Worst case (all-meta) is O(visible) but rare and bounded by one pass.

#### 3.4 Clamp / anchor / empty

Cursor-position invariants respect landability: initial / post-refresh clamps
(`040-state.py:4130,5259,5971`) and `_apply_cursor_anchor` / `_reanchor_cursor`
(`:7097,7005`) snap to the nearest landable. When `_next_landable` finds none
(empty or all-meta), the cursor **parks** — existing `0 <= cursor < len(vis)`
guards make row actions no-op. A `BrowserConfig.on_empty` option, fzf-style:
`'wait'` (default — park and let the user exit) or `'exit'` (quit when no
landable row exists).

### 4. Rendering — reuse the chrome + content pipeline

Meta rows go through the **same** `_compose_row` path as normal rows
(`040-state.py:3507` = `format_row_chrome(item, ctx) + format_row_content(item,
ctx)`), with `ctx.kind == 'meta'` available so a shared hook can branch. **No
`format_meta` hook.**

- **Chrome.** `default_row_chrome` (`040-state.py:2699`) already blanks the
  selection marker when unselected and the expander when `not has_children`.
  A meta row is never selected and is a leaf, so its chrome reduces to *aligned
  indentation* — content lines up under normal rows' content. (The renderer
  forces the blank marker/expander for `kind=='meta'` so this holds even if a
  recipe leaves `has_children=True`.)
- **Content.** Default = the title segment (no id/chips — those are content
  decorations). Recipes override `format_row_content` (branching on `ctx.kind`)
  for richer content; `browse-git`'s `git_row_content` filler branch
  (`recipes/browse-git:950`) **already returns the right segments** and is reused
  unchanged.
- **Full-width** (divider spanning from column 0, no indentation): the recipe
  overrides `format_row` (the whole-row hook) — same mechanism normal rows use.
- **Content as a raw ANSI string.** `format_row_content` may return a `str`
  (ANSI allowed) instead of a segment list — for passthrough of pre-colored
  text (external tools, future colored graph spine). The renderer detects `str`
  vs `list` and applies §4.5. (Segments stay the norm for normal rows so the
  cursor reverse-video / search overlay compose cleanly; a raw ANSI string under
  the cursor or matching search is best-effort.)

Why reuse beats a separate hook, and why no `paint()` helper: see the rev-2
review notes — segments already carry color via `fg`, so a "dim" divider is just
`style('dim')`; a string is only needed for passthrough.

### 4.5 ANSI handling (#10)

Applies to any **raw ANSI string** rendered as row content, and to the **preview
pane**. Shares the existing `_sanitize_preview` machinery (`050-render.py:142`).

1. **Sanitize on receipt.** Strip every escape *except* SGR colour sequences:
   keep `\e[…m`, drop all other CSI (cursor moves, clears, etc.) and bare `\e`.
   (Intent of the user's `\e[.*[a-ln-z]|\e`; implemented as a robust
   per-sequence scan, not a greedy regex.)
2. **Truncate** to the pane width with `_truncate_visible` (`050-render.py:866`)
   — one-pass, ANSI-aware (counts visible cells, passes escapes through).
3. **Reset, conditionally.** If the (sanitized) string contains **no** ANSI,
   emit nothing extra. If it contains any ANSI, emit `\e[m` at the end.
4. **Background restore.** If a background colour is set for the row **and** the
   string contains any background code (`40–49`, `100–109`), re-emit
   `\e[<row-bg>m` after the reset so the trailing pad keeps the row background.

### 5. Search and filtering (#11)

**Defaults:** meta rows do **not** participate in search or filtering, and when
user filtering (`&`) is active **all meta rows are hidden**. (Search already
skips non-`'normal'` entries — `040-state.py:1858`; this extends the same
exclusion to filtering.)

**Per-Browser overrides** (`BrowserConfig`):

- `meta_search_highlight: bool = False` — whether an active search query may
  paint highlight spans on meta rows.
- `meta_filter_mode: str = 'hide'`:
  - `'hide'` (default) — meta rows hide while a filter is active (git-like; the
    graph art is meaningless when filtered).
  - `'show'` — meta rows stay visible regardless of filter (header-like; a
    section divider survives even if its section filters away).
  - `'filter'` — meta rows participate in filtering like content rows.

This subsumes the earlier "orphaned divider over an empty section" edge: a recipe
that wants its header to persist uses `'show'`.

### 6. IDs (note, not a change)

`Item.id` stays `Any`-*hashable*. Recipes that want structured ids for
namespacing can already use tuples / namedtuples (`Item(id=('filler', n))`); only
the bare-tuple *shorthand* is reserved for positional fields. With `meta` as a
flag, recipes need not encode meta-ness in the id at all (browse-git's
`'filler:'` prefix becomes unnecessary as a *marker*, though ids stay unique).
Broadening to non-hashable ids is rejected (hot-path lookups) and out of scope.

### Config surface (new)

`BrowserConfig`: `on_empty='wait'`, `meta_search_highlight=False`,
`meta_filter_mode='hide'`. No new row hook.

## Migrations

### browse-git (the validation)

- `_commit_graph_items` (`recipes/browse-git:893`): set `meta=True` on filler
  `Item`s; drop the `(id, is_filler)` row-tracking and the `_graph_rows_by_ns`
  write (ids may stay unique for cache hygiene). `git_row_content`'s filler
  branch is **kept** — it already produces the aligned graph segments.
- **Delete:** `_skip_fillers` (`:1391`), `_next_non_filler` (`:1441`),
  `_on_selection_change` (`:1466`), globals `_graph_rows_by_ns` /
  `_prev_cursor_index`, the doc block (`:109`), the `on_cursor_change=` /
  `on_selection_change=` wiring (`:1628`), and their tests.
- `meta_filter_mode` stays `'hide'` (fillers vanish under filter — correct).

The clean deletion is the proof the primitive is correctly shaped.

### browse-claude (orphaned-subagent dividers)

In `_list_session_children` (`recipes/browse-claude:975`), when a session has
subagents, bracket the subagent block with two meta rows:

```
── Subagents ──        (meta)   ← also an insertion sentinel
<subagent group rows>
── Session ──          (meta)   ← also an insertion sentinel
<message rows>
```

Emitted only when subagents are present. They give visual separation **and**
double as **insertion sentinels** for positioning new rows via the `update_data`
API. `meta_filter_mode` is likely `'show'` here (keep the labels under a filter)
— recipe's call.

## Testing

Through a real headless `Browser` (not a fabricated ctx — per prior hook-arity
surprises):

- **Nav skip:** arrows/page across one meta row and a (short) run; Home/End;
  click → closest (tie → down); selection-toggle steps; `cursor_to(meta)` lands
  exactly and does not crash.
- **No-landable:** all-meta and empty lists park cleanly; `on_empty='exit'`
  quits, `'wait'` stays.
- **Render:** chrome reduces to aligned indentation; default content = title;
  `format_row_content` returning segments and returning an ANSI string both
  render; a meta row painted *under* the cursor doesn't crash.
- **ANSI handling:** non-SGR escapes stripped, SGR kept; no trailing reset for
  plain strings, `\e[m` for ANSI ones; bg-restore fires only when a row bg is set
  *and* a bg code is present; shared behavior verified against the preview path.
- **Search/filter:** defaults (meta excluded from search, hidden under filter);
  `meta_search_highlight=True`; `meta_filter_mode` `show` / `filter` / `hide`.
- **Data:** `to_item` accepts `meta` via dict and `Item`; tuple/namedtuple ids.
- **Selection:** a meta row never enters `selected` (space, Ctrl-A, invert).
- **Migrations:** browse-git fillers skip *preventively* and are unselectable;
  browse-claude dividers appear around subagents and not otherwise. Remove the
  obsolete browse-git filler-skip tests.

Baseline before work: **2989 tests, 0 failures, 4 skipped** (`main@43ea321`).

## Risks / edge cases

- **Scroll geometry** is the real cost: the cursor's landable row and the
  viewport math must stay consistent with meta rows interleaved
  (`_snap_list_scroll_to_row` `:6916`, `_active_list_row` `:6937`, page math).
  This is the "perturbs scroll/`_layout_*` geometry" concern the list-columns
  design flagged (`...2026-06-03-list-columns-design.md:92`).
- **Raw ANSI string under cursor / search** — reverse-video and highlight
  overlay on embedded SGR is best-effort; meta rows avoid it (skip + no
  highlight by default), normal rows should keep using segments.
- **Scope row / insert mode** — the depth-0 scope row is always `'normal'`; meta
  interacts with neither beyond occupying ordinary slots.

## Estimated impact

Production code is roughly **net-flat-to-negative** — the framework absorbs what
browse-git sheds, and reusing the pipeline (no `format_meta`, no `paint`) keeps
additions small. The filter-mode logic is the main net-new piece.

| Area | Added | Removed/moved | Net |
|---|---|---|---|
| `030-data.py` (flag + coercion + doc) | ~16 | 0 | +16 |
| `040-state.py` (emit kind, `_next_landable`, clamp/anchor, chrome guard) | ~32 | mod ~8 | +30 |
| `070-actions.py` (rewire ~7 move sites incl. click-closest) | ~22 | mod ~22 | +22 |
| `050-render.py` (str-content branch + §4.5 ANSI; share `_sanitize_preview`) | ~22 | 0 | +22 |
| filter/search (3-mode + highlight gate) | ~25 | mod ~5 | +25 |
| config (`on_empty`, `meta_filter_mode`, `meta_search_highlight`) | ~8 | 0 | +8 |
| `browse-git` (delete workaround; `meta=True`) | ~3 | ~114 | −111 |
| `browse-claude` (dividers) | ~10 | 0 | +10 |
| **Production total** | **~138** | **~118** | **≈ +20** |

Tests: roughly **+180–240** added, **~50–80** removed (obsolete filler-skip).

The cost is the cursor/scroll-geometry correctness work in `040-state.py` /
`070-actions.py` and the 3-mode filter logic — not code volume.
