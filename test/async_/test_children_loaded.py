"""Worker-delivery timing for ``on_children_loaded`` (#600).

``on_children_loaded(ctx, parent_ids)`` fires once per main-loop drain
with the list of parents whose ``get_children`` fetch SETTLED in that
drain — the moment ``state._loading[pid]`` transitions True→False
*because children became available* (the worker's ``complete`` op tail,
or the legacy ``apply_children_results`` deque). It is the
source-agnostic counterpart to ``Pending.then()``.

These tests use the real worker (`make_browser` starts it) and the
deterministic `run_until_idle()` pump. As in `test_lifecycle_hooks.py`,
`run_until_idle()` drains the post queue and applies worker deliveries
but does NOT itself run the per-drain `_fire_*` methods (the real main
loop does); the tests call `b._fire_children_loaded_if_pending()`
explicitly to observe one drain's worth of settlements.
"""

import threading
import unittest

from test.async_._helpers import Item, make_browser


class TestOnChildrenLoadedTiming(unittest.TestCase):

    def test_uncached_expand_fires_after_delivery(self):
        # Expanding an uncached parent kicks an async fetch. The hook
        # fires only AFTER the worker delivers, with children populated.
        def get_children(pid, *, reload=False):
            if pid == 'p':
                return [Item(id='c1'), Item(id='c2')]
            return []

        fired = []
        b = make_browser(
            get_children=get_children,
            on_children_loaded=lambda ctx, pids: fired.append(
                {pid: [it.id for it in (ctx.cached_children(pid) or [])]
                 for pid in pids}))
        try:
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            # Before the worker delivers nothing has settled.
            b._fire_children_loaded_if_pending()
            self.assertEqual(fired, [])
            # Let the worker deliver, then drain the fire.
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            self.assertEqual(fired, [{'p': ['c1', 'c2']}])
        finally:
            b.stop_workers()

    def test_cached_expand_does_not_fire(self):
        # An already-cached expand runs no fetch → no on_children_loaded
        # (the cached case is served by on_expand + ctx.cached_children).
        fired = []
        ex = []
        b = make_browser(
            on_expand=lambda ctx, ids: ex.append(list(ids)),
            on_children_loaded=lambda ctx, pids: fired.append(list(pids)))
        try:
            # Pre-cache p's children directly so expansion is a cache hit.
            b._state._children['p'] = [Item(id='c1')]
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            b.run_until_idle()
            b._fire_expand_collapse_if_pending()
            b._fire_children_loaded_if_pending()
            self.assertEqual(ex, [['p']])           # on_expand still fires
            self.assertEqual(fired, [])             # but not children_loaded
        finally:
            b.stop_workers()

    def test_empty_result_fires_with_empty_children(self):
        # get_children returning [] settles → fires with cached == [].
        seen = []
        b = make_browser(
            get_children=lambda pid, *, reload=False: [],
            on_children_loaded=lambda ctx, pids: seen.append(
                {pid: ctx.cached_children(pid) for pid in pids}))
        try:
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            self.assertEqual(seen, [{'p': []}])
        finally:
            b.stop_workers()

    def test_none_result_does_not_fire(self):
        # Returning None posts no ``complete`` → loading stays True →
        # the hook never fires.
        fired = []
        b = make_browser(
            get_children=lambda pid, *, reload=False: None,
            on_children_loaded=lambda ctx, pids: fired.append(list(pids)))
        try:
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            self.assertEqual(fired, [])
            # Loading is still flagged True (no settlement happened).
            self.assertTrue(b._state._loading.get('p'))
        finally:
            b.stop_workers()

    def test_error_fires_once_with_empty_children(self):
        # A raising get_children is caught at the worker boundary; an
        # empty delivery is synthesised (loading clears) → fires with [].
        seen = []
        b = make_browser(
            get_children=self._raiser,
            on_children_loaded=lambda ctx, pids: seen.append(
                {pid: ctx.cached_children(pid) for pid in pids}))
        try:
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            self.assertEqual(seen, [{'p': []}])
            self.assertIn('get_children', '\n'.join(b._log))
        finally:
            b.stop_workers()

    @staticmethod
    def _raiser(pid, *, reload=False):
        raise RuntimeError('kaboom')

    def test_generator_fires_once_at_completion(self):
        # A generator get_children yields in chunks (no per-chunk
        # ``complete``); only the trailing StopIteration ``complete``
        # settles → exactly ONE fire, not one per yield.
        def get_children(pid, *, reload=False):
            yield [Item(id='c1')]
            yield [Item(id='c2'), Item(id='c3')]

        fired = []
        b = make_browser(
            get_children=get_children,
            on_children_loaded=lambda ctx, pids: fired.append(
                {pid: [it.id for it in (ctx.cached_children(pid) or [])]
                 for pid in pids}))
        try:
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0], {'p': ['c1', 'c2', 'c3']})
        finally:
            b.stop_workers()

    def test_refresh_over_several_expanded_parents_batches(self):
        # ctx.refresh() (no id) invalidates every cached parent and
        # re-fetches each. The settlements that land in one drain batch
        # into a single on_children_loaded call.
        def get_children(pid, *, reload=False):
            if pid is None or pid == '/':
                return [Item(id='a', has_children=True),
                        Item(id='b', has_children=True)]
            if pid == 'a':
                return [Item(id='a1')]
            if pid == 'b':
                return [Item(id='b1')]
            return []

        fired = []
        b = make_browser(
            get_children=get_children, root_id='/',
            on_children_loaded=lambda ctx, pids: fired.append(set(pids)))
        try:
            b.refresh('/')
            b.run_until_idle()
            b.expand('a')
            b.expand('b')
            b.run_until_idle()
            # Drain the expand-time settlements out of the way.
            b._fire_children_loaded_if_pending()
            fired.clear()

            # Full refresh re-fetches /, a, b.
            b.refresh()
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            # Every parent that settled is delivered; flattening the
            # per-drain batches must cover all three.
            settled = set().union(*fired) if fired else set()
            self.assertEqual(settled, {'/', 'a', 'b'})
        finally:
            b.stop_workers()

    def test_collapse_before_delivery_still_fires(self):
        # Collapsing a node before its in-flight fetch lands does not
        # cancel the fetch — the settlement still fires on_children_loaded
        # (pinned as documented).
        delivered = threading.Event()

        def get_children(pid, *, reload=False):
            if pid == 'p':
                return [Item(id='c1')]
            return []

        fired = []
        b = make_browser(
            get_children=get_children,
            on_children_loaded=lambda ctx, pids: fired.append(list(pids)))
        try:
            b.update_data([('upsert', 'p', None, {'has_children': True})])
            b.drain_main_queue()
            b.expand('p')
            # Collapse immediately (before draining the worker delivery).
            b._state.expanded.discard('p')
            b.run_until_idle()
            b._fire_children_loaded_if_pending()
            self.assertEqual(fired, [['p']])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
