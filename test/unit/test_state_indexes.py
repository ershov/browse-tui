"""Tests for the state-level indexes added in ticket #267.

Three new fields on ``State`` are maintained alongside ``_children``:

* ``_items_by_id``  — primary id -> Item lookup.
* ``_parent_of_id`` — child-id -> parent-id reverse lookup.
* ``_loading``      — explicit loading flag per parent.

These exist as foundations for the upcoming ``update_data`` push API
(see ``docs/superpowers/specs/2026-05-08-streaming-push-api-design.md``).
This ticket is purely additive — no public API consumes the indexes
yet — but every existing mutation site of ``_children`` must keep
them coherent. These tests pin each path:

* Default state: indexes start empty, ``_loading`` empty.
* ``cache_invalidate_subtree`` and ``cache_invalidate_all`` drop entries.
* ``apply_children_results`` populates entries and clears ``_loading``.
* ``_do_refresh`` / ``_do_expand`` / ``_update_children_for_cursor``
  set ``_loading[parent] = True`` at dispatch.
* ``from_flat_tree`` pre-populates indexes for the eager case.
* End-to-end run through worker delivery keeps indexes in lockstep.
"""

import threading
import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Mirror the wiring that other state-touching tests do: production
# builds concatenate the source files; the loader needs Item/to_item/
# notify_wake injected explicitly.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

Browser = _state.Browser
State = _state.State
Item = _data.Item
cache_invalidate_subtree = _state.cache_invalidate_subtree
cache_invalidate_all = _state.cache_invalidate_all


class TestStateIndexDefaults(unittest.TestCase):
    """Fresh State / Browser has empty indexes."""

    def test_state_defaults(self):
        s = State()
        self.assertEqual(s._items_by_id, {})
        self.assertEqual(s._parent_of_id, {})
        self.assertEqual(s._loading, {})

    def test_browser_state_defaults(self):
        b = Browser(_headless=True)
        self.assertEqual(b._state._items_by_id, {})
        self.assertEqual(b._state._parent_of_id, {})
        self.assertEqual(b._state._loading, {})


class TestCacheInvalidateSubtree(unittest.TestCase):
    """``cache_invalidate_subtree`` drops indexes alongside ``_children``."""

    def _populate(self):
        s = State(root_id='/')
        a = Item(id='a')
        b = Item(id='b')
        c = Item(id='c')
        s._children['/'] = [a, b]
        s._children['a'] = [c]
        s._items_by_id = {'a': a, 'b': b, 'c': c}
        s._parent_of_id = {'a': '/', 'b': '/', 'c': 'a'}
        s._loading = {'/': False, 'a': False}
        return s, a, b, c

    def test_drops_children_entry(self):
        s, *_ = self._populate()
        cache_invalidate_subtree(s, '/')
        self.assertNotIn('/', s._children)

    def test_drops_items_by_id_for_children(self):
        s, *_ = self._populate()
        cache_invalidate_subtree(s, '/')
        self.assertNotIn('a', s._items_by_id)
        self.assertNotIn('b', s._items_by_id)
        # 'c' is a child of 'a', not '/', so it stays.
        self.assertIn('c', s._items_by_id)

    def test_drops_parent_of_id_for_children(self):
        s, *_ = self._populate()
        cache_invalidate_subtree(s, '/')
        self.assertNotIn('a', s._parent_of_id)
        self.assertNotIn('b', s._parent_of_id)
        self.assertEqual(s._parent_of_id.get('c'), 'a')

    def test_drops_loading_flag(self):
        s, *_ = self._populate()
        cache_invalidate_subtree(s, '/')
        self.assertNotIn('/', s._loading)
        self.assertIn('a', s._loading)

    def test_unknown_id_is_noop(self):
        s, *_ = self._populate()
        before_items = dict(s._items_by_id)
        before_parents = dict(s._parent_of_id)
        before_loading = dict(s._loading)
        cache_invalidate_subtree(s, 'never-cached')
        self.assertEqual(s._items_by_id, before_items)
        self.assertEqual(s._parent_of_id, before_parents)
        self.assertEqual(s._loading, before_loading)

    def test_marks_visible_dirty(self):
        s = State()
        s._visible_dirty = False
        cache_invalidate_subtree(s, 'whatever')
        self.assertTrue(s._visible_dirty)


class TestCacheInvalidateAll(unittest.TestCase):
    """``cache_invalidate_all`` empties everything in lockstep."""

    def test_clears_all_indexes(self):
        s = State()
        a, b, c = Item(id='a'), Item(id='b'), Item(id='c')
        s._children = {'/': [a, b], 'a': [c]}
        s._items_by_id = {'a': a, 'b': b, 'c': c}
        s._parent_of_id = {'a': '/', 'b': '/', 'c': 'a'}
        s._loading = {'/': False, 'a': True}

        cache_invalidate_all(s)

        self.assertEqual(s._children, {})
        self.assertEqual(s._items_by_id, {})
        self.assertEqual(s._parent_of_id, {})
        self.assertEqual(s._loading, {})
        self.assertTrue(s._visible_dirty)


class TestApplyChildrenResultsIndexes(unittest.TestCase):
    """``apply_children_results`` populates indexes and clears ``_loading``."""

    def test_first_delivery_populates(self):
        b = Browser(_headless=True)
        b.set_children('p', [{'id': 'x'}, {'id': 'y'}])
        b.apply_children_results()
        s = b._state
        self.assertIn('x', s._items_by_id)
        self.assertIn('y', s._items_by_id)
        self.assertEqual(s._parent_of_id['x'], 'p')
        self.assertEqual(s._parent_of_id['y'], 'p')
        # set_children doesn't go through dispatch — the apply path
        # still records "no longer loading" once data arrives.
        self.assertFalse(s._loading['p'])

    def test_replacement_drops_previous_entries(self):
        b = Browser(_headless=True)
        b.set_children('p', [{'id': 'x'}, {'id': 'y'}])
        b.apply_children_results()
        # Now replace with a different set.
        b.set_children('p', [{'id': 'z'}])
        b.apply_children_results()
        s = b._state
        self.assertEqual([k.id for k in s._children['p']], ['z'])
        self.assertNotIn('x', s._items_by_id)
        self.assertNotIn('y', s._items_by_id)
        self.assertIn('z', s._items_by_id)
        self.assertNotIn('x', s._parent_of_id)
        self.assertNotIn('y', s._parent_of_id)
        self.assertEqual(s._parent_of_id['z'], 'p')

    def test_clears_loading_flag(self):
        b = Browser(_headless=True)
        # Simulate a dispatch having set the flag.
        b._state._loading['p'] = True
        b.set_children('p', [{'id': 'x'}])
        b.apply_children_results()
        self.assertFalse(b._state._loading['p'])


class TestDispatchSetsLoading(unittest.TestCase):
    """Dispatch paths flip ``_loading[parent] = True`` synchronously."""

    def test_do_refresh_sets_loading(self):
        b = Browser(_headless=True)
        # Use the synchronous internal entry — no worker needed.
        Pending = _state.Pending
        p = Pending()
        b._do_refresh('/', p)
        self.assertTrue(b._state._loading['/'])
        # And the dispatch tracker matches.
        self.assertIn('/', b._state._children_pending)

    def test_do_refresh_coalesces_does_not_double_flag(self):
        # Second call while the first is still in flight is a piggyback;
        # the flag stays True (not toggled off and on).
        b = Browser(_headless=True)
        Pending = _state.Pending
        p1 = Pending()
        p2 = Pending()
        b._do_refresh('/', p1)
        b._do_refresh('/', p2)
        self.assertTrue(b._state._loading['/'])

    def test_do_expand_sets_loading_when_uncached(self):
        b = Browser(_headless=True)
        Pending = _state.Pending
        p = Pending()
        b._do_expand('child', p)
        self.assertTrue(b._state._loading['child'])
        self.assertIn('child', b._state._children_pending)

    def test_do_expand_does_not_touch_loading_when_cached(self):
        # If children are already cached, no fetch is dispatched and
        # _loading is untouched (it'll be False from the prior delivery).
        b = Browser(_headless=True)
        b._state._children['x'] = [Item(id='c')]
        b._state._loading['x'] = False
        Pending = _state.Pending
        p = Pending()
        b._do_expand('x', p)
        self.assertFalse(b._state._loading['x'])

    def test_update_children_for_cursor_sets_loading(self):
        b = Browser(show_children_pane=True, _headless=True)
        # Seed: cursor on a row with children, but children not yet cached.
        item = Item(id='parent', has_children=True)
        b._state._children[None] = [item]
        b._state._items_by_id['parent'] = item
        b._state._parent_of_id['parent'] = None
        b._state._visible_dirty = True
        # Visible cache rebuild moves cursor=0 onto our parent row.
        _ = _state.visible_items(b._state)
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertTrue(b._state._loading['parent'])
        self.assertIn('parent', b._state._children_pending)


class TestFromFlatTreeIndexes(unittest.TestCase):
    """``Browser.from_flat_tree`` pre-populates indexes for the eager case."""

    def test_flat_rows_index_under_root(self):
        b = Browser.from_flat_tree(
            [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}], _headless=True,
        )
        s = b._state
        self.assertEqual(set(s._items_by_id), {'a', 'b', 'c'})
        for cid in ('a', 'b', 'c'):
            self.assertEqual(s._parent_of_id[cid], None)
            self.assertIs(s._items_by_id[cid], s._children[None][
                ['a', 'b', 'c'].index(cid)
            ])

    def test_parent_pointer_mode_indexes_correctly(self):
        rows = [
            {'id': 'p'},
            {'id': 'a', 'parent': 'p'},
            {'id': 'b', 'parent': 'p'},
        ]
        b = Browser.from_flat_tree(rows, _headless=True)
        s = b._state
        self.assertEqual(s._parent_of_id['p'], None)
        self.assertEqual(s._parent_of_id['a'], 'p')
        self.assertEqual(s._parent_of_id['b'], 'p')
        self.assertIs(s._items_by_id['a'], s._children['p'][0])

    def test_depth_mode_indexes_correctly(self):
        rows = [
            {'id': 'p', 'depth': 0},
            {'id': 'a', 'depth': 1},
            {'id': 'b', 'depth': 1},
        ]
        b = Browser.from_flat_tree(rows, _headless=True)
        s = b._state
        self.assertEqual(s._parent_of_id['p'], None)
        self.assertEqual(s._parent_of_id['a'], 'p')
        self.assertEqual(s._parent_of_id['b'], 'p')

    def test_loading_flag_set_to_false_for_each_parent(self):
        rows = [
            {'id': 'p'},
            {'id': 'a', 'parent': 'p'},
        ]
        b = Browser.from_flat_tree(rows, _headless=True)
        s = b._state
        # Parents that have populated children lists are not loading.
        for parent_id in s._children.keys():
            self.assertIs(s._loading[parent_id], False)


class TestRefreshInvalidationDropsIndexes(unittest.TestCase):
    """End-to-end: ``refresh`` invalidates indexes alongside ``_children``."""

    def test_refresh_subtree_drops_indexes_then_repopulates(self):
        # Seed via from_flat_tree so indexes start populated.
        b = Browser.from_flat_tree(
            [{'id': 'a'}, {'id': 'b'}], root_id='/', _headless=True,
        )
        s = b._state
        self.assertIn('a', s._items_by_id)

        # Refresh '/' through the synchronous dispatch path.
        Pending = _state.Pending
        p = Pending()
        b._do_refresh('/', p)
        # After dispatch (cache invalidated, fetch enqueued):
        # indexes for the dropped subtree are gone.
        self.assertNotIn('a', s._items_by_id)
        self.assertNotIn('b', s._items_by_id)
        self.assertNotIn('a', s._parent_of_id)
        self.assertNotIn('b', s._parent_of_id)
        # Loading flag is now set for the in-flight fetch.
        self.assertTrue(s._loading['/'])

    def test_full_refresh_clears_everything(self):
        b = Browser.from_flat_tree(
            [{'id': 'a'}, {'id': 'b'}], root_id='/', _headless=True,
        )
        s = b._state
        Pending = _state.Pending
        p = Pending()
        # Full refresh: id=None re-routes to root_id internally.
        b._do_refresh(None, p)
        self.assertEqual(s._items_by_id, {})
        self.assertEqual(s._parent_of_id, {})
        # And the new fetch is in flight for root_id.
        self.assertTrue(s._loading['/'])


class TestEndToEndDispatchAndDelivery(unittest.TestCase):
    """Full lockstep: dispatch -> worker delivery -> indexes coherent.

    Drives the children worker and runs ``apply_children_results``
    on the main thread to confirm that:

    * Dispatch sets ``_loading[parent] = True``.
    * Delivery clears ``_loading[parent]`` to False.
    * Delivery populates ``_items_by_id`` / ``_parent_of_id``.
    * ``_children`` and the indexes stay in lockstep across the round-trip.
    """

    def test_dispatch_then_delivery(self):
        results_seen = threading.Event()

        def get_children(parent_id):
            return [Item(id=f'{parent_id}/a'), Item(id=f'{parent_id}/b')]

        b = Browser(get_children=get_children, _headless=True)
        b.start_workers()
        try:
            pending = b.refresh('P')
            # refresh() posts work to the main queue; drain it so
            # dispatch actually runs and the worker gets enqueued.
            b.drain_main_queue()
            # Now _loading['P'] is True from the dispatch path.
            self.assertTrue(b._state._loading['P'])

            b.run_until_idle()
            self.assertTrue(pending.done)
        finally:
            b.stop_workers()

        s = b._state
        # Worker delivered: cache and indexes both populated.
        self.assertEqual([k.id for k in s._children['P']], ['P/a', 'P/b'])
        self.assertIn('P/a', s._items_by_id)
        self.assertIn('P/b', s._items_by_id)
        self.assertEqual(s._parent_of_id['P/a'], 'P')
        self.assertEqual(s._parent_of_id['P/b'], 'P')
        # Loading cleared.
        self.assertFalse(s._loading['P'])

    def test_refresh_swap_drops_old_indexes(self):
        """A refresh that yields a different child set evicts old index rows."""
        call = {'n': 0}

        def get_children(parent_id):
            call['n'] += 1
            if call['n'] == 1:
                return [Item(id='old-a'), Item(id='old-b')]
            return [Item(id='new-c')]

        b = Browser(get_children=get_children, _headless=True)
        b.start_workers()
        try:
            b.refresh('P')
            b.run_until_idle()
            s = b._state
            self.assertIn('old-a', s._items_by_id)

            b.refresh('P')
            b.run_until_idle()
            self.assertNotIn('old-a', s._items_by_id)
            self.assertNotIn('old-b', s._items_by_id)
            self.assertNotIn('old-a', s._parent_of_id)
            self.assertIn('new-c', s._items_by_id)
            self.assertEqual(s._parent_of_id['new-c'], 'P')
            self.assertFalse(s._loading['P'])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
