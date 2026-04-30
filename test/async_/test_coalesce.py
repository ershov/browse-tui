"""Coalescing tests: duplicate refreshes for the same id share one fetch.

Ticket #29 — phase 3. ``_do_refresh`` enqueues the worker only the first
time ``id_`` enters ``_children_pending``; subsequent refreshes register
their Pending in ``_children_in_flight[id_]`` and resolve together when
the single fetch delivers.

Tests use a gated ``get_children`` rather than time-based asserts: the
fetch blocks on a ``threading.Event`` until the test releases it, so
"two refreshes coalesce into one fetch" is provable deterministically
(at gate-release time, both Pendings are already in the in-flight list).
"""

import threading
import time
import unittest

from test.async_._helpers import Item, make_browser


class TestCoalesceChildrenQueue(unittest.TestCase):

    def test_two_refreshes_same_id_one_fetch(self):
        # Two refresh(id) calls land while the first fetch is gated; the
        # second must be coalesced onto the first -- a single fetch, two
        # resolved Pendings.
        gate = threading.Event()
        fetch_count = [0]

        def slow_kids(pid):
            fetch_count[0] += 1
            gate.wait(timeout=2.0)
            return [Item(id=f'{pid}/c')]

        b = make_browser(get_children=slow_kids, root_id='/')
        try:
            # First refresh -- the post lambda runs on the next drain;
            # we drive that drain by calling drain_main_queue ourselves
            # (don't run_until_idle yet, since the gate is closed).
            p1 = b.refresh('/')
            b.drain_main_queue()
            # The first _do_refresh has now enqueued 'A' in the worker
            # FIFO; the worker thread has popped it and is blocked in
            # gate.wait(). The cache_invalidate_subtree dropped the
            # cache, but _children_pending still contains '/'.
            self.assertIn('/', b._state._children_pending)

            # Second refresh -- post + drain. Since '/' is already in
            # _children_pending, this must NOT enqueue again.
            p2 = b.refresh('/')
            b.drain_main_queue()
            # Both pendings are now registered in _children_in_flight['/'];
            # the worker is still blocked on the gate.
            self.assertEqual(len(b._children_in_flight['/']), 2)

            # Release the gate; worker delivers; both pendings resolve.
            gate.set()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(fetch_count[0], 1)
            self.assertTrue(p1.done)
            self.assertTrue(p2.done)
        finally:
            gate.set()  # ensure worker exits even on assertion failure
            b.stop_workers()

    def test_three_refreshes_same_id_one_fetch(self):
        # Three refresh()es of the same id while the fetch is gated --
        # all three pendings resolve from the single fetch.
        gate = threading.Event()
        fetch_count = [0]

        def slow_kids(_pid):
            fetch_count[0] += 1
            gate.wait(timeout=2.0)
            return []

        b = make_browser(get_children=slow_kids, root_id='/')
        try:
            p1 = b.refresh('/')
            b.drain_main_queue()
            p2 = b.refresh('/')
            b.drain_main_queue()
            p3 = b.refresh('/')
            b.drain_main_queue()
            self.assertEqual(len(b._children_in_flight['/']), 3)
            gate.set()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(fetch_count[0], 1)
            self.assertTrue(p1.done)
            self.assertTrue(p2.done)
            self.assertTrue(p3.done)
        finally:
            gate.set()
            b.stop_workers()

    def test_three_threads_same_id_one_fetch(self):
        # Three concurrent threads refresh the same id. After all three
        # post + the main-thread drain, exactly one fetch should have
        # been dispatched (the second & third Pendings coalesce onto the
        # first because _do_refresh runs serially on the main thread).
        gate = threading.Event()
        fetch_count = [0]

        def slow_kids(_pid):
            fetch_count[0] += 1
            gate.wait(timeout=2.0)
            return []

        b = make_browser(get_children=slow_kids, root_id='/')
        try:
            results = []
            pendings = []

            def submit():
                p = b.refresh('/')
                pendings.append(p)

            threads = [threading.Thread(target=submit) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # All three post lambdas are now sitting in _main_queue.
            # Drain them on the main thread; _do_refresh runs three times
            # in sequence, but only the first enqueues a worker fetch.
            b.drain_main_queue()
            self.assertEqual(len(b._children_in_flight['/']), 3)
            for p in pendings:
                p.then(lambda: results.append('done'))
            gate.set()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(fetch_count[0], 1)
            self.assertEqual(len(results), 3)
            self.assertTrue(all(p.done for p in pendings))
        finally:
            gate.set()
            b.stop_workers()

    def test_different_ids_not_coalesced(self):
        # refresh('A') and refresh('B') are different ids; each gets its
        # own fetch. (The point is to confirm we coalesce on id, not
        # globally.)
        gate = threading.Event()
        seen = []

        def slow_kids(pid):
            seen.append(pid)
            gate.wait(timeout=2.0)
            return []

        b = make_browser(get_children=slow_kids)
        try:
            pa = b.refresh('A')
            pb = b.refresh('B')
            b.drain_main_queue()
            # Both should be registered as separate in-flight fetches.
            self.assertEqual(len(b._children_in_flight['A']), 1)
            self.assertEqual(len(b._children_in_flight['B']), 1)
            gate.set()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(sorted(seen), ['A', 'B'])
            self.assertTrue(pa.done)
            self.assertTrue(pb.done)
        finally:
            gate.set()
            b.stop_workers()

    def test_resolved_then_refetch_does_fetch_again(self):
        # First refresh resolves, then a later refresh of the same id
        # *should* trigger a fresh fetch -- coalescing only collapses
        # in-flight duplicates, not subsequent calls. This guards
        # against accidentally turning the dedup gate into a
        # cache-forever check.
        fetch_count = [0]

        def kids(_pid):
            fetch_count[0] += 1
            return []

        b = make_browser(get_children=kids, root_id='/')
        try:
            p1 = b.refresh('/')
            b.run_until_idle(timeout=2.0)
            self.assertTrue(p1.done)
            self.assertEqual(fetch_count[0], 1)
            # Second refresh after the first has resolved must fetch again.
            p2 = b.refresh('/')
            b.run_until_idle(timeout=2.0)
            self.assertTrue(p2.done)
            self.assertEqual(fetch_count[0], 2)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
