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

from test.async_._helpers import Browser, Item, Pending, State, make_browser


# ---- 1. Construction defaults / kwargs -------------------------------------


class TestConstructionDefaults(unittest.TestCase):
    """Exhaustively poke the new __init__ signature."""

    def test_no_kwargs_defaults(self):
        b = Browser(_headless=True)
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
        b = Browser(
            _headless=True,
            title='foo',
            get_children=lambda _id: [],
            root_id='/',
            initial_scope='/x',
        )
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
        b = Browser(_headless=True, actions=[sentinel])
        try:
            self.assertEqual(b.actions, [sentinel])
        finally:
            b.stop_workers()

    def test_callable_kwargs_stored(self):
        fmt = lambda item, ctx: [(str(item.id), '', False)]
        on_enter = lambda ctx: None
        b = Browser(_headless=True, format_item=fmt, on_enter=on_enter)
        try:
            self.assertIs(b.format_item, fmt)
            self.assertIs(b.on_enter, on_enter)
        finally:
            b.stop_workers()

    def test_show_flags_can_be_disabled(self):
        b = Browser(
            _headless=True,
            show_preview=False,
            show_children_pane=False,
            multi_select=False,
            print_format='{title}',
        )
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
        b = make_browser(get_children=lambda _id: [(i,) for i in ids])
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
        def gc(id_):
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
        def gc(id_):
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
        b = make_browser(get_children=lambda _id: [])
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
        def gc(id_):
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',)])
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
        def gc(parent_id):
            if parent_id in ('', None):
                return root_children
            return sub.get(parent_id, [])
        return gc

    def test_anchor_seeded_from_initial_cursor(self):
        # After the first apply_children_results, the anchor should be
        # primed with the cursor's row id (lazy init).
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        def gc(_id):
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

        def gc(parent_id):
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
        def gc(_id):
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
        b = make_browser(get_children=lambda _id: [('A',), ('B',), ('C',)])
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
        def gc(parent_id):
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
        def gc(parent_id):
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
        def gc(parent_id):
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
        def gc(parent_id):
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


if __name__ == '__main__':
    unittest.main()
