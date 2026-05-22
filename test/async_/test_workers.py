"""Worker-thread tests: lifecycle, FIFO order, latest-wins, error handling.

The children worker is a strict FIFO queue: any ids submitted via
``refresh()`` get fetched in submission order. The preview worker
serves the latest ``_preview_req`` with single-flight semantics --
at most one ``get_preview`` runs at a time -- and delivers results
through the FIFO post queue (#442). A re-request of the same id
while a fetch is in flight is dropped (the worker dedups by
``local_id``).

Both workers must catch user-callback exceptions: the test process must
not die because ``get_children`` raised, and pending callers must still
see their Pending resolve so .then() chains don't strand.
"""

import io
import sys
import threading
import time
import unittest

from test.async_._helpers import (
    Browser, BrowserConfig, Item, Pending, make_browser, get_preview_text,
)


class TestWorkerLifecycle(unittest.TestCase):
    """start_workers / stop_workers spawn and join the threads cleanly."""

    def test_start_workers_creates_live_threads(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertIsNone(b._children_thread)
        self.assertIsNone(b._preview_thread)
        b.start_workers()
        try:
            self.assertTrue(b._children_thread.is_alive())
            self.assertTrue(b._preview_thread.is_alive())
            self.assertTrue(b._workers_running)
        finally:
            b.stop_workers()

    def test_stop_workers_joins_threads(self):
        b = make_browser()
        ct = b._children_thread
        pt = b._preview_thread
        b.stop_workers()
        # Both threads should have observed _stop and exited.
        self.assertFalse(ct.is_alive())
        self.assertFalse(pt.is_alive())
        self.assertFalse(b._workers_running)

    def test_stop_workers_joins_promptly(self):
        # No outstanding work -> stop should be near-instant.
        b = make_browser()
        t0 = time.monotonic()
        b.stop_workers(timeout=1.0)
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_start_is_idempotent(self):
        b = make_browser()
        try:
            ct = b._children_thread
            pt = b._preview_thread
            b.start_workers()  # second call is a no-op
            self.assertIs(b._children_thread, ct)
            self.assertIs(b._preview_thread, pt)
        finally:
            b.stop_workers()


class TestChildrenWorkerFifo(unittest.TestCase):
    """Children worker drains _children_queue in submission order."""

    def test_refresh_fetches_and_resolves(self):
        calls = []
        def get_children(id_, *, reload=False):
            calls.append(id_)
            return [(f'{id_}/a',), (f'{id_}/b',)]
        b = make_browser(get_children=get_children)
        try:
            p = b.refresh('A')
            self.assertIsInstance(p, Pending)
            b.run_until_idle()
            self.assertTrue(p.done)
            self.assertEqual(calls, ['A'])
            self.assertIn('A', b._state._children)
            ids = [it.id for it in b._state._children['A']]
            self.assertEqual(ids, ['A/a', 'A/b'])
        finally:
            b.stop_workers()

    def test_multiple_refreshes_fifo(self):
        order = []
        # Block per-id so we can confirm ordering even on a fast machine:
        # each fetch records the id then sleeps a touch -- a LIFO worker
        # would interleave or reverse the writes.
        def get_children(id_, *, reload=False):
            order.append(id_)
            time.sleep(0.005)
            return []
        b = make_browser(get_children=get_children)
        try:
            pa = b.refresh('A')
            pb = b.refresh('B')
            pc = b.refresh('C')
            b.run_until_idle()
            self.assertEqual(order, ['A', 'B', 'C'])
            self.assertTrue(pa.done and pb.done and pc.done)
        finally:
            b.stop_workers()

    def test_two_threads_same_id_both_resolve(self):
        # Phase 3 (ticket #29) coalesces: only one fetch happens, but
        # both Pendings still resolve. Dedicated coalescing tests live
        # in test_coalesce.py; this one keeps the cross-thread "submit
        # from another thread" coverage.
        calls = []
        def get_children(id_, *, reload=False):
            calls.append(id_)
            return []
        b = make_browser(get_children=get_children)
        try:
            results = []
            def submit_from_thread():
                p = b.refresh('A')
                p.then(lambda: results.append('thread'))
            t = threading.Thread(target=submit_from_thread)
            p_main = b.refresh('A')
            p_main.then(lambda: results.append('main'))
            t.start()
            t.join()
            b.run_until_idle()
            # Coalescing — exactly one fetch even though two Pendings
            # asked for it. The race between "main posts then drains"
            # and "thread posts then main drains" can land them in
            # either order, but in either case the second arrives while
            # the first is still pending and joins the in-flight list.
            self.assertEqual(calls, ['A'])
            self.assertTrue(p_main.done)
            self.assertEqual(sorted(results), ['main', 'thread'])
        finally:
            b.stop_workers()

    def test_get_children_error_resolves_pending_with_empty_cache(self):
        def boom(id_, *, reload=False):
            raise RuntimeError('kaboom')
        b = make_browser(get_children=boom)
        try:
            p = b.refresh('A')
            b.run_until_idle()
            self.assertTrue(p.done)
            self.assertEqual(b._state._children['A'], [])
            self.assertIn('kaboom', b.error_text)
            self.assertIn('RuntimeError', b.error_text)
        finally:
            b.stop_workers()


class TestPreviewWorker(unittest.TestCase):
    """Preview worker: single-slot, latest-wins, error-translated."""

    def test_preview_request_fetched(self):
        seen = []
        def get_preview(id_):
            seen.append(id_)
            return f'preview of {id_}'
        b = make_browser(get_preview=get_preview)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            b.request_preview('A')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'A'), 'preview of A')
        finally:
            b.stop_workers()

    def test_preview_latest_wins(self):
        # Block the first fetch so the second can land in the slot before
        # the worker has finished the first. After both complete, the
        # cache must contain at least 'B' (the newer request).
        gate = threading.Event()
        def get_preview(id_):
            if id_ == 'A':
                gate.wait(timeout=1.0)
            return f'preview of {id_}'
        b = make_browser(get_preview=get_preview)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            b._state._items_by_id['B'] = Item(id='B')
            b.request_preview('A')
            time.sleep(0.01)  # let the worker pick up 'A'
            b.request_preview('B')
            gate.set()
            b.run_until_idle()
            # B is the authoritative latest request -- it must be in
            # the cache. A may or may not be (depends on whether the
            # worker delivered its result before B clobbered the slot,
            # which it does in our implementation, but tests assert
            # weakly to stay robust to plausible reorderings).
            self.assertEqual(get_preview_text(b, 'B'), 'preview of B')
        finally:
            b.stop_workers()

    def test_preview_error_translated(self):
        def bad(id_):
            raise ValueError('nope')
        b = make_browser(get_preview=bad)
        try:
            b._state._items_by_id['X'] = Item(id='X')
            b.request_preview('X')
            b.run_until_idle()
            text = (get_preview_text(b, 'X') or '')
            self.assertTrue(text.startswith('[error]'))
            self.assertIn('ValueError', text)
            self.assertIn('nope', text)
        finally:
            b.stop_workers()

    def test_no_get_preview_returns_empty_string(self):
        b = make_browser()  # get_preview is None
        try:
            b._state._items_by_id['X'] = Item(id='X')
            b.request_preview('X')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'X'), '')
        finally:
            b.stop_workers()


# --- #431/#442: single-flight worker invariant --------------------------
#
# The preview worker must process at most one ``get_preview`` fetch at a
# time. ``request_preview`` overwrites the single-slot ``_preview_req``;
# the worker reads the slot, fetches, and POSTS the result to the FIFO
# main-thread queue (#442). Recipes' ``set_preview`` writes also route
# through the post queue, so both sides share the same FIFO lane. The
# single-flight invariant is independent of that delivery channel —
# at most one ``get_preview`` runs at any moment.


class TestPreviewWorkerSingleFlight(unittest.TestCase):
    """At-most-one in-flight ``get_preview`` regardless of request burst."""

    def test_rapid_requests_keep_max_concurrency_at_one(self):
        # 20 rapid ``request_preview`` calls with different ids. The
        # worker runs each fetch end-to-end before pulling the next id
        # off the single-slot request, so concurrency never exceeds 1.
        # Most fetches are superseded before the worker reaches them
        # (the slot is overwritten on every call), so the total number
        # of ``get_preview`` invocations is much less than 20.
        lock = threading.Lock()
        state = {'in_flight': 0, 'max_in_flight': 0, 'total_calls': 0}

        def get_preview(id_):
            with lock:
                state['in_flight'] += 1
                state['total_calls'] += 1
                if state['in_flight'] > state['max_in_flight']:
                    state['max_in_flight'] = state['in_flight']
            # Brief sleep so concurrent calls (if any) would overlap.
            time.sleep(0.01)
            with lock:
                state['in_flight'] -= 1
            return f'preview of {id_}'

        b = make_browser(get_preview=get_preview)
        try:
            ids = [f'id-{n}' for n in range(20)]
            for id_ in ids:
                b._state._items_by_id[id_] = Item(id=id_)
            for id_ in ids:
                b.request_preview(id_)
            b.run_until_idle()
            # Hard invariant: never more than one fetch in flight.
            self.assertEqual(
                state['max_in_flight'], 1,
                'single-flight invariant violated: observed '
                f'{state["max_in_flight"]} concurrent get_preview calls',
            )
            # Soft invariant: most requests should be superseded before
            # the worker reaches them. We assert << 20 (not == 1)
            # because the first call may run to completion before the
            # rest are queued, and timing is jittery. In practice this
            # is ~1-3 calls on a fast machine.
            self.assertLess(
                state['total_calls'], 20,
                'expected most requests to be superseded; got '
                f'{state["total_calls"]} actual fetches',
            )
            # The last id is the authoritative request — its preview
            # must be in the cache.
            self.assertEqual(get_preview_text(b, ids[-1]),
                             f'preview of {ids[-1]}')
        finally:
            b.stop_workers()


class TestSetPreviewWorkerRaceSemantics(unittest.TestCase):
    """#442 worker-vs-recipe race: FIFO post queue, last-poster-wins.

    Post-#442 the worker also delivers via the post queue (no more
    single-slot ``_preview_result`` lane). So whichever side posts
    last to the same id is the final state. In the gated test below
    the recipe queues its write while the worker is still inside
    ``get_preview``, so the recipe lands first and the worker's
    ``_deliver_preview`` posts after — worker wins for that timing.
    Both writes reach the cache; only the order differs.
    """

    def test_worker_delivery_lands_after_recipe_when_recipe_queues_first(self):
        gate = threading.Event()

        def get_preview(id_):
            # Block until the test releases the gate so we can interleave
            # the worker's delivery with a recipe ``set_preview``.
            gate.wait(timeout=2.0)
            return 'from-worker'

        b = make_browser(get_preview=get_preview)
        try:
            b._state._items_by_id['a'] = Item(id='a')
            # Kick the worker; it blocks inside get_preview.
            b.request_preview('a')
            # Wait briefly to let the worker thread pick up the request.
            time.sleep(0.02)
            # Recipe writes via set_preview — queued on the post queue
            # while the worker is still inside get_preview.
            b.set_preview('a', 'from-recipe')
            # Release the worker; it now posts _deliver_preview AFTER
            # the recipe's closure (which was already in the queue).
            gate.set()
            b.run_until_idle()
            # FIFO: recipe ran first, then worker overwrote.
            self.assertEqual(get_preview_text(b, 'a'), 'from-worker')
        finally:
            gate.set()  # in case of test failure, unblock the worker
            b.stop_workers()

    def test_recipe_set_preview_with_no_worker_lands(self):
        # The documented escape hatch: construct with get_preview=None
        # so there is no worker to race. Recipes that need a guaranteed
        # write should use this pattern.
        b = make_browser(get_preview=None)
        try:
            b._state._items_by_id['a'] = Item(id='a')
            b.set_preview('a', 'from-recipe')
            b.run_until_idle()
            self.assertEqual(get_preview_text(b, 'a'), 'from-recipe')
        finally:
            b.stop_workers()


# --- #442: post-queue delivery + cache-aware cursor handling -------------
#
# The preview worker's redesign (#442) replaces the single-slot
# ``_preview_result`` lane with FIFO post-queue delivery, dedups by
# ``local_id``, re-fires when ``item.preview`` is cleared while the
# cursor sits still, and skips ``request_preview`` for already-cached
# items on cursor moves. These tests pin those four behaviors plus
# scroll preservation, conditional redraw, and the preserved
# single-flight invariant.


def _seed_two_items(b, ids=('A', 'B')):
    """Register `ids` under root and put `_visible_cache` in a sane state."""
    from test.async_._helpers import Item as _Item  # local re-export
    children = []
    for id_ in ids:
        item = _Item(id=id_)
        b._state._items_by_id[id_] = item
        children.append(item)
    b._state._children[None] = children
    # Mark visible cache dirty so visible_items rebuilds against the new
    # children list on the next call.
    b._state._visible_dirty = True
    return {id_: b._state._items_by_id[id_] for id_ in ids}


class TestPreviewWorkerRedesign442(unittest.TestCase):
    """#442: stuck-blank fix, cache-aware fetches, post-queue delivery."""

    def test_stuck_blank_re_fires_when_preview_cleared_in_place(self):
        # Cursor on A with item.preview populated. Mutate item.preview =
        # None (simulating clear_preview / invalidate_preview / a
        # children-mutation handler that dropped the umbrella body).
        # Run _update_preview_for_cursor; a fresh fetch must fire.
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'fresh-{id_}'

        b = make_browser(get_preview=get_preview)
        try:
            items = _seed_two_items(b)
            b._state.cursor = 0  # cursor on 'A'
            # First pass: register cursor + prime the cache.
            b._update_preview_for_cursor()
            b.run_until_idle()
            self.assertEqual(calls, ['A'])
            self.assertEqual(items['A'].preview, 'fresh-A')

            # External mutation: cache cleared while cursor sits on A.
            items['A'].preview = None
            calls.clear()

            # Re-tick the cursor helper. Stuck-blank fix: same cursor +
            # item.preview is None → re-fire request_preview.
            b._update_preview_for_cursor()
            self.assertEqual(b._preview_req, 'A')
            b.run_until_idle()
            self.assertEqual(calls, ['A'])
            self.assertEqual(items['A'].preview, 'fresh-A')
        finally:
            b.stop_workers()

    def test_skip_fetch_when_cursor_moves_to_already_cached_item(self):
        # Cursor moves A → B with B.preview already populated. No
        # get_preview call should fire (cache paint is sufficient).
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'fetched-{id_}'

        b = make_browser(get_preview=get_preview)
        try:
            items = _seed_two_items(b)
            # Pre-populate both items' previews so the fetch path
            # should skip on every cursor move.
            items['A'].preview = 'cached-A'
            items['B'].preview = 'cached-B'

            b._state.cursor = 0
            b._update_preview_for_cursor()
            # No fetch should have fired (cache covers A).
            b.run_until_idle()
            self.assertEqual(calls, [])

            # Move cursor to B (also cached).
            b._state.cursor = 1
            b._update_preview_for_cursor()
            b.run_until_idle()
            self.assertEqual(calls, [],
                             'cursor move to cached item must not fetch')
            self.assertEqual(b._preview_cursor_id, 'B')
        finally:
            b.stop_workers()

    def test_refresh_preserves_scroll_when_cursor_stays_put(self):
        # Cursor on A scrolled to row 10. item.preview cleared. Re-tick
        # _update_preview_for_cursor → fresh fetch. After delivery,
        # _preview_scroll must be preserved (not reset to 0).
        def get_preview(id_):
            return 'line\n' * 50  # plenty of content so scroll=10 valid

        b = make_browser(get_preview=get_preview)
        try:
            items = _seed_two_items(b)
            b._state.cursor = 0
            b._update_preview_for_cursor()
            b.run_until_idle()

            # User scrolls.
            b._preview_scroll = 10

            # External mutation clears the cache.
            items['A'].preview = None

            # Re-tick → fresh fetch fires.
            b._update_preview_for_cursor()
            b.run_until_idle()

            # The same-cursor branch early-returns before the
            # _preview_scroll = 0 reset, so scroll survives.
            self.assertEqual(b._preview_scroll, 10)
            # And the cache was refilled.
            self.assertEqual(items['A'].preview, 'line\n' * 50)
        finally:
            b.stop_workers()

    def test_scroll_resets_on_cursor_change(self):
        # Cursor on A scrolled to row 10. Move cursor to B. Scroll
        # must reset to 0 (the cursor-change branch hits the reset).
        b = make_browser(get_preview=lambda id_: f'p-{id_}')
        try:
            items = _seed_two_items(b)
            b._state.cursor = 0
            b._update_preview_for_cursor()
            b.run_until_idle()

            b._preview_scroll = 10

            b._state.cursor = 1
            b._update_preview_for_cursor()
            self.assertEqual(b._preview_scroll, 0,
                             'cursor change must reset scroll to 0')
        finally:
            b.stop_workers()

    def test_latest_wins_all_deliveries_land_via_post_queue(self):
        # Fire request_preview for A, B, C in sequence with a slow
        # get_preview. Drain. The post-queue delivery means each
        # completed fetch caches its result — different from the old
        # single-slot model where in-flight results would be clobbered.
        gate = threading.Event()
        order = []
        lock = threading.Lock()

        def get_preview(id_):
            with lock:
                order.append(id_)
            # Block only the first call so the request slot can be
            # clobbered while the worker is still computing.
            if id_ == 'A':
                gate.wait(timeout=2.0)
            return f'preview-{id_}'

        b = make_browser(get_preview=get_preview)
        try:
            items = _seed_two_items(b, ids=('A', 'B', 'C'))
            b.request_preview('A')
            # Wait for the worker to pick up A.
            time.sleep(0.02)
            b.request_preview('B')
            b.request_preview('C')
            gate.set()
            b.run_until_idle()
            # The authoritative final id (C) must be cached.
            self.assertEqual(items['C'].preview, 'preview-C')
            # A's in-flight delivery isn't lost — it lands via post queue.
            self.assertEqual(items['A'].preview, 'preview-A')
            # B was clobbered before the worker reached it (most likely
            # — single-flight + slot-clobber). Either it ran or it
            # didn't; if it did, its preview is cached too. We assert
            # the weak invariant: all completed fetches must be in the
            # cache.
            for id_ in order:
                self.assertEqual(items[id_].preview, f'preview-{id_}')
        finally:
            gate.set()
            b.stop_workers()

    def test_conditional_redraw_only_when_cursor_matches_delivered_id(self):
        # Worker delivers for id X while cursor is on Y. Assert
        # _needs_redraw does NOT add 'preview'. Then move cursor to X
        # → next _update_preview_for_cursor should flag redraw (via
        # the cursor-change branch, or because the cached value is
        # now present and the renderer reads it).
        gate = threading.Event()

        def get_preview(id_):
            gate.wait(timeout=2.0)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview)
        try:
            items = _seed_two_items(b)
            # Cursor on B (index 1) — _preview_cursor_id starts None.
            b._state.cursor = 1
            b._update_preview_for_cursor()  # → _preview_cursor_id = 'B'
            # Clear redraw state to isolate the next observation.
            b._needs_redraw.discard('preview')

            # Force a fetch for A while cursor is on B.
            b.request_preview('A')
            time.sleep(0.02)  # worker picks up A
            gate.set()
            # Drain only the post queue (run_until_idle would also
            # tick the cursor helper indirectly; we want to assert on
            # the delivery alone).
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0:
                b.drain_main_queue()
                if items['A'].preview == 'p-A':
                    break
                time.sleep(0.005)
            self.assertEqual(items['A'].preview, 'p-A')
            # Cursor is on B (id_ != cursor) → no preview redraw flag.
            self.assertNotIn('preview', b._needs_redraw)

            # Move cursor to A → _update_preview_for_cursor flags
            # redraw (cursor-change branch) and skips the fetch
            # because A is already cached.
            b._state.cursor = 0
            b._update_preview_for_cursor()
            self.assertIn('preview', b._needs_redraw)
        finally:
            gate.set()
            b.stop_workers()

    def test_single_flight_invariant_under_rapid_requests(self):
        # 20 rapid request_preview calls; assert max concurrency stays
        # at 1. Same invariant as #431's TestPreviewWorkerSingleFlight
        # — repeated here under the #442 redesign so a future change
        # that accidentally drops single-flight is caught.
        lock = threading.Lock()
        state = {'in_flight': 0, 'max_in_flight': 0}

        def get_preview(id_):
            with lock:
                state['in_flight'] += 1
                if state['in_flight'] > state['max_in_flight']:
                    state['max_in_flight'] = state['in_flight']
            time.sleep(0.005)
            with lock:
                state['in_flight'] -= 1
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview)
        try:
            ids = [f'id-{n}' for n in range(20)]
            for id_ in ids:
                b._state._items_by_id[id_] = Item(id=id_)
            for id_ in ids:
                b.request_preview(id_)
            b.run_until_idle()
            self.assertEqual(state['max_in_flight'], 1,
                             'single-flight invariant violated under #442')
        finally:
            b.stop_workers()


class TestHeadlessAndIdle(unittest.TestCase):
    """Headless flag is observable; idle Browser is idle immediately."""

    def test_headless_flag_observable_no_terminal_writes(self):
        # Phase 1 does not init the terminal at all, but the flag must
        # still be readable by downstream code (and by the renderer in #10).
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            b = Browser(BrowserConfig(_headless=True))
            self.assertTrue(b._headless)
            b.start_workers()
            b.stop_workers()
        finally:
            sys.stdout = old_stdout
        # No terminal escape sequences from start/stop in headless mode.
        self.assertEqual(captured.getvalue(), '')

    def test_idle_browser_returns_immediately(self):
        b = make_browser()
        try:
            t0 = time.monotonic()
            b.run_until_idle(timeout=2.0)
            elapsed = time.monotonic() - t0
            # No work submitted -- run_until_idle hits the idle predicate
            # on the first iteration.
            self.assertLess(elapsed, 0.1)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
