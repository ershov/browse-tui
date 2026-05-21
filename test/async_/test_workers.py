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
