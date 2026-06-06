"""Tests for generator support in ``get_children`` (#272).

Ticket #272 builds on #271's ``update_data``-based delivery: when
``get_children`` returns a generator, the worker iterates it and emits
one ``update_data`` batch per yield (no trailing ``complete`` per
chunk). On clean ``StopIteration`` a final ``[complete(parent_id)]``
batch clears loading. On a mid-stream exception, partial deliveries
stay in place, ``_loading`` stays True, and the error surfaces via
``browser.error``.

Yielded chunk type discrimination:

* ``list`` → batch of items (each coerced via ``to_item``).
* anything else (``Item``, ``tuple``, ``dict``, ``str``) → single item.
"""

import threading
import time
import unittest

from test.async_._helpers import Item, _state, make_browser

upsert = _state.upsert


class TestListYieldingGenerator(unittest.TestCase):
    """Generator that yields lists of items — each list is one batch."""

    def test_list_yields_land_in_order(self):
        def kids(_pid, *, reload=False):
            yield [Item(id='a'), Item(id='b')]
            yield [Item(id='c')]

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            ids = [k.id for k in b._state._children['/']]
            self.assertEqual(ids, ['a', 'b', 'c'])
        finally:
            b.stop_workers()

    def test_clean_exhaustion_clears_loading(self):
        def kids(_pid, *, reload=False):
            yield [Item(id='a')]

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # Trailing ``complete`` posted on StopIteration cleared it.
            self.assertFalse(b._state._loading['/'])
        finally:
            b.stop_workers()


class TestSingleItemYieldingGenerator(unittest.TestCase):
    """Each yield is a single Item / tuple / dict / str — coerced via to_item."""

    def test_single_yields_all_coerced(self):
        def kids(_pid, *, reload=False):
            yield Item(id='a', title='A')
            yield ('b', 'B')
            yield {'id': 'c', 'title': 'C'}
            yield 'd'

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            cached = b._state._children['/']
            ids = [k.id for k in cached]
            self.assertEqual(ids, ['a', 'b', 'c', 'd'])
            self.assertEqual(cached[0].title, 'A')
            self.assertEqual(cached[1].title, 'B')   # 2-tuple
            self.assertEqual(cached[2].title, 'C')   # dict
            self.assertEqual(cached[3].title, 'd')   # str default
        finally:
            b.stop_workers()


class TestMixedYields(unittest.TestCase):
    """Generator that mixes list yields and single-item yields."""

    def test_mixed_list_and_single_yields(self):
        def kids(_pid, *, reload=False):
            yield Item(id='a')                       # single Item
            yield [Item(id='b'), Item(id='c')]       # batch
            yield ('d', 'D')                         # single tuple
            yield [{'id': 'e'}, 'f']                 # batch (dict + str)

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            ids = [k.id for k in b._state._children['/']]
            self.assertEqual(ids, ['a', 'b', 'c', 'd', 'e', 'f'])
        finally:
            b.stop_workers()


class TestEmptyGenerator(unittest.TestCase):
    """Immediate StopIteration with no yields — like an empty list return."""

    def test_empty_generator_clears_loading(self):
        def kids(_pid, *, reload=False):
            return
            yield  # noqa — unreachable, makes this a generator function

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # Trailing ``complete`` cleared loading.
            self.assertFalse(b._state._loading['/'])
        finally:
            b.stop_workers()

    def test_empty_generator_leaves_cache_as_empty_list(self):
        def kids(_pid, *, reload=False):
            return
            yield  # noqa

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # Visible-tree builder distinguishes "absent" (placeholder)
            # from "empty list" (render nothing). Empty generator must
            # yield the latter so the placeholder row goes away.
            self.assertEqual(b._state._children['/'], [])
        finally:
            b.stop_workers()

    def test_empty_generator_resolves_pending(self):
        events = []

        def kids(_pid, *, reload=False):
            return
            yield  # noqa

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/').then(lambda: events.append('done'))
            b.run_until_idle()
            self.assertEqual(events, ['done'])
        finally:
            b.stop_workers()


class TestGeneratorRaisesMidStream(unittest.TestCase):
    """Mid-stream exception: prior items survive, loading stays, error surfaces."""

    def test_partial_items_present_after_raise(self):
        def kids(_pid, *, reload=False):
            yield [Item(id='a'), Item(id='b')]
            raise RuntimeError('boom')

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            ids = [k.id for k in b._state._children['/']]
            # Items delivered before the exception remain.
            self.assertEqual(ids, ['a', 'b'])
        finally:
            b.stop_workers()

    def test_loading_stays_true_after_raise(self):
        def kids(_pid, *, reload=False):
            yield [Item(id='a')]
            raise RuntimeError('boom')

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # Per spec: "loading stays unless caller cleared explicitly".
            # No trailing ``complete`` on mid-stream exception.
            self.assertTrue(b._state._loading['/'])
        finally:
            b.stop_workers()

    def test_error_surfaces_via_browser_error(self):
        def kids(_pid, *, reload=False):
            yield [Item(id='a')]
            raise RuntimeError('boom')

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            log = '\n'.join(b._log)
            self.assertIn('boom', log)
            self.assertIn('RuntimeError', log)
        finally:
            b.stop_workers()

    def test_pending_still_resolves_after_raise(self):
        events = []

        def kids(_pid, *, reload=False):
            yield [Item(id='a')]
            raise RuntimeError('boom')

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/').then(lambda: events.append('done'))
            b.run_until_idle()
            # Worker has finished its job (stopped pulling) — chain fires
            # so callers don't strand on a misbehaving generator.
            self.assertEqual(events, ['done'])
        finally:
            b.stop_workers()


class TestIncrementalVisibility(unittest.TestCase):
    """Multiple chunks: between batches, the cache reflects items so far.

    Uses ``drain_main_queue`` to step the main thread one drain at a
    time and observe intermediate states. The worker thread is allowed
    to advance through ``threading.Event`` gates so we can interleave
    its yields with our drains.
    """

    def test_intermediate_cache_states_visible(self):
        gate1 = threading.Event()
        gate2 = threading.Event()

        def kids(_pid, *, reload=False):
            yield [Item(id='a')]
            gate1.wait(timeout=2.0)
            yield [Item(id='b')]
            gate2.wait(timeout=2.0)
            yield [Item(id='c')]

        def wait_for_ids(expected, deadline=2.0):
            """Drain + sleep until ``b._state._children['/']`` matches."""
            t_end = time.monotonic() + deadline
            while time.monotonic() < t_end:
                b.drain_main_queue()
                cached = [k.id for k in b._state._children.get('/', [])]
                if cached == expected:
                    return
                time.sleep(0.005)
            self.fail(
                f'timed out waiting for ids {expected!r}; '
                f'last seen: {cached!r}'
            )

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            # First yield: wait until 'a' lands. The worker is now
            # blocked on gate1 — so once we see 'a' in the cache,
            # nothing else is pending until we release the gate.
            wait_for_ids(['a'])
            # Loading is still True — no trailing complete yet.
            self.assertTrue(b._state._loading.get('/', False))

            # Release gate1 so the worker yields 'b'.
            gate1.set()
            wait_for_ids(['a', 'b'])
            self.assertTrue(b._state._loading.get('/', False))

            # Release gate2 — generator yields 'c' and exhausts.
            gate2.set()
            b.run_until_idle()
            self.assertEqual(
                [k.id for k in b._state._children['/']], ['a', 'b', 'c']
            )
            # Trailing ``complete`` cleared loading on exhaustion.
            self.assertFalse(b._state._loading['/'])
        finally:
            gate1.set()
            gate2.set()
            b.stop_workers()


class TestPendingFiresAfterExhaustion(unittest.TestCase):
    """Pending callback runs exactly once, AFTER the generator exhausts.

    Mid-stream batches must NOT fire the pending chain — only the
    final post-exhaustion housekeeping triggers ``_post_children_delivery``.
    """

    def test_pending_fires_once_after_all_yields(self):
        observed = []

        def kids(_pid, *, reload=False):
            yield [Item(id='a')]
            yield [Item(id='b')]
            yield [Item(id='c')]

        b = make_browser(get_children=kids, root_id='/')
        try:
            (b.refresh('/')
                .then(lambda: observed.append(
                    [k.id for k in b._state._children.get('/', [])]
                )))
            b.run_until_idle()
            # Exactly one resolution, observing the post-exhaustion
            # state (all three items present).
            self.assertEqual(observed, [['a', 'b', 'c']])
        finally:
            b.stop_workers()

    def test_pending_fires_once_for_empty_generator(self):
        observed = []

        def kids(_pid, *, reload=False):
            return
            yield  # noqa — empty generator

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/').then(lambda: observed.append('done'))
            b.run_until_idle()
            self.assertEqual(observed, ['done'])
        finally:
            b.stop_workers()


class TestListReturnRegression(unittest.TestCase):
    """Sanity: a non-generator list return still uses #271's path."""

    def test_plain_list_return_still_works(self):
        def kids(_pid, *, reload=False):
            return [Item(id='a'), Item(id='b'), Item(id='c')]

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            ids = [k.id for k in b._state._children['/']]
            self.assertEqual(ids, ['a', 'b', 'c'])
            # Trailing ``complete`` from #271's batch cleared loading.
            self.assertFalse(b._state._loading['/'])
        finally:
            b.stop_workers()

    def test_plain_empty_list_return_still_works(self):
        b = make_browser(get_children=lambda _, *, reload=False: [], root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            self.assertEqual(b._state._children['/'], [])
            self.assertFalse(b._state._loading['/'])
        finally:
            b.stop_workers()

    def test_none_return_still_leaves_loading_true(self):
        b = make_browser(get_children=lambda _, *, reload=False: None, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            # Per #271: ``None`` skips the batch; loading stays True.
            self.assertTrue(b._state._loading['/'])
        finally:
            b.stop_workers()


class TestGeneratorCustomAttrsSurvive(unittest.TestCase):
    """Recipe-attached custom attrs survive through generator delivery."""

    def test_custom_attrs_preserved_via_fields_of_item(self):
        def kids(_pid, *, reload=False):
            it1 = Item(id='x', title='X')
            it1.size = 42
            it1.path = '/x'
            yield it1
            it2 = Item(id='y', title='Y')
            it2.tag = '!'
            yield [it2]

        b = make_browser(get_children=kids, root_id='/')
        try:
            b.refresh('/')
            b.run_until_idle()
            cached = b._state._children['/']
            self.assertEqual(cached[0].size, 42)
            self.assertEqual(cached[0].path, '/x')
            self.assertEqual(cached[1].tag, '!')
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
