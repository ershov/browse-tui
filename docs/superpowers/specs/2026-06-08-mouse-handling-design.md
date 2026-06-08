# Mouse handling — divider drag-resize design

**Status:** DEFERRED (2026-06-08)
**Date:** 2026-06-08

> **DEFERRED 2026-06-08.** This feature is postponed; no implementation is
> scheduled. The design below was completed and reviewed (decisions in §9 are
> final) and is recorded here in case the work is picked up later. The
> dedicated `worktree-mouse-handling` worktree/branch was removed; nothing was
> implemented.

## 1. Goal & scope

Make pane dividers **drag-resizable with the mouse**, live, in every layout
family (`h`/`v`/`m`/`pc`). The split is stored as a **percentage** (a ratio
float), exactly as the keyboard `-`/`=`/`+`/`_` resize already does, so the
chosen size survives terminal resizes.

Both dividers are draggable:

- **Main divider** — list ↔ rest. Adjusts the existing `Browser.list_ratio`.
- **Inner divider** — children ↔ preview (or list ↔ children in `m`). Adjusts a
  **new** `Browser.children_ratio` override (§4), because the children pane is
  currently *content-sized* (no stored ratio exists for it yet).

### 1.1 Governing principles

- **Mirror the keyboard resize.** Dragging the main divider must land on the
  same `list_ratio` the keyboard path would produce for an equivalent size, and
  go through the same clamps (`_clamp_list_ratio`, layout minimums). No second
  source of truth for "how big is the list."
- **Ratio, not pixels.** Every drag converts the pointer position to a fraction
  of the relevant axis and stores the fraction. Re-layout (resize, layout
  switch) recomputes pixels from the fraction.
- **Live.** The pane resizes on every drag-motion event, not just on release.
- **Additive at the terminal layer.** New event kinds (`mouse-drag`,
  `mouse-up`) are decoded from sequences that are *currently dropped* as
  `_mouse`. Existing `mouse-click` / `scroll-up` / `scroll-down` semantics are
  unchanged.
- **Graceful headless degradation.** Like today's `_dispatch_mouse`, drag
  handling no-ops when `term_size` / `layout_panes` aren't injected.

### 1.2 Out of scope — deferred (original request #1, #2)

The original request also asked for **clicking the expand marker to toggle
expand/collapse** and **clicking the selection marker to toggle selection**.
Per direction ("drop chrome click detection for now") these are **deferred**.
They depend on hit-testing the row *chrome* (`default_row_chrome` — the
`'* '` selection gutter + `'▼ '`/`'▶ '` expander), which this design does not
build.

Recorded decisions for when they are implemented (do not lose these):

- A marker click **first moves the cursor to the clicked row**, then toggles —
  consistent with the keyboard model where expand/select act on the focused row.
- The expander toggle reuses `ctx.expand(id)` / `state.expanded.discard(id)`;
  the selection toggle reuses the `state.selected` add/discard +
  `_fire_selection_change()` path (as `_select_toggle_down/up` do).
- Hit-testing needs marker column spans. Two options were identified:
  (a) compute from the **default** chrome geometry (sel = first 2 cols;
  expander = 2 cols after `2·depth` indent) — correct for every shipping recipe,
  none of which override `format_row_chrome`; or (b) have `render_list` record
  exact per-row spans (robust to chrome overrides). Pick at that time.

Also out of scope for v1: dragging an inner divider that doesn't currently
exist (e.g. no children pane shown), and any non-left-button gesture.

## 2. Background — how things work today

### 2.1 Event pipeline

`020-terminal.py` enables mouse tracking in `_enter_raw`:

```
\033[?1049h\033[?25l\033[?1000h\033[?1006h
```

`?1000h` is **normal** tracking — button **press/release only, no motion**.
`?1006h` selects SGR extended coordinates. `read_key` decodes the SGR form
`ESC [ < Cb ; Cx ; Cy M|m` to:

| `Cb` | final | emitted | 
|------|-------|---------|
| 0    | `M`   | `mouse-click:Cy:Cx` (left press) |
| 64   | `M`   | `scroll-up:Cy:Cx` |
| 65   | `M`   | `scroll-down:Cy:Cx` |
| *anything else* | | `_mouse` (release, drag, right-click → dropped) |

`070-actions.py:_dispatch_mouse` is **stateless**: it resolves the layout via
the injected `layout_panes`, finds the pane under `(row,col)` with `_pane_at`,
and applies per-pane behaviour (list click → move cursor; preview click →
dismiss help; wheel → scroll the pane). It runs before mode handling, so it
fires in normal and search/filter-edit modes alike.

### 2.2 Layout geometry & divider rects

`050-render.py:layout_panes(...)` returns a dict of `Rect`s (1-based,
inclusive-top, exclusive-right/bottom) keyed `list`, `children`, `preview`,
`sep_main`, `sep_inner`, `info_bar`. The divider that carries each split:

| split | main divider (list ↔ rest) | inner divider |
|-------|----------------------------|---------------|
| `h`   | row `list_rect.bottom` (== `info_bar.top`); horizontal | row `preview_rect.top` (children ↔ preview), only when children shown; horizontal |
| `v`   | `sep_main` (1 col, full body height); vertical | `sep_inner` (1 col); vertical — children ↔ preview **width** |
| `m`   | `sep_main` (1 col, full body height); vertical | `sep_inner` (1 row, left column); horizontal — list ↔ children **height** |
| `pc`  | `sep_main` (1 col, full body height); vertical | `sep_inner` (1 row, right column); horizontal — children ↔ preview **height** |

In `h`, `sep_main`/`sep_inner` are `None`; the separators are folded into the
pane rects whose first row *is* the separator. `_pane_at` already returns
`None` for the `sep_main` column (it sits in the exclusive gap between
`list_rect.right` and the right area) and `'info_bar'` for the `h` divider
row — both currently no-ops, so intercepting them for drag-start is clean.

### 2.3 `list_ratio` and the keyboard resize

`Browser.list_ratio` is a float clamped to `[_LIST_RATIO_MIN, _LIST_RATIO_MAX]`
= `[0.005, 0.995]` by `_clamp_list_ratio`. The layout helpers read it as
"fraction along the **primary** split axis" — rows for `h`, cols for `v`/`m`/`pc`.
`_resize_list(ctx, direction)` computes a step, applies it to the list
height/width, re-derives the ratio (`new_list_h/rows` or `new_list_w/cols`),
clamps, and forces a full redraw. The drag path computes the ratio **directly
from the pointer position** rather than stepping, but lands in the same field
and reuses the same clamps.

### 2.4 Children-pane sizing (why a new ratio is needed)

The children pane has **no stored ratio**. `_layout_*` size it from
`children_rows_needed` (content) capped at `_CHILDREN_CAP_FRAC = 0.25` of the
sub-area. To drag-resize it we introduce an explicit override (§4) that, when
set, supersedes the content+cap heuristic.

## 3. Terminal layer changes (`020-terminal.py`)

### 3.1 Enable motion reporting

Swap `?1000h` → `?1002h` (**button-event** tracking: motion reported **only
while a button is held** — gives us drag without the `?1003h` any-motion
firehose). Update the enter/leave sequences:

- `_enter_raw`: `...\033[?1002h\033[?1006h`
- `_leave_raw`: `\033[?1006l\033[?1002l\033[?25h\033[?1049l`

### 3.2 Decode drag + release

Extend the SGR branch in `read_key`. SGR `Cb` bit layout: low 2 bits = button,
bit 5 (value 32) = motion, bit 6 (value 64) = wheel. New mapping (additions in
**bold**, existing rows unchanged):

| `Cb` | final | emitted |
|------|-------|---------|
| 0    | `M`   | `mouse-click:Cy:Cx` |
| **0**| **`m`** | **`mouse-up:Cy:Cx`** (left release) |
| **32** | **`M`** | **`mouse-drag:Cy:Cx`** (left-button motion) |
| 64   | `M`   | `scroll-up:Cy:Cx` |
| 65   | `M`   | `scroll-down:Cy:Cx` |
| other | | `_mouse` |

Because we use `?1002h` (not `?1003h`), no button-less motion (`Cb` 35) is
delivered, so there is no idle-motion event stream to filter.

## 4. New state — `Browser.children_ratio`

Add `children_ratio: float | None`, default **`None`** (= "auto": today's
content+25%-cap sizing). When non-`None` it is the children pane's fraction of
the **secondary** region along the secondary axis:

| split | secondary axis | region (denominator) | what the rest gets |
|-------|----------------|----------------------|--------------------|
| `h`   | rows | rows below the main divider (children + preview) | preview |
| `pc`  | rows | right-column body height | preview |
| `v`   | cols | right-area width | preview |
| `m`   | rows | left-column body height | list (top), children (bottom) |

Threading: `layout_panes` gains a `children_ratio=None` kwarg, forwarded to each
`_layout_*`. In a helper, when `children_ratio is not None` **and** the children
pane is shown, size the children pane from the ratio instead of
`children_rows_needed`; the explicit `_CHILDREN_CAP_FRAC` cap is **bypassed**
(user intent overrides the heuristic), but the helper still clamps to keep
children ≥ 1 cell and the adjacent pane ≥ its existing minimum (`_PREV_MIN` for
preview, ≥ 1 list row in `m`). `None` preserves byte-for-byte current behaviour.

**Persistence:** once a drag sets `children_ratio`, it is **kept for the
lifetime of the run** — it is *not* reset by toggling the children pane off/on
or by switching layout. It starts at `None` (auto-size), is in-memory only
(nothing persists it across launches), and carries across a layout switch as a
fraction of whichever secondary region the new layout defines (§4 table). This
mirrors how `list_ratio` already survives layout switches with its axis
reinterpreted (rows ↔ cols).

## 5. Drag state machine (`070-actions.py`)

`_dispatch_mouse` becomes drag-aware via a single new field
`Browser._drag` (`None` when idle).

### 5.1 The drag record

On a `mouse-click` (press) that lands on a divider, set:

```
browser._drag = {
    'divider': 'main' | 'inner',
    'split':   <current split>,     # frozen for the gesture
}
```

We recompute the layout fresh on each motion event (cheap, and correct across a
mid-drag `SIGWINCH`), so the record only needs which divider and which split.

### 5.2 Dispatch flow

```
on 'mouse-click:R:C':
    d = _divider_at(layout, R, C, split, show_children)   # 'main' | 'inner' | None
    if d is not None:
        browser._drag = {'divider': d, 'split': split}
        return True                      # do NOT run pane click handling
    ... existing pane handling (cursor move / help dismiss) ...

on 'mouse-drag:R:C':
    if browser._drag is None: return True          # drag not on a divider → ignore
    _apply_divider_drag(browser, browser._drag, R, C)
    return True

on 'mouse-up:R:C':
    browser._drag = None
    return True
```

Notes:
- A press on a divider that is **released without moving** produces no
  `mouse-drag`, so nothing resizes and `_drag` is cleared on `mouse-up` — a bare
  click on the divider is a no-op (matches today's info_bar/sep behaviour).
- A drag that **starts off a divider** (in the list/preview) leaves `_drag` at
  `None`; the initial press already did its normal thing (cursor move) and the
  motion events are ignored. List/preview content is not drag-anything in v1.
- `mouse-up` with no active drag is a harmless no-op.

### 5.3 `_divider_at(layout, row, col, split, show_children)`

Returns `'main'`, `'inner'`, or `None`. **Vertical** dividers (the 1-column
`sep_main`/`sep_inner`) get a **±1-column grab allowance** — they match when
`col ∈ [sep.left − 1, sep.left + 1]` (within the divider's row range) — so the
1-col target is forgiving. **Horizontal** dividers (full-width, 1 row) keep an
exact row match.

- **main:**
  - `h` → `row == list_rect.bottom` (horizontal; exact row).
  - `v`/`m`/`pc` → `sep_main` is vertical: `sep_main.top <= row < sep_main.bottom`
    **and** `sep_main.left − 1 <= col <= sep_main.left + 1`.
- **inner:** only when a children pane is present.
  - `h` → `row == preview_rect.top` **and** `children_rect is not None`
    (horizontal; exact row).
  - `v` → `sep_inner` is vertical: same ±1-column rule as `sep_main` above.
  - `m`/`pc` → `sep_inner` is horizontal: `point_in_rect` against `sep_inner`
    (exact row; the ±1 allowance is column-only and these are full-width rows).
- main is tested first; in `h`-without-children the two coincide and resolve to
  main (list ↔ preview), which is correct. In the vertical layouts `sep_main` and
  `sep_inner` are separated by the children column (typically `max(8, …)` cols),
  so their ±1 zones are disjoint; only at a degenerate 1-col children column can
  the two zones share a single column, and the main-first test order makes that
  resolve to main deterministically.

### 5.4 `_apply_divider_drag(browser, drag, R, C)`

Recompute the current `layout`, then convert the pointer to a ratio and store it
live, then `browser._needs_redraw.add('all')`.

**Main divider:**
- `h`: `list_height = R - 1` (list spans rows `[1, R)`); `ratio = list_height / rows`.
- `v`/`m`/`pc`: `list_width = C - 1` (list spans cols `[1, C)`); `ratio = list_width / cols`.
- `browser.list_ratio = _clamp_list_ratio(ratio)`. Layout helpers still enforce
  the visible minimums (≥1 list cell, preview keeps `_PREV_MIN`/≥1 content), so
  an out-of-bounds drag saturates rather than breaking the layout.

**Inner divider** (compute `children_ratio` from the boundary, per §4 axes):
- `h`: `children_height = R - list_rect.bottom`; region `= rows - list_height`;
  `children_ratio = children_height / region`.
- `pc`: `children_height = R - body_top`; region = right-column body height.
- `v`: `children_width = C - right_area_left`; region = right-area width.
- `m`: children sits at the bottom of the left column, so
  `children_height = body_bottom - R`; region = left-column body height.
- Clamp the fraction so both sides keep ≥ their minimum (children ≥ 1 cell,
  neighbour ≥ its min). Store on `browser.children_ratio`.

All arithmetic uses the freshly recomputed rects; exact ±1 separator offsets are
finalised during implementation against the live layout helpers (a UI test
pins the observed result, §7).

## 6. Affected files (summary)

| File | Change |
|------|--------|
| `src-tui/020-terminal.py` | `?1000h`→`?1002h`; decode `mouse-up` (Cb 0 `m`) and `mouse-drag` (Cb 32 `M`). |
| `src-tui/050-render.py` | `layout_panes` + `_layout_*` accept `children_ratio`; size children from it when set (bypass 25% cap, keep minimums). |
| `src-tui/040-state.py` | `Browser.children_ratio` field (default `None`); kept for the run — no reset on toggle / split change. |
| `src-tui/070-actions.py` | `_drag` state; `_divider_at`; `_apply_divider_drag`; drag-aware branches in `_dispatch_mouse`; pass `children_ratio` where it already passes `list_ratio`. |
| `test/ui/test_mouse.py` | drag sequences (press-on-divider → drag → release) for each layout. |
| `test/unit/test_actions.py` | unit coverage of ratio math, `_divider_at`, `children_ratio` threading, no-op paths. |

The keyboard resize (`_resize_list`) also reads/writes only `list_ratio` today;
no change there. If desired, a follow-up can give it `children_ratio` awareness,
but that's not required for this feature.

## 7. Testing

- **Unit (`test/unit/test_actions.py`):** with `term_size`/`layout_panes`
  injected, feed `mouse-click`/`mouse-drag`/`mouse-up` sequences and assert
  `list_ratio` / `children_ratio` land where expected (and clamp at bounds);
  assert a press off-divider does **not** start a drag; assert `mouse-up`/
  `mouse-drag` with no active drag are no-ops; assert headless (no injection)
  no-ops.
- **Layout (`test/unit`):** `layout_panes(..., children_ratio=r)` produces the
  expected children size in each split; `children_ratio=None` is byte-for-byte
  identical to current output.
- **UI end-to-end (`test/ui/test_mouse.py`):** drive real SGR bytes through the
  tmux fixture — `\033[<0;C;RM` (press), `\033[<32;C;RM` (drag), `\033[<0;C;Rm`
  (release) — for `h` (main + inner) and at least one of `v`/`pc` (vertical main
  + inner) and `m` (vertical main + horizontal inner); assert the pane boundary
  moved. Use the existing `wait_for`-on-post-event-marker pattern.
- **Regression:** existing click + wheel tests must stay green (the `?1002h`
  switch and new event kinds must not perturb them).

## 8. Alternatives considered

- **`?1003h` (any-motion) instead of `?1002h`.** Rejected — reports motion with
  no button held, an idle event stream we'd only have to filter. `?1002h` gives
  exactly the drag events we need.
- **Commit the resize only on `mouse-up`.** Rejected — the request is explicit
  about *live*, and the redraw path is already cheap (`_needs_redraw.add('all')`).
- **Make the inner children pane stay content-sized and ignore its divider.**
  Rejected — the request is "both dividers"; a `children_ratio` override is the
  minimal honest way to give the inner divider something to move.
- **Renderer-recorded marker spans for chrome clicks now.** Deferred with the
  marker-click features (§1.2).

## 9. Resolved decisions

1. **Vertical-divider grab width — ±1 column.** The 1-column `sep_main`/
   `sep_inner` match when the pointer is within ±1 column of the separator
   (§5.3). Horizontal dividers stay exact-row.
2. **`children_ratio` is kept for the run.** No reset on children-pane toggle or
   layout switch; it persists in-memory for the session once dragged (§4).
