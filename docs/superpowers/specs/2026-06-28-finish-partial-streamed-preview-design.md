# `v`/`e` on a parked streaming preview: finish vs. re-fetch

**Date:** 2026-06-28
**Status:** Accepted — implementing **Approach B**

Approaches specified — **A** (finish the parked generator), **B** (re-fetch
a fresh preview, leave the parked one alone — the simpler current
baseline), and a **B+** variant (re-fetch, then drop the parked stream and
overwrite the cache). See *Comparison* for the trade-off.

## Decision

**Approach B** — re-fetch a fresh preview and leave the parked stream
alone — chosen 2026-06-28 **for simplicity and lowest risk** (no
cross-thread coordination, no streaming-core changes). Every current
recipe's `get_preview` is replayable (file / git / process reads), so B's
replay-dependence does not bite today. **A** remains the documented
upgrade path if a non-replayable streaming source is ever added; **B+** is
not pursued (it pays cross-thread cost without A's payoff).

## Problem

Pressing `v` (view in `$PAGER`) / `e` (edit in `$EDITOR`) on a row whose
preview is a streaming (generator) `get_preview` pages a **truncated**
transcript. The framework's preview worker drains a generator only up to
a screen-derived cap (`STREAM_CAP_FACTOR=3` screens / `MIN_CAP_LINES=50`)
and then *pauses* it (`_stream_preview_from_generator`), leaving a partial
prefix in `Item.preview`. `_run_external_on_preview` (the shared body of
`v`/`e` in `070-actions.py`) read that prefix as-is. The reported repro:
the top-level `('session', jsonl)` scope-root in `browse-claude`, whose
preview is the `_preview_umbrella` generator.

A first fix (already on the `worktree-finish-partial-preview` baseline)
made `v`/`e` detect the partial cache via a new
`Browser.preview_cache_is_partial(item_id)` predicate and, when partial,
**re-run `get_preview` and drain a fresh generator to its end**.

That works for `browse-claude` but is the wrong long-term shape:

* **Incorrect for non-replayable streaming sources.** A generator backed
  by a one-shot subprocess, a consumed iterator, or a network socket
  cannot be re-run to reproduce the same content. Re-fetching either
  fails, blocks, or yields different output. The bytes already streamed
  into the parked generator are the *only* authoritative continuation.
* **Discards streamed work + re-runs side effects.** A fresh generator
  re-renders everything from scratch and re-runs the recipe's streaming
  side effects (`browse-claude`'s `_preview_umbrella` eager-pushes
  children and caches leaf bodies via `update_data`).
* **Leaves the pane cache partial.** The live pane still holds the
  truncated prefix, so scrolling the pane after viewing re-streams.

## Approach A: finish the parked stream

When the cursor's preview is an **unfinished parked stream**, *finish that
same generator* and page the now-complete `Item.preview`, rather than
starting a parallel one. The framework already has every primitive
needed — this wires them together behind one method.

`v`/`e` resolve the complete preview through a single new entry point,
`Browser.materialize_full_preview(item_id)`, which has three cases:

| Cache state                              | Action                                            |
|------------------------------------------|---------------------------------------------------|
| Complete (string / exhausted gen / push) | return `Item.preview` as-is                       |
| **Paused partial stream**                | **drive the parked generator to completion**, return the now-complete `Item.preview` |
| Absent (pane hidden, nothing streamed)   | one-shot `get_preview` + drain (nothing in progress to finish) |

Only the *absent* case still "requests another one", and only because
there is no in-progress stream to continue — it is the first and only
consumption of that source, so it is correct even for a non-replayable
generator.

## Driving the parked generator to completion

The parked generator is owned by the **worker thread**, blocked in the
in-pause wait loop of `_stream_preview_from_generator`. Two existing
mechanisms get it to the end:

* `signal_preview_demand(item_id)` (#274) sets `_preview_resume_pull` and
  wakes the worker, which un-parks and resumes `next(gen)`.
* `_preview_at_tail` (#457 Shift-End tail-pin): when set, the cap check
  advances the cap window *inline* and keeps pulling instead of pausing.

So: set `_preview_at_tail`, signal demand once, and the worker drains the
rest of the generator in a single pass without re-parking. Chunks land on
`Item.preview` through the normal `append_preview` → post-queue path, so
both `Item.preview` and the incremental `preview_render` cache stay
coherent — the pane is left **complete**, not partial.

`materialize_full_preview` runs on the main thread (an explicit keypress),
so it pumps the post queue itself while the worker drains:

```
def materialize_full_preview(self, item_id, timeout=DRAIN_TIMEOUT):
    item   = self._state._items_by_id.get(item_id)
    cached = getattr(item, 'preview', None) if item else None

    # Complete cache — string preview, exhausted generator, push-mode.
    if cached is not None and not self.preview_cache_is_partial(item_id):
        return cached

    # Paused partial stream — finish the SAME parked generator.
    if cached is not None:
        saved_tail = self._preview_at_tail
        self._preview_at_tail = True            # drain without re-pausing
        try:
            self.signal_preview_demand(item_id)  # un-park the worker
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.drain_main_queue()          # apply append ops
                if self._preview_req is None and self._main_queue.empty():
                    break                        # generator exhausted
                if self._preview_req not in (None, item_id):
                    break                        # superseded (defensive)
                time.sleep(0.002)
        finally:
            self._preview_at_tail = saved_tail
        return getattr(item, 'preview', None)

    # No cache + no stream in progress — one-shot fetch + drain.
    if self.get_preview is None:
        return None
    result = self.get_preview(item_id)
    if inspect.isgenerator(result):
        text = ''.join(_coerce_chunk(c) for c in result)
    else:
        text = result
    if text is not None and item is not None:
        item.preview = text                      # cache (no parked gen)
    return text
```

Termination: the worker clears `_preview_req` to `None` on `StopIteration`
(generator exhausted), having already posted every chunk. The pump breaks
on `_preview_req is None` + empty queue. The cursor cannot move during the
pump (main thread blocked), so `_preview_req` stays `== item_id` until
exhaustion; the `not in (None, item_id)` guard is belt-and-braces. The
`timeout` is a generous safety net (default ~30 s) for a wedged/very slow
source — CPU-bound recipe generators finish in milliseconds (validated
below); on timeout we restore `_preview_at_tail` and page what accumulated.

### `_run_external_on_preview` becomes thin

```
if get_preview is None and getattr(item, 'preview', None) is None:
    ctx.flash('No preview available'); return
try:
    text = ctx._browser.materialize_full_preview(item.id)
except Exception as e:
    ctx.error(f'preview: {type(e).__name__}: {e}'); return
if text is None:
    ctx.flash('No preview available'); return
# ... write text to tempfile, run $PAGER/$EDITOR (unchanged) ...
```

The generator-draining + cache-write logic moves out of `070-actions.py`
into `materialize_full_preview` (preview logic belongs on `Browser`);
`inspect` is already imported in `040-state.py`.

## Validation

A scratch experiment drove a real `Browser` + worker with a 300-line
generator (headless cap = 50):

```
paused at        : 50 lines (gen yielded 50 so far)
after finish     : 300 lines in 3 ms
gen total yielded: 300        # single pass — reused the 50, pulled 250 more
COMPLETE         : True        # Item.preview == full content
_preview_paused  : None,  _preview_req: None
```

`300`, not `350` (50 paused + 300 fresh), confirms the *same* generator is
finished rather than re-run.

## Approach B (CHOSEN): re-fetch, leave the parked stream alone

The simpler option, and the current baseline on this branch. When the
cursor's preview is a paused partial stream, **do not touch** the worker's
parked generator or its partial cache. On `v`/`e`, run a fresh
`get_preview(item_id)` synchronously, drain a generator result to its end,
and page that. The drained text is **not** written back to `Item.preview`,
so the live pane keeps its parked generator and resumes normally on
scroll — the existing preview is left entirely alone.

`_run_external_on_preview` resolution (no `Browser` method needed):

* complete cache (`preview_cache_is_partial` is `False`) → page `Item.preview`;
* paused partial **or** absent cache → fresh `get_preview`; drain if it's
  a generator, else use the string; write the string back to the cache
  **only** on a true miss (`Item.preview is None`), never when partial;
* `get_preview` is `None` / returns `None` with no cache → "No preview
  available" (fall back to the partial cache if one exists).

No worker interaction, no streaming-core state, no cross-thread timing: a
self-contained synchronous fetch in the action handler that reads one
predicate (`preview_cache_is_partial`).

### Trade-offs vs A

* **Re-renders from scratch** — discards the streamed prefix (~2% of the
  work for `browse-claude`; negligible) and re-runs the recipe's streaming
  side effects (idempotent for every current recipe).
* **Incorrect for a non-replayable source** — a generator backed by a
  one-shot subprocess / consumed iterator / socket cannot be re-run to
  reproduce its content. Every *current* recipe's `get_preview` is
  replayable (file / git / process reads), so this is a forward-looking
  limitation, not a present bug.
* **Pane cache stays partial** — the pane re-streams on later scroll
  (unchanged pane behavior), unlike A which leaves it complete.
* **No cross-thread coupling** — nothing to wedge, no tail-pin co-opt, no
  pump/timeout. Lowest risk.

### Volume (Approach B)

* `040-state.py`: just `preview_cache_is_partial` (~18 lines; shared with A).
* `070-actions.py`: the re-fetch/drain resolution in
  `_run_external_on_preview` (~30 lines).
* Tests: ~130 lines (paused→full, cold-cache→drain, cache-left-intact,
  plus the existing cache-hit / empty-string / None cases).
* Already implemented on this branch's baseline — net additional work ≈ 0
  beyond polish + the regenerated binary.

### B+ variant: also drop the parked stream and overwrite the cache

B leaves the pane cache partial. **B+** keeps B's synchronous re-fetch but
additionally completes the pane: after draining the fresh preview, it
**drops** the worker's parked generator and **overwrites** `Item.preview`
with the fetched complete text. After `v`/`e` the pane is complete too
(like A) — reached by replacement rather than by finishing the stream.

The drop is mandatory: overwriting `Item.preview` while a generator is
still parked desyncs the pane — a later scroll resumes the parked
generator, which appends *past* the overwritten full text (duplication).
So B+ must tear the parked generator down first.

`_kick_after_invalidate` already performs exactly this abandon (close the
gen, clear `_preview_paused`), but it then re-fetches via
`request_preview` — which B+ must **not** do, since that fresh async
stream would race the overwrite. So B+ needs a small
`_abandon_parked_no_refetch(id)` (the abandon block of
`_kick_after_invalidate`, minus the `request_preview` kick, plus clearing
`_preview_req` so the worker doesn't re-fetch), then `set_preview`:

```
full = drain(get_preview(id))         # synchronous, same as B
self._abandon_parked_no_refetch(id)   # close parked gen, clear request slot
self.set_preview(id, full)            # overwrite cache (drops/rebuilds render)
# page(full)
```

This is cross-thread (it closes the worker's parked generator and clears
the request slot), but it is a *one-shot* teardown reusing a tested
pattern — no completion pump, tail-pin co-opt, or timeout as in A.

Trade-offs:

* **Completes the pane cache** — the one thing it buys over B — but by
  overwrite, so `preview_render` is dropped and rebuilt on the next paint
  (a one-time full re-render), unlike A's incremental cache.
* **Still re-renders from scratch and is replay-dependent**, like B.
* **Worst option for a non-replayable source**: it both re-fetches
  (can't reproduce the content) *and* destroys the parked generator that
  held the only authentic continuation.
* Pays cross-thread cost in the same class as A, yet without A's reuse or
  non-replayable correctness — its only edge over A is the simpler
  one-shot teardown vs. A's drive-pump.

Volume (B+): `040-state.py` + `_abandon_parked_no_refetch` (~18 lines);
`070-actions.py` re-fetch/drain + abandon + `set_preview` (~35); tests B's
~130 + drop / overwrite / no-duplication-on-scroll coverage (~50).
≈ **~60 logic + ~180 test**.

## Comparison

|                                    | A: finish parked        | B: re-fetch, leave alone | B+: re-fetch, drop+overwrite |
|------------------------------------|-------------------------|--------------------------|------------------------------|
| Core logic (new/changed)           | ~100 lines              | ~45 lines (baseline)     | ~60 lines                    |
| Tests                              | ~200 lines              | ~130 lines (baseline)    | ~180 lines                   |
| New `Browser` method               | `materialize_full_preview` | none                  | `_abandon_parked_no_refetch` |
| Touches streaming core             | yes (pump/tail-pin/demand) | no                    | yes (one-shot abandon)       |
| Complexity / risk                  | medium                  | low                      | medium-low                   |
| Reuses streamed work (single pass) | yes                     | no (re-renders)          | no (re-renders)              |
| Correct for non-replayable source  | yes                     | no                       | no (worst — destroys partial)|
| Pane cache after `v`/`e`           | complete (incremental)  | partial                  | complete (forces re-render)  |
| Re-runs recipe side effects        | no                      | yes (idempotent today)   | yes (idempotent today)       |

**Recommendation:** **A** is the architecturally-correct shape — the
parked bytes are the only authoritative continuation for a non-replayable
source, it reuses the streamed work, and it leaves the pane complete with
the render cache intact. **B** is the simplest and correct for every recipe
that exists today, accepting a partial pane that re-streams on scroll.
**B+** completes the pane like A but by re-fetch-and-replace; note it pays
cross-thread cost in A's class while keeping B's re-render and
replay-dependence — so if the cross-thread coordination is being paid for
anyway, A returns more for it. Pick **B** for minimal risk, **A** for
correctness-by-construction, and **B+** only if a complete pane is wanted
*and* the drive-pump in A is specifically the thing to avoid.

**→ Decided: B** (2026-06-28), for simplicity / lowest risk — see the
*Decision* section at the top.

## Out of scope / deferred

* **In-flight (mid-pull, not-yet-paused) window.** `preview_cache_is_partial`
  flags only the *paused* state — the steady state the cursor lands on.
  The sub-millisecond window where the worker is actively pulling but has
  not hit the first cap is not detected (a cache-present + in-flight read
  would page the partial). Same deferred limitation as the baseline;
  catching it needs cross-thread in-flight tracking that risks false
  positives on settled string previews. No regression — no worse than the
  pre-fix cache read.
* **Resuming across a real cursor move.** Not applicable: `v`/`e` block
  the main thread, so no cursor move races the drive.
* **Dedicated drain flag.** A new `_preview_force_drain` honored at the
  cap check was considered and rejected in favor of reusing the existing
  `_preview_at_tail` (save/restore inside the method) — fewer moving
  parts, no new streaming-loop state.

## Testing

(`test/unit/test_actions.py`, `TestViewEditDefaults`)

* `test_v_pages_full_preview_when_stream_paused` /
  `test_e_edits_full_preview_when_stream_paused` — `v`/`e` on a paused
  stream page the full content via a fresh fetch.
* `test_paused_partial_cache_is_left_intact_for_the_pane` — the
  leave-alone invariant: `v`/`e` must NOT mutate the worker's partial
  cache, so the parked generator keeps resuming on scroll.
* `test_v_drains_generator_on_cold_cache` — an absent-cache generator
  result is drained to a string (also covers the latent
  generator-`.encode()` crash).
* `test_cache_hit_skips_get_preview` (pre-existing) — a complete cache is
  used with no `get_preview` re-run.
* Full `./run-tests-parallel.sh` (touches the ctx/preview path; UI tests
  spawn the real binary). Rebuild `browse-tui` via `./build-tui.sh`.
* End-to-end pty check against the real `browse-claude` + the reported
  session: `g` then `v` pages the full transcript; the pane cache stays
  partial (left alone) and re-streams on later scroll.

## Files touched

* `src-tui/040-state.py` — new `preview_cache_is_partial` predicate.
* `src-tui/070-actions.py` — `_run_external_on_preview` re-fetch/drain
  resolution (`import inspect`; complete cache used as-is; paused-partial
  / absent re-fetched and a generator drained to its end; a drained or
  partial value is never written back; a non-generator string is cached
  only on a true miss).
* `test/unit/test_actions.py` — the `TestViewEditDefaults` coverage above.
* `browse-tui` — regenerated artifact.
