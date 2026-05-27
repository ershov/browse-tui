# Children prefetch slot: latest-wins single-flight for cursor-driven children fetches

Date: 2026-05-27

## Problem

`_update_children_for_cursor` (`src-tui/040-state.py:6031`) runs at the top of every
main-loop iteration and appends the cursor item's id to `_children_queue` for any
cursor row whose children aren't yet cached. The children worker is strict FIFO: it
drains the queue serially without coalescing.

Under rapid cursor movement the queue accumulates one entry per cursor position
visited. In a live `browse-git` trace, 15 rapid `j` keys produced **32
`get_children` calls** vs. **3 `get_preview` calls** for the same navigation — the
preview path is latest-wins, the children path is not. The user sees the children
pane lag behind the cursor by hundreds of milliseconds: every commit the cursor
passed over has to fetch in order before the cursor's current commit appears.

The preview worker already solves this with a single-slot, latest-wins design
(`_preview_req` at `040-state.py:2560`; worker loop at `040-state.py:4752`). The
children path needs the same throttle for its cursor-driven enqueues, while
preserving FIFO semantics for paths that legitimately need it: initial fetch,
explicit refresh, expand, and recipe-driven `refresh()`/`expand()`.

## Goals

* Cursor-driven children prefetch becomes **latest-wins**: rapid cursor movement
  results in at most one fetch per "settled" cursor stop, not one per cursor
  position visited.
* **Single-flight** invariant: at most one `get_children` call runs at any time
  across both paths. Preserves the existing single-worker thread.
* **Cursor pane responsiveness during scroll**: when the cursor moves, fetching the
  new cursor's children takes priority over draining FIFO entries. Once the cursor
  settles, FIFO drains.
* **No new concurrency primitives** in the framework; no new burden on recipes
  (no per-id locks like `bacc176`'s browse-claude fix).

## Non-goals

* FIFO ordering policy is unchanged. LIFO, cursor-aware ordering, etc. are deferred
  (see "Future work"). The slot eliminates the dominant source of FIFO bloat during
  scroll; whether the residual FIFO ordering matters is a question we can answer
  empirically once the slot ships.
* No separate worker thread for prefetch. The same thread handles both slot and
  FIFO, in that priority order.
* No change to `_children_queue` semantics for explicit paths (`_dispatch_children`,
  `_do_initial_fetch`, `_dispatch_children_for_pending`).
* No change to the public `refresh()` / `expand()` API or the children-pane
  renderer.
* No change to `get_children` callback semantics. Recipes do **not** need to be
  idempotent.

## Design

### Data model (additions to `Browser.__init__`)

Two new attributes on `Browser`, mirroring `_preview_req` and the preview worker's
private `local_id` memo:

```python
# Latest-wins single slot for cursor-driven children prefetch.
# Mirrors ``_preview_req``: main thread writes the cursor's id, worker
# reads and decides whether to fetch. Worker skips when slot value
# matches ``_children_prefetch_local_id`` (already handled), when the
# id is in ``_children`` (cached), or in ``_children_pending`` (FIFO
# owns this fetch). ``None`` means "no cursor item wants children."
self._children_prefetch_req = None

# Worker's "what I just fetched or decided to skip" memo. Exposed as
# an attribute so ``run_until_idle`` can observe quiescence without
# touching worker internals. Single-attribute reads/writes are
# GIL-safe; no lock needed.
self._children_prefetch_local_id = None
```

Wake signalling re-uses the existing `self._children_event` — no new event.

### `_update_children_for_cursor` rewrite (`040-state.py`)

Main-thread side stays small. The slot always reflects the cursor's intent;
the worker owns the decisions about cache hits and FIFO promotion (see Worker
Pass 1 below).

```python
def _update_children_for_cursor(self):
    if not self.show_children_pane:
        return
    state = self._state
    vis = visible_items(state)
    new_id = None
    if 0 <= state.cursor < len(vis):
        entry = vis[state.cursor]
        if entry.kind == 'normal' and entry.item.has_children:
            new_id = entry.item.id
    if new_id is None:
        return
    if new_id in state._children:
        return  # already cached
    if self._children_prefetch_req == new_id:
        # Same cursor — re-fire wake so a cache-clear-in-place
        # (e.g. ``clear_children`` while the cursor stayed put)
        # gets refetched. Mirrors the preview "stuck blank" pattern.
        self._children_prefetch_local_id = None
        self._children_event.set()
        return
    self._children_prefetch_req = new_id
    self._children_event.set()
```

Notes:
* `new_id == None` is a valid no-op state — the worker treats `None` as no-work.
* The "already in pending" gate is intentionally absent: the worker's Pass 1
  promotion path (below) inspects pending + FIFO and either pulls the FIFO
  entry over (with its `reload` flag) or no-ops when the work is already in
  flight from a previous slot iteration.

### Worker loop restructure (`_children_worker`)

Today's structure drains the FIFO completely on each wake. The new structure runs
two passes per outer iteration:

```python
def _children_worker(self):
    while not self._stop:
        did_work = False

        # Pass 1: drain the cursor-prefetch slot until quiescent.
        # Slot-first means continuous scroll preempts FIFO — once the
        # cursor settles (slot value matches local_id), Pass 2 runs.
        while not self._stop:
            slot_id = self._children_prefetch_req
            if (slot_id is None
                    or slot_id == self._children_prefetch_local_id):
                break
            self._children_prefetch_local_id = slot_id
            # Promotion: when slot_id is pending, an explicit FIFO
            # entry probably has the work — take it (with its reload
            # flag) and fetch via the slot path. If pending but NOT
            # in FIFO, the work is in flight from a previous slot
            # iteration's helper call; no-op (local_id catches the
            # loop on next read).
            reload_ = False
            if slot_id in self._state._children_pending:
                with self._children_queue_lock:
                    target = None
                    for q_entry in list(self._children_queue):
                        if q_entry[0] == slot_id:
                            target = q_entry
                            break
                    if target is not None:
                        try:
                            self._children_queue.remove(target)
                            reload_ = target[1]
                        except ValueError:
                            target = None
                if target is None:
                    continue
            else:
                # Fresh: commit by marking pending so a concurrent
                # ``_dispatch_children`` for the same id short-circuits.
                self._state._children_pending.add(slot_id)
            self._fetch_and_deliver_children(slot_id, reload_)
            did_work = True

        # Pass 2: drain one FIFO entry under the lock so it doesn't
        # race the Pass 1 promotion scan.
        popped = None
        if not self._stop:
            with self._children_queue_lock:
                if self._children_queue:
                    popped = self._children_queue.popleft()
        if popped is not None:
            id_, reload_ = popped
            self._fetch_and_deliver_children(id_, reload_)
            did_work = True

        if did_work:
            continue

        # No work — sleep, with the standard clear-then-check-then-wait
        # pattern so a wake that arrives between checks isn't lost.
        self._children_event.clear()
        slot_id = self._children_prefetch_req
        if ((slot_id is None
                or slot_id == self._children_prefetch_local_id)
                and not self._children_queue
                and not self._stop):
            self._children_event.wait()
            # On wake from idle, reset local_id so a re-request of the
            # same id (e.g. cache cleared in place) refetches.
            self._children_prefetch_local_id = None
```

`_fetch_and_deliver_children(id_, reload_)` is the extracted body of the existing
FIFO loop: calls `get_children`, handles generator vs. iterable returns, catches
exceptions, posts the `update_data` batch and `_post_children_delivery`. It also
shorts on `not reload_ and id_ in state._children` (cache hit + no-reload),
posting only the housekeeping. Both Pass 1 and Pass 2 route through this helper;
the cache check lives there exclusively (Pass 1 does NOT pre-gate cache).

A `threading.Lock` (`_children_queue_lock`) guards the multi-step FIFO ops:
the Pass 1 promotion (scan + `remove`) and the Pass 2 `popleft`. Single-step
`.append()` from other dispatch paths stays unlocked — CPython deque ops are
GIL-atomic at the single-call level; only multi-call patterns need explicit
serialisation against concurrent popleft.

### Dedup interplay

Slot and FIFO share `_children_pending` as the "in-flight committed" gate:

1. **Fresh slot fetch** (id not in pending): worker adds to pending before
   calling the helper. Any concurrent `_dispatch_children` for the same id
   short-circuits via its existing pending check.
2. **Promoted slot fetch** (id already in pending — FIFO has it): worker
   removes the FIFO entry under `_children_queue_lock` and fetches with the
   FIFO entry's `reload` flag. Pending stays set; the helper's delivery
   discards.
3. **In-flight elsewhere** (id in pending, no FIFO entry): worker's previous
   slot iteration already kicked the helper and the delivery is mid-flight on
   the post queue. No-op; `local_id == slot_id` short-circuits the loop on
   the next read.

`_post_children_delivery` on the main thread is the single discard point;
both Pass 1 and Pass 2 deliveries route through it. Pending objects registered
in `_children_in_flight` resolve naturally regardless of which path drove the
fetch.

### Lifecycle

* **`start_workers` / `stop_workers`**: no change. The single children thread runs
  both slot and FIFO.
* **`run_until_idle`** (`040-state.py:4227`): the idle predicate gains a
  children-prefetch clause:

  ```python
  children_prefetch_busy = (
      self._children_prefetch_req is not None
      and self._children_prefetch_req != self._children_prefetch_local_id
  )
  ```

  Added to the existing idle conjunction alongside `_children_queue`,
  `_children_pending`, preview busy, etc.

* **`__repr__`** debug dump: include `_children_prefetch_req` and
  `_children_prefetch_local_id`.

### Edge cases

* **Cursor on item with `has_children=False`**: `new_id = None`; slot stores
  `None`; worker treats as no-work. Equivalent to today's early-return.

* **Cache invalidation while cursor stays on the same id**: handled by the
  idempotent re-fire path in `_update_children_for_cursor` (resets
  `_children_prefetch_local_id` and sets the event). Worker wakes / re-evaluates
  and refetches because slot != local_id.

* **Bulk expand (`alt-right`)**: `_expand_recursive` calls `ctx.expand` for each
  uncached subtree; those go through `_dispatch_children` → FIFO. Slot still
  holds the cursor row's id. Worker fetches the cursor row's children first
  (Pass 1), then drains FIFO one entry per iteration (Pass 2). Top-down loading
  order is preserved because the DFS push order in `_expand_subtree` is
  unchanged.

* **Continuous scroll with non-empty FIFO**: slot-first means FIFO drains only
  between cursor stops. This is *deferred*, not *lost* — once the slot's value
  stabilises (`slot_id == prefetch_local_id`), the slot loop exits and Pass 2
  takes one FIFO entry per outer iteration until the queue drains. Document this
  in the worker docstring so future readers don't mistake it for starvation.

* **`set_children` / direct cache writes**: unchanged. They bypass the worker and
  write `state._children` directly. The slot worker sees the cache populated and
  skips.

* **Headless tests**: no cursor moves from a user. Tests drive the slot directly
  by writing `state.cursor`, then calling `b._update_children_for_cursor()`, then
  `b.run_until_idle()`. Same shape as existing preview tests.

## Testing

Add to `test/async_/test_workers.py`:

1. **`test_rapid_cursor_moves_coalesce_children_prefetch`** — analogue of the
   existing `test_rapid_requests_keep_max_concurrency_at_one`. Drive 20 cursor
   moves through `state.cursor = N; b._update_children_for_cursor()` with a slow
   `get_children`. Assert total calls ≪ 20, last cursor's id is cached, max
   in-flight == 1.

2. **`test_children_prefetch_max_in_flight_is_one`** — hard single-flight
   invariant across mixed slot + FIFO sources.

3. **`test_slot_first_priority_during_scroll`** — fill FIFO with N entries (via
   repeated `b.refresh(id_)`) then drive 20 cursor moves. Assert at least one
   cursor-row delivery lands before all FIFO entries finish.

4. **`test_fifo_drains_after_cursor_settles`** — same fixture, then stop driving
   cursor moves. Assert FIFO eventually drains.

5. **`test_cache_invalidation_re_kicks_slot`** — cursor on X, X cached, then
   `clear_children(X)` via update_data. Assert worker refetches X.

6. **`test_concurrent_explicit_and_prefetch_no_duplicate_fetch`** — slot has X,
   FIFO gets X via explicit `refresh(X)`. Assert `get_children(X)` runs exactly
   once.

UI test: add a rapid-scroll fixture to `test/ui/test_recipe_browse_git.py` that
sends N `j` keys with tight pacing, captures the children pane, and asserts the
final cursor's files-changed list renders within a fixed budget (independent of
N). Today this scales with N; after the slot, it should be constant.

## Future work

* **FIFO ordering policy.** Once the slot is in production, evaluate whether
  residual FIFO entries cause user-visible lag during bulk expand or recipe-driven
  explicit work. If so, revisit LIFO + reverse-push or cursor-aware ordering as a
  follow-up design.
* **Children-prefetch streaming** (analogue to streaming preview generators).
  Out of scope for this change.
