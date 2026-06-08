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
_state.expand_path_rows = _data.expand_path_rows
_state.notify_wake = _term.notify_wake

Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
State = _state.State
Item = _data.Item
cache_invalidate_subtree = _state.cache_invalidate_subtree
cache_invalidate_all = _state.cache_invalidate_all
apply_ops = _state.apply_ops
KEEP_PARENT = _state.KEEP_PARENT
_index_set = _state._index_set


class TestStateIndexDefaults(unittest.TestCase):
    """Fresh State / Browser has empty indexes."""

    def test_state_defaults(self):
        s = State()
        self.assertEqual(s._items_by_id, {})
        self.assertEqual(s._parent_of_id, {})
        self.assertEqual(s._loading, {})

    def test_browser_state_defaults(self):
        b = Browser(BrowserConfig(_headless=True))
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
        b = Browser(BrowserConfig(_headless=True))
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
        b = Browser(BrowserConfig(_headless=True))
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
        b = Browser(BrowserConfig(_headless=True))
        # Simulate a dispatch having set the flag.
        b._state._loading['p'] = True
        b.set_children('p', [{'id': 'x'}])
        b.apply_children_results()
        self.assertFalse(b._state._loading['p'])


class TestColWidthCacheInvalidation(unittest.TestCase):
    """The per-parent ``max_col_width`` cache (``_col_width_cache``) is
    dropped in lockstep with the ``_children`` mutation sites — end-to-end
    through the real ``Browser`` for the refresh and update_data paths.

    These tests prime ``_col_width_cache`` directly (simulating a prior
    render's memo) rather than through ``RowContext.max_col_width`` — the
    measurement lives in 050-render, not loaded here; what's under test is
    that the state-level mutation paths evict the entry.
    """

    def test_fresh_state_and_browser_have_empty_cache(self):
        self.assertEqual(State()._col_width_cache, {})
        self.assertEqual(
            Browser(BrowserConfig(_headless=True))._state._col_width_cache, {})

    def test_refresh_delivery_drops_entry(self):
        # Worker delivery (``apply_children_results``) replaces a parent's
        # child list via ``_index_drop_children`` -> the column-width entry
        # for that parent is evicted.
        b = Browser(BrowserConfig(_headless=True))
        b.set_children('p', [{'id': 'x'}, {'id': 'y'}])
        b.apply_children_results()
        b._state._col_width_cache['p'] = {'col': 7}   # prime (as a render would)
        # Re-deliver under the same parent (a refresh / re-fetch).
        b.set_children('p', [{'id': 'z'}])
        b.apply_children_results()
        self.assertNotIn('p', b._state._col_width_cache)

    def test_update_data_upsert_drops_entry(self):
        b = Browser(BrowserConfig(_headless=True))
        b.set_children('p', [{'id': 'x'}])
        b.apply_children_results()
        b._state._col_width_cache['p'] = {'col': 3}
        apply_ops(b._state, [('upsert', 'w', 'p', {'col': 'wider-value'})])
        self.assertNotIn('p', b._state._col_width_cache)

    def test_update_data_mod_drops_entry(self):
        b = Browser(BrowserConfig(_headless=True))
        b.set_children('p', [{'id': 'x', 'col': 'a'}])
        b.apply_children_results()
        b._state._col_width_cache['p'] = {'col': 1}
        apply_ops(b._state, [('mod', 'x', KEEP_PARENT, {'col': 'longer'})])
        self.assertNotIn('p', b._state._col_width_cache)

    def test_cache_invalidate_subtree_drops_entry(self):
        s = State(root_id='/')
        s._children['/'] = [Item(id='a')]
        s._parent_of_id = {'a': '/'}
        s._col_width_cache['/'] = {'col': 9}
        cache_invalidate_subtree(s, '/')
        self.assertNotIn('/', s._col_width_cache)

    def test_cache_invalidate_all_clears_cache(self):
        s = State(root_id='/')
        s._col_width_cache = {'/': {'col': 9}, 'a': {'col': 2}}
        cache_invalidate_all(s)
        self.assertEqual(s._col_width_cache, {})


class TestDispatchSetsLoading(unittest.TestCase):
    """Dispatch paths flip ``_loading[parent] = True`` synchronously."""

    def test_do_refresh_sets_loading(self):
        b = Browser(BrowserConfig(_headless=True))
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
        b = Browser(BrowserConfig(_headless=True))
        Pending = _state.Pending
        p1 = Pending()
        p2 = Pending()
        b._do_refresh('/', p1)
        b._do_refresh('/', p2)
        self.assertTrue(b._state._loading['/'])

    def test_do_expand_sets_loading_when_uncached(self):
        b = Browser(BrowserConfig(_headless=True))
        Pending = _state.Pending
        p = Pending()
        b._do_expand('child', p)
        self.assertTrue(b._state._loading['child'])
        self.assertIn('child', b._state._children_pending)

    def test_do_expand_does_not_touch_loading_when_cached(self):
        # If children are already cached, no fetch is dispatched and
        # _loading is untouched (it'll be False from the prior delivery).
        b = Browser(BrowserConfig(_headless=True))
        b._state._children['x'] = [Item(id='c')]
        b._state._loading['x'] = False
        Pending = _state.Pending
        p = Pending()
        b._do_expand('x', p)
        self.assertFalse(b._state._loading['x'])

    def test_update_children_for_cursor_writes_prefetch_slot(self):
        # Post-#481: cursor-driven children fetch goes through the
        # latest-wins prefetch slot rather than the FIFO. The slot
        # write happens here; the worker later picks it up and (when
        # it commits to fetching) adds the id to ``_children_pending``.
        b = Browser(BrowserConfig(show_children_pane=True, _headless=True))
        item = Item(id='parent', has_children=True)
        b._state._children[None] = [item]
        b._state._items_by_id['parent'] = item
        b._state._parent_of_id['parent'] = None
        b._state._visible_dirty = True
        _ = _state.visible_items(b._state)
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertEqual(b._children_prefetch_req, 'parent')
        # ``_loading`` and ``_children_pending`` are no longer set at
        # slot-write time — the worker owns them when it commits.
        self.assertNotIn('parent', b._state._loading)
        self.assertNotIn('parent', b._state._children_pending)


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


class TestFromFlatTreePathSep(unittest.TestCase):
    """``path_sep`` builds the synthesised tree (no parent/depth column)."""

    @staticmethod
    def _visible(b):
        """Visible (id, has_children, depth) tuples, all nodes expanded."""
        s = b._state
        # Expand every cached parent so the whole tree renders inline.
        s.expanded = set(s._items_by_id)
        s._visible_dirty = True
        return [
            (e.item.id, e.item.has_children, e.depth)
            for e in _state.visible_items(s)
        ]

    def test_builds_synthesised_tree_with_has_children_markers(self):
        rows = [
            'docs/api/auth.md',
            'docs/api/users.md',
            'docs/README.md',
            '/etc/passwd',
        ]
        b = Browser.from_flat_tree(rows, path_sep='/', _headless=True)
        s = b._state
        # Intermediate prefixes are synthesised as real nodes.
        self.assertEqual(
            set(s._items_by_id),
            {
                'docs', 'docs/api', 'docs/api/auth.md', 'docs/api/users.md',
                'docs/README.md', '/etc', '/etc/passwd',
            },
        )
        # Parent links follow the prefix structure; top level -> root (None).
        self.assertIsNone(s._parent_of_id['docs'])
        self.assertEqual(s._parent_of_id['docs/api'], 'docs')
        self.assertEqual(s._parent_of_id['docs/api/auth.md'], 'docs/api')
        self.assertEqual(s._parent_of_id['docs/README.md'], 'docs')
        self.assertIsNone(s._parent_of_id['/etc'])
        self.assertEqual(s._parent_of_id['/etc/passwd'], '/etc')
        # has_children is derived from structure: only the prefix nodes
        # that actually parent something are expandable.
        expandable = {i for i, hc in
                      ((it.id, it.has_children)
                       for it in s._items_by_id.values()) if hc}
        self.assertEqual(expandable, {'docs', 'docs/api', '/etc'})

    def test_visible_items_renders_tree_without_runtime_fetch(self):
        # A get_children that raises proves nothing is fetched at runtime:
        # the eager cache must satisfy every read visible_items performs.
        def _boom(pid, *, reload=False):
            raise AssertionError(f'runtime fetch for {pid!r}')

        rows = ['docs/api/auth.md', 'docs/README.md']
        b = Browser.from_flat_tree(
            rows, path_sep='/', get_children=_boom, _headless=True,
        )
        # No parent is left in a loading state (nothing pending to fetch).
        self.assertTrue(all(v is False for v in b._state._loading.values()))
        visible = self._visible(b)
        # Full expanded tree renders straight from the pre-populated cache.
        self.assertEqual(
            visible,
            [
                ('docs', True, 0),
                ('docs/api', True, 1),
                ('docs/api/auth.md', False, 2),
                ('docs/README.md', False, 1),
            ],
        )

    def test_title_is_last_segment(self):
        b = Browser.from_flat_tree(
            ['docs/api/auth.md'], path_sep='/', _headless=True,
        )
        s = b._state
        self.assertEqual(s._items_by_id['docs'].title, 'docs')
        self.assertEqual(s._items_by_id['docs/api'].title, 'api')
        self.assertEqual(s._items_by_id['docs/api/auth.md'].title, 'auth.md')

    def test_multi_char_separator(self):
        rows = ['os::path::join', 'os::path::split']
        b = Browser.from_flat_tree(rows, path_sep='::', _headless=True)
        s = b._state
        self.assertEqual(
            set(s._items_by_id),
            {'os', 'os::path', 'os::path::join', 'os::path::split'},
        )
        self.assertTrue(s._items_by_id['os'].has_children)
        self.assertTrue(s._items_by_id['os::path'].has_children)
        self.assertFalse(s._items_by_id['os::path::join'].has_children)

    def test_carried_has_children_on_leaf_is_overridden(self):
        # ``has_children`` is a known Item field, so a dict row carrying
        # it flows through ``to_item``. In path mode the flag is DERIVED
        # from the tree and the incoming value ignored: a childless leaf
        # ends up False (no phantom ▶), while the synthesised prefix is
        # True because it actually parents the leaf.
        rows = [{'id': 'docs/a.md', 'has_children': True}]
        b = Browser.from_flat_tree(rows, path_sep='/', _headless=True)
        s = b._state
        self.assertFalse(s._items_by_id['docs/a.md'].has_children)
        self.assertTrue(s._items_by_id['docs'].has_children)


class TestFromFlatTreePathSepPrecedence(unittest.TestCase):
    """``parent`` > ``depth`` > ``path_sep`` > flat."""

    def test_parent_column_beats_path_sep(self):
        # Ids contain the separator but a parent column is present, so
        # parent-pointer mode wins: ids are NOT split into prefix nodes.
        rows = [
            {'id': 'a/b'},
            {'id': 'a/b/c', 'parent': 'a/b'},
        ]
        b = Browser.from_flat_tree(rows, path_sep='/', _headless=True)
        s = b._state
        # Only the two literal ids exist — no synthesised 'a' prefix.
        self.assertEqual(set(s._items_by_id), {'a/b', 'a/b/c'})
        self.assertIsNone(s._parent_of_id['a/b'])
        self.assertEqual(s._parent_of_id['a/b/c'], 'a/b')

    def test_depth_column_beats_path_sep(self):
        rows = [
            {'id': 'a/b', 'depth': 0},
            {'id': 'a/b/c', 'depth': 1},
        ]
        b = Browser.from_flat_tree(rows, path_sep='/', _headless=True)
        s = b._state
        # Depth-coded grouping, ids untouched: no synthesised 'a' prefix.
        self.assertEqual(set(s._items_by_id), {'a/b', 'a/b/c'})
        self.assertIsNone(s._parent_of_id['a/b'])
        self.assertEqual(s._parent_of_id['a/b/c'], 'a/b')

    def test_path_sep_beats_flat(self):
        # No parent/depth column: path_sep splits, vs. the flat fallback
        # which would put both ids directly under root.
        rows = ['a/b', 'a/b/c']
        b = Browser.from_flat_tree(rows, path_sep='/', _headless=True)
        s = b._state
        # Synthesised prefix node 'a' appears; tree is nested.
        self.assertEqual(set(s._items_by_id), {'a', 'a/b', 'a/b/c'})
        self.assertIsNone(s._parent_of_id['a'])
        self.assertEqual(s._parent_of_id['a/b'], 'a')
        self.assertEqual(s._parent_of_id['a/b/c'], 'a/b')

    def test_no_path_sep_stays_flat(self):
        # Without path_sep, slash-bearing ids stay flat under root.
        rows = ['a/b', 'a/b/c']
        b = Browser.from_flat_tree(rows, _headless=True)
        s = b._state
        self.assertEqual(set(s._items_by_id), {'a/b', 'a/b/c'})
        self.assertIsNone(s._parent_of_id['a/b'])
        self.assertIsNone(s._parent_of_id['a/b/c'])


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

        def get_children(parent_id, *, reload=False):
            return [Item(id=f'{parent_id}/a'), Item(id=f'{parent_id}/b')]

        b = Browser(BrowserConfig(get_children=get_children, _headless=True))
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

        def get_children(parent_id, *, reload=False):
            call['n'] += 1
            if call['n'] == 1:
                return [Item(id='old-a'), Item(id='old-b')]
            return [Item(id='new-c')]

        b = Browser(BrowserConfig(get_children=get_children, _headless=True))
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


class TestIndexSetHashability(unittest.TestCase):
    """``_index_set`` raises a clear error on an unhashable id (spec 5.2).

    The forward-index write for child delivery goes through ``_index_set``,
    where a recipe child id is first hashed. A ``list`` / ``dict`` / ``set``
    id must surface a message that names the offender, not a bare
    ``unhashable type`` from deep in framework internals.
    """

    def test_hashable_id_writes_through(self):
        s = State()
        it = Item(id=('msg', '/p.jsonl', 7))   # a structured (tuple) id
        _index_set(s, it)
        self.assertIs(s._items_by_id[('msg', '/p.jsonl', 7)], it)

    def test_unhashable_list_id_raises_clear_error(self):
        s = State()
        it = Item(id=['not', 'hashable'])
        with self.assertRaises(TypeError) as cm:
            _index_set(s, it)
        msg = str(cm.exception)
        self.assertIn('Item.id must be hashable', msg)
        self.assertIn("['not', 'hashable']", msg)   # names the value
        self.assertIn('list', msg)                  # names the type

    def test_unhashable_id_via_child_ingestion_raises(self):
        # The recipe child-ingestion write goes through ``_index_set`` (via
        # ``_index_add_children``), so a recipe that hands in an unhashable
        # id gets the clear, offender-naming error at the write site.
        s = State()
        _index_add_children = _state._index_add_children
        with self.assertRaises(TypeError) as cm:
            _index_add_children(s, 'p', [Item(id=['bad'])])
        self.assertIn('Item.id must be hashable', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
