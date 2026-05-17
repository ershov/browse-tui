"""Tests for FILTER_EDIT-mode key dispatch (070-actions.py).

Covers the ``&`` entry, in-edit keystrokes (printable / Backspace /
Ctrl-W / Ctrl-U / Ctrl-X), commit / cancel / clear-all exits, and
fall-through for non-overridden keys.

See ``docs/superpowers/specs/2026-05-17-filter-design.md``.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_actions = load('_browse_tui_actions', '070-actions.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions._recompute_filter_hidden = _state._recompute_filter_hidden
_actions._AnchorSentinel = _state._AnchorSentinel
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode


Item = _data.Item
Browser = _state.Browser
Mode = _state.Mode
dispatch_key = _actions.dispatch_key
default_actions = _actions.default_actions


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(**kw)


def _ctx_for(browser):
    _context = load('_browse_tui_context', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


def _browser_with(ids):
    b = _make_browser()
    b._state._children[None] = [Item(id=x) for x in ids]
    return b


# ---- entry --------------------------------------------------------------


class TestAmpersandEntersFilterMode(unittest.TestCase):

    def test_amp_enters_filter_mode_and_appends_placeholder(self):
        b = _browser_with(['foo', 'bar'])
        try:
            ctx = _ctx_for(b)
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertEqual(b._filters, [])
            self.assertTrue(dispatch_key(b, ctx, '&'))
            self.assertIs(b._mode, Mode.FILTER_EDIT)
            self.assertEqual(b._filters, [''])
        finally:
            b.stop_workers()

    def test_default_actions_registers_amp(self):
        keys = {a.key for a in default_actions()}
        self.assertIn('&', keys)


# ---- typing -------------------------------------------------------------


class TestTypingMutatesLastEntry(unittest.TestCase):

    def test_each_char_appends_to_last_entry(self):
        b = _browser_with(['foo', 'bar', 'baz'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'f')
            dispatch_key(b, ctx, 'o')
            dispatch_key(b, ctx, 'o')
            self.assertEqual(b._filters, ['foo'])
        finally:
            b.stop_workers()

    def test_typing_narrows_visible_list(self):
        b = _browser_with(['foo', 'bar', 'baz'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'f')
            vis = _state.visible_items(b._state)
            ids = [e.item.id for e in vis if e.kind == 'normal']
            self.assertEqual(ids, ['foo'])
        finally:
            b.stop_workers()

    def test_space_inserts_literal_space(self):
        b = _browser_with(['foo bar'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'f')
            dispatch_key(b, ctx, 'o')
            dispatch_key(b, ctx, 'o')
            dispatch_key(b, ctx, 'space')
            dispatch_key(b, ctx, 'b')
            self.assertEqual(b._filters, ['foo b'])
        finally:
            b.stop_workers()

    def test_stacking_two_filters_AND(self):
        b = _browser_with(['foo-bar', 'foo-only', 'bar-only'])
        try:
            ctx = _ctx_for(b)
            # First filter: foo
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'f')
            dispatch_key(b, ctx, 'o')
            dispatch_key(b, ctx, 'o')
            dispatch_key(b, ctx, 'enter')
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertEqual(b._filters, ['foo'])
            # Second filter: bar
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'b')
            dispatch_key(b, ctx, 'a')
            dispatch_key(b, ctx, 'r')
            dispatch_key(b, ctx, 'enter')
            self.assertEqual(b._filters, ['foo', 'bar'])
            # Only foo-bar matches both
            vis_ids = [e.item.id for e in _state.visible_items(b._state)
                       if e.kind == 'normal']
            self.assertEqual(vis_ids, ['foo-bar'])
        finally:
            b.stop_workers()


# ---- backspace / kill ---------------------------------------------------


class TestBackspaceAndKills(unittest.TestCase):

    def test_backspace_drops_last_char(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            for ch in 'foob':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'backspace')
            self.assertEqual(b._filters, ['foo'])
        finally:
            b.stop_workers()

    def test_backspace_noop_on_empty_last_entry(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'backspace')   # empty -> no-op
            self.assertEqual(b._filters, [''])
            self.assertIs(b._mode, Mode.FILTER_EDIT)
        finally:
            b.stop_workers()

    def test_ctrl_w_kills_last_word(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            for ch in 'foo':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'space')
            for ch in 'bar':
                dispatch_key(b, ctx, ch)
            self.assertEqual(b._filters, ['foo bar'])
            dispatch_key(b, ctx, 'ctrl-w')
            self.assertEqual(b._filters, ['foo '])
        finally:
            b.stop_workers()

    def test_ctrl_u_clears_in_progress_only(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            # Commit 'first', then start typing 'sec' (in-progress).
            dispatch_key(b, ctx, '&')
            for ch in 'first':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'enter')
            dispatch_key(b, ctx, '&')
            for ch in 'sec':
                dispatch_key(b, ctx, ch)
            self.assertEqual(b._filters, ['first', 'sec'])
            dispatch_key(b, ctx, 'ctrl-u')
            self.assertEqual(b._filters, ['first', ''])
            self.assertIs(b._mode, Mode.FILTER_EDIT)
        finally:
            b.stop_workers()


# ---- exits --------------------------------------------------------------


class TestEnterCommitOrClear(unittest.TestCase):

    def test_enter_non_empty_commits(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            for ch in 'foo':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'enter')
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertEqual(b._filters, ['foo'])
        finally:
            b.stop_workers()

    def test_enter_empty_clears_all(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            # Commit one filter then re-enter with empty -> clears.
            dispatch_key(b, ctx, '&')
            for ch in 'foo':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'enter')
            self.assertEqual(b._filters, ['foo'])
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'enter')
            self.assertEqual(b._filters, [])
            self.assertIs(b._mode, Mode.NORMAL)
        finally:
            b.stop_workers()


class TestCtrlXClearsAll(unittest.TestCase):

    def test_ctrl_x_drops_all_filters(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            for ch in 'foo':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'enter')
            dispatch_key(b, ctx, '&')
            for ch in 'bar':
                dispatch_key(b, ctx, ch)
            self.assertEqual(b._filters, ['foo', 'bar'])
            dispatch_key(b, ctx, 'ctrl-x')
            self.assertEqual(b._filters, [])
            self.assertIs(b._mode, Mode.NORMAL)
        finally:
            b.stop_workers()


class TestCtrlCAndEscCancel(unittest.TestCase):

    def test_ctrl_c_pops_in_progress_and_exits(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            # Commit one filter then start a second.
            dispatch_key(b, ctx, '&')
            for ch in 'foo':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'enter')
            dispatch_key(b, ctx, '&')
            for ch in 'bar':
                dispatch_key(b, ctx, ch)
            self.assertEqual(b._filters, ['foo', 'bar'])
            dispatch_key(b, ctx, 'ctrl-c')
            self.assertEqual(b._filters, ['foo'])
            self.assertIs(b._mode, Mode.NORMAL)
        finally:
            b.stop_workers()

    def test_esc_pops_in_progress_and_exits(self):
        b = _browser_with(['x'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            for ch in 'foo':
                dispatch_key(b, ctx, ch)
            dispatch_key(b, ctx, 'esc')
            self.assertEqual(b._filters, [])
            self.assertIs(b._mode, Mode.NORMAL)
        finally:
            b.stop_workers()


# ---- fall-through -------------------------------------------------------


class TestNonOverriddenKeysFallThrough(unittest.TestCase):

    def test_down_arrow_moves_cursor_while_prompt_open(self):
        b = _browser_with(['a', 'b', 'c'])
        try:
            ctx = _ctx_for(b)
            self.assertEqual(b._state.cursor, 0)
            dispatch_key(b, ctx, '&')
            self.assertIs(b._mode, Mode.FILTER_EDIT)
            # Down arrow falls through to normal nav.
            dispatch_key(b, ctx, 'down')
            self.assertEqual(b._state.cursor, 1)
            # Prompt stays open; last entry unchanged.
            self.assertIs(b._mode, Mode.FILTER_EDIT)
            self.assertEqual(b._filters, [''])
        finally:
            b.stop_workers()

    def test_pgdn_falls_through(self):
        b = _browser_with(['a', 'b', 'c'])
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, '&')
            dispatch_key(b, ctx, 'pgdn')
            # Cursor moved (no exception). Prompt still open.
            self.assertIs(b._mode, Mode.FILTER_EDIT)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
