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
# ``_fire_context_menu`` seeds the unified modal-anchor slot (#1101) via the
# ``_list_pane_bounds`` / ``_list_cursor_cell`` helpers, which live in
# 060-context and read the live layout through ``term_size`` / ``layout_panes``
# there. The concatenated build resolves all these by shared namespace; the
# isolated load wires them across modules.
_state._list_pane_bounds = _context._list_pane_bounds
_state._list_cursor_cell = _context._list_cursor_cell
_context.term_size = _term.term_size
_context.layout_panes = _render.layout_panes

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
_actions._resolve_landing = _state._resolve_landing
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
Mode = _state.Mode
mark_cursor_changed = _state.mark_cursor_changed
dispatch_key = _actions.dispatch_key
complete = _state.complete
clear_children = _state.clear_children
upsert = _state.upsert


def _seed(b, items):
    b.update_data([
        ('upsert', it.id, None, {k: v for k, v in vars(it).items()
                                 if not k.startswith('_')})
        for it in items
    ])
    b.drain_main_queue()


def _err_log(b):
    """Joined message log — ``Browser.error`` always appends to it."""
    return '\n'.join(b._log)


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
        self.assertIn('boom', _err_log(b))
        self.assertIn('on_cursor_change', _err_log(b))


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
        self.assertIn('nope', _err_log(b))
        self.assertIn('on_scope_change', _err_log(b))


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
        self.assertIn('selection boom', _err_log(b))
        self.assertIn('on_selection_change', _err_log(b))

    def test_payload_is_in_selection_insertion_order(self):
        # ``state.selected`` is an OrderedSet, so the emitted id list comes
        # out in the order ids were selected — NOT sorted. Use ids whose
        # selection order differs from their sorted order to pin it.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(ids)))
        b.select(['c', 'a', 'b'], replace=True)
        b.drain_main_queue()
        self.assertEqual(fired, [['c', 'a', 'b']])

    def test_incremental_adds_accumulate_in_order(self):
        # Successive non-replace selects append in add-order; the final
        # fire reflects the full insertion order (not sorted, no dupes).
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(ids)))
        b.select(['b'], replace=False)
        b.select(['a'], replace=False)
        b.select(['c'], replace=False)
        b.drain_main_queue()
        self.assertEqual(fired[-1], ['b', 'a', 'c'])


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

    def test_missing_handlers_skip_prep_no_snapshot(self):
        # With BOTH on_expand and on_collapse unset (#627), the fire path
        # early-returns BEFORE the set diff / snapshot — so ``_last_expanded``
        # is NOT advanced. Hooks are construction-time-fixed, so a snapshot
        # going stale while unset is harmless (matches on_cursor_change).
        b = self._tree()                 # no on_expand / on_collapse
        try:
            self.assertEqual(b._last_expanded, set())   # initial baseline
            b._state.expanded = {'A'}
            b._fire_expand_collapse_if_pending()
            # Prep skipped → snapshot left at its initial value, NOT {'A'}.
            self.assertEqual(b._last_expanded, set())
        finally:
            b.stop_workers()

    def test_one_handler_set_still_runs_prep(self):
        # The early-return needs BOTH unset; with only on_expand set the
        # diff/snapshot still run (and a collapse with no on_collapse is a
        # silent-but-snapshotted no-op).
        ex = []
        b = self._tree(on_expand=lambda ctx, ids: ex.append(list(ids)))
        try:
            b._state.expanded = {'A'}
            b._fire_expand_collapse_if_pending()
            self.assertEqual(ex, [['A']])
            self.assertEqual(b._last_expanded, {'A'})   # prep ran
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
            self.assertIn('expand boom', _err_log(b))
            self.assertIn('on_expand', _err_log(b))
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


class TestOnChildrenLoaded(unittest.TestCase):
    """``on_children_loaded(ctx, parent_ids)`` fires once per drain with the
    list of parent ids whose ``get_children`` fetch SETTLED this drain.

    These synchronous tests drive the two genuine-settlement sites by hand
    — the ``complete`` op (``update_data`` / worker batch tail) and the
    ``apply_children_results`` legacy deque (``set_children``) — and pin the
    critical exclusion: ``clear_children`` also clears ``_loading`` but
    DROPS the cache, so it must NOT fire. Worker-delivery timing lives in
    ``test/async_/test_children_loaded.py``.
    """

    def test_complete_op_fires_with_parent(self):
        # A ``complete`` op (the worker's batch tail) settles loading →
        # fires once with the parent id; children are available.
        fired = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: fired.append(list(pids))))
        b._state._loading['p'] = True
        b.update_data([upsert('c', 'p', title='C'), complete('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        self.assertEqual(fired, [['p']])

    def test_complete_op_children_available_at_fire(self):
        # At fire time ``ctx.cached_children(parent)`` is populated.
        seen = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: seen.append(
                {pid: ctx.cached_children(pid) for pid in pids})))
        b._state._loading['p'] = True
        b.update_data([upsert('c', 'p', title='C'), complete('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        self.assertEqual(len(seen), 1)
        self.assertEqual([it.id for it in seen[0]['p']], ['c'])

    def test_empty_complete_fires_with_empty_children(self):
        # ``get_children`` returning ``[]`` settles via a bare
        # ``complete(p)`` and a ``_children[p] = []`` cache entry (the
        # real worker creates the latter in ``_post_children_delivery``;
        # seeded here to model the full delivery). Fires with
        # ``cached_children == []`` (not None). The worker-flow timing of
        # this is pinned in ``test/async_/test_children_loaded.py``.
        seen = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: seen.append(
                {pid: ctx.cached_children(pid) for pid in pids})))
        b._state._children['p'] = []        # what _post_children_delivery does
        b._state._loading['p'] = True
        b.update_data([complete('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        self.assertEqual(seen, [{'p': []}])

    def test_apply_children_results_fires(self):
        # The legacy ``set_children`` deque path settles via
        # ``apply_children_results`` — it too must fire.
        fired = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: fired.append(list(pids))))
        b._state._loading['p'] = True
        b.set_children('p', [Item(id='c')])
        b.apply_children_results()
        b._fire_children_loaded_if_pending()
        self.assertEqual(fired, [['p']])

    def test_clear_children_does_not_fire(self):
        # ``clear_children`` sets ``_loading=False`` but DROPS the cache
        # (cached_children → None). It is NOT a settlement → must not fire.
        fired = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: fired.append(list(pids))))
        # Seed a settled parent first, fire & drain it so the pending set
        # is empty, then clear it.
        b._state._loading['p'] = True
        b.update_data([upsert('c', 'p'), complete('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        fired.clear()
        b.update_data([clear_children('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        self.assertEqual(fired, [])
        # And the cache really did revert to "not fetched".
        self.assertIsNone(Context(b).cached_children('p'))

    def test_batches_multiple_parents_in_one_drain(self):
        # Several settlements before a drain coalesce into ONE call.
        fired = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: fired.append(set(pids))))
        b._state._loading.update({'p': True, 'q': True})
        b.update_data([upsert('c', 'p'), complete('p'),
                       upsert('d', 'q'), complete('q')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], {'p', 'q'})

    def test_second_drain_does_not_refire(self):
        # The pending set is drained on fire — a subsequent drain with no
        # new settlement fires nothing.
        fired = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_children_loaded=lambda ctx, pids: fired.append(list(pids))))
        b._state._loading['p'] = True
        b.update_data([upsert('c', 'p'), complete('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        b._fire_children_loaded_if_pending()
        self.assertEqual(fired, [['p']])

    def test_missing_handler_skips_harvest(self):
        # No handler installed (#627): the settlement harvest is gated on
        # ``_on_children_loaded is not None``, so the pending set stays EMPTY
        # — no per-settlement set.update() work when nobody is listening.
        # (``_set_loading(..., settled=True)`` still records into the cheap
        # per-pass ``_settled_parents`` list; only the harvest is skipped.)
        b = Browser(BrowserConfig(_headless=True))
        b._state._loading['p'] = True
        b.update_data([complete('p')])
        b.drain_main_queue()
        self.assertEqual(b._children_loaded_pending, set())
        # The fire path short-circuits on the empty set — still a no-op.
        b._fire_children_loaded_if_pending()
        self.assertEqual(b._children_loaded_pending, set())

    def test_exception_routed_to_error(self):
        def bad(ctx, pids):
            raise RuntimeError('loaded boom')
        b = Browser(BrowserConfig(_headless=True, on_children_loaded=bad))
        b._state._loading['p'] = True
        b.update_data([complete('p')])
        b.drain_main_queue()
        b._fire_children_loaded_if_pending()
        b.drain_main_queue()
        self.assertIn('loaded boom', _err_log(b))
        self.assertIn('on_children_loaded', _err_log(b))
        # Pending still cleared despite the throw.
        self.assertEqual(b._children_loaded_pending, set())


class TestOnSearchChange(unittest.TestCase):
    """``on_search_change(ctx, query)`` — drain-time diff of the effective
    search query against ``_last_search_query``. Fires once per drain on
    the final value; clearing to ``''`` is a change → fires once; an
    identical re-set is a no-op.
    """

    def test_fires_on_change_with_new_query(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_search_change=lambda ctx, q: fired.append(q)))
        b.set_search_query('foo')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(fired, ['foo'])

    def test_payload_matches_ctx_search_query(self):
        # The query payload is what the recipe would read off ctx.
        seen = []
        b = Browser(BrowserConfig(_headless=True,
                    on_search_change=lambda ctx, q: seen.append(
                        (q, ctx.search_query))))
        b.set_search_query('bar')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(seen, [('bar', 'bar')])

    def test_clear_fires_once_with_empty_string(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_search_change=lambda ctx, q: fired.append(q)))
        # Establish a non-empty query and fire it first.
        b.set_search_query('hello')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        fired.clear()
        # Clearing back to '' is a real change → fires once with ''.
        b.clear_search()
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(fired, [''])
        # A second drain with no further change does not re-fire.
        b._fire_search_change_if_pending()
        self.assertEqual(fired, [''])

    def test_identical_reset_is_no_op(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_search_change=lambda ctx, q: fired.append(q)))
        b.set_search_query('same')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(fired, ['same'])
        # Re-setting the same query does not change the effective value.
        b.set_search_query('same')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(fired, ['same'])

    def test_coalesces_rapid_edits_to_final_value(self):
        # Several edits before a single drain coalesce to the latest.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_search_change=lambda ctx, q: fired.append(q)))
        b.set_search_query('f')
        b.set_search_query('fo')
        b.set_search_query('foo')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(fired, ['foo'])

    def test_missing_handler_skips_prep_no_snapshot(self):
        # No handler (#627): the fire path early-returns BEFORE the diff, so
        # ``_last_search_query`` is NOT advanced. Acceptable because hooks
        # are construction-time-fixed (matches on_cursor_change).
        b = Browser(BrowserConfig(_headless=True))
        b.set_search_query('x')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        self.assertEqual(b._last_search_query, '')   # snapshot left at init

    def test_exception_routed_to_error(self):
        def bad(ctx, q):
            raise RuntimeError('search boom')
        b = Browser(BrowserConfig(_headless=True, on_search_change=bad))
        b.set_search_query('q')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        b.drain_main_queue()
        self.assertIn('search boom', _err_log(b))
        self.assertIn('on_search_change', _err_log(b))


class TestOnFilterChange(unittest.TestCase):
    """``on_filter_change(ctx, filters)`` — drain-time diff of
    ``tuple(self.filters)`` against ``_last_filters``. ``set`` / ``add`` /
    ``clear`` fire; an identical re-set is a no-op; ``add_filter('')`` is a
    no-op because ``filters`` drops empties.
    """

    def test_set_filters_fires_with_tuple(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_filter_change=lambda ctx, f: fired.append(f)))
        b.set_filters(['a', 'b'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(fired, [('a', 'b')])

    def test_payload_matches_ctx_filters(self):
        seen = []
        b = Browser(BrowserConfig(_headless=True,
                    on_filter_change=lambda ctx, f: seen.append(
                        (f, ctx.filters))))
        b.set_filters(['x'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(seen, [(('x',), ('x',))])

    def test_add_filter_fires(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_filter_change=lambda ctx, f: fired.append(f)))
        b.set_filters(['a'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        fired.clear()
        b.add_filter('b')
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(fired, [('a', 'b')])

    def test_clear_filters_fires_with_empty_tuple(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_filter_change=lambda ctx, f: fired.append(f)))
        b.set_filters(['a', 'b'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        fired.clear()
        b.clear_filters()
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(fired, [()])

    def test_identical_reset_is_no_op(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_filter_change=lambda ctx, f: fired.append(f)))
        b.set_filters(['a', 'b'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(fired, [('a', 'b')])
        # Re-setting the identical list does not change the tuple.
        b.set_filters(['a', 'b'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(fired, [('a', 'b')])

    def test_add_empty_filter_is_no_op(self):
        # add_filter('') returns early (no post); filters drops empties so
        # even if it ran the effective tuple would be unchanged.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_filter_change=lambda ctx, f: fired.append(f)))
        b.add_filter('')
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(fired, [])

    def test_missing_handler_skips_prep_no_snapshot(self):
        # No handler (#627): the fire path early-returns BEFORE building the
        # filters tuple, so ``_last_filters`` is NOT advanced. Acceptable
        # because hooks are construction-time-fixed (matches on_cursor_change).
        b = Browser(BrowserConfig(_headless=True))
        b.set_filters(['z'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        self.assertEqual(b._last_filters, ())        # snapshot left at init

    def test_exception_routed_to_error(self):
        def bad(ctx, f):
            raise RuntimeError('filter boom')
        b = Browser(BrowserConfig(_headless=True, on_filter_change=bad))
        b.set_filters(['q'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        b.drain_main_queue()
        self.assertIn('filter boom', _err_log(b))
        self.assertIn('on_filter_change', _err_log(b))


class TestOnResize(unittest.TestCase):
    """``on_resize(ctx, cols, rows)`` — fires once per main-loop tick when
    the *pane layout* changed since the last fire (terminal resize, split
    selector, list-ratio nudge, pane toggle), not just on SIGWINCH.

    ``_layout_for`` (050-render) records a layout SIGNATURE on the Browser
    every paint — ``(cols, rows, preview_rect, children_rect)`` — and
    ``_fire_resize_if_layout_changed`` fires when that differs from the
    last-fired signature, passing the signature's ``cols``/``rows`` to the
    callback. ``term_size`` is stubbed onto the render module here (the
    production build resolves it by concatenation); ``set_split`` /
    ``set_list_ratio`` are driven through their real (post → drain) path
    so the wiring is exercised end-to-end.
    """

    def _stub_term_size(self, ret):
        """Patch ``term_size`` onto _render (read by ``_layout_for``); return
        a restorer. ``ret`` is a ``(cols, rows)`` tuple or a callable
        returning one (or raising).
        """
        prev = getattr(_render, 'term_size', None)
        had = hasattr(_render, 'term_size')
        _render.term_size = ret if callable(ret) else (lambda: ret)

        def restore():
            if had:
                _render.term_size = prev
            elif hasattr(_render, 'term_size'):
                del _render.term_size
        return restore

    def _paint(self, b):
        """Recompute + record the layout signature, mirroring a render pass
        (the run loop calls ``_layout_for`` from ``render_full`` /
        ``render_partial`` right before re-deriving fires next tick).
        """
        _render._layout_for(b)

    def test_fires_on_terminal_size_change(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        restore = self._stub_term_size((80, 24))
        try:
            self._paint(b)                    # baseline at 80x24
            b._fire_resize_if_layout_changed()
            fired.clear()                     # ignore the initial fire
            restore()
            restore = self._stub_term_size((120, 40))
            self._paint(b)                    # terminal grew
            b._fire_resize_if_layout_changed()
            self.assertEqual(fired, [(120, 40)])
        finally:
            restore()

    def test_fires_on_split_change_same_size(self):
        # A split-selector change leaves cols/rows untouched but reshapes
        # the preview pane — must still fire (the SIGWINCH-only path didn't).
        fired = []
        b = Browser(BrowserConfig(_headless=True, split='h', show_preview=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        restore = self._stub_term_size((100, 40))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()
            fired.clear()
            b.set_split('v')                  # real post → drain path
            b.drain_main_queue()
            self.assertEqual(b.split, 'v')
            self._paint(b)
            b._fire_resize_if_layout_changed()
            self.assertEqual(fired, [(100, 40)])   # cols/rows unchanged
        finally:
            restore()

    def test_fires_on_list_ratio_change_same_size(self):
        # A list-ratio nudge leaves cols/rows untouched but moves the
        # preview pane (its height in 'h', width in 'v') — must fire.
        fired = []
        b = Browser(BrowserConfig(_headless=True, split='h', list_ratio=0.30,
                    show_preview=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        restore = self._stub_term_size((100, 40))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()
            fired.clear()
            b.set_list_ratio(0.70)            # real post → drain path
            b.drain_main_queue()
            self._paint(b)
            b._fire_resize_if_layout_changed()
            self.assertEqual(fired, [(100, 40)])   # cols/rows unchanged
        finally:
            restore()

    def test_no_fire_when_layout_unchanged(self):
        # Two identical paints: the signature matches the baseline, so a
        # second fire is a no-op (the broadened fire must not loop).
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        restore = self._stub_term_size((100, 40))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()    # initial fire
            self._paint(b)                        # nothing changed
            b._fire_resize_if_layout_changed()
            self.assertEqual(fired, [(100, 40)])  # exactly once
        finally:
            restore()

    def test_second_call_does_not_refire(self):
        # No-loop guard: after a fire, repeated calls with no new paint and
        # an unchanged signature do nothing.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        restore = self._stub_term_size((90, 30))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()
            b._fire_resize_if_layout_changed()
            b._fire_resize_if_layout_changed()
            self.assertEqual(fired, [(90, 30)])
        finally:
            restore()

    def test_unset_handler_is_noop(self):
        # No on_resize (#627): a layout change + fire is a safe no-op.
        b = Browser(BrowserConfig(_headless=True, show_preview=True))  # no on_resize
        restore = self._stub_term_size((100, 40))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()       # must not raise
            b.set_split('v')
            b.drain_main_queue()
            self._paint(b)
            b._fire_resize_if_layout_changed()       # still a no-op
        finally:
            restore()

    def test_no_layout_yet_does_not_fire(self):
        # Before the first paint the signature is unset → no fire, no crash
        # (headless runs never paint, so this is also the headless contract).
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        self.assertIsNone(b._layout_sig)
        b._fire_resize_if_layout_changed()
        self.assertEqual(fired, [])

    def test_no_tty_zero_dims_do_not_fire(self):
        # Headless / no-tty term_size returns (0, 0); ``_layout_for`` still
        # records a signature, but the fire must not emit garbage dims.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_resize=lambda ctx, c, r: fired.append((c, r))))
        restore = self._stub_term_size((0, 0))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()
            self.assertEqual(fired, [])
        finally:
            restore()

    def test_exception_in_handler_routed_to_error(self):
        def bad(ctx, c, r):
            raise RuntimeError('resize boom')
        b = Browser(BrowserConfig(_headless=True, on_resize=bad))
        restore = self._stub_term_size((110, 35))
        try:
            self._paint(b)
            b._fire_resize_if_layout_changed()
            b.drain_main_queue()
            self.assertIn('resize boom', _err_log(b))
            self.assertIn('on_resize', _err_log(b))
            # Baseline still advanced despite the throw — a re-fire loop on
            # the same (still-failing) layout would spam the error log.
            fired_again = []
            b._fire_resize_if_layout_changed()
            self.assertEqual(_err_log(b).count('resize boom'), 1)
        finally:
            restore()


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
        b._children_loaded_pending = {'p'}
        b._fire_children_loaded_if_pending()
        b.set_search_query('q')
        b.drain_main_queue()
        b._fire_search_change_if_pending()
        b.set_filters(['f'])
        b.drain_main_queue()
        b._fire_filter_change_if_pending()
        b._layout_sig = (80, 24, None, None)
        b._fire_resize_if_layout_changed()
        b._fire_on_quit()
        # on_context_menu fire is a no-op when unset (with and without an
        # anchor — the right-click trigger always passes one).
        b._fire_context_menu()
        b._fire_context_menu(anchor=(3, 5))


class TestOnContextMenu(unittest.TestCase):
    """The ``on_context_menu`` hook + its right-click default trigger.

    These drive a REAL headless Browser (not a fake ctx) so the hook is
    called with exactly the Context the framework builds — browse-tui
    swallows hook exceptions, so a fake ctx would hide an arity bug. The
    right-click cases feed a synthesized ``right-click:R:C`` through the
    same ``dispatch_key`` entry the app uses, patching ``term_size`` /
    ``layout_panes`` onto ``_actions`` like ``TestMouseDispatch`` does so
    the list-pane geometry resolves.
    """

    def _patch_term(self, cols, rows):
        prev_ts = getattr(_actions, 'term_size', None)
        prev_lp = getattr(_actions, 'layout_panes', None)
        had_ts = hasattr(_actions, 'term_size')
        had_lp = hasattr(_actions, 'layout_panes')
        _actions.term_size = lambda: (cols, rows)
        _actions.layout_panes = _render.layout_panes

        def restore():
            if had_ts:
                _actions.term_size = prev_ts
            elif hasattr(_actions, 'term_size'):
                del _actions.term_size
            if had_lp:
                _actions.layout_panes = prev_lp
            elif hasattr(_actions, 'layout_panes'):
                del _actions.layout_panes

        return restore

    def test_fire_calls_handler_with_single_context_arg(self):
        """The hook is invoked with EXACTLY one positional arg, a Context."""
        seen = []
        b = Browser(BrowserConfig(_headless=True,
                    on_context_menu=lambda ctx: seen.append(ctx)))
        try:
            b._fire_context_menu()
            self.assertEqual(len(seen), 1)
            self.assertIsInstance(seen[0], Context)
        finally:
            b.stop_workers()

    def test_fire_seeds_modal_anchor_from_click_row(self):
        """During the fire, a right-click click cell seeds the modal-anchor slot.

        ``_fire_context_menu`` opens a modal-anchor CHAIN (#1101): the click
        cell's ROW seeds ``_modal_anchor``'s ``y`` (the right-click trigger
        drops the menu under the pointer), with the list pane's column span as
        the horizontal extents. ``ctx.menu`` is a no-op on a headless Browser,
        so observe the seeded slot directly off the Browser inside the handler.
        """
        slots = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_context_menu=lambda ctx: slots.append(ctx._browser._modal_anchor)))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        restore = self._patch_term(80, 40)
        try:
            _context.term_size = lambda: (80, 40)   # match the patched layout
            layout = _render.layout_panes(80, 40, show_preview=False,
                                          show_children_pane=False)
            L = layout['list'].left
            R = layout['list'].right - 1
            b._fire_context_menu(anchor=(7, 3))
            self.assertEqual(slots, [(7, L, R)])     # click ROW + pane span
            # Cleared after the fire so the next open re-seeds.
            self.assertIsNone(b._modal_anchor)
        finally:
            _context.term_size = _term.term_size
            restore()
            b.stop_workers()

    def test_fire_swallows_handler_exception_to_error_log(self):
        def boom(ctx):
            raise RuntimeError('kaboom')

        b = Browser(BrowserConfig(_headless=True, on_context_menu=boom))
        try:
            b._fire_context_menu()  # must not raise
            b.drain_main_queue()
            self.assertIn('on_context_menu', _err_log(b))
            self.assertIn('kaboom', _err_log(b))
            # Slot still cleared even when the handler raised.
            self.assertIsNone(b._modal_anchor)
        finally:
            b.stop_workers()

    def test_fire_clears_modal_anchor_even_when_handler_raises(self):
        """The modal-anchor slot is cleared in ``finally`` — a raising handler
        that had advanced the slot does not leak it past the fire."""
        def boom(ctx):
            # Simulate a menu having advanced the chain's anchor, then the
            # handler blowing up before the fire returns normally.
            ctx._browser._modal_anchor = (9, 1, 20)
            raise RuntimeError('kaboom')

        b = Browser(BrowserConfig(_headless=True, on_context_menu=boom))
        try:
            b._fire_context_menu()  # must not raise
            self.assertIsNone(b._modal_anchor)  # cleared despite raise
        finally:
            b.stop_workers()

    def test_right_click_fires_and_repositions_cursor(self):
        """A right-click on a list row moves the cursor there, then fires."""
        seen_cursor = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_context_menu=lambda ctx: seen_cursor.append(
                ctx.cursor.id if ctx.cursor else None)))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c'), Item(id='d')])
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx(b)
            layout = _render.layout_panes(80, 40, show_preview=False,
                                          show_children_pane=False)
            target_row = layout['list'].top + 2  # third row → item 'c'
            self.assertTrue(
                dispatch_key(b, ctx, f'right-click:{target_row}:5'))
            self.assertEqual(b._state.cursor, 2)        # cursor moved first
            self.assertEqual(seen_cursor, ['c'])        # then fired w/ target
        finally:
            restore()
            b.stop_workers()

    def test_right_click_seeds_modal_anchor_from_click_row(self):
        """The right-click trigger seeds the modal-anchor slot from the click
        ROW (#1101), with the list pane's column span as the extents."""
        slots = []
        b = Browser(BrowserConfig(
            _headless=True,
            on_context_menu=lambda ctx: slots.append(
                ctx._browser._modal_anchor)))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        restore = self._patch_term(80, 40)
        try:
            _context.term_size = lambda: (80, 40)   # match the patched layout
            ctx = _ctx(b)
            layout = _render.layout_panes(80, 40, show_preview=False,
                                          show_children_pane=False)
            r = layout['list'].top + 1
            L = layout['list'].left
            R = layout['list'].right - 1
            dispatch_key(b, ctx, f'right-click:{r}:9')
            self.assertEqual(slots, [(r, L, R)])     # click ROW + pane span
        finally:
            _context.term_size = _term.term_size
            restore()
            b.stop_workers()

    def test_right_click_no_handler_is_noop_cursor_unmoved(self):
        """A None handler: right-click moves nothing and raises nothing."""
        b = Browser(BrowserConfig(_headless=True))  # no on_context_menu
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c'), Item(id='d')])
        b._state.cursor = 1
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx(b)
            layout = _render.layout_panes(80, 40, show_preview=False,
                                          show_children_pane=False)
            target_row = layout['list'].top + 3  # would be item 'd'
            # Returns True (event consumed) but does no prep / no fire.
            self.assertTrue(
                dispatch_key(b, ctx, f'right-click:{target_row}:5'))
            self.assertEqual(b._state.cursor, 1)        # unmoved
            self.assertEqual(_err_log(b), '')           # nothing logged
        finally:
            restore()
            b.stop_workers()

    # ---- keyboard triggers (\ and F1, permanent context-menu) -----------
    # Since #1061 ``\`` and F1 PERMANENTLY open the context menu in NORMAL
    # mode (no longer cycle-layout / toggle-help). When the recipe sets no
    # ``on_context_menu`` the press is a harmless no-op.

    def test_backslash_fires_when_handler_set_in_normal_mode(self):
        fired = []
        b = Browser(BrowserConfig(
            _headless=True, on_context_menu=lambda ctx: fired.append(ctx)))
        _seed(b, [Item(id='a'), Item(id='b')])
        try:
            ctx = _ctx(b)
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertTrue(dispatch_key(b, ctx, '\\'))
            self.assertEqual(len(fired), 1)
        finally:
            b.stop_workers()

    def test_f1_fires_when_handler_set_in_normal_mode(self):
        fired = []
        b = Browser(BrowserConfig(
            _headless=True, on_context_menu=lambda ctx: fired.append(ctx)))
        _seed(b, [Item(id='a'), Item(id='b')])
        try:
            ctx = _ctx(b)
            self.assertTrue(dispatch_key(b, ctx, 'f1'))
            self.assertEqual(len(fired), 1)
            # F1 no longer toggles help — it triggered the menu instead.
            self.assertFalse(b._help_mode)
        finally:
            b.stop_workers()

    def test_backslash_is_noop_when_no_handler(self):
        """No handler: ``\\`` triggers the menu, which is a no-op — it must
        NOT cycle the layout (its old default, removed in #1061) and must
        not raise."""
        b = Browser(BrowserConfig(_headless=True))  # no on_context_menu
        _seed(b, [Item(id='a'), Item(id='b')])
        try:
            ctx = _ctx(b)
            before = b.split
            self.assertTrue(dispatch_key(b, ctx, '\\'))  # consumed
            b.drain_main_queue()
            self.assertEqual(b.split, before)           # layout unchanged
            self.assertEqual(_err_log(b), '')           # nothing logged
        finally:
            b.stop_workers()

    def test_f1_is_noop_when_no_handler(self):
        """No handler: F1 triggers the menu (no-op) — it must NOT toggle the
        help screen (its old default, removed in #1061) and must not raise."""
        b = Browser(BrowserConfig(_headless=True))  # no on_context_menu
        _seed(b, [Item(id='a'), Item(id='b')])
        try:
            ctx = _ctx(b)
            self.assertFalse(b._help_mode)
            self.assertTrue(dispatch_key(b, ctx, 'f1'))  # consumed
            self.assertFalse(b._help_mode)              # help NOT toggled
            self.assertEqual(_err_log(b), '')           # nothing logged
        finally:
            b.stop_workers()

    def test_f1_does_not_fire_menu_in_search_edit_mode(self):
        """F1 falls through SEARCH_EDIT into normal dispatch — but the
        context-menu trigger is gated on NORMAL, so the menu must NOT
        open while a search query is being edited."""
        fired = []
        b = Browser(BrowserConfig(
            _headless=True, on_context_menu=lambda ctx: fired.append(ctx)))
        _seed(b, [Item(id='a'), Item(id='b')])
        try:
            ctx = _ctx(b)
            b._mode = Mode.SEARCH_EDIT
            dispatch_key(b, ctx, 'f1')
            self.assertEqual(fired, [])                  # menu not opened
        finally:
            b.stop_workers()

    def test_f1_does_not_fire_menu_in_filter_edit_mode(self):
        """Same gating for FILTER_EDIT: editing a filter must not pop the menu."""
        fired = []
        b = Browser(BrowserConfig(
            _headless=True, on_context_menu=lambda ctx: fired.append(ctx)))
        _seed(b, [Item(id='a'), Item(id='b')])
        try:
            ctx = _ctx(b)
            b._mode = Mode.FILTER_EDIT
            dispatch_key(b, ctx, 'f1')
            self.assertEqual(fired, [])                  # menu not opened
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
