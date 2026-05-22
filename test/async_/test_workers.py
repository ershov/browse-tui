"""Worker-thread tests: lifecycle, FIFO order, latest-wins, error handling.

The children worker is a strict FIFO queue: any ids submitted via
``refresh()`` get fetched in submission order. The preview worker is a
single-slot latest-wins design ported from plan-tui -- a flurry of
cursor-move events leaves only the latest id pending, with at most one
in-flight fetch.

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


# --- #431: single-flight worker invariant -------------------------------
#
# The preview worker must process at most one ``get_preview`` fetch at a
# time. ``request_preview`` overwrites the single-slot ``_preview_req``;
# the worker reads the slot, fetches, and discards the result if the
# slot has been clobbered. After #431 (recipe-facing ``set_preview``
# moved to the post queue) this invariant must still hold — recipes do
# not contend with the worker on its own lane.


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
    """#431 worker-vs-recipe race: documented, deliberate, unchanged.

    Within a single ``run_until_idle`` iteration:
      1. ``drain_main_queue`` runs first → recipe ``set_preview`` lands.
      2. ``apply_preview_result`` runs second → worker delivery overwrites.

    So if both write for the same id around the same time, the worker
    wins. Recipes that want a guaranteed write should construct the
    Browser with ``get_preview=None`` (no worker to race).
    """

    def test_worker_overwrites_recipe_set_preview_for_same_id(self):
        gate = threading.Event()

        def get_preview(id_):
            # Block until the test releases the gate so we can race
            # the worker's delivery against a recipe ``set_preview``.
            gate.wait(timeout=2.0)
            return 'from-worker'

        b = make_browser(get_preview=get_preview)
        try:
            b._state._items_by_id['a'] = Item(id='a')
            # Kick the worker; it blocks inside get_preview.
            b.request_preview('a')
            # Wait briefly to let the worker thread pick up the request.
            time.sleep(0.02)
            # Recipe writes via set_preview — queued on the post queue.
            b.set_preview('a', 'from-recipe')
            # Release the worker; now both writes are in flight.
            gate.set()
            b.run_until_idle()
            # apply_preview_result runs after drain_main_queue per the
            # documented ordering — the worker wins.
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
