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

Follow-up gap (#954): moves onto rows with a *cached* preview had the
inverse problem — the #442 cached-skip in `_update_preview_for_cursor`
bypassed the worker entirely, so the pane swapped content immediately
on every move. Rapid scrolling over already-visited rows still
churned, with full panes instead of blanks. Fixed by routing every
cursor move through the worker (cached or not) and making delivery —
or its cache-hit equivalent, a settle nudge — the single swap signal.

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
* **D. Children pane settles with the preview** (#959 follow-up) — the
  children pane renders a displayed parent id that lags the cursor
  until the preview settles, so the grid stops reshaping mid-scroll
  and both panes always describe the same row.

Together: a held `j` shows the original preview *and* the original
children grid steadily with `⧗` in the divider; after the cursor
settles, one fetch fires and both panes swap in one paint. Cached rows
behave identically (#954) — the only difference is that the settled
row is served from cache (no `get_preview` re-run) instead of fetched.

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
   * slot unchanged → fall through to serve the settled id (step 3).
3. New (#954): read the settled item's `Item.preview` off-thread.
   * Cached (non-`None`) → post a `_settle_cached_preview(id)`
     closure and `continue` — `get_preview` is never re-run for a
     cache hit (the #442 cached-revisit promise). `local_id` stays
     adopted, same memo semantics as a delivery. The nudge, on the
     main thread: drain `_preview_req` if it still equals the id
     (exactly as `_deliver_preview` does), and when the id is still
     cursored set the per-visit delivery bit (§B) + flag the preview
     redraw; the post wakes the loop. The off-thread cache read races
     main-thread mutation the same way the worker's `_preview_req`
     reads do — a misread either nudges a just-cleared cache
     (recovered by the #442 same-cursor re-fire next tick) or
     refetches a just-filled one (harmless). No locking.
   * Uncached → the existing `_abandon_paused_preview_if_any` +
     `get_preview` call.

Feeding the worker (#954): `_update_preview_for_cursor` lost its
cached-skip early return — a cursor change to a normal item *always*
fires `request_preview`, so the worker is the single settle authority
for cached and uncached rows alike. The new-id-is-`None` path and the
#442 same-cursor re-fire are unchanged.

Everything else is untouched. Notably:

* `request_preview` stays as-is (slot + event set). No timestamps, no
  timers, no main-loop changes.
* A move-away-and-back during one sleep window ends with the slot equal
  to the adopted id — the worker proceeds to serve it (fetch or nudge).
  Correct, and the reason no per-request stamp is needed.
* Abandoning a paused streaming generator is *not* delayed: the pause
  loop watches `_preview_req` itself and self-abandons on the cursor-move
  wake (`_preview_resume_event`), before the worker's outer loop (and
  its debounce) is even reached.
* `run_until_idle` already treats "slot set, not paused" as busy, so a
  debouncing worker counts as busy and tests wait naturally.
* Shutdown: the sleep is well under `stop_workers`' join timeout; the
  worker re-checks `_stop` after sleeping.

Scope of the delay (accepted, for simplicity): *every* request is
debounced — including the startup fetch, the #442 same-id re-fire
after `invalidate_preview` / `drop_preview_cache` (the cache is `None`
at kick time, so the worker takes the fetch path), and the cache-hit
nudge (#954, where the delay *is* the settle window). Stale-hold (B)
masks all of them.

### Config

`BrowserConfig.preview_debounce: float = 0.2` — seconds of cursor
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

### Per-visit delivery bit (#954)

`Browser._preview_visit_delivered` — one bool next to
`_preview_cursor_id`. Cleared when `_update_preview_for_cursor`
observes a cursor change; set — only when the delivered id is still
cursored — by `_deliver_preview`, the `set_preview`/`append_preview`
apply paths in `update_data` (where they flag the preview redraw), and
the worker's `_settle_cached_preview` nudge. It distinguishes "content
is in hand" from "the cursor has settled on this content": a cached
row's preview is non-`None` from the first tick of the visit, but the
pane must not swap to it until the worker says the cursor stopped.
(Starts `True` — no visit window exists until the cursor helper
observes one, so render paths that never run the helper paint from
cache unheld.)

### Paint rule

In `render_preview`, when not in help mode and the cursor row pends,
paint the snapshot instead:

* **Hold (paint snapshot)** when the cursor entry is a pending
  placeholder row, or resolves to a normal item with `preview is None`
  **or** no delivery for this visit (`not _preview_visit_delivered`) —
  and a snapshot exists. Re-wrap from the snapshot's raw text if the
  pane geometry or ANSI policy changed (resize, screen-restore);
  otherwise reuse its wrapped rows.
* **Blank (current behavior)** when the visible list is empty, no
  snapshot exists yet (startup), or the item's preview is a delivered
  value — including `''` — for a settled visit.

The two hold legs cover distinct waits. `preview is None` keeps its
meaning from the original §B: a delivery is pending or imminent —
which also preserves the invalidate-on-the-cursored-item hold. The
bit adds the cached-row settle window (#954). The streaming-vs-cached
distinction falls out of who sets the bit: a streaming first chunk
lands through the `append_preview` apply path and sets it, so
progressive streaming still swaps in as soon as content exists; a
cached row's bit flips only on the settle nudge, so the hold lasts
the debounce. Meta-row handling is unchanged from the original §B:
meta rows never pend (no request is ever made for them, so nothing
would end the hold).

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

Cached moves blink it too (#954, no extra wiring): every cursor move
now sets the slot, so the glyph shows for the length of the settle
(~one debounce) and the nudge drains the slot — the same ON/OFF
mechanics as a fetch, minus the fetch.

**Repaint wiring:** the label lives in two places — the preview header
row (`'h'` layout, painted under the `'preview'` redraw key) and the
standalone bottom info bar (other layouts, `'info'` key). Rather than
chasing every transition site, the main loop memoizes the predicate
value once per tick (after `_update_preview_for_cursor`) and flags
`{'preview', 'info'}` when it flips. All transitions already wake the
loop: cursor moves happen in-loop, deliveries post + wake, and the
streaming pause/exhaustion paths call `notify_wake()`. The row cache
no-ops whichever of the two paints didn't actually change.

## D. Children pane settles with the preview (#959)

With A–C in place the children pane became the remaining churn: its
subject was the live cursor, so a scroll burst reshaped the grid (and
the whole layout — the pane's height derives from its content) on
every row, and after the burst the pane described the new row while
the preview still held the old one. The invariant this section adds:
the children pane always describes the same row the preview pane does
— old/old before settle, new/new (or new + loading hint) in the
settle paint, never new-preview/old-children.

### Displayed-id lag

`Browser._children_displayed_id` — the parent id the children pane
currently renders. The render layer (`_layout_for`,
`render_children_grid`, `render_children_list`, and the mouse
dispatcher's layout mirror) resolves the pane's subject from it via
`_items_by_id` instead of from the cursor. The children REQUEST
pipeline (prefetch slot, FIFO, worker, `_update_children_for_cursor`)
is untouched and keeps following the live cursor — only DISPLAY lags,
so fetches still start at move time and are usually cached by settle.

A displayed parent that a data update has removed resolves to `None`
and the pane hides — honest (the branch is gone), and the next settle
re-advances.

### Advance rule

The main loop advances the id once per tick (next to the §C label
memo; inert when the pane is disabled), resolving the visible cursor
entry:

* **normal row** — advance only when `_preview_visit_delivered` is
  set (§B): the preview's settle signal is the pane's swap signal,
  which is what makes the swap joint. With `show_preview` off the bit
  keeps its `True` init (the preview helper returns before ever
  clearing it), so the rule degrades to advance-every-tick — the
  pre-#959 immediate pane. Verified, not special-cased.
* **meta row** — advance immediately. Mirrors the §B meta exemption:
  no preview is ever requested for meta rows, so no delivery would end
  a settle gate, and the preview paints meta rows honestly right away
  — the pane must match rather than keep describing the previous
  branch. Navigation skips meta, so this can't fire mid-scroll.
* **pending placeholder / no cursor row** — hold. A placeholder holds
  both panes (the preview keeps its snapshot, §B); an empty visible
  list blanks the preview while the children pane holds — deliberate:
  hold-on-no-cursor is the contract.

An actual id change flags `'all'`: the pane's geometry derives from
the displayed item, so an id change can reshape the layout — the same
full repaint a children delivery flags today. Cost is once per
settle, not per move.

### What falls out (no special cases)

* Children cached by settle time (the usual case — the fetch started
  at move time) → the settle paint swaps preview and pane together.
* Children still in flight at settle → the same paint shows the new
  preview and the existing cached-is-`None` rendering for the NEW
  branch: the one-row `⧗ loading…` hint, which the delivery's
  existing `'all'`-flag + wake then grows into the grid. The hint can
  only ever appear for the settled row — never for rows the cursor
  merely skimmed.
* Leaf at settle → the pane hides in the settle paint, not mid-scroll.
* `clear_children` on the settled row → the hint reappears (id
  unchanged, cache `None`) and regrows on the re-delivery; the
  cursor-based re-fire in `_update_children_for_cursor` covers the
  refetch.

## Out of scope / rejected

* **Adaptive debounce** (immediate fetch on the first move after quiet,
  debounce only under rapid movement) — rejected in design discussion in
  favor of the fixed sleep; revisit only if the fixed delay feels bad.
* **Dimming/styling the held-over content** — the `⧗` label prefix is
  the only staleness cue; keep the content itself plain.
* **Exempting the startup fetch from the debounce** — accepted ~200 ms
  later first preview; the startup `run_until_idle` wait usually still
  absorbs it.
* **Relaxing the #456 abandoned-partial clear** (e.g. an "incomplete"
  flag with revisit semantics) — the snapshot makes it unnecessary.
* **CLI flag for the knob** — recipes pass `preview_debounce` via
  `BrowserConfig`; a flag can come later if wanted.
* **Drop-cache-and-refetch for cached moves** (#954) — clearing
  `item.preview` on every cursor change would have routed cached rows
  through the existing pending machinery in ~4 lines, but it disables
  the preview cache for navigation: every revisit re-runs
  `get_preview`, expensive for browse-claude-class recipes. Rejected
  in favor of the settle nudge, which keeps the #442 promise.
* **Main-loop settle timer for cached moves** (#954) — swapping on a
  main-thread timeout instead of a worker nudge fails structurally:
  `read_key` has no timeout, so an idle loop would never wake to end
  the settle without new wake machinery. The worker already owns the
  settle wait.
* **Children fetched by the preview worker, delivered as one batch**
  (#959) — rejected: serializes the two fetches behind one thread,
  breaks `get_children`'s single-thread contract (it would now run on
  the preview worker too), and needs cross-worker dedup/waiting
  against the children pipeline. The displayed-id lag gets the joint
  swap from rendering alone.
* **Joint release that also delays the preview on slow children**
  (#959) — holding the preview hostage to the slower fetch punishes
  the common case; superseded by the hint-at-settle rule (§D), which
  swaps the preview on time and is honest about the children still
  loading.

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
* **Cached settle (#954, headless + render):** a move onto a cached row
  holds the old content through the settle, then swaps with zero
  `get_preview` calls (counted); a burst over cached rows nudges only
  the settled row; `run_until_idle` returns once the nudge drains the
  slot; the glyph shows during a cached settle and clears after.
* **Indicator:** label is `⧗ Preview` during debounce + fetch and while
  a stream pulls; drops to `Preview` on delivery, on exhaustion, and
  while paused at the cap; reappears on demand-resume.
* **Children settle (#959, headless render + UI):** the displayed id
  holds through an open visit and advances (flagging `'all'`, once)
  when the delivery bit flips; both delivery orderings (cached by
  settle → one paint swaps both panes; still fetching → settle paint
  shows hint for the new branch, grows on delivery); leaf-hide
  deferred to settle; placeholder/empty hold; meta advances
  immediately; `show_preview=False` degrades to per-move advance;
  removed displayed parent hides the pane; `clear_children` on the
  settled row re-hints and regrows.
* **Suite impact:** the 0.2 s default adds latency wherever existing
  tests await a preview delivery. Expectation: most waits use the 2 s
  `run_until_idle` default and just get slower; any test that becomes
  timing-sensitive sets `preview_debounce=0` explicitly. Verified by a
  full suite run during implementation.
