"""Tests for the children-worker → ``update_data`` delivery path (#271).

Ticket #271 rewrote ``_children_worker`` to deliver via ``update_data``
instead of the legacy ``_children_results`` deque. New behaviour:

* Empty list return → batch is just ``[complete(parent_id)]`` — clears
  the loading flag, leaves cache as an empty list, no items added.
* ``None`` return → no batch posted; ``_loading`` stays True (recipe
  will push from elsewhere). Pendings still resolve so chains fire.
* Mixed-shape return list (Item, str, tuple, dict) is coerced via
  ``to_item`` before batching.
* Pending chain fires AFTER the batch is applied — chains observe the
  post-batch state.
* Watcher push that races a worker fetch now appends rather than
  clobbers (the documented behavioural change).
"""

import threading
import unittest

from test.async_._helpers import Item, Pending, _state, make_browser

upsert = _state.upsert


class TestEmptyListReturn(unittest.TestCase):
    """Empty list return → batch of just ``[complete(parent_id)]``."""

    def test_empty_list_clears_loading_flag(self):
        b = make_browser(get_children=lambda _: [], root_id='/')
        try:
            p = b.refresh('/')
            b.run_until_idle()
            self.assertTrue(p.done)
            # Trailing complete cleared the loading flag.
            self.assertFalse(b._state._loading['/'])
        finally:
            b.stop_workers()

    def test_empty_list_leaves_cache_as_empty_list(self):
        # Visible-tree builder distinguishes "absent" (placeholder) from
        # "empty list" (render nothing) — empty return must yield the
        # latter so the placeholder row goes away.
        b = make_browser(get_children=lambda _: [], root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            self.assertEqual(b._state._children['/'], [])
        finally:
            b.stop_workers()


class TestNoneReturn(unittest.TestCase):
    """``None`` return: no batch posted; ``_loading`` stays True."""

    def test_none_return_leaves_loading_true(self):
        b = make_browser(get_children=lambda _: None, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # ``_do_refresh`` set ``_loading['/']=True`` at dispatch;
            # worker returned None so no trailing ``complete`` fires.
            self.assertTrue(b._state._loading['/'])
        finally:
            b.stop_workers()

    def test_none_return_does_not_populate_cache_with_items(self):
        b = make_browser(get_children=lambda _: None, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # Post-delivery ensures the parent's cache entry exists as
            # an empty list (so the placeholder row clears) but no
            # items are added.
            self.assertEqual(b._state._children.get('/', []), [])
        finally:
            b.stop_workers()

    def test_none_return_still_resolves_pending(self):
        # Pending must fire so ``refresh().then()`` chains do not
        # strand on a None-returning recipe — the worker has finished
        # its job even if the data will arrive via a separate push.
        events = []
        b = make_browser(get_children=lambda _: None, root_id='/')
        try:
            b.refresh('/').then(lambda: events.append('done'))
            b.run_until_idle()
            self.assertEqual(events, ['done'])
        finally:
            b.stop_workers()


class TestMixedShapeReturn(unittest.TestCase):
    """Mixed Item / str / tuple / dict in one return list — all coerced."""

    def test_mixed_shapes_all_land_in_cache(self):
        def kids(_pid):
            return [
                Item(id='a', title='A'),                # Item
                'b',                                    # str → leaf
                ('c', 'C'),                             # 2-tuple → id+title
                {'id': 'd', 'title': 'D', 'tag': '!'},  # dict
            ]

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            kids_cached = b._state._children['/']
            ids = [k.id for k in kids_cached]
            self.assertEqual(ids, ['a', 'b', 'c', 'd'])
            # to_item rules carried through to the batch.
            self.assertEqual(kids_cached[0].title, 'A')
            self.assertEqual(kids_cached[1].title, 'b')   # str defaults
            self.assertEqual(kids_cached[2].title, 'C')   # 2-tuple
            self.assertEqual(kids_cached[3].title, 'D')   # dict
            self.assertEqual(kids_cached[3].tag, '!')
        finally:
            b.stop_workers()

    def test_custom_attrs_survive_to_cache(self):
        # Items with recipe-attached custom attrs must round-trip
        # through the upsert ops (``fields_of`` includes ``__dict__``).
        def kids(_pid):
            it = Item(id='x', title='X')
            it.size = 42
            it.path = '/x'
            return [it]

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            cached = b._state._children['/'][0]
            self.assertEqual(cached.size, 42)
            self.assertEqual(cached.path, '/x')
        finally:
            b.stop_workers()


class TestPendingFiresAfterApply(unittest.TestCase):
    """Pending callbacks observe post-batch state on the main thread."""

    def test_then_sees_cache_populated(self):
        observed = []

        def kids(_pid):
            return [Item(id='a'), Item(id='b')]

        b = make_browser(get_children=kids, root_id='/')
        try:
            (b.refresh('/')
                .then(lambda: observed.append(
                    [k.id for k in b._state._children.get('/', [])]
                )))
            b.run_until_idle()
            # The chain saw the post-batch state (both items present).
            self.assertEqual(observed, [['a', 'b']])
        finally:
            b.stop_workers()

    def test_then_sees_loading_cleared(self):
        observed = []

        b = make_browser(get_children=lambda _: [], root_id='/')
        try:
            (b.refresh('/')
                .then(lambda: observed.append(
                    b._state._loading.get('/', None)
                )))
            b.run_until_idle()
            # The trailing ``complete`` op cleared loading before the
            # chain fired.
            self.assertEqual(observed, [False])
        finally:
            b.stop_workers()

    def test_then_sees_indexes_consistent(self):
        # ``_items_by_id`` and ``_parent_of_id`` are populated by the
        # apply path; the chain must observe them in lockstep with
        # ``_children``.
        observed = []

        def kids(_pid):
            return [Item(id='a'), Item(id='b')]

        b = make_browser(get_children=kids, root_id='/')
        try:
            def check():
                s = b._state
                observed.append((
                    set(s._items_by_id),
                    {k: s._parent_of_id.get(k) for k in ('a', 'b')},
                ))
            b.refresh('/').then(check)
            b.run_until_idle()
            self.assertEqual(observed, [({'a', 'b'}, {'a': '/', 'b': '/'})])
        finally:
            b.stop_workers()


class TestWatcherDuringFetch(unittest.TestCase):
    """The clobber → append behavioural change (spec Section 3).

    A watcher push that races a ``get_children`` call must end up in
    the cache alongside the worker's return value. Before #271 the
    worker's delivery clobbered the cache entry; after #271 each
    worker item is upserted, leaving prior pushes intact.
    """

    def test_watcher_push_during_blocked_fetch_survives(self):
        gate = threading.Event()
        released = threading.Event()

        def slow_kids(_pid):
            # Block the worker so we can land a watcher push first.
            gate.wait(timeout=2.0)
            released.set()
            return [Item(id='a', title='A'), Item(id='b', title='B')]

        b = make_browser(get_children=slow_kids, root_id='p')
        try:
            # Trigger refresh (worker blocks in slow_kids).
            p = b.refresh('p')
            # Drain the post queue so _do_refresh runs and the worker
            # picks up the request.
            b.drain_main_queue()
            # The worker is now blocked at gate.wait. From a separate
            # thread, push a watcher item under 'p'. ``update_data``
            # is thread-safe; the post lands on the main queue.

            def watcher_push():
                b.update_data([upsert('w', 'p', title='watcher')])

            wt = threading.Thread(target=watcher_push)
            wt.start()
            wt.join(timeout=2.0)

            # Drain the watcher's post so the upsert lands BEFORE the
            # worker delivers — that's the racy scenario we care about.
            b.drain_main_queue()
            self.assertIn('w', [k.id for k in b._state._children['p']])

            # Release the worker; it builds the upsert+complete batch
            # and posts it.
            gate.set()
            b.run_until_idle(timeout=2.0)

            self.assertTrue(p.done)
            ids = [k.id for k in b._state._children['p']]
            # Watcher's 'w' AND worker's 'a','b' must all be present
            # — no clobber. Order is watcher-first, worker-after.
            self.assertEqual(set(ids), {'w', 'a', 'b'})
            self.assertEqual(len(ids), 3)
            # ``_loading['p']`` cleared by trailing ``complete``.
            self.assertFalse(b._state._loading['p'])
        finally:
            gate.set()
            b.stop_workers()

    def test_watcher_push_after_worker_returns_still_appends(self):
        # The reverse ordering: worker delivers first, watcher pushes
        # after. Both items must end up in the cache.
        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            root_id='p',
        )
        try:
            b.refresh('p')
            b.run_until_idle()
            self.assertEqual([k.id for k in b._state._children['p']], ['a'])

            # Watcher pushes a new child after the worker has delivered.
            b.update_data([upsert('w', 'p', title='watcher')])
            b.drain_main_queue()
            ids = [k.id for k in b._state._children['p']]
            self.assertEqual(set(ids), {'a', 'w'})
        finally:
            b.stop_workers()


class TestWorkerDoesNotUseLegacyDeque(unittest.TestCase):
    """Regression guard: the worker delivery path no longer touches
    the legacy ``_children_results`` deque (used by ``set_children``).
    """

    def test_worker_delivery_does_not_enqueue_to_children_results(self):
        b = make_browser(
            get_children=lambda _: [Item(id='a')],
            root_id='/',
        )
        try:
            b.refresh('/')
            b.run_until_idle()
            # Worker delivered via update_data; the legacy deque is
            # untouched.
            self.assertEqual(len(b._children_results), 0)
            # And the cache populated correctly.
            self.assertEqual([k.id for k in b._state._children['/']], ['a'])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
