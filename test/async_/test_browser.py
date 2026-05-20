"""Browser-engine tests: construction kwargs, thread-safe public ops,
``from_flat_tree`` adapter.

These tests exercise the public surface added in ticket #8: the full
``__init__`` signature, the new public ops (``cursor_to``, ``expand``,
``select``, ``quit``, ``watch``), and the ``from_flat_tree`` classmethod
that pre-populates the children cache from a flat list.

Render layer, default actions, and the main loop are NOT exercised here
(they belong to tickets #10/#12/#13). We only verify that the engine
stores the kwargs, queues the ops correctly, and that the cache is
populated as expected.
"""

import threading
import time
import unittest

from test.async_._helpers import (
    Browser, BrowserConfig, Item, Pending, State, make_browser
)


# ---- 1. Construction defaults / kwargs -------------------------------------


class TestConstructionDefaults(unittest.TestCase):
    """Exhaustively poke the new __init__ signature."""

    def test_no_kwargs_defaults(self):
        b = Browser(BrowserConfig(_headless=True))
        try:
            self.assertEqual(b.title, 'browse-tui')
            self.assertIsNone(b._state.root_id)
            self.assertEqual(b._state.selected, set())
            self.assertEqual(b._state.cursor, 0)
            self.assertTrue(b.show_preview)
            self.assertTrue(b.show_children_pane)
            self.assertTrue(b.multi_select)
            self.assertEqual(b.print_format, '{id}')
            self.assertIsNone(b.on_enter)
            self.assertIsNone(b.format_item)
            self.assertEqual(b.actions, [])
            # quit fields default
            self.assertFalse(b._quit_requested)
            self.assertEqual(b._quit_code, 0)
            self.assertEqual(b._quit_output, '')
        finally:
            b.stop_workers()

    def test_initial_scope_pushed(self):
        b = Browser(BrowserConfig(
            _headless=True,
            title='foo',
            get_children=lambda _id, *, reload=False: [],
            root_id='/',
            initial_scope='/x',
        ))
        try:
            self.assertEqual(b.title, 'foo')
            self.assertEqual(b._state.root_id, '/')
            self.assertEqual(b._state.scope_stack, ['/x'])
        finally:
            b.stop_workers()

    def test_actions_stored_opaquely(self):
        # Action class doesn't exist yet; we just want the kwarg to be
        # accepted and stored.
        sentinel = object()
        b = Browser(BrowserConfig(_headless=True, actions=[sentinel]))
        try:
            self.assertEqual(b.actions, [sentinel])
        finally:
            b.stop_workers()

    def test_callable_kwargs_stored(self):
        fmt = lambda item, ctx: [(str(item.id), '', False)]
        on_enter = lambda ctx: None
        b = Browser(BrowserConfig(_headless=True, format_item=fmt, on_enter=on_enter))
        try:
            self.assertIs(b.format_item, fmt)
            self.assertIs(b.on_enter, on_enter)
        finally:
            b.stop_workers()

    def test_show_flags_can_be_disabled(self):
        b = Browser(BrowserConfig(
            _headless=True,
            show_preview=False,
            show_children_pane=False,
            multi_select=False,
            print_format='{title}',
        ))
        try:
            self.assertFalse(b.show_preview)
            self.assertFalse(b.show_children_pane)
            self.assertFalse(b.multi_select)
            self.assertEqual(b.print_format, '{title}')
        finally:
            b.stop_workers()


# ---- 2. cursor_to ----------------------------------------------------------


class TestCursorTo(unittest.TestCase):

    def _browser_with_root_children(self, ids):
        # One-level cache: root_id -> [Item, Item, ...]; no nested children.
        b = make_browser(get_children=lambda _id, *, reload=False: [(i,) for i in ids])
        b.refresh()  # populate root
        b.run_until_idle()
        return b

    def test_cursor_to_visible_id_resolves_and_sets_index(self):
        b = self._browser_with_root_children(['A', 'B', 'C'])
        try:
            p = b.cursor_to('B')
            self.assertIsInstance(p, Pending)
            b.run_until_idle()
            self.assertTrue(p.done)
            # Visible list has 3 entries; 'B' is at index 1.
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_cursor_to_nonexistent_resolves_best_effort(self):
        b = self._browser_with_root_children(['A', 'B'])
        try:
            b._state.cursor = 0  # baseline
            p = b.cursor_to('Z')
            b.run_until_idle()
            self.assertTrue(p.done)
            # Cursor should not have moved -- best-effort resolution.
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_cursor_to_with_on_complete_kwarg(self):
        b = self._browser_with_root_children(['A', 'B'])
        try:
            events = []
            b.cursor_to('A', on_complete=lambda: events.append('cb'))
            b.run_until_idle()
            self.assertEqual(events, ['cb'])
        finally:
            b.stop_workers()


# ---- 3. expand -------------------------------------------------------------


class TestExpand(unittest.TestCase):

    def test_expand_uncached_triggers_fetch_and_resolves(self):
        seen = []
        def gc(id_, *, reload=False):
            seen.append(id_)
            return [(f'{id_}/x',)]
        b = make_browser(get_children=gc)
        try:
            p = b.expand('A')
            self.assertIsInstance(p, Pending)
            self.assertFalse(p.done)
            b.run_until_idle()
            self.assertTrue(p.done)
            self.assertIn('A', b._state.expanded)
            self.assertIn('A', b._state._children)
            self.assertEqual(seen, ['A'])
        finally:
            b.stop_workers()

    def test_expand_already_cached_resolves_without_extra_fetch(self):
        seen = []
        def gc(id_, *, reload=False):
            seen.append(id_)
            return []
        b = make_browser(get_children=gc)
        try:
            # Pre-populate the cache so expand() takes the fast path.
            b._state._children['A'] = []
            p = b.expand('A')
            b.run_until_idle()
            self.assertTrue(p.done)
            self.assertIn('A', b._state.expanded)
            # No fetch happened -- get_children was never called.
            self.assertEqual(seen, [])
        finally:
            b.stop_workers()

    def test_expand_chain(self):
        events = []
        b = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            b.expand('A').then(
                lambda: b.expand('B').then(
                    lambda: events.append('done')))
            b.run_until_idle()
            self.assertEqual(events, ['done'])
            self.assertIn('A', b._state.expanded)
            self.assertIn('B', b._state.expanded)
        finally:
            b.stop_workers()


# ---- 4. select -------------------------------------------------------------


class TestSelect(unittest.TestCase):

    def test_select_adds_ids(self):
        b = make_browser()
        try:
            b.select(['a', 'b'])
            b.drain_main_queue()
            self.assertEqual(b._state.selected, {'a', 'b'})
        finally:
            b.stop_workers()

    def test_select_replace_clears_existing(self):
        b = make_browser()
        try:
            b.select(['a', 'b'])
            b.drain_main_queue()
            b.select(['c'], replace=True)
            b.drain_main_queue()
            self.assertEqual(b._state.selected, {'c'})
        finally:
            b.stop_workers()


# ---- 5. quit ---------------------------------------------------------------


class TestQuit(unittest.TestCase):

    def test_quit_sets_fields(self):
        b = make_browser()
        try:
            b.quit(code=2, output='bye')
            b.drain_main_queue()
            self.assertTrue(b._quit_requested)
            self.assertEqual(b._quit_code, 2)
            self.assertEqual(b._quit_output, 'bye')
        finally:
            b.stop_workers()


# ---- 6. watch --------------------------------------------------------------


class TestWatch(unittest.TestCase):

    def test_watch_calls_callback_repeatedly(self):
        b = make_browser()
        try:
            calls = []
            stop = threading.Event()
            def cb(browser):
                calls.append(1)
                if len(calls) >= 5:
                    stop.set()
            t = b.watch(cb, interval=0.01)
            self.assertTrue(t.daemon)
            stop.wait(timeout=1.0)
            self.assertGreaterEqual(len(calls), 2)
        finally:
            b.stop_workers()

    def test_watch_no_interval_runs_once(self):
        b = make_browser()
        try:
            calls = []
            def cb(browser):
                calls.append(1)
            t = b.watch(cb)  # interval=None -> single call
            t.join(timeout=1.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(calls, [1])
        finally:
            b.stop_workers()

    def test_watch_exception_surfaced_via_error(self):
        b = make_browser()
        try:
            def boom(browser):
                raise RuntimeError('watcher exploded')
            t = b.watch(boom)  # one-shot; exception kills the thread
            t.join(timeout=1.0)
            self.assertFalse(t.is_alive())
            # The error message is delivered to the main thread via post().
            b.drain_main_queue()
            self.assertIn('watcher', b.error_text)
            self.assertIn('RuntimeError', b.error_text)
            self.assertIn('exploded', b.error_text)
        finally:
            b.stop_workers()


# ---- 7. from_flat_tree -----------------------------------------------------


class TestFromFlatTree(unittest.TestCase):

    def test_parent_pointer_mode(self):
        rows = [
            {'id': 'r', 'has_children': True, 'parent': None},
            {'id': 'a', 'has_children': True, 'parent': 'r'},
            {'id': 'b', 'parent': 'r'},
            {'id': 'a/1', 'parent': 'a'},
        ]
        b = Browser.from_flat_tree(rows, _headless=True)
        try:
            # Only 'r' has parent=None -> root-level children is ['r'].
            self.assertEqual(
                [it.id for it in b._state._children[None]], ['r'])
            self.assertEqual(
                [it.id for it in b._state._children['r']], ['a', 'b'])
            self.assertEqual(
                [it.id for it in b._state._children['a']], ['a/1'])
        finally:
            b.stop_workers()

    def test_depth_coded_mode(self):
        rows = [
            {'id': 'r', 'has_children': True, 'depth': 0},
            {'id': 'a', 'has_children': True, 'depth': 1},
            {'id': 'a/1', 'depth': 2},
            {'id': 'b', 'depth': 1},
        ]
        b = Browser.from_flat_tree(rows, _headless=True)
        try:
            # root_id is None by default
            self.assertEqual(
                [it.id for it in b._state._children[None]], ['r'])
            self.assertEqual(
                [it.id for it in b._state._children['r']], ['a', 'b'])
            self.assertEqual(
                [it.id for it in b._state._children['a']], ['a/1'])
        finally:
            b.stop_workers()

    def test_depth_coded_and_parent_pointer_match(self):
        # Same logical tree expressed two different ways yields identical
        # cache structure (modulo iteration order, which the fixture
        # preserves on purpose).
        pp = [
            {'id': 'r', 'has_children': True},
            {'id': 'a', 'has_children': True, 'parent': 'r'},
            {'id': 'b', 'parent': 'r'},
        ]
        dc = [
            {'id': 'r', 'has_children': True, 'depth': 0},
            {'id': 'a', 'has_children': True, 'depth': 1},
            {'id': 'b', 'depth': 1},
        ]
        b1 = Browser.from_flat_tree(pp, _headless=True)
        b2 = Browser.from_flat_tree(dc, _headless=True)
        try:
            self.assertEqual(
                [it.id for it in b1._state._children['r']],
                [it.id for it in b2._state._children['r']],
            )
        finally:
            b1.stop_workers()
            b2.stop_workers()

    def test_mixed_input_shapes(self):
        rows = [
            Item(id='x', has_children=True),  # Item
            {'id': 'y', 'parent': 'x'},        # dict
            ('z', 'Title z', '', '', False),   # tuple (5-arg full)
        ]
        b = Browser.from_flat_tree(rows, _headless=True)
        try:
            # 'x' has parent attribute? No -- it's an Item with no .parent.
            # Hierarchy detection: dict 'y' has parent='x' -> parent-pointer
            # mode. So 'x' (no parent) and 'z' (no parent attr after
            # to_item) end up at root_id=None, 'y' under 'x'.
            self.assertEqual(
                [it.id for it in b._state._children[None]], ['x', 'z'])
            self.assertEqual(
                [it.id for it in b._state._children['x']], ['y'])
        finally:
            b.stop_workers()

    def test_empty_rows(self):
        b = Browser.from_flat_tree([], _headless=True)
        try:
            self.assertEqual(b._state._children, {})
        finally:
            b.stop_workers()

    def test_no_hierarchy_metadata_all_root_level(self):
        rows = [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
        b = Browser.from_flat_tree(rows, _headless=True)
        try:
            self.assertEqual(
                [it.id for it in b._state._children[None]],
                ['a', 'b', 'c'])
        finally:
            b.stop_workers()

    def test_get_children_uses_cache_no_runtime_calls(self):
        # The synthesised get_children pulls from the cache; no user
        # callback runs at runtime.
        rows = [
            {'id': 'r', 'has_children': True},
            {'id': 'a', 'parent': 'r'},
        ]
        b = Browser.from_flat_tree(rows, _headless=True, root_id=None)
        try:
            # The Browser's get_children should be derived from the cache
            # directly: calling it doesn't do any user IO, just returns
            # cached items.
            self.assertEqual(
                [it.id for it in b.get_children('r')], ['a'])
            self.assertEqual(b.get_children('nonexistent'), [])
        finally:
            b.stop_workers()


# ---- 8. Thread-safe ops from background threads ----------------------------


class TestThreadSafeOps(unittest.TestCase):

    def test_refresh_from_background_thread(self):
        seen = []
        def gc(id_, *, reload=False):
            seen.append(id_)
            return [(f'{id_}/c',)]
        b = make_browser(get_children=gc)
        try:
            def submit():
                b.refresh('A')
            t = threading.Thread(target=submit)
            t.start(); t.join()
            b.run_until_idle()
            self.assertIn('A', b._state._children)
            self.assertEqual(seen, ['A'])
        finally:
            b.stop_workers()

    def test_cursor_to_from_background_thread(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',)])
        try:
            b.refresh()
            b.run_until_idle()
            holder = {}
            def submit():
                holder['p'] = b.cursor_to('B')
            t = threading.Thread(target=submit)
            t.start(); t.join()
            b.run_until_idle()
            self.assertTrue(holder['p'].done)
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_select_from_background_thread(self):
        b = make_browser()
        try:
            def submit():
                b.select(['x', 'y'])
            t = threading.Thread(target=submit)
            t.start(); t.join()
            b.drain_main_queue()
            self.assertEqual(b._state.selected, {'x', 'y'})
        finally:
            b.stop_workers()


# ---- 8. Sticky cursor anchor (id-based positioning) ------------------------


from test.async_._helpers import _state  # access apply_ops helpers


class TestCursorAnchor(unittest.TestCase):
    """``Browser._cursor_anchor`` makes the cursor sticky by item id.

    The cursor's identity is the id of the item under it (its row in the
    visible list). Background mutations re-snap the cursor's index so
    the same id stays selected; user keystrokes re-anchor.
    """

    def _children_pair(self, root_children, sub):
        """Build a get_children that returns ``root_children`` for the
        root id and ``sub`` for the named parent.
        """
        def gc(parent_id, *, reload=False):
            if parent_id in ('', None):
                return root_children
            return sub.get(parent_id, [])
        return gc

    def test_anchor_seeded_from_initial_cursor(self):
        # After the first apply_children_results, the anchor should be
        # primed with the cursor's row id (lazy init).
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            # _reanchor_cursor runs in run() at startup; tests skip
            # run(), so trigger the equivalent explicitly.
            b._reanchor_cursor()
            self.assertEqual(b._cursor_anchor[0], 'A')
        finally:
            b.stop_workers()

    def test_cursor_follows_id_when_items_insert_above(self):
        # Cursor on 'B' (idx 1). update_data inserts a new row above it.
        # Without the anchor, cursor would stay at idx 1 (now 'NEW').
        # With the anchor, cursor follows 'B' to its new idx.
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(b._cursor_anchor[0], 'B')

            # Push a new sibling 'NEW' above 'B' by rewriting the
            # children list in the desired order.
            b.update_data([
                _state.clear_children(None),
                _state.upsert('NEW', None),
                _state.upsert('A', None),
                _state.upsert('B', None),
                _state.upsert('C', None),
            ])
            b.run_until_idle()
            # Cursor stayed on 'B', which is now at idx 2.
            vis = _state.visible_items(b._state)
            self.assertEqual(b._state.cursor, 2)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()

    def test_anchor_falls_back_to_next_sibling_when_primary_removed(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            # Anchor snapshot should be [B, C, A] (primary, next, prev).
            self.assertEqual(b._cursor_anchor[0], 'B')
            self.assertIn('C', b._cursor_anchor)
            self.assertIn('A', b._cursor_anchor)

            # Remove B. Cursor must land on C (next sibling).
            b.update_data([_state.remove('B')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'C')
            # Primary in the anchor is still 'B' — fallback hit doesn't
            # re-snapshot.
            self.assertEqual(b._cursor_anchor[0], 'B')
        finally:
            b.stop_workers()

    def test_anchor_falls_back_to_prev_sibling_when_last_item(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('C')   # last item
            b.run_until_idle()
            # Remove C → no next, fall back to prev (B).
            b.update_data([_state.remove('C')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()

    def test_anchor_walks_to_parent_when_whole_sibling_group_gone(self):
        # Root has A; A has A1, A2, A3. Cursor on A2. Remove all of A's
        # children. Cursor should fall back to A (parent).
        gc = self._children_pair(
            [('A', None, None, '', True)],
            {'A': [('A1',), ('A2',), ('A3',)]},
        )
        b = make_browser(get_children=gc)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('A')
            b.run_until_idle()
            b.cursor_to('A2')
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor[0], 'A2')
            # Anchor chain includes A1, A3, and A (parent).
            self.assertIn('A', b._cursor_anchor)
            self.assertIn('A1', b._cursor_anchor)
            self.assertIn('A3', b._cursor_anchor)

            # Wipe all A's children.
            b.update_data([
                _state.remove('A1'),
                _state.remove('A2'),
                _state.remove('A3'),
            ])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A')
        finally:
            b.stop_workers()

    def test_anchor_walks_to_root_through_streaming_layers(self):
        # Deep tree built layer by layer (root → A → A.A → A.A.A) so
        # the cursor falls all the way up when intermediates are
        # missing, then promotes back down as layers arrive.
        gc = self._children_pair(
            [('A', None, None, '', True)],
            {'A':     [('A.A', None, None, '', True)],
             'A.A':   [('A.A.A',)]},
        )
        b = make_browser(get_children=gc)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('A')
            b.run_until_idle()
            b.expand('A.A')
            b.run_until_idle()
            b.cursor_to('A.A.A')
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor[0], 'A.A.A')

            # Remove the leaf — cursor falls to grandparent's level
            # (next/prev are siblings of A.A.A, but there are none → walk
            # ancestors: A.A first).
            b.update_data([_state.remove('A.A.A')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A.A')

            # Also remove A.A — cursor falls one more level up to A.
            b.update_data([_state.remove('A.A')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A')

            # Bring A.A.A back — cursor jumps right back to it (primary
            # still parked in the anchor). A.A stays in state.expanded
            # from the original expansion.
            b.update_data([_state.upsert('A.A', 'A', has_children=True),
                           _state.upsert('A.A.A', 'A.A')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A.A.A')
        finally:
            b.stop_workers()

    def test_anchor_resnapshots_on_primary_hit(self):
        # When the primary lands, the snapshot is refreshed — capturing
        # the *current* neighbours. Later if primary is removed again,
        # the fallback uses fresh neighbours, not stale ones.
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            old_anchor = list(b._cursor_anchor)
            self.assertEqual(old_anchor[0], 'B')

            # Rewrite [A, B, C] as [X, B, Y] (cursor stays on B but its
            # neighbours change).
            b.update_data([
                _state.clear_children(None),
                _state.upsert('X', None),
                _state.upsert('B', None),
                _state.upsert('Y', None),
            ])
            b.run_until_idle()
            # Primary still hit → resnapshot. Old neighbours (A, C)
            # have been replaced by the fresh ones (X, Y).
            self.assertEqual(b._cursor_anchor[0], 'B')
            self.assertNotIn('A', b._cursor_anchor)
            self.assertNotIn('C', b._cursor_anchor)
            self.assertIn('X', b._cursor_anchor)
            self.assertIn('Y', b._cursor_anchor)
        finally:
            b.stop_workers()

    def test_anchor_returns_to_primary_when_it_reappears(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()

            # Remove B → cursor falls to C.
            b.update_data([_state.remove('B')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'C')
            # Primary stays parked.
            self.assertEqual(b._cursor_anchor[0], 'B')

            # Re-add B → cursor returns to it.
            b.update_data([
                _state.upsert('A', None),
                _state.upsert('B', None),
                _state.upsert('C', None),
            ])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()

    def test_slow_refresh_preserves_cursor_id(self):
        # The classic "Ctrl-R while parked on item C": the cursor must
        # be back on C once the refresh completes, regardless of how
        # long the worker took.
        items = [('A',), ('B',), ('C',), ('D',), ('E',)]
        def gc(_id, *, reload=False):
            # Tiny sleep simulates a slow recipe — long enough that the
            # cache invalidation between refresh start and worker
            # delivery is observable.
            time.sleep(0.03)
            return items
        b = make_browser(get_children=gc)
        try:
            b.refresh()
            b.run_until_idle(timeout=2.0)
            b.cursor_to('C')
            b.run_until_idle(timeout=2.0)
            self.assertEqual(b._state.cursor, 2)

            # Slow full refresh: worker re-fetches over ~30ms.
            b.refresh()
            b.run_until_idle(timeout=2.0)

            # Cursor identity preserved across the reload.
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'C')
        finally:
            b.stop_workers()

    def test_slow_refresh_three_levels_restores_deep_cursor(self):
        # The full case: 3-deep tree (root → A → A.A → A.A.A), cursor
        # parked on the deepest leaf. A slow full refresh invalidates
        # every cache and re-fetches each level over the worker
        # (10ms/level), so the visible list passes through several
        # intermediate states:
        #
        #   [pending root]                         (right after refresh)
        #   [A, pending for A]                     (after root delivery)
        #   [A, A.A, pending for A.A]              (after A delivery)
        #   [A, A.A, A.A.A]                        (after A.A delivery)
        #
        # The cursor anchor walks UP its ancestor chain as the deeper
        # ids vanish, then back DOWN as each delivery surfaces them.
        # By the time the refresh fully completes, the cursor must be
        # back on A.A.A (primary hit re-snapshots the chain).
        sub = {
            'A':   [('A.A', None, None, '', True)],
            'A.A': [('A.A.A',)],
        }
        root = [('A', None, None, '', True)]

        def gc(parent_id, *, reload=False):
            time.sleep(0.01)   # widen the loading window per level
            if parent_id in ('', None):
                return root
            return sub.get(parent_id, [])

        b = make_browser(get_children=gc)
        try:
            b.refresh()
            b.run_until_idle(timeout=3.0)
            b.expand('A')
            b.run_until_idle(timeout=3.0)
            b.expand('A.A')
            b.run_until_idle(timeout=3.0)
            b.cursor_to('A.A.A')
            b.run_until_idle(timeout=3.0)
            # Pre-refresh sanity: cursor on the deepest leaf.
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A.A.A')

            # The slow full refresh.
            b.refresh()
            b.run_until_idle(timeout=3.0)

            # Post-refresh: cursor identity preserved end-to-end,
            # despite the visible list collapsing and rebuilding
            # layer-by-layer.
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A.A.A')
            # Resnapshot fired on the primary hit, so the chain reflects
            # the rebuilt tree (ancestors A.A and A in the snapshot).
            self.assertEqual(b._cursor_anchor[0], 'A.A.A')
            self.assertIn('A.A', b._cursor_anchor)
            self.assertIn('A', b._cursor_anchor)
        finally:
            b.stop_workers()

    def test_slow_refresh_falls_back_when_cursor_item_gone(self):
        # If the slow reload returns a list that no longer contains the
        # cursor item, the cursor lands on a tier-2 fallback (next
        # sibling).
        delivery = {'first': True}
        def gc(_id, *, reload=False):
            time.sleep(0.02)
            if delivery['first']:
                delivery['first'] = False
                return [('A',), ('B',), ('C',), ('D',), ('E',)]
            # Second call (the refresh): C is gone.
            return [('A',), ('B',), ('D',), ('E',)]
        b = make_browser(get_children=gc)
        try:
            b.refresh()
            b.run_until_idle(timeout=2.0)
            b.cursor_to('C')
            b.run_until_idle(timeout=2.0)
            self.assertEqual(b._cursor_anchor[0], 'C')

            b.refresh()
            b.run_until_idle(timeout=2.0)
            # C is gone — fallback to next sibling D.
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'D')
            # Primary parked — if C reappeared later, the cursor would
            # jump back.
            self.assertEqual(b._cursor_anchor[0], 'C')
        finally:
            b.stop_workers()

    def test_anchor_works_on_scope_root_row(self):
        # When the user is scoped, the first visible row is a
        # ``scope_root`` entry — the cursor must anchor to it just like
        # a normal row so background mutations don't drift it.
        gc = self._children_pair(
            [('P', None, None, '', True)],
            {'P': [('X',), ('Y',)]},
        )
        b = make_browser(get_children=gc, initial_scope='P')
        try:
            b.refresh()
            b.run_until_idle(timeout=2.0)
            # Cursor starts at idx 0 (the scope_root row for 'P').
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[0].kind, 'scope_root')
            self.assertEqual(vis[0].item.id, 'P')
            # Seed the anchor as run() would after startup.
            b._reanchor_cursor()
            self.assertEqual(b._cursor_anchor[0], 'P')

            # Background mutation rewrites P's children; cursor must
            # stay on the scope_root row (id 'P'), not drift.
            b.update_data([
                _state.clear_children('P'),
                _state.upsert('NEW', 'P'),
                _state.upsert('X', 'P'),
                _state.upsert('Y', 'P'),
            ])
            b.run_until_idle(timeout=2.0)
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'P')
        finally:
            b.stop_workers()

    def test_anchor_clamps_to_index_when_entire_chain_missing(self):
        # When primary AND every fallback id is gone, the cursor falls
        # back to the index clamp (existing behavior).
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            # Replace every item with completely fresh ids.
            b.update_data([
                _state.remove('A'),
                _state.remove('B'),
                _state.remove('C'),
                _state.upsert('X', None),
                _state.upsert('Y', None),
            ])
            b.run_until_idle()
            # Anchor has no match → cursor clamped to within [0, len).
            vis = _state.visible_items(b._state)
            self.assertGreaterEqual(b._state.cursor, 0)
            self.assertLess(b._state.cursor, len(vis))
        finally:
            b.stop_workers()


# ---- 9. Scroll-to-fit on expansion (`_expand_goal`) ------------------------


class TestExpandGoal(unittest.TestCase):
    """Expanding a row sets a sticky scroll-to-fit goal that adjusts the
    list viewport so the parent row plus its subtree are visible. The
    goal survives async deliveries and clears on:
      - subtree fully loaded
      - subtree too big for the viewport (scroll-cap)
      - user cursor move
      - manual wheel scroll
    """

    def _pin_height(self, browser, height):
        """Replace ``_list_pane_height_safe`` with a fixed-value stub so
        we don't have to wire ``term_size`` / ``layout_panes`` for the
        geometry math.
        """
        browser._list_pane_height_safe = lambda: height

    def _tree(self, parent_children):
        """Build a get_children callable from a dict mapping parent id →
        list of (id, parent, _, _, has_children) tuples. None / '' → root.
        """
        def gc(parent_id, *, reload=False):
            return parent_children.get(parent_id, parent_children.get(None, []))
        return gc

    def test_user_expand_with_cached_children_scrolls_subtree_into_view(self):
        # Root has [P, X1, ..., X20]. P has 4 cached children.
        # Pane height = 5. Cursor on P (idx 0). Without the goal,
        # expanding P would leave P.1..P.4 below the bottom of the
        # 5-row pane (idx 5..8 visible).
        gc = self._tree({
            None: [('P', None, None, '', True)] + [(f'X{i}',) for i in range(20)],
            'P':  [(f'P.{i}',) for i in range(1, 5)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            # Pre-cache P's children so the cached-expand branch fires.
            b.expand('P')
            b.run_until_idle()
            # Collapse so the user-style fresh expand sets the goal.
            b._state.expanded.discard('P')
            _state.mark_visible_dirty(b._state)
            b._list_scroll = 0

            # User-driven expand (autoscroll=True).
            b.expand('P', autoscroll=True)
            b.run_until_idle()

            # Subtree (P plus P.1..P.4) is 5 rows — exactly the pane
            # height. Scroll should be 0 (P at top), last child at the
            # bottom row.
            self.assertEqual(b._list_scroll, 0)
            # Fully loaded → goal cleared.
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()

    def test_user_expand_scrolls_down_when_subtree_extends_past_bottom(self):
        # Cursor on P at idx 5 (which is mid-pane initially). Pane
        # height = 5. P has 3 children. After expand, subtree occupies
        # idx 5..8. Pane currently shows idx 0..4. Goal must scroll
        # down to put idx 8 at the bottom → scroll = 4.
        rows = [('R0',), ('R1',), ('R2',), ('R3',), ('R4',),
                ('P', None, None, '', True), ('Z',)]
        gc = self._tree({
            None: rows,
            'P':  [(f'P.{i}',) for i in range(3)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            # Position viewport at top, cursor on P.
            b._list_scroll = 0
            b.cursor_to('P')
            b.run_until_idle()
            # Pre-cache so we hit the cached path.
            b.expand('P')
            b.run_until_idle()
            b._state.expanded.discard('P')
            _state.mark_visible_dirty(b._state)
            b._list_scroll = 0

            b.expand('P', autoscroll=True)
            b.run_until_idle()
            # P at idx 5, last child at idx 8, height 5 → desired
            # scroll = max(8 - 5 + 1, 0) = 4. Capped at p_idx=5.
            self.assertEqual(b._list_scroll, 4)
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()

    def test_user_expand_scrolls_up_when_parent_above_viewport(self):
        # 30-item list. P at idx 5. User scrolled past it (scroll=15)
        # so P is above the viewport. Programmatically expanding P
        # with autoscroll=True should scroll UP to bring P into view.
        rows = [('R0',), ('R1',), ('R2',), ('R3',), ('R4',),
                ('P', None, None, '', True)] + [(f'Z{i}',) for i in range(24)]
        gc = self._tree({
            None: rows,
            'P':  [(f'P.{i}',) for i in range(3)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('P')                     # pre-cache
            b.run_until_idle()
            b._state.expanded.discard('P')
            _state.mark_visible_dirty(b._state)
            b._list_scroll = 15               # P at idx 5 is above viewport

            b.expand('P', autoscroll=True)
            b.run_until_idle()
            # Acceptable range: [last_idx - h + 1, p_idx] =
            # [8 - 5 + 1, 5] = [4, 5]. Current scroll 15 is above; clamp
            # picks 5 (closest to 15, capped at p_idx).
            self.assertEqual(b._list_scroll, 5)
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()

    def test_user_expand_no_scroll_when_subtree_already_fits(self):
        # P at idx 0, subtree of 3 children. Pane is 10 tall — the
        # whole thing already fits without scrolling.
        gc = self._tree({
            None: [('P', None, None, '', True), ('Z',)],
            'P':  [('P1',), ('P2',), ('P3',)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 10)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('P')                     # pre-cache
            b.run_until_idle()
            b._state.expanded.discard('P')
            _state.mark_visible_dirty(b._state)
            b._list_scroll = 0

            b.expand('P', autoscroll=True)
            b.run_until_idle()
            self.assertEqual(b._list_scroll, 0)
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()

    def test_user_expand_oversized_subtree_parks_parent_and_clears_goal(self):
        # P has 20 children, pane height = 5. Subtree doesn't fit
        # below the parent → scroll-cap: parent at top, goal cleared.
        gc = self._tree({
            None: [('P', None, None, '', True)],
            'P':  [(f'P.{i}',) for i in range(20)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('P')                     # pre-cache
            b.run_until_idle()
            b._state.expanded.discard('P')
            _state.mark_visible_dirty(b._state)
            b._list_scroll = 0

            b.expand('P', autoscroll=True)
            b.run_until_idle()
            # P sits at idx 0 → desired = p_idx = 0 → no scroll change,
            # but the goal clears (cap hit).
            self.assertEqual(b._list_scroll, 0)
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()

    def test_async_subtree_streams_into_view_as_deliveries_arrive(self):
        # P uncached. Slow get_children returns 3 children after a
        # small delay. On first expand the visible list shows
        # [P, pending]; goal scrolls to put pending at bottom. As
        # delivery lands, goal re-applies and scrolls further to fit
        # the actual children.
        children = [(f'P.{i}',) for i in range(3)]
        rows = [('R0',), ('R1',), ('R2',), ('R3',), ('R4',),
                ('P', None, None, '', True)]
        delivered = {'first': True}
        def gc(parent_id, *, reload=False):
            if parent_id in ('', None):
                return rows
            if parent_id == 'P':
                if delivered['first']:
                    delivered['first'] = False
                    time.sleep(0.02)
                return children
            return []
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b._list_scroll = 0
            b.cursor_to('P')                  # cursor on P (idx 5)
            b.run_until_idle()

            # Expand P (uncached → slow delivery).
            b.expand('P', autoscroll=True)
            b.run_until_idle()
            # After full delivery, subtree is 4 rows (P + 3 children),
            # last at idx 8. desired = clamp(0, 8-5+1, 5) = 4.
            self.assertEqual(b._list_scroll, 4)
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()

    def test_programmatic_expand_default_autoscroll_false_no_scroll(self):
        # Browser.expand(id) without autoscroll=True must NOT scroll —
        # recipes doing bulk setup should not surprise the user.
        rows = [('R0',), ('R1',), ('R2',), ('R3',), ('R4',),
                ('P', None, None, '', True)]
        gc = self._tree({
            None: rows,
            'P':  [(f'P.{i}',) for i in range(3)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b._list_scroll = 0

            b.expand('P')                     # no autoscroll kwarg
            b.run_until_idle()
            # No goal, no scroll change.
            self.assertIsNone(b._expand_goal)
            self.assertEqual(b._list_scroll, 0)
        finally:
            b.stop_workers()

    def test_slow_delivery_scrolls_in_two_stages(self):
        # Worker blocks on a gate so we can inspect the half-loaded
        # state between the placeholder appearing and the real
        # children arriving. The goal should:
        #   Stage 1 (expand posted, worker blocked):
        #     - visible list has [..., P, pending<P>]
        #     - _expand_goal is parked
        #     - _list_scroll moved just enough to show the placeholder
        #   Stage 2 (worker released, children delivered):
        #     - visible list has [..., P, P.0, P.1, P.2]
        #     - _expand_goal cleared (subtree fully loaded)
        #     - _list_scroll moved further to fit the last child
        delivery_gate = threading.Event()
        children = [(f'P.{i}',) for i in range(3)]
        rows = [('R0',), ('R1',), ('R2',), ('R3',), ('R4',),
                ('P', None, None, '', True)]
        def gc(parent_id, *, reload=False):
            if parent_id in ('', None):
                return rows
            if parent_id == 'P':
                delivery_gate.wait(timeout=1.0)
                return children
            return []
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b._list_scroll = 0
            b.cursor_to('P')                  # cursor on P (idx 5)
            b.run_until_idle()
            self.assertEqual(b._list_scroll, 1)  # cursor-anchor snap

            # ---- Stage 1: expand, worker blocked --------------------
            b._list_scroll = 0
            b.expand('P', autoscroll=True)
            b.drain_main_queue()              # runs _do_expand, queues fetch
            # The visible list now has a pending placeholder under P
            # (visible_items emits it lazily for expanded-but-uncached
            # parents).
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[5].item.id, 'P')
            self.assertEqual(vis[6].kind, 'pending')
            # Goal is parked (subtree not yet loaded).
            self.assertIsNotNone(b._expand_goal)
            self.assertEqual(b._expand_goal['parent_id'], 'P')
            # Scroll adjusted to keep the placeholder in view.
            # p_idx=5, last_idx=6, height=5 → lo=2, hi=5 →
            # clamp(0, 2, 5) = 2.
            self.assertEqual(b._list_scroll, 2)

            # ---- Stage 2: worker delivers ---------------------------
            delivery_gate.set()
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[5].item.id, 'P')
            self.assertEqual(vis[6].item.id, 'P.0')
            self.assertEqual(vis[8].item.id, 'P.2')
            # Subtree fully loaded → goal cleared.
            self.assertIsNone(b._expand_goal)
            # last_idx=8, height=5 → lo=4, hi=5 → clamp(2, 4, 5) = 4.
            self.assertEqual(b._list_scroll, 4)
        finally:
            delivery_gate.set()
            b.stop_workers()

    def test_user_cursor_move_clears_parked_goal(self):
        # Goal parked on a slow-loading subtree → user presses 'j'
        # mid-load → goal cleared, subsequent delivery doesn't snap.
        children = [(f'P.{i}',) for i in range(3)]
        delivery_gate = threading.Event()
        rows = [('R0',), ('R1',), ('R2',), ('R3',), ('R4',),
                ('P', None, None, '', True)]
        def gc(parent_id, *, reload=False):
            if parent_id in ('', None):
                return rows
            if parent_id == 'P':
                delivery_gate.wait(timeout=1.0)
                return children
            return []
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('P')
            b.run_until_idle()
            b._list_scroll = 0

            # Kick a slow expansion. The worker blocks on
            # delivery_gate, so the goal is parked.
            b.expand('P', autoscroll=True)
            # Pump the main thread without finishing the worker.
            b.drain_main_queue()
            self.assertIsNotNone(b._expand_goal)

            # Simulate a user keypress that moves the cursor.
            from test.async_._helpers import _state as _state_mod
            ctx_mod = _state_mod.__dict__.get('Context')
            # Use _handle_one_key directly with a fake context.
            class _Ctx:
                def __init__(self, b): self._browser = b
                cursor = None
                selected = []
                targets = []
            # Inject minimal Context surface.
            # Actually use the real Context loader:
            from test.unit._loader import load
            _ctx_mod = load('_browse_tui_ctx', '060-context.py')
            _ctx_mod.visible_items = _state_mod.visible_items
            _state_mod.Context = _ctx_mod.Context
            ctx = _ctx_mod.Context(b)
            # Also need dispatch_key wired:
            _actions_mod = load('_browse_tui_act', '070-actions.py')
            _actions_mod.visible_items = _state_mod.visible_items
            _actions_mod.mark_visible_dirty = _state_mod.mark_visible_dirty
            _actions_mod.mark_cursor_changed = _state_mod.mark_cursor_changed
            _actions_mod.Mode = _state_mod.Mode
            _state_mod.dispatch_key = _actions_mod.dispatch_key
            _state_mod._handle_insert_key = _actions_mod._handle_insert_key
            b._handle_one_key(ctx, 'j')
            # Cursor moved → goal cleared.
            self.assertIsNone(b._expand_goal)

            # Release the worker; subsequent delivery must NOT snap
            # the viewport.
            scroll_before_delivery = b._list_scroll
            delivery_gate.set()
            b.run_until_idle()
            self.assertEqual(b._list_scroll, scroll_before_delivery)
        finally:
            delivery_gate.set()
            b.stop_workers()

    def test_re_expand_of_already_expanded_does_not_reset_goal(self):
        # An already-expanded id, re-expanded with autoscroll=True, is
        # a no-op for the goal: nothing new is being revealed.
        gc = self._tree({
            None: [('P', None, None, '', True)],
            'P':  [(f'P.{i}',) for i in range(3)],
        })
        b = make_browser(get_children=gc)
        self._pin_height(b, 5)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('P', autoscroll=True)
            b.run_until_idle()
            self.assertIsNone(b._expand_goal)  # fully loaded after first
            # Re-expand: no goal should be set again.
            b.expand('P', autoscroll=True)
            b.run_until_idle()
            self.assertIsNone(b._expand_goal)
        finally:
            b.stop_workers()


# ---- 10. Cursor hide-displacement (per-batch, walk-back) -----------------


class TestHideDisplacement(unittest.TestCase):
    """When the cursor's row gets hidden by an ``update_data`` batch,
    the cursor walks back through the pre-mutation visible list to find
    the previous visible row. See
    ``docs/superpowers/specs/2026-05-16-row-visibility-design.md``.

    Distinct from anchor displacement: anchor handles *deletion*; this
    handles *hide*.
    """

    def test_cursor_on_hidden_row_walks_back(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',), ('D',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('C')
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 2)

            b.update_data([_state.mod('C', hidden=True)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            # 'C' gone; cursor on 'B' (the row above it in pre-mutation).
            self.assertEqual([e.item.id for e in vis], ['A', 'B', 'D'])
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()

    def test_cursor_lands_on_first_when_all_above_hidden(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            # Hide A and B in one batch — no earlier visible row.
            b.update_data([
                _state.mod('A', hidden=True),
                _state.mod('B', hidden=True),
            ])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['C'])
            # Walk-back found nothing → first visible row.
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(vis[b._state.cursor].item.id, 'C')
        finally:
            b.stop_workers()

    def test_cursor_on_first_row_hidden_lands_on_new_first(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 0)
            b.update_data([_state.mod('A', hidden=True)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['B', 'C'])
            # Walk-back from row 0 finds nothing → first row of new.
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()

    def test_unrelated_hide_does_not_move_cursor(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 1)
            # Hide 'C', cursor on 'B' — no displacement needed.
            b.update_data([_state.mod('C', hidden=True)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['A', 'B'])
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()

    def test_hidden_ancestor_displaces_cursor_on_descendant(self):
        gc_root = [('P', None, None, '', True), ('X',)]

        def gc(parent_id, *, reload=False):
            if parent_id in (None, ''):
                return gc_root
            if parent_id == 'P':
                return [('P1',), ('P2',)]
            return []

        b = make_browser(get_children=gc)
        try:
            b.refresh()
            b.run_until_idle()
            b.expand('P')
            b.run_until_idle()
            b.cursor_to('P2')
            b.run_until_idle()
            # Hide P → subtree (P, P1, P2) all invisible. Cursor was
            # on P2 (row 2 pre-mutation: P, P1, P2, X). Walk back:
            # row 1 (P1) hidden, row 0 (P) hidden → fall to first
            # visible (X).
            b.update_data([_state.mod('P', hidden=True)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['X'])
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_delete_uses_anchor_not_hide_displacement(self):
        # If the cursor's id is *removed* (not hidden), the anchor's
        # fallback chain (next-sibling-first) handles it.
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',), ('D',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            b.update_data([_state.remove('B')])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['A', 'C', 'D'])
            # Anchor's chain: B missing → next sibling C (row 1).
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(vis[b._state.cursor].item.id, 'C')
        finally:
            b.stop_workers()

    def test_reanchor_after_displacement(self):
        # After hide-displacement the new cursor row id becomes the
        # primary anchor.
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor[0], 'B')
            b.update_data([_state.mod('B', hidden=True)])
            b.run_until_idle()
            # New cursor on 'A'; anchor primary refreshed.
            self.assertEqual(b._cursor_anchor[0], 'A')
        finally:
            b.stop_workers()

    def test_hide_then_show_in_same_batch_no_net_movement(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('A',), ('B',), ('C',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('B')
            b.run_until_idle()
            b.update_data([
                _state.mod('B', hidden=True),
                _state.mod('B', hidden=False),
            ])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['A', 'B', 'C'])
            # Net effect: nothing hidden post-batch — cursor stays.
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
        finally:
            b.stop_workers()


# ---- 11. Cursor pin (PIN_FIRST / PIN_LAST) ----------------------------


class TestCursorPin(unittest.TestCase):
    """Positional pin tier on the cursor anchor.

    ``PIN_FIRST`` / ``PIN_LAST`` make the cursor stick to row 0 or the
    last visible row across background mutations. The pin is engaged
    by ``Browser.nav_home`` / ``Browser.nav_end`` (or the keybinds via
    ``_nav_home`` / ``_nav_end``), and is cleared by any other cursor
    motion. See
    ``docs/superpowers/specs/2026-05-17-cursor-pin-design.md``.
    """

    def test_sentinels_are_distinct(self):
        self.assertIsNot(_state.PIN_FIRST, _state.PIN_LAST)

    def test_sentinel_reprs(self):
        self.assertEqual(repr(_state.PIN_FIRST), '<PIN_FIRST>')
        self.assertEqual(repr(_state.PIN_LAST), '<PIN_LAST>')

    def test_nav_home_sets_pin_first(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            # Move cursor away so nav_home has to displace it.
            b.cursor_to('C')
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 2)
            b.nav_home()
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
        finally:
            b.stop_workers()

    def test_nav_end_sets_pin_last(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_end()
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 2)
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
        finally:
            b.stop_workers()

    def test_pin_first_follows_new_first_row(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_home()
            b.run_until_idle()
            # Insert a new row at the top via clear+upserts in the new order.
            b.update_data([
                _state.clear_children(None),
                _state.upsert('A', None),
                _state.upsert('B', None),
                _state.upsert('C', None),
            ])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['A', 'B', 'C'])
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(vis[b._state.cursor].item.id, 'A')
            # Pin still engaged.
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
        finally:
            b.stop_workers()

    def test_pin_last_follows_new_last_row(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_end()
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 1)
            # Append a new row.
            b.update_data([_state.upsert('C', None)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['A', 'B', 'C'])
            self.assertEqual(b._state.cursor, 2)
            self.assertEqual(vis[b._state.cursor].item.id, 'C')
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
        finally:
            b.stop_workers()

    def test_pin_last_follows_when_last_row_hidden(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_end()
            b.run_until_idle()
            # Hide the current last row — pin should jump to new last.
            b.update_data([_state.mod('C', hidden=True)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['A', 'B'])
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(vis[b._state.cursor].item.id, 'B')
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
        finally:
            b.stop_workers()

    def test_pin_first_clears_on_j(self):
        # Pressing 'j' (cursor down) should clear PIN_FIRST and
        # capture an id-based anchor.
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_home()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
            # Build a Context + dispatch_key so we go through the
            # real key-handling path.
            from test.unit._loader import load
            _ctx_mod = load('_browse_tui_ctx', '060-context.py')
            _ctx_mod.visible_items = _state.visible_items
            ctx = _ctx_mod.Context(b)
            _actions_mod = load('_browse_tui_act', '070-actions.py')
            _actions_mod.visible_items = _state.visible_items
            _actions_mod.mark_visible_dirty = _state.mark_visible_dirty
            _actions_mod.mark_cursor_changed = _state.mark_cursor_changed
            _actions_mod.PIN_FIRST = _state.PIN_FIRST
            _actions_mod.PIN_LAST = _state.PIN_LAST
            _actions_mod.Mode = _state.Mode
            _state.dispatch_key = _actions_mod.dispatch_key
            _state._handle_insert_key = _actions_mod._handle_insert_key
            b._handle_one_key(ctx, 'j')
            # Cursor moved to row 1 → pin cleared.
            self.assertEqual(b._state.cursor, 1)
            self.assertNotIsInstance(
                b._cursor_anchor[0], _state._AnchorSentinel
            )
            self.assertEqual(b._cursor_anchor[0], 'B')
        finally:
            b.stop_workers()

    def test_pin_first_swapped_by_pin_last(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_home()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
            b.nav_end()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
            self.assertEqual(b._state.cursor, 2)
        finally:
            b.stop_workers()

    def test_pin_clears_on_cursor_to(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_end()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
            b.cursor_to('B')
            b.run_until_idle()
            # cursor_to seeded an id-based anchor.
            self.assertEqual(b._cursor_anchor[0], 'B')
        finally:
            b.stop_workers()

    def test_pin_empty_list_keeps_pin(self):
        # Pin engaged with no rows → cursor parked; pin survives.
        b = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_home()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
            # Now arrival — pin lands cursor on row 0.
            b.update_data([_state.upsert('A', None)])
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_pin_first_with_hidden_first_row(self):
        # PIN_FIRST with the original first row hidden → cursor on new first.
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_home()
            b.run_until_idle()
            b.update_data([_state.mod('A', hidden=True)])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['B', 'C'])
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
        finally:
            b.stop_workers()

    def test_action_layer_g_sets_pin(self):
        # `g` and `home` keybinds engage PIN_FIRST via _nav_home.
        b = make_browser(get_children=lambda _id, *, reload=False: [('A',), ('B',), ('C',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('C')
            b.run_until_idle()

            from test.unit._loader import load
            _ctx_mod = load('_browse_tui_ctx', '060-context.py')
            _ctx_mod.visible_items = _state.visible_items
            ctx = _ctx_mod.Context(b)
            _actions_mod = load('_browse_tui_act', '070-actions.py')
            _actions_mod.visible_items = _state.visible_items
            _actions_mod.mark_visible_dirty = _state.mark_visible_dirty
            _actions_mod.mark_cursor_changed = _state.mark_cursor_changed
            _actions_mod.PIN_FIRST = _state.PIN_FIRST
            _actions_mod.PIN_LAST = _state.PIN_LAST
            _actions_mod.Mode = _state.Mode
            _state.dispatch_key = _actions_mod.dispatch_key
            _state._handle_insert_key = _actions_mod._handle_insert_key

            b._handle_one_key(ctx, 'g')
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
            # Then `end` flips to PIN_LAST.
            b._handle_one_key(ctx, 'end')
            self.assertEqual(b._state.cursor, 2)
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
        finally:
            b.stop_workers()


# ---- 12. Interactive filter (`&`) integration ----------------------------
#
# Covers the cross-cutting behaviours called out in the design spec:
# update_data re-triggers filter recompute, cursor hide-displacement
# follows a filter-hidden row, PIN_FIRST/LAST survive filter narrowing,
# select-all drops filter-hidden rows (WYSIWYG), and search runs only
# over filter-passing rows.


def _wire_actions(b):
    """Helper: load+wire the actions module for filter dispatch."""
    from test.unit._loader import load
    _actions_mod = load('_browse_tui_act', '070-actions.py')
    _actions_mod.visible_items = _state.visible_items
    _actions_mod.mark_visible_dirty = _state.mark_visible_dirty
    _actions_mod.mark_cursor_changed = _state.mark_cursor_changed
    _actions_mod._recompute_filter_hidden = _state._recompute_filter_hidden
    _actions_mod._AnchorSentinel = _state._AnchorSentinel
    _actions_mod.PIN_FIRST = _state.PIN_FIRST
    _actions_mod.PIN_LAST = _state.PIN_LAST
    _actions_mod.Mode = _state.Mode
    _state.dispatch_key = _actions_mod.dispatch_key
    _state._handle_insert_key = _actions_mod._handle_insert_key
    _ctx_mod = load('_browse_tui_ctx', '060-context.py')
    _ctx_mod.visible_items = _state.visible_items
    return _ctx_mod.Context(b)


class TestFilterUpdateDataIntegration(unittest.TestCase):
    """``update_data`` re-fires ``_recompute_filter_hidden`` after each batch."""

    def test_streaming_match_unhides_scaffold_parent(self):
        # Start: only non-matching items. Apply a filter — parent hidden.
        # Then stream in a matching child — parent should resurrect.
        b = make_browser(get_children=lambda _id, *, reload=False: [])
        try:
            b.refresh()
            b.run_until_idle()
            b.update_data([
                ('upsert', 'parent', None,
                 {'title': 'parent', 'has_children': True}),
            ])
            b.run_until_idle()
            b.set_filters(['child'])
            b.run_until_idle()
            # Optimistic-pending: parent has has_children=True with no
            # cached children, so it stays visible.
            self.assertFalse(b._state._items_by_id['parent']._filter_hidden)
            # Stream a non-matching child first — parent flips to hidden.
            b.update_data([
                ('upsert', 'one', 'parent', {'title': 'one'}),
                ('complete', 'parent'),
            ])
            b.run_until_idle()
            self.assertTrue(b._state._items_by_id['parent']._filter_hidden)
            self.assertTrue(b._state._items_by_id['one']._filter_hidden)
            # Now stream a matching child — parent resurrects as scaffold.
            b.update_data([
                ('upsert', 'child-foo', 'parent', {'title': 'child-foo'}),
            ])
            b.run_until_idle()
            self.assertFalse(b._state._items_by_id['parent']._filter_hidden)
            self.assertFalse(b._state._items_by_id['child-foo']._filter_hidden)
        finally:
            b.stop_workers()


class TestFilterAppliesToStreamedChildren(unittest.TestCase):
    """Items delivered via ``get_children`` (apply_children_results) get
    filter-evaluated when an active filter is in place."""

    def test_get_children_children_get_flagged(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('apple',), ('banana',), ('cherry',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.set_filters(['app'])
            b.run_until_idle()
            vis_ids = [
                e.item.id for e in _state.visible_items(b._state)
                if e.kind == 'normal'
            ]
            self.assertEqual(vis_ids, ['apple'])
            self.assertTrue(b._state._items_by_id['banana']._filter_hidden)
        finally:
            b.stop_workers()

    def test_filter_first_then_load_children(self):
        # Worker delivery order: filter is set *before* children arrive.
        # Items come in via the get_children path (apply_children_results).
        events = []

        def get_children(parent_id, *, reload=False):
            events.append(parent_id)
            return [('apple',), ('banana',), ('cherry',)]

        b = make_browser(get_children=get_children)
        try:
            # Set filter first (no items yet).
            b.set_filters(['app'])
            b.run_until_idle()
            # Now refresh — children deliver via the worker path.
            b.refresh()
            b.run_until_idle()
            vis_ids = [
                e.item.id for e in _state.visible_items(b._state)
                if e.kind == 'normal'
            ]
            self.assertEqual(vis_ids, ['apple'])
        finally:
            b.stop_workers()


class TestFilterCursorDisplacement(unittest.TestCase):
    """Cursor walks back when its row vanishes due to filter narrowing."""

    def test_cursor_displaces_when_row_hidden_by_filter(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('apple',), ('banana',), ('cherry',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.cursor_to('banana')
            b.run_until_idle()
            self.assertEqual(b._state.cursor, 1)
            # Filter 'app' — only apple matches; banana row vanishes.
            b.set_filters(['app'])
            b.run_until_idle()
            # Cursor walks back to apple (idx 0 in the new visible list).
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()


class TestPinSurvivesFilter(unittest.TestCase):
    """PIN_FIRST / PIN_LAST re-bind to the filtered visible list."""

    def test_pin_first_clamps_to_filter_top(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('apple',), ('banana',), ('cherry',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_home()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
            # Filter to 'che' — only cherry remains. Pin stays first.
            b.set_filters(['che'])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            self.assertEqual([e.item.id for e in vis], ['cherry'])
            self.assertEqual(b._state.cursor, 0)
            self.assertEqual(b._cursor_anchor, [_state.PIN_FIRST])
        finally:
            b.stop_workers()

    def test_pin_last_clamps_to_filter_bottom(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('apple',), ('banana',), ('apricot',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.nav_end()
            b.run_until_idle()
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
            # Filter 'ap' — apple + apricot match; cursor at apricot.
            b.set_filters(['ap'])
            b.run_until_idle()
            vis = _state.visible_items(b._state)
            ids = [e.item.id for e in vis]
            self.assertEqual(ids, ['apple', 'apricot'])
            self.assertEqual(b._state.cursor, 1)
            self.assertEqual(b._cursor_anchor, [_state.PIN_LAST])
        finally:
            b.stop_workers()


class TestSelectAllRespectsFilter(unittest.TestCase):
    """Ctrl-A select-all-visible drops filter-hidden ids (WYSIWYG)."""

    def test_select_all_keeps_only_visible(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('apple',), ('banana',), ('cherry',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            b.select(['apple', 'banana', 'cherry'], replace=True)
            b.run_until_idle()
            # Filter 'app' — banana / cherry hidden.
            b.set_filters(['app'])
            b.run_until_idle()
            # select-all-visible via dispatch.
            ctx = _wire_actions(b)
            b._handle_one_key(ctx, 'ctrl-a')
            self.assertEqual(b._state.selected, {'apple'})
        finally:
            b.stop_workers()


class TestSearchWithinFilter(unittest.TestCase):
    """`/` search corpus narrows to filter-passing rows."""

    def test_search_only_finds_filter_passing_rows(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [
            ('apple-a',), ('apple-b',), ('banana-a',),
        ])
        try:
            b.refresh()
            b.run_until_idle()
            # Filter 'apple' — banana-a hidden.
            b.set_filters(['apple'])
            b.run_until_idle()
            # _search_find walks visible_items, which now excludes banana.
            idx = _state._search_find(b._state, 'banana', 0, 1)
            self.assertIsNone(idx)
            # Search for 'apple' — finds apple-a (first match).
            idx = _state._search_find(b._state, 'apple', -1, 1)
            self.assertIsNotNone(idx)
            vis = _state.visible_items(b._state)
            self.assertEqual(vis[idx].item.id, 'apple-a')
        finally:
            b.stop_workers()


class TestFilterApi(unittest.TestCase):
    """``Browser.filters`` / ``set_filters`` / ``add_filter`` / ``clear_filters``."""

    def test_filters_property_excludes_empty_placeholder(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('a',), ('b',)])
        try:
            b.refresh()
            b.run_until_idle()
            # In FILTER_EDIT with empty placeholder, filters() returns ().
            b._filters = ['']
            b._mode = _state.Mode.FILTER_EDIT
            self.assertEqual(b.filters, ())
            # Once user has typed, the live entry surfaces.
            b._filters = ['f']
            self.assertEqual(b.filters, ('f',))
            b._mode = _state.Mode.NORMAL
        finally:
            b.stop_workers()

    def test_set_filters_forces_normal_mode(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('a',), ('b',)])
        try:
            b.refresh()
            b.run_until_idle()
            b._mode = _state.Mode.FILTER_EDIT
            b._filters = ['half-typed']
            b.set_filters(['final'])
            b.run_until_idle()
            self.assertIs(b._mode, _state.Mode.NORMAL)
            self.assertEqual(b._filters, ['final'])
        finally:
            b.stop_workers()

    def test_add_filter_appends(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('a',), ('b',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.set_filters(['first'])
            b.run_until_idle()
            b.add_filter('second')
            b.run_until_idle()
            self.assertEqual(b._filters, ['first', 'second'])
        finally:
            b.stop_workers()

    def test_clear_filters_drops_all(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [('a',), ('b',)])
        try:
            b.refresh()
            b.run_until_idle()
            b.set_filters(['x', 'y'])
            b.run_until_idle()
            b.clear_filters()
            b.run_until_idle()
            self.assertEqual(b._filters, [])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
