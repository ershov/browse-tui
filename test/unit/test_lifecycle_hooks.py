"""Tests for the Browser lifecycle hooks.

All hooks follow the uniform ``(ctx, <subject>)`` convention:
``on_cursor_change(ctx, id)`` fires at most once per main-loop tick
when the cursor row id changed; ``on_selection_change(ctx, ids)``
carries the resulting selected id list; ``on_scope_change(ctx,
scope_id, prev_scope_id, direction)`` fires after every scope
transition with the new + previous scope ids (``None`` at root) and
``'in'``/``'out'``; ``on_quit(ctx, code)`` fires once during shutdown
with the stashed exit code.
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_context = load('_browse_tui_context', '060-context.py')
_actions = load('_browse_tui_actions', '070-actions.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_state.Context = _context.Context        # hooks need Context in scope
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.VisibleEntry = _state.VisibleEntry
_context.visible_items = _state.visible_items

# The expand/collapse hook tests drive the real keyboard handlers
# (`right`/`left`/alt-right/alt-left) through ``dispatch_key`` so the
# whole input path is exercised; the production single-file build
# resolves these cross-module names by concatenation.
_actions.write = _term.write
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.current_scope = _state.current_scope
_actions._search_find = _state._search_find
_actions._search_jump_nearest = _state._search_jump_nearest
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode
_actions.point_in_rect = _render.point_in_rect
_actions._sub_needed_rows = _render._sub_needed_rows
_actions._fmt_child = _render._fmt_child
_actions.scope_into = _state.scope_into
_actions.scope_out = _state.scope_out

Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Item = _data.Item
Context = _context.Context
mark_cursor_changed = _state.mark_cursor_changed
dispatch_key = _actions.dispatch_key


def _seed(b, items):
    b.update_data([
        ('upsert', it.id, None, {k: v for k, v in vars(it).items()
                                 if not k.startswith('_')})
        for it in items
    ])
    b.drain_main_queue()


class TestOnCursorChange(unittest.TestCase):

    def test_fires_once_per_drain(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(id)))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        b._last_cursor_id = None  # ensure first move is observed
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, ['b'])

    def test_payload_is_id_and_ctx_agrees(self):
        # The id payload matches what the recipe would read off ctx.
        seen = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: seen.append(
                        (id, ctx.cursor.id if ctx.cursor else None))))
        _seed(b, [Item(id='a'), Item(id='b')])
        b._last_cursor_id = None
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(seen, [('b', 'b')])

    def test_id_is_none_on_empty(self):
        # No items → cursor row resolves to None.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(id)))
        b._last_cursor_id = 'sentinel'  # force a delta to None
        b._state.cursor = 0
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, [None])

    def test_coalesces_rapid_moves(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(id)))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        # Several moves in quick succession (no fire between).
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._state.cursor = 2
        mark_cursor_changed(b)
        b._state.cursor = 0
        mark_cursor_changed(b)
        # Single fire reflecting the latest position.
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, ['a'])

    def test_no_fire_when_id_unchanged(self):
        # Cursor anchor re-positioning often calls mark_cursor_changed
        # without changing the id. The hook must not fire in that case.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(1)))
        _seed(b, [Item(id='a'), Item(id='b')])
        b._state.cursor = 0
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(len(fired), 1)
        # Re-mark with the cursor unchanged.
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(len(fired), 1)  # no new fire

    def test_exception_routed_to_error(self):
        def bad(ctx, id):
            raise RuntimeError('boom')
        b = Browser(BrowserConfig(_headless=True, on_cursor_change=bad))
        _seed(b, [Item(id='a'), Item(id='b')])
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        b.drain_main_queue()
        self.assertIn('boom', b.error_text)
        self.assertIn('on_cursor_change', b.error_text)


class TestOnScopeChange(unittest.TestCase):

    def _seed(self, b):
        b.update_data([
            ('upsert', 'a', None, {'has_children': True}),
            ('upsert', 'b', None, {'has_children': True}),
            ('upsert', 'a1', 'a', {'has_children': True}),
            ('upsert', 'a2', 'a', {}),
        ])
        b.drain_main_queue()

    def test_scope_into_payload_from_root(self):
        # Scoping in from the root: scope_id == target, prev is None,
        # direction == 'in'.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(fired, [('a', None, 'in')])

    def test_scope_into_nested_carries_prev(self):
        # Scoping a level deeper: prev is the scope we were in.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_into('a1')
        b.drain_main_queue()
        self.assertEqual(fired[-1], ('a1', 'a', 'in'))

    def test_scope_out_to_root_scope_id_none(self):
        # Scoping out to the root: scope_id is None, prev is the scope
        # we left, direction == 'out'.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(fired[-1], (None, 'a', 'out'))

    def test_scope_out_nested_carries_both(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_into('a1')
        b.drain_main_queue()
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(fired[-1], ('a', 'a1', 'out'))

    def test_direct_fire_defaults(self):
        # Calling the private fire directly (no transition) passes the
        # current scope as both ids with no direction.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        b._fire_scope_change()
        self.assertEqual(fired, [(None, None, None)])

    def test_exception_routed_to_error(self):
        def bad(ctx, sid, prev, d):
            raise ValueError('nope')
        b = Browser(BrowserConfig(_headless=True, on_scope_change=bad))
        b._fire_scope_change()
        b.drain_main_queue()
        self.assertIn('nope', b.error_text)
        self.assertIn('on_scope_change', b.error_text)


class TestOnSelectionChange(unittest.TestCase):

    def test_fires_on_select_all_visible(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(
                        set(ids))))
        b.update_data([
            ('upsert', 'a', None, {}),
            ('upsert', 'b', None, {}),
        ])
        b.drain_main_queue()
        b.select_all_visible()
        b.drain_main_queue()
        self.assertEqual(fired, [{'a', 'b'}])

    def test_payload_is_id_list(self):
        # The payload is a list of ids (the resulting set), not Items.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(ids)))
        b.update_data([
            ('upsert', 'a', None, {}),
            ('upsert', 'b', None, {}),
        ])
        b.drain_main_queue()
        b.select_all_visible()
        b.drain_main_queue()
        self.assertEqual(len(fired), 1)
        payload = fired[0]
        self.assertIsInstance(payload, list)
        self.assertEqual(set(payload), {'a', 'b'})

    def test_fires_on_clear_with_empty_list(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(ids)))
        b._state.selected = {'a'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(fired, [[]])

    def test_no_fire_on_clear_when_already_empty(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(1)))
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(fired, [])

    def test_fires_on_select_no_op_is_silent(self):
        # ctx.select() with a set already containing those ids → no change.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(1)))
        b._state.selected = {'a'}
        b.select(['a'], replace=False)
        b.drain_main_queue()
        self.assertEqual(fired, [])

    def test_exception_routed_to_error(self):
        def bad(ctx, ids):
            raise RuntimeError('selection boom')
        b = Browser(BrowserConfig(_headless=True, on_selection_change=bad))
        b._state.selected = {'a'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertIn('selection boom', b.error_text)
        self.assertIn('on_selection_change', b.error_text)


class TestOnQuit(unittest.TestCase):

    def test_fires_once_with_code(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_quit=lambda ctx, code: fired.append(code)))
        b._quit_code = 7
        b._fire_on_quit()
        b._fire_on_quit()  # second call is a no-op
        self.assertEqual(fired, [7])

    def test_default_code_is_zero(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_quit=lambda ctx, code: fired.append(code)))
        b._fire_on_quit()
        self.assertEqual(fired, [0])

    def test_exception_swallowed(self):
        def bad(ctx, code):
            raise RuntimeError('cleanup blew up')
        b = Browser(BrowserConfig(_headless=True, on_quit=bad))
        # Must not raise.
        b._fire_on_quit()


def _ctx(b):
    """Build a real Context for the given (headless) browser."""
    return Context(b)


class TestOnExpandCollapse(unittest.TestCase):
    """``on_expand(ctx, ids)`` / ``on_collapse(ctx, ids)`` — drain-time
    set-diff of ``state.expanded``. Each fires once per drain with the
    list of ids that entered / left the set; a drain that nets to no
    change fires nothing.
    """

    def _tree(self, **kw):
        # Root has two branches (A, B) and a leaf C; A has a sub-branch
        # A1 (with leaf A1a) and leaf A2; B has leaf B1. Children are
        # pre-cached so expansion is synchronous (no worker needed).
        b = Browser(BrowserConfig(_headless=True, **kw))
        s = b._state
        s._children[None] = [
            Item(id='A', has_children=True),
            Item(id='B', has_children=True),
            Item(id='C'),
        ]
        s._children['A'] = [
            Item(id='A1', has_children=True),
            Item(id='A2'),
        ]
        s._children['A1'] = [Item(id='A1a')]
        s._children['B'] = [Item(id='B1')]
        mark_cursor_changed(b)
        b.drain_main_queue()
        return b

    def test_keyboard_right_fires_expand_once(self):
        ex, col = [], []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)),
                       on_collapse=lambda ctx, ids: col.append(list(ids)))
        try:
            ctx = _ctx(b)
            b._state.cursor = 0          # on A
            self.assertTrue(dispatch_key(b, ctx, 'right'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [['A']])
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_keyboard_left_fires_collapse_once(self):
        ex, col = [], []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)),
                       on_collapse=lambda ctx, ids: col.append(list(ids)))
        try:
            ctx = _ctx(b)
            b._state.expanded = {'A'}
            b._last_expanded = {'A'}     # already baselined as expanded
            b._state.cursor = 0          # on A
            self.assertTrue(dispatch_key(b, ctx, 'left'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(col, [['A']])
            self.assertEqual(ex, [])
        finally:
            b.stop_workers()

    def test_ctx_expand_other_than_cursor(self):
        # A programmatic expand of a node that is NOT the cursor fires
        # on_expand for that node.
        ex = []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)))
        try:
            b._state.cursor = 0          # cursor on A
            b.expand('B')                # expand B instead
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [['B']])
        finally:
            b.stop_workers()

    def test_repress_right_on_expanded_fires_nothing(self):
        ex, col = [], []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)),
                       on_collapse=lambda ctx, ids: col.append(list(ids)))
        try:
            ctx = _ctx(b)
            b._state.expanded = {'A'}
            b._last_expanded = {'A'}
            b._state.cursor = 0          # on A (already expanded)
            # Re-pressing right navigates to the first child; it does not
            # mutate the expanded set.
            self.assertTrue(dispatch_key(b, ctx, 'right'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [])
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_alt_right_recursive_fires_one_call_with_all_ids(self):
        ex = []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)))
        try:
            ctx = _ctx(b)
            b._state.cursor = 0          # on A → parent is root
            self.assertTrue(dispatch_key(b, ctx, 'alt-right'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            # Branches A, A1, B all open in ONE on_expand call.
            self.assertEqual(len(ex), 1)
            self.assertEqual(set(ex[0]), {'A', 'A1', 'B'})
        finally:
            b.stop_workers()

    def test_alt_left_recursive_fires_one_collapse_with_all_ids(self):
        col = []
        b = self._tree(on_collapse=lambda ctx, ids: col.append(list(ids)))
        try:
            ctx = _ctx(b)
            b._state.expanded = {'A', 'A1', 'B'}
            b._last_expanded = {'A', 'A1', 'B'}
            b._state.cursor = 0          # on A → parent is root
            self.assertTrue(dispatch_key(b, ctx, 'alt-left'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(len(col), 1)
            self.assertEqual(set(col[0]), {'A', 'A1', 'B'})
        finally:
            b.stop_workers()

    def test_collapse_all_fires_one_call_with_whole_set(self):
        col = []
        b = self._tree(on_collapse=lambda ctx, ids: col.append(list(ids)))
        try:
            b._state.expanded = {'A', 'A1', 'B'}
            b._last_expanded = {'A', 'A1', 'B'}
            b.collapse_all()
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(len(col), 1)
            self.assertEqual(set(col[0]), {'A', 'A1', 'B'})
        finally:
            b.stop_workers()

    def test_expand_subtree_fires_one_call(self):
        ex = []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)))
        try:
            b.expand_subtree('A')
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            # A and its cached sub-branch A1 open together in one call.
            self.assertEqual(len(ex), 1)
            self.assertEqual(set(ex[0]), {'A', 'A1'})
        finally:
            b.stop_workers()

    def test_add_and_remove_in_one_drain_fires_both(self):
        ex, col = [], []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(set(ids)),
                       on_collapse=lambda ctx, ids: col.append(set(ids)))
        try:
            b._state.expanded = {'A'}
            b._last_expanded = {'A'}
            # Net change in one drain: A leaves, B enters.
            b._state.expanded = {'B'}
            b._fire_expand_collapse_if_pending()
            self.assertEqual(col, [{'A'}])
            self.assertEqual(ex, [{'B'}])
        finally:
            b.stop_workers()

    def test_expand_then_collapse_same_id_nets_to_nothing(self):
        ex, col = [], []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)),
                       on_collapse=lambda ctx, ids: col.append(list(ids)))
        try:
            # Add then remove the same id between two fires → the set is
            # back at baseline at diff time → neither hook fires.
            b._state.expanded.add('A')
            b._state.expanded.discard('A')
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [])
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_startup_expand_before_run_fires_on_first_drain(self):
        # ``b.expand(x)`` issued before the loop runs is seen by the
        # first drain because ``_last_expanded`` starts empty.
        ex = []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)))
        try:
            self.assertEqual(b._last_expanded, set())
            b.expand('A')                # pre-run expansion
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [['A']])
        finally:
            b.stop_workers()

    def test_missing_handlers_are_silent(self):
        b = self._tree()                 # no on_expand / on_collapse
        try:
            b._state.expanded = {'A'}
            b._fire_expand_collapse_if_pending()
            # Baseline still advances so the no-handler case can't leak a
            # stale delta into a later fire.
            self.assertEqual(b._last_expanded, {'A'})
        finally:
            b.stop_workers()

    def test_exception_routed_to_error(self):
        def bad(ctx, ids):
            raise RuntimeError('expand boom')
        b = self._tree(on_expand=bad)
        try:
            b._state.expanded = {'A'}
            b._fire_expand_collapse_if_pending()
            b.drain_main_queue()
            self.assertIn('expand boom', b.error_text)
            self.assertIn('on_expand', b.error_text)
        finally:
            b.stop_workers()


class TestScopeReBaseline(unittest.TestCase):
    """A scope transition restores a per-scope expanded set; that restore
    must NOT masquerade as expands / collapses. ``scope_into`` /
    ``scope_out`` (Browser methods AND the keyboard handlers) re-baseline
    ``_last_expanded`` after the transition, so only ``on_scope_change``
    fires.
    """

    def _seed(self, b):
        b.update_data([
            ('upsert', 'a', None, {'has_children': True}),
            ('upsert', 'b', None, {'has_children': True}),
            ('upsert', 'a1', 'a', {'has_children': True}),
            ('upsert', 'a2', 'a', {}),
        ])
        b.drain_main_queue()

    def _hooks(self, ex, col, scope):
        return dict(
            on_expand=lambda ctx, ids: ex.append(list(ids)),
            on_collapse=lambda ctx, ids: col.append(list(ids)),
            on_scope_change=lambda ctx, sid, prev, d: scope.append((sid, d)),
        )

    def test_scope_into_with_restored_set_fires_only_scope(self):
        ex, col, scope = [], [], []
        b = Browser(BrowserConfig(_headless=True,
                                  **self._hooks(ex, col, scope)))
        self._seed(b)
        # Give the target scope 'a' a restored expanded set so the
        # transition would naively look like an expand of {a1}.
        b._state._expanded_by_scope['a'] = {'a1'}
        try:
            b.scope_into('a')
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(scope[-1], ('a', 'in'))
            self.assertEqual(ex, [])
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_scope_out_fires_only_scope(self):
        ex, col, scope = [], [], []
        b = Browser(BrowserConfig(_headless=True,
                                  **self._hooks(ex, col, scope)))
        self._seed(b)
        try:
            b.scope_into('a')
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            # The expanded set under 'a' differs from root's; scoping out
            # restores root's set — must not fire expand/collapse.
            b._state.expanded.add('a1')      # mutate while scoped in
            b.scope_out()
            b.drain_main_queue()
            ex.clear(); col.clear()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(scope[-1], (None, 'out'))
            self.assertEqual(ex, [])
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_keyboard_scope_down_up_re_baselines(self):
        # The keyboard alt-down / alt-up handlers bypass Browser.scope_*
        # and operate on state directly; they must re-baseline too.
        ex, col, scope = [], [], []
        b = Browser(BrowserConfig(_headless=True,
                                  **self._hooks(ex, col, scope)))
        self._seed(b)
        b._state._expanded_by_scope['a'] = {'a1'}
        try:
            ctx = _ctx(b)
            b._state.cursor = 0          # on 'a'
            self.assertTrue(dispatch_key(b, ctx, 'alt-down'))
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(scope[-1], ('a', 'in'))
            self.assertEqual(ex, [])
            self.assertEqual(col, [])
            # Scope back out.
            self.assertTrue(dispatch_key(b, ctx, 'alt-up'))
            b.drain_main_queue()
            ex.clear(); col.clear()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(scope[-1], (None, 'out'))
            self.assertEqual(ex, [])
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_keyboard_scope_down_into_uncached_no_spurious_expand(self):
        # Scoping into a branch whose children aren't cached makes the
        # keyboard handler post an async ``ctx.expand`` for the scope row
        # to kick the fetch. That scope-row expansion must NOT later
        # surface as a user-facing on_expand — the re-baseline accounts
        # for it (mirrors Browser.scope_into, which expands synchronously
        # before re-baselining).
        ex, col, scope = [], [], []
        b = Browser(BrowserConfig(
            _headless=True,
            get_children=lambda pid: ([Item(id='z1')] if pid == 'z' else []),
            **self._hooks(ex, col, scope)))
        # Root has branch 'z' with NO cached children.
        b.update_data([('upsert', 'z', None, {'has_children': True})])
        b.drain_main_queue()
        b.start_workers()                # so the uncached fetch can settle
        try:
            ctx = _ctx(b)
            b._state.cursor = 0          # on 'z'
            self.assertTrue(dispatch_key(b, ctx, 'alt-down'))
            b.run_until_idle()           # let the worker deliver z's kids
            b._fire_expand_collapse_if_pending()
            self.assertEqual(scope[-1], ('z', 'in'))
            self.assertEqual(ex, [])     # no spurious expand of the scope row
            self.assertEqual(col, [])
        finally:
            b.stop_workers()

    def test_genuine_expand_after_scope_fires_normally(self):
        # Baseline must be correctly re-anchored: a real expand after a
        # scope transition fires on_expand as usual.
        ex = []
        b = Browser(BrowserConfig(_headless=True,
                    on_expand=lambda ctx, ids: ex.append(list(ids))))
        self._seed(b)
        try:
            b.scope_into('a')
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            ex.clear()
            b.expand('a1')               # genuine expand inside scope 'a'
            b.drain_main_queue()
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [['a1']])
        finally:
            b.stop_workers()


class TestDefaultsAreNoOp(unittest.TestCase):

    def test_no_hooks_no_explosion(self):
        b = Browser(BrowserConfig(_headless=True))
        # All fire methods should be safe no-ops.
        b._cursor_change_pending = True
        b._fire_cursor_change_if_pending()
        b._fire_scope_change()
        b._fire_selection_change()
        b._state.expanded = {'x'}
        b._fire_expand_collapse_if_pending()
        b._fire_on_quit()


if __name__ == '__main__':
    unittest.main()
