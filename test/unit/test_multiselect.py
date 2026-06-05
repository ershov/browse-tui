"""Unit tests for multi-select keybindings and handlers (070-actions.py).

Exercises ``space`` / ``alt- `` toggle-and-move, ``ctrl-a`` select-all,
``ctrl-n`` deselect-all, and confirms ``default_actions()`` registers
the four new bindings. Driven through ``dispatch_key`` so the precondition
gates and key routing are part of every test path.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Wire up the cross-module references (production builds get them via
# concatenation; the loader needs them by hand).
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions._resolve_landing = _state._resolve_landing
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Action = _actions.Action
default_actions = _actions.default_actions
dispatch_key = _actions.dispatch_key


def _make_browser(**kw):
    """Build a headless Browser; tests call stop_workers in tearDown."""
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


def _ctx_for(browser):
    """Build a real Context for the given browser."""
    _context = load('_browse_tui_context', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


def _three_item_browser():
    """Browser pre-populated with three flat root children: a, b, c."""
    b = _make_browser()
    b._state._children[None] = [Item(id='a'), Item(id='b'), Item(id='c')]
    return b


# ---- space (toggle + cursor down) ----------------------------------------


class TestSpaceToggleDown(unittest.TestCase):

    def test_space_toggles_selection_and_moves_down(self):
        b = _three_item_browser()
        try:
            ctx = _ctx_for(b)
            self.assertEqual(b._state.cursor, 0)
            self.assertTrue(dispatch_key(b, ctx, 'space'))
            self.assertEqual(b._state.selected, {'a'})
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_space_on_already_selected_deselects(self):
        b = _three_item_browser()
        b._state.selected = {'a'}
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'space'))
            self.assertEqual(b._state.selected, set())
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_space_on_last_item_keeps_cursor(self):
        b = _three_item_browser()
        b._state.cursor = 2
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'space'))
            self.assertEqual(b._state.selected, {'c'})
            self.assertEqual(b._state.cursor, 2)
        finally:
            b.stop_workers()


# ---- alt-space (toggle + cursor up) --------------------------------------


class TestAltSpaceToggleUp(unittest.TestCase):

    def test_alt_space_toggles_and_moves_up(self):
        b = _three_item_browser()
        b._state.cursor = 2
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt- '))
            self.assertEqual(b._state.selected, {'c'})
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_alt_space_at_top_keeps_cursor(self):
        b = _three_item_browser()
        try:
            ctx = _ctx_for(b)
            self.assertEqual(b._state.cursor, 0)
            self.assertTrue(dispatch_key(b, ctx, 'alt- '))
            self.assertEqual(b._state.selected, {'a'})
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()


# ---- ctrl-a (select all visible) -----------------------------------------


class TestCtrlASelectAll(unittest.TestCase):

    def test_ctrl_a_selects_all_visible(self):
        b = _three_item_browser()
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-a'))
            self.assertEqual(b._state.selected, {'a', 'b', 'c'})
        finally:
            b.stop_workers()

    def test_ctrl_a_skips_pending_placeholders(self):
        # Parent expanded but children not yet cached → visible list
        # includes a 'pending' placeholder row whose item carries the
        # sentinel id. ctrl-a must skip it.
        b = _make_browser()
        parent = Item(id='p', has_children=True)
        b._state._children[None] = [parent]
        b._state.expanded.add('p')   # expanded but no _children['p']
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-a'))
            # Only 'p' (the normal row) is selected; the sentinel
            # placeholder id stays out of state.selected.
            self.assertEqual(b._state.selected, {'p'})
            # Sanity check: the placeholder really is in the visible list.
            kinds = [e.kind for e in _state.visible_items(b._state)]
            self.assertIn('pending', kinds)
        finally:
            b.stop_workers()

    def test_ctrl_a_drops_selection_of_hidden_rows(self):
        # Pre-selected hidden id is dropped: Ctrl-A first clears the set,
        # then re-adds visible rows. Hidden rows aren't in visible_items,
        # so they don't come back.
        b = _make_browser()
        a = Item(id='a')
        b_hidden = Item(id='b', hidden=True)
        c = Item(id='c')
        b._state._children[None] = [a, b_hidden, c]
        b._state.selected = {'a', 'b', 'c'}  # 'b' is hidden
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-a'))
            # 'b' is hidden → not in visible_items → not re-added.
            self.assertEqual(b._state.selected, {'a', 'c'})
        finally:
            b.stop_workers()

    def test_ctrl_a_drops_selection_of_collapsed_children(self):
        # Pre-selected child of a collapsed parent is dropped too —
        # it's not currently visible. Matches the WYSIWYG semantic.
        b = _make_browser()
        parent = Item(id='p', has_children=True)
        child = Item(id='p1')
        b._state._children[None] = [parent]
        b._state._children['p'] = [child]
        # Note: 'p' is NOT in state.expanded — child not visible.
        b._state.selected = {'p', 'p1'}
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-a'))
            # 'p' is visible (added); 'p1' is hidden inside collapse (dropped).
            self.assertEqual(b._state.selected, {'p'})
        finally:
            b.stop_workers()


# ---- ctrl-n (clear selection) --------------------------------------------


class TestCtrlNClear(unittest.TestCase):

    def test_ctrl_n_clears_selection(self):
        b = _three_item_browser()
        b._state.selected = {'a', 'c'}
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-n'))
            self.assertEqual(b._state.selected, set())
        finally:
            b.stop_workers()

    def test_ctrl_n_no_op_on_empty(self):
        b = _three_item_browser()
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'ctrl-n'))
            self.assertEqual(b._state.selected, set())
        finally:
            b.stop_workers()


# ---- default_actions registry --------------------------------------------


class TestDefaultActionsIncludesMultiSelect(unittest.TestCase):

    def test_multi_select_keys_present(self):
        keys = {a.key for a in default_actions()}
        for required in ('space', 'alt- ', 'ctrl-a', 'ctrl-n'):
            self.assertIn(required, keys,
                          f'missing multi-select key: {required!r}')


if __name__ == '__main__':
    unittest.main()
