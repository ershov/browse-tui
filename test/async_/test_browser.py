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


if __name__ == '__main__':
    unittest.main()
