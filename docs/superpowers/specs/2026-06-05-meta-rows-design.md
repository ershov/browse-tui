# Meta rows — non-content rows the cursor skips

**Status:** Draft for review (rev 3)
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
  `on_cursor_change`.
- Module globals `_graph_rows_by_ns` + `_prev_cursor_index`.

The workaround is reactive (not preventive), leaks across the two cursor-move
choke points, and is non-reusable. The need was already diagnosed in
`docs/superpowers/specs/2026-06-01-browse-git-improvements-design.md:95`:
connector rows "map to no `Item` — they'd need a **new synthetic,
non-selectable row kind**."

## Goals

- A recipe marks a row `meta=True`: a non-content row the cursor **skips**
  (best-effort, §3.1), that is **never selectable**, and **does not participate
  in search/filter by default**.
- Cursor-skip is **preventive** and **uniform** across keyboard and mouse — no
  bounce, no `on_cursor_change` round-trip.
- Meta rows **reuse the normal chrome + content rendering pipeline**; no second
  hook, indentation/alignment for free.
- Row content may be **segments** (default) **or a raw ANSI string** — for **any
  row, including normal ones** (§4) — with defined ANSI handling shared with the
  preview pane.
- Per-Browser control over meta behavior under search and filter.
- Migrate `browse-git` filler rows onto the primitive and **delete** the
  `_skip_fillers` / `_on_selection_change` / globals workaround.
- Add `browse-claude` orphaned-subagent section dividers as meta rows.

## Non-goals

- **A navigable meta header** (cursor *lands* on a header, Enter collapses its
  section). Forward-compatible (landing is tolerated, §3.1), but skip-by-default
  ships and the opt-out is deferred.
- **A standalone `selectable=False` on *content* rows** (the load-more
  sentinel). Orthogonal axis, different machinery, deferred.
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
Item(id='sep:subagents', title='── Subagents ──', meta=True)
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
but it **can** happen — explicit `cursor_to`, or a list with no landable row —
and **this is not an error**. Recipes must tolerate it: `ctx.cursor` already
returns `None` for non-`'normal'` rows (`050-render.py:1996`), so cursor-item
actions no-op there. The renderer also paints a meta row correctly when it *is*
the cursor row (§4).

#### 3.2 Unified move policy

Every cursor *move* knows its start and intended target, so it has a direction.
A row is *landable* when `kind != 'meta'` (`'normal'` and `'pending'` stay
landable — unchanged). One resolver serves **all** move sites:

```python
def _resolve_landing(vis, target, before):
    """Land on the nearest landable row from `target`, scanning in the
    direction of travel (sign(target - before); tie → down), then the
    other way if that runs off the end. None when no landable row exists."""
    direction = +1 if target >= before else -1
    idx = _next_landable(vis, target, direction)      # inclusive from target
    return idx if idx is not None else _next_landable(vis, target, -direction)
```

- **Arrows / page** (`070-actions.py:101-167`): `target = before ± step`.
- **Home / End** (`:118`): `target = 0` / `len-1`. Falls out correctly — Home's
  direction is *up*, finds nothing above 0, reverses to the first landable going
  down; End mirrors it.
- **Mouse click** (`_click_list_row`, `:1770`): `target = clicked index`. Same
  resolver — skip continues in the click's direction of travel. No separate
  "closest" rule.
- **Selection-toggle steps** (`_select_toggle_down/up`, `:502,526`): route the
  cursor step through the resolver — no separate hook (this is what browse-git's
  second hook existed for).
- **API `cursor_to(id)`**: the **one exception** — honored exactly, no skip
  (explicit recipe intent; landing tolerated per §3.1).

#### 3.3 Complexity assumption (#4)

**We expect few meta rows and no long runs.** `_next_landable` is a plain linear
scan from the cursor; in practice it advances 1–2 rows. No precomputed
landable-index structure (browse-git's `_graph_rows_by_ns` existed only because
its reactive hook couldn't scan the live list; the preventive design doesn't
need it). Worst case (all-meta) is one O(visible) pass, rare.

#### 3.4 Clamp / anchor / delete / empty

Cursor-position invariants respect landability:

- Initial / post-refresh clamps (`040-state.py:4130,5259,5971`) snap via the
  resolver.
- **Deletion / displacement.** When the cursor's item is removed (an `update_data`
  remove, a refresh that shrinks the list) or hidden/filtered out, the framework
  already re-anchors the cursor onto a surviving row
  (`_compute_anchor_snapshot` `:6950`, `_reanchor_cursor` `:7005`,
  `_apply_hide_displacement` `:7038`, `_apply_cursor_anchor` `:7097`). The
  replacement index it picks must pass through `_resolve_landing` (with
  `before` = the pre-removal cursor) so it lands on a landable row, not an
  adjacent meta row. **Explicit test coverage required.**
- **Empty / all-meta.** When the resolver returns `None`, the cursor **parks** —
  existing `0 <= cursor < len(vis)` guards make row actions no-op. A
  `BrowserConfig.on_empty` option, fzf-style: `'wait'` (default — park, user
  exits) or `'exit'` (quit when no landable row exists).

### 4. Rendering — reuse the chrome + content pipeline

Meta rows go through the **same** `_compose_row` path as normal rows
(`040-state.py:3507` = `format_row_chrome(item, ctx) + format_row_content(item,
ctx)`), with `ctx.kind == 'meta'` available so a shared hook can branch. **No
`format_meta` hook.**

- **Chrome.** `default_row_chrome` (`040-state.py:2699`) already blanks the
  selection marker when unselected and the expander when not expandable. A meta
  row is never selected and is a leaf, so its chrome reduces to *aligned
  indentation* — content lines up under normal rows' content. (The renderer
  forces the blank marker/expander for `kind=='meta'` so this holds even if a
  recipe leaves `has_children=True`.)
- **Content.** Default = the title segment. Recipes override `format_row_content`
  (branching on `ctx.kind`) for richer content; `browse-git`'s `git_row_content`
  filler branch (`recipes/browse-git:950`) is reused unchanged.
- **Full-width** (divider from column 0, no indentation): the recipe overrides
  `format_row` (the whole-row hook) — same mechanism normal rows use.

#### 4.1 Content as segments OR a raw ANSI string — for any row (#6)

`format_row_content` (and `format_row`) may return either a **list of segments**
`[(text, fg, bold), …]` (the existing structured form; color rides `fg`, never
embedded in text, so width math stays exact) **or a single `str` that may carry
ANSI/SGR** (passthrough of pre-colored text). The renderer detects `str` vs
`list`. **This applies to every row, normal and meta alike** — a uniform content
representation; segments stay the convenient default, strings cover free-form /
passthrough content.

**Cursor row and search matches.** On the cursor row, or a row matching an active
search query, content is rendered as its **visible text** under the
reverse-video / highlight overlay — segment `fg` **and** embedded SGR alike are
dropped (visible-text strip, the same helper `cell_width` uses). This is exactly
how segment rows behave today (the cursor line already collapses to plain text),
so allowing ANSI strings everywhere costs nothing here. Elsewhere, color is
preserved: segments via the per-segment writer, strings via §4.2.

#### 4.2 ANSI handling (#10)

Applies to any **raw ANSI string** rendered as row content, and to the **preview
pane**. Shares the existing `_sanitize_preview` machinery (`050-render.py:142`).

1. **Sanitize on receipt.** Strip every escape *except* SGR colour sequences:
   keep `\e[…m`, drop all other CSI (cursor moves, clears, etc.) and bare `\e`.
   (Intent of the user's `\e[.*[a-ln-z]|\e`; implemented as a robust
   per-sequence scan, not a greedy regex.)
2. **Truncate** to the pane width with `_truncate_visible` (`050-render.py:866`)
   — one-pass, ANSI-aware (counts visible cells, passes escapes through).
3. **Reset, conditionally.** No ANSI in the (sanitized) string → emit nothing
   extra. Any ANSI present → emit `\e[m` at the end.
4. **Background restore.** Whenever the row has a background set (driven by the
   row's `row_bg` attribute, **not** a content scan), re-emit `\e[<row-bg>m`
   after the reset so the trailing pad keeps the row background. The reset
   clears the bg regardless of whether the content set one, so restoring on the
   attribute keeps the stripe alive even when the content carried only fg codes.

### 5. Search and filtering (#11)

**Defaults:** meta rows do **not** participate in search or filtering, and when
user filtering (`&`) is active **all meta rows are hidden**. (Search already
skips non-`'normal'` entries — `040-state.py:1858`; this extends the same
exclusion to filtering.)

**Per-Browser overrides** (`BrowserConfig`):

- `meta_search_highlight: bool = False` — whether an active search query may
  paint highlight spans on meta rows.
- `meta_filter_mode: str = 'hide'`:
  - `'hide'` (default) — meta rows hide while a filter is active (git-like).
  - `'show'` — meta rows stay visible regardless of filter (header-like; a
    section divider survives even if its section filters away).
  - `'filter'` — meta rows participate in filtering like content rows.

### Config surface (new)

`BrowserConfig`: `on_empty='wait'`, `meta_search_highlight=False`,
`meta_filter_mode='hide'`. No new row hook.

## Migrations

### browse-git (the validation)

- `_commit_graph_items` (`recipes/browse-git:893`): set `meta=True` on filler
  `Item`s (id `'filler:<ns>:<n>'`); drop the `(id, is_filler)`
  row-tracking and the `_graph_rows_by_ns` write. `git_row_content`'s filler
  branch is **kept** — it already produces the aligned graph segments.
- **Delete:** `_skip_fillers` (`:1391`), `_next_non_filler` (`:1441`),
  `_on_selection_change` (`:1466`), globals `_graph_rows_by_ns` /
  `_prev_cursor_index`, the doc block (`:109`), the `on_cursor_change=` /
  `on_selection_change=` wiring (`:1628`), and their tests.
- `meta_filter_mode` stays `'hide'` (fillers vanish under filter — correct).

The clean deletion is the proof the primitive is correctly shaped.

### browse-claude (orphaned-subagent dividers)

Tree mode (the default) surfaces *orphaned* subagents — those with no
dispatching `Agent`/`Task` call in the main thread — at the top of the listing,
in `_list_tree_roots` (`recipes/browse-claude`), ahead of the session's turn /
span umbrellas. When that orphan block is non-empty, bracket it with two meta
rows (session-namespaced ids so they stay unique and stable as sentinels, e.g.
`f'{jsonl_path}#sep:subagents'` / `f'{jsonl_path}#sep:session'`):

```
--- Subagents:         (meta)   ← also an insertion sentinel
<orphaned subagent rows>
--- Session:           (meta)   ← also an insertion sentinel
<turn / span umbrellas>
```

Emitted only when there are orphaned subagents (sessions without them render
exactly as before). They give visual separation **and** double as **insertion
sentinels** for positioning new rows via the `update_data` API — keep the ids
stable. browse-claude sets `meta_filter_mode='show'` so the labels persist under
an active filter.

## Testing

Through a real headless `Browser` (not a fabricated ctx):

- **Nav skip (unified):** arrows/page across one meta row and a short run;
  Home/End; mouse click onto a meta row skips in the click's direction;
  selection-toggle steps; `cursor_to(meta)` lands exactly and does not crash.
- **Delete / displacement:** removing the row under the cursor when a meta row is
  adjacent re-anchors onto a landable row (the #3 corner case).
- **No-landable:** all-meta and empty lists park cleanly; `on_empty='exit'`
  quits, `'wait'` stays.
- **Render:** chrome reduces to aligned indentation; default content = title;
  `format_row_content` returning segments and returning an ANSI string both
  render — **including on a normal row**; a meta row painted *under* the cursor
  doesn't crash; an ANSI-string row under the cursor shows a clean reverse-video
  highlight (SGR dropped).
- **ANSI handling:** non-SGR escapes stripped, SGR kept; no trailing reset for
  plain strings, `\e[m` for ANSI ones; bg-restore fires only when a row bg is set
  *and* a bg code is present; behavior matches the preview path.
- **Search/filter:** defaults (meta excluded from search, hidden under filter);
  `meta_search_highlight=True`; `meta_filter_mode` `show` / `filter` / `hide`.
- **Data / selection:** `to_item` accepts `meta` via dict and `Item`; a meta row
  never enters `selected` (space, Ctrl-A, invert).
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
- **Scope row / insert mode** — the depth-0 scope row is always `'normal'`; meta
  interacts with neither beyond occupying ordinary slots.

## Estimated impact

Production code is roughly **net-flat-to-negative** — the framework absorbs what
browse-git sheds, and reusing the pipeline (no `format_meta`, no `paint`; the
string-content branch is uniform, not kind-gated) keeps additions small. The
filter-mode logic is the main net-new piece.

| Area | Added | Removed/moved | Net |
|---|---|---|---|
| `030-data.py` (flag + coercion + doc) | ~16 | 0 | +16 |
| `040-state.py` (emit kind, `_resolve_landing`, clamp/anchor/delete, chrome guard) | ~32 | mod ~8 | +30 |
| `070-actions.py` (rewire ~7 move sites through the resolver) | ~20 | mod ~22 | +20 |
| `050-render.py` (str-content branch + §4.2 ANSI; share `_sanitize_preview`) | ~22 | 0 | +22 |
| filter/search (3-mode + highlight gate) | ~25 | mod ~5 | +25 |
| config (`on_empty`, `meta_filter_mode`, `meta_search_highlight`) | ~8 | 0 | +8 |
| `browse-git` (delete workaround; `meta=True`) | ~3 | ~114 | −111 |
| `browse-claude` (dividers) | ~10 | 0 | +10 |
| **Production total** | **~136** | **~118** | **≈ +18** |

Tests: roughly **+180–240** added, **~50–80** removed (obsolete filler-skip).

The cost is the cursor/scroll-geometry correctness work in `040-state.py` /
`070-actions.py` and the 3-mode filter logic — not code volume.
