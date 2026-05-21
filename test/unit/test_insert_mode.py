"""Tests for insert-mode key handling and ``Context.insert`` setup (#21).

The key handler is in ``070-actions.py`` (``_handle_insert_key``); the
entry point is ``Context.insert`` (in ``060-context.py``). These tests
cover both — entering insert mode wires the right Browser fields, and
the key handler moves the marker / indents / outdents / confirms /
cancels per spec.

Verification approach mirrors test_actions: load each module
independently and inject the cross-module names. We pre-populate the
Browser with a known tree via ``from_flat_tree`` so ``visible_items``
returns deterministic geometry without spinning workers.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_context = load('_browse_tui_context', '060-context.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Cross-module wiring — same shape as test_actions / test_pick.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_render.VisibleEntry = _state.VisibleEntry

_context.visible_items = _state.visible_items
_context.term_size = _term.term_size
_context.term_suspend = _term.term_suspend
_context.term_resume = _term.term_resume
_context.move = _term.move
_context.clear_line = _term.clear_line
_context.set_style = _term.set_style
_context.reset_style = _term.reset_style
_context.write = _term.write
_context.flush = _term.flush
_context.read_key = _term.read_key
_context.layout_panes = _render.layout_panes
_context.auto_insert_depth = _state.auto_insert_depth

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode
_actions.current_scope = _state.current_scope
_actions.auto_insert_depth = _state.auto_insert_depth
_actions.resolve_insert = _state.resolve_insert
_actions.term_size = _term.term_size
_actions.layout_panes = _render.layout_panes


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context
_handle_insert_key = _actions._handle_insert_key


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_tree_browser(headless=True):
    """Build a Browser whose visible list has known structure.

        a (depth 0, has_children, expanded)
          a1 (depth 1)
          a2 (depth 1)
        b (depth 0)
        c (depth 0)
    """
    b = Browser.from_flat_tree(
        [
            {'id': 'a', 'has_children': True},
            {'id': 'a1', 'parent': 'a'},
            {'id': 'a2', 'parent': 'a'},
            {'id': 'b'},
            {'id': 'c'},
        ],
        _headless=headless,
    )
    b._state.expanded.add('a')
    _state.mark_visible_dirty(b._state)
    return b


# ---------------------------------------------------------------------------
# Context.insert entry
# ---------------------------------------------------------------------------


class TestContextInsertEntry(unittest.TestCase):

    def test_insert_in_headless_is_noop(self):
        b = _make_tree_browser(headless=True)
        try:
            ctx = Context(b)
            called = []
            ctx.insert('create', lambda r, d: called.append((r, d)))
            self.assertFalse(b._insert_mode)
            self.assertIsNone(b._insert_callback)
            self.assertEqual(called, [])
        finally:
            b.stop_workers()

    def test_insert_sets_browser_state(self):
        # Non-headless entry: state fields populated, callback parked.
        b = _make_tree_browser(headless=False)
        try:
            b._state.cursor = 0  # cursor on 'a'
            ctx = Context(b)
            cb = lambda r, d: None
            ctx.insert('create', cb)
            self.assertTrue(b._insert_mode)
            self.assertEqual(b._insert_label, 'create')
            self.assertIs(b._insert_callback, cb)
            # Default placement: cursor + 1 (gap right after cursor row).
            self.assertEqual(b._insert_pos, 1)
            # Auto-depth landed somewhere sensible (between 'a' and 'a1':
            # 'a1' is deeper than 'a', so auto returns 1 — diving in).
            self.assertEqual(b._insert_depth, 1)
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_insert_with_empty_visible_list_is_noop(self):
        b = Browser(BrowserConfig(_headless=False))
        try:
            ctx = Context(b)
            ctx.insert('create', lambda r, d: None)
            self.assertFalse(b._insert_mode)
        finally:
            b.stop_workers()


# ---------------------------------------------------------------------------
# _handle_insert_key behaviour
# ---------------------------------------------------------------------------


class TestInsertKeyMovement(unittest.TestCase):

    def setUp(self):
        self.b = _make_tree_browser(headless=False)
        self.b._state.cursor = 0
        ctx = Context(self.b)
        ctx.insert('create', lambda r, d: None)
        self.ctx = ctx

    def tearDown(self):
        self.b.stop_workers()

    def test_down_moves_insert_pos(self):
        start = self.b._insert_pos
        _handle_insert_key(self.b, self.ctx, 'down')
        self.assertEqual(self.b._insert_pos, start + 1)

    def test_up_moves_insert_pos(self):
        # Move down first, then up.
        _handle_insert_key(self.b, self.ctx, 'down')
        self.b._insert_pos  # snapshot
        _handle_insert_key(self.b, self.ctx, 'up')
        self.assertEqual(self.b._insert_pos, 1)

    def test_up_clamps_to_min_pos(self):
        # min_pos is 1 — shouldn't go below.
        self.b._insert_pos = 1
        _handle_insert_key(self.b, self.ctx, 'up')
        self.assertEqual(self.b._insert_pos, 1)

    def test_down_clamps_to_max_pos(self):
        vis = _state.visible_items(self.b._state)
        max_pos = len(vis)
        self.b._insert_pos = max_pos
        _handle_insert_key(self.b, self.ctx, 'down')
        self.assertEqual(self.b._insert_pos, max_pos)

    def test_home_jumps_to_top(self):
        self.b._insert_pos = 4
        _handle_insert_key(self.b, self.ctx, 'home')
        self.assertEqual(self.b._insert_pos, 1)

    def test_end_jumps_to_bottom(self):
        vis = _state.visible_items(self.b._state)
        _handle_insert_key(self.b, self.ctx, 'end')
        self.assertEqual(self.b._insert_pos, len(vis))


class TestInsertKeyIndentOutdent(unittest.TestCase):

    def setUp(self):
        self.b = _make_tree_browser(headless=False)
        self.b._state.cursor = 0
        ctx = Context(self.b)
        ctx.insert('create', lambda r, d: None)
        self.ctx = ctx

    def tearDown(self):
        self.b.stop_workers()

    def test_right_indents_into_above(self):
        # Move marker to gap 4 (after a2, before b). Above is 'a2' at
        # depth 1. depth equals above.depth (1), so right should set
        # depth = 2 (child of a2).
        self.b._insert_pos = 3
        self.b._insert_depth = 1
        _handle_insert_key(self.b, self.ctx, 'right')
        self.assertEqual(self.b._insert_depth, 2)

    def test_left_outdents_to_root(self):
        # Marker at gap 3 with depth 1 (between a2 and b, sibling of a's
        # children). Left should outdent → depth 0, position adjusts to
        # after the entire 'a' subtree (gap 3 in this case stays).
        self.b._insert_pos = 3
        self.b._insert_depth = 1
        _handle_insert_key(self.b, self.ctx, 'left')
        self.assertEqual(self.b._insert_depth, 0)
        # Marker should move to after-subtree position. Visible:
        # [a, a1, a2, b, c]. Subtree of 'a' ends at index 3, so
        # after-last-child = 3.
        self.assertEqual(self.b._insert_pos, 3)


class TestInsertKeyConfirmCancel(unittest.TestCase):

    def setUp(self):
        self.b = _make_tree_browser(headless=False)
        self.b._state.cursor = 2  # cursor on 'a2' (deeper position)
        self.calls = []
        ctx = Context(self.b)
        ctx.insert('create', lambda r, d: self.calls.append((r, d)))
        self.ctx = ctx

    def tearDown(self):
        self.b.stop_workers()

    def test_enter_calls_callback_with_resolved_position(self):
        # Marker placed at gap 3 (after a2), depth 1 → ('after', 'a2').
        self.b._insert_pos = 3
        self.b._insert_depth = 1
        _handle_insert_key(self.b, self.ctx, 'enter')
        self.assertEqual(self.calls, [('after', 'a2')])
        self.assertFalse(self.b._insert_mode)
        self.assertIsNone(self.b._insert_callback)

    def test_enter_with_invalid_position_does_not_call_callback(self):
        # Force an invalid resolution: pos 0.
        self.b._insert_pos = 0
        self.b._insert_depth = 0
        _handle_insert_key(self.b, self.ctx, 'enter')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.b._insert_mode)

    def test_esc_exits_without_callback(self):
        _handle_insert_key(self.b, self.ctx, 'esc')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.b._insert_mode)
        self.assertIsNone(self.b._insert_callback)

    def test_q_exits_without_callback(self):
        _handle_insert_key(self.b, self.ctx, 'q')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.b._insert_mode)

    def test_ctrl_c_exits_without_callback(self):
        _handle_insert_key(self.b, self.ctx, 'ctrl-c')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.b._insert_mode)


if __name__ == '__main__':
    unittest.main()
