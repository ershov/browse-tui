# Preview flicker: debounce + stale-hold + loading indicator

**Date:** 2026-06-12
**Status:** Draft

## Problem

When the cursor runs over items (held `j`/`k`, mouse wheel), the preview
pane flashes: each move lands on an item whose `Item.preview` cache is
`None`, the renderer's documented fallthrough paints blank rows, and the
content pops in only when the worker delivers. Fast scrolling produces
blank → partial → blank → partial churn. Streaming previews make it
worse: a stream abandoned mid-pull has its partial buffer deliberately
cleared (#456), so even just-painted content vanishes.

There is no explicit "erase before request" step to remove — the blank
paint *is* the erase, a consequence of rendering from the cursor item's
empty cache.

## Approach

Three independent, composable changes:

* **A. Debounce** — the preview worker waits for the cursor to settle
  before fetching. Removes wasted fetches and generator open/close churn
  during scrolling.
* **B. Stale-hold** — the renderer keeps showing the last painted
  preview until replacement content arrives. Removes the visible blank.
* **C. Loading indicator** — a `⧗ ` prefix on the right-aligned
  `Preview` label while a fetch for the cursored item is outstanding, so
  held-over content is distinguishable from settled content.

Together: a held `j` shows the original preview steadily with `⧗`
in the divider; after the cursor settles, one fetch fires and the pane
swaps once.

## A. Debounce in `_preview_worker`

Deliberately imprecise sleep-and-recheck (per design discussion —
simplicity over exact timing; effective delay may stretch toward 2× under
continuous movement):

1. Worker wakes, adopts the request id from the latest-wins slot
   (`local_id = req`, event cleared) — unchanged.
2. New: if `preview_debounce > 0`, `time.sleep(preview_debounce)`,
   then re-check `_stop` and re-read `_preview_req`:
   * slot changed → `continue` (top of loop adopts the new id and
     debounces again);
   * slot unchanged → fall through to the existing
     `_abandon_paused_preview_if_any` + `get_preview` call.

Everything else is untouched. Notably:

* `request_preview` stays as-is (slot + event set). No timestamps, no
  timers, no main-loop changes.
* A move-away-and-back during one sleep window ends with the slot equal
  to the adopted id — the worker proceeds to fetch it. Correct, and the
  reason no per-request stamp is needed.
* Abandoning a paused streaming generator is *not* delayed: the pause
  loop watches `_preview_req` itself and self-abandons on the cursor-move
  wake (`_preview_resume_event`), before the worker's outer loop (and
  its debounce) is even reached.
* `run_until_idle` already treats "slot set, not paused" as busy, so a
  debouncing worker counts as busy and tests wait naturally.
* Shutdown: the sleep is well under `stop_workers`' join timeout; the
  worker re-checks `_stop` after sleeping.

Scope of the delay (accepted, for simplicity): *every* fetch is
debounced — including the startup fetch and the #442 same-id re-fire
after `invalidate_preview` / `drop_preview_cache`. Stale-hold (B) masks
both.

### Config

`BrowserConfig.preview_debounce: float = 0.15` — seconds of cursor
quiet before a preview fetch; `0` disables (immediate fetch, current
behavior). Mirrored onto `Browser` like the other preview knobs.

## B. Stale-hold in `render_preview`

### Invariant the design leans on

After #442, `item.preview is None` on the cursored item always means "a
delivery is pending or imminent": every fetch delivers at least `''`,
and any cache drop while cursored is re-fired by
`_update_preview_for_cursor` each tick. So `None` is a reliable
"replacement is coming" signal, and `''` (delivered-empty) is a real,
paintable result that must blank the pane.

### Snapshot

A browser-level snapshot of the last successfully painted per-item
preview: raw text, the scroll offset it was showing, and the wrapped
rows with their `(width, ansi_on)` geometry (mirroring the per-item
`PreviewRender` wrap cache). Captured at the end of every normal
per-item content paint — a few reference assignments, no copying.

Why a snapshot rather than "keep painting the previous item's cache":
#456 nulls an abandoned stream's partial buffer right when the cursor
moves off it — the previous item's cache is exactly what disappears in
the worst flicker case. The snapshot is owned by the render layer and
survives that clear; #456 semantics stay untouched.

### Paint rule

In `render_preview`, when not in help mode and the pane would currently
paint blank-because-pending, paint the snapshot instead:

* **Hold (paint snapshot)** when the cursor entry is a pending
  placeholder row, or resolves to an item with `preview is None` — and a
  snapshot exists. Re-wrap from the snapshot's raw text if the pane
  geometry or ANSI policy changed (resize, screen-restore); otherwise
  reuse its wrapped rows.
* **Blank (current behavior)** when the visible list is empty, no
  snapshot exists yet (startup), or the item's preview is a delivered
  value — including `''`.

The stale branch is self-contained: it must not write the snapshot's
wrap into the cursored item's `preview_render` (that cache belongs to
the item's own content), must not update the snapshot from itself, and
skips the #274 demand-signal block and the tail-pin/scroll writebacks.

The held view is frozen at the snapshot's scroll offset; the
cursor-move scroll reset and subsequent scroll keys take effect when the
real content swaps in. Because the row-level pane cache diffs rows,
repeatedly painting the identical snapshot during a scroll burst emits
no terminal bytes — flicker is gone by construction.

A pleasant side effect: `invalidate_preview` on the *cursored* item
(e.g. the resize-refetch path) now holds the item's own previous content
during the refetch instead of blanking.

## C. Loading indicator

`_preview_label` gains a loading variant: `⧗ Preview` (prefix, since the
label is right-aligned in the divider) when a preview is outstanding for
the cursored item. `Help` mode never shows it.

**Predicate** (same logic `run_until_idle` already uses for
`preview_busy`):

```
loading = (_preview_req is not None
           and _preview_req == _preview_cursor_id
           and not (paused stream for that same id))
```

This yields exactly the requested lifecycle: ON from the cursor move
(request slot set, covers the debounce window and the fetch), ON while
a streaming generator is actively pulling (slot stays set until
exhaustion), OFF when a non-streaming delivery lands (delivery drains
the slot), OFF on stream exhaustion/error (worker clears the slot), and
OFF while a streaming preview is **paused** at its buffer cap (slot
still set, but the paused id matches). Demand-resume turns it back ON
via the next chunk's repaint.

**Repaint wiring:** the label lives in two places — the preview header
row (`'h'` layout, painted under the `'preview'` redraw key) and the
standalone bottom info bar (other layouts, `'info'` key). Rather than
chasing every transition site, the main loop memoizes the predicate
value once per tick (after `_update_preview_for_cursor`) and flags
`{'preview', 'info'}` when it flips. All transitions already wake the
loop: cursor moves happen in-loop, deliveries post + wake, and the
streaming pause/exhaustion paths call `notify_wake()`. The row cache
no-ops whichever of the two paints didn't actually change.

## Out of scope / rejected

* **Adaptive debounce** (immediate fetch on the first move after quiet,
  debounce only under rapid movement) — rejected in design discussion in
  favor of the fixed sleep; revisit only if the fixed delay feels bad.
* **Dimming/styling the held-over content** — the `⧗` label prefix is
  the only staleness cue; keep the content itself plain.
* **Exempting the startup fetch from the debounce** — accepted ~150 ms
  later first preview; the startup `run_until_idle` wait usually still
  absorbs it.
* **Relaxing the #456 abandoned-partial clear** (e.g. an "incomplete"
  flag with revisit semantics) — the snapshot makes it unnecessary.
* **CLI flag for the knob** — recipes pass `preview_debounce` via
  `BrowserConfig`; a flag can come later if wanted.

## Testing

* **Debounce (headless):** counting `get_preview` stub — a burst of
  cursor moves within the window yields one fetch for the final id;
  `preview_debounce=0` fetches immediately; a request landing during the
  sleep restarts cleanly (final id wins). `run_until_idle` returns once
  the debounced fetch delivers.
* **Stale-hold (UI/pty + headless render):** move from a painted item to
  an uncached one — pane keeps the old content until delivery, then
  swaps; delivered `''` blanks; streaming first chunk swaps; abandoned
  partial + revisit still refetches fresh (#456 regression guard);
  resize while holding re-wraps the snapshot; empty visible list blanks.
* **Indicator:** label is `⧗ Preview` during debounce + fetch and while
  a stream pulls; drops to `Preview` on delivery, on exhaustion, and
  while paused at the cap; reappears on demand-resume.
* **Suite impact:** the 0.15 s default adds latency wherever existing
  tests await a preview delivery. Expectation: most waits use the 2 s
  `run_until_idle` default and just get slower; any test that becomes
  timing-sensitive sets `preview_debounce=0` explicitly. Verified by a
  full suite run during implementation.
