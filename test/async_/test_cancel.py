"""End-to-end Pending.cancel() tests across the worker pipeline.

Ticket #30 -- non-strict cancellation. ``Pending.cancel()`` suppresses
chained ``.then()`` callbacks from firing, but the underlying worker
fetch still runs to completion and populates the cache. These tests
gate the fetch on a ``threading.Event`` to prove the worker really
did finish (cache populated, ``done=True``) while the chain stayed
silent.
"""

import threading
import unittest

from test.async_._helpers import Item, make_browser


class TestPendingCancelEndToEnd(unittest.TestCase):

    def test_cancel_during_in_flight_suppresses_chain(self):
        # While the worker is blocked in get_children, cancel the
        # Pending. After the gate releases and the worker delivers,
        # the chain must not fire -- but the cache must still populate.
        gate = threading.Event()
        ran = []

        def slow_kids(_pid, *, reload=False):
            gate.wait(timeout=2.0)
            return [Item(id='child')]

        b = make_browser(get_children=slow_kids, root_id='/')
        try:
            p = b.refresh('/')
            p.then(lambda: ran.append('cb'))
            # Drain the post-queue so _do_refresh enqueues into the
            # worker FIFO; the worker is now blocked on the gate.
            b.drain_main_queue()
            # Cancel while the fetch is in-flight.
            p.cancel()
            # Release the gate; worker delivers; main thread resolves
            # the (cancelled) Pending.
            gate.set()
            b.run_until_idle(timeout=2.0)
            # Chain didn't fire.
            self.assertEqual(ran, [])
            self.assertTrue(p.cancelled)
            self.assertTrue(p.done)
            # Cache populated nonetheless -- that's the non-strict
            # contract: worker still ran, only the chain was muted.
            self.assertIn('/', b._state._children)
        finally:
            gate.set()
            b.stop_workers()

    def test_cancel_one_pending_does_not_affect_other_in_flight(self):
        # Two refresh()es of the same id coalesce onto one fetch, but
        # they each get their own Pending. Cancelling one must not
        # affect the other -- the surviving Pending's chain still fires.
        gate = threading.Event()
        ran = []

        def slow_kids(_pid, *, reload=False):
            gate.wait(timeout=2.0)
            return []

        b = make_browser(get_children=slow_kids, root_id='/')
        try:
            p1 = b.refresh('/')
            p1.then(lambda: ran.append('p1'))
            b.drain_main_queue()
            p2 = b.refresh('/')
            p2.then(lambda: ran.append('p2'))
            b.drain_main_queue()
            # Both Pendings are registered against the single in-flight
            # fetch. Cancel only p1.
            p1.cancel()
            gate.set()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(ran, ['p2'])
            self.assertTrue(p1.cancelled)
            self.assertTrue(p1.done)
            self.assertFalse(p2.cancelled)
            self.assertTrue(p2.done)
        finally:
            gate.set()
            b.stop_workers()

    def test_cancel_before_worker_dispatch_suppresses_chain(self):
        # Cancel before draining the post queue (i.e. before _do_refresh
        # has even enqueued the worker request). The worker still runs
        # because cancellation is non-strict.
        ran = []
        b = make_browser(get_children=lambda _, *, reload=False: [Item(id='c')], root_id='/')
        try:
            p = b.refresh('/')
            p.then(lambda: ran.append('cb'))
            p.cancel()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(ran, [])
            self.assertTrue(p.cancelled)
            self.assertTrue(p.done)
            # Cache still populated -- non-strict.
            self.assertIn('/', b._state._children)
        finally:
            b.stop_workers()

    def test_browser_cancel_sugar_cancels_multiple(self):
        # Browser.cancel(*pendings) is sugar for calling .cancel() on each.
        gate = threading.Event()
        ran = []

        def slow_kids(_pid, *, reload=False):
            gate.wait(timeout=2.0)
            return []

        b = make_browser(get_children=slow_kids, root_id='/')
        try:
            p1 = b.refresh('A')
            p1.then(lambda: ran.append('a'))
            b.drain_main_queue()
            p2 = b.refresh('B')
            p2.then(lambda: ran.append('b'))
            b.drain_main_queue()
            b.cancel(p1, p2)
            gate.set()
            b.run_until_idle(timeout=2.0)
            self.assertEqual(ran, [])
            self.assertTrue(p1.cancelled)
            self.assertTrue(p2.cancelled)
        finally:
            gate.set()
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
