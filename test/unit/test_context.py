"""Tests for the Context wrapper (060-context.py).

Context is the surface action handlers see — a thin wrapper around
Browser that adds main-thread-only sub-flows. These unit tests exercise
the selection helpers (cursor / selected / targets), the thread-safe
pass-throughs (refresh / message / error), and the headless-mode
contract for run_external / input / confirm.

The full TTY paths for input, confirm, run_external (suspending the
real terminal) are deferred to ticket #14's UI tests.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_context = load('_browse_tui_context', '060-context.py')

# Production builds resolve these via concatenation; under tests we
# wire them in by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_context.visible_items = _state.visible_items
# Terminal helpers are only used in non-headless paths but we still
# inject them so the module loads cleanly.
_context.term_suspend = _term.term_suspend
_context.term_resume = _term.term_resume
_context.term_size = _term.term_size
_context.move = _term.move
_context.clear_line = _term.clear_line
_context.clear_columns = _term.clear_columns
_context.set_style = _term.set_style
_context.reset_style = _term.reset_style
_context.write = _term.write
_context.flush = _term.flush
_context.read_key = _term.read_key
# layout_panes lives in 050-render.py; load and inject if needed.
_render = load('_browse_tui_render', '050-render.py')
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_context.layout_panes = _render.layout_panes


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Pending = _state.Pending
Context = _context.Context


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


# --- Construction ---------------------------------------------------------


class TestContextConstruction(unittest.TestCase):

    def test_stores_browser_reference(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIs(ctx._browser, b)
        finally:
            b.stop_workers()


# --- cursor property ------------------------------------------------------


class TestCursorProperty(unittest.TestCase):

    def test_empty_visible_list_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.cursor)
        finally:
            b.stop_workers()

    def test_normal_entry_returns_item(self):
        b = _make_browser()
        items = [Item(id='A'), Item(id='B')]
        b._state._children[None] = items
        b._state.cursor = 1
        try:
            ctx = Context(b)
            self.assertIs(ctx.cursor, items[1])
        finally:
            b.stop_workers()

    def test_pending_placeholder_yields_none(self):
        # Expanded-but-uncached parent gives a 'pending' kind row.
        b = _make_browser()
        b._state._children[None] = [Item(id='A', has_children=True)]
        b._state.expanded.add('A')
        # Visible list now has [normal A, pending placeholder].
        b._state.cursor = 1  # placeholder
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.cursor)
        finally:
            b.stop_workers()


# --- selected property ----------------------------------------------------


class TestSelectedProperty(unittest.TestCase):

    def test_empty_selection_returns_empty_list(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.selected, [])
        finally:
            b.stop_workers()

    def test_selection_with_visible_items(self):
        b = _make_browser()
        items = [Item(id='A'), Item(id='B'), Item(id='C')]
        b._state._children[None] = items
        b._state.selected = {'A', 'C'}
        try:
            ctx = Context(b)
            sel = ctx.selected
            self.assertEqual({it.id for it in sel}, {'A', 'C'})
        finally:
            b.stop_workers()

    def test_selection_in_cache_but_not_visible(self):
        # Item 'X' is cached as a child of 'P' but 'P' is not expanded,
        # so 'X' isn't in the visible list — selected should still surface
        # it from the cache.
        b = _make_browser()
        b._state._children[None] = [Item(id='P', has_children=True)]
        b._state._children['P'] = [Item(id='X')]
        b._state.selected = {'X'}
        try:
            ctx = Context(b)
            sel = ctx.selected
            self.assertEqual([it.id for it in sel], ['X'])
        finally:
            b.stop_workers()


# --- targets property -----------------------------------------------------


class TestTargetsProperty(unittest.TestCase):

    def test_empty_no_cursor_returns_empty(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.targets, [])
        finally:
            b.stop_workers()

    def test_no_selection_returns_cursor(self):
        b = _make_browser()
        items = [Item(id='A')]
        b._state._children[None] = items
        try:
            ctx = Context(b)
            t = ctx.targets
            self.assertEqual(len(t), 1)
            self.assertIs(t[0], items[0])
        finally:
            b.stop_workers()

    def test_selection_overrides_cursor(self):
        b = _make_browser()
        items = [Item(id='A'), Item(id='B')]
        b._state._children[None] = items
        b._state.cursor = 1  # cursor on B
        b._state.selected = {'A'}
        try:
            ctx = Context(b)
            t = ctx.targets
            self.assertEqual([it.id for it in t], ['A'])
        finally:
            b.stop_workers()


# --- pass-through ---------------------------------------------------------


class TestPassThrough(unittest.TestCase):

    def test_refresh_returns_pending(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            p = ctx.refresh('id-x')
            self.assertIsInstance(p, Pending)
        finally:
            b.stop_workers()

    def test_message_sets_message_text(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.message('hello')
            b.drain_main_queue()
            self.assertEqual(b._message_text, 'hello')
        finally:
            b.stop_workers()

    def test_error_sets_error_text(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.error('bad')
            b.drain_main_queue()
            self.assertEqual(b._error_text, 'bad')
        finally:
            b.stop_workers()

    def test_quit_sets_fields(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.quit(code=3, output='gone')
            b.drain_main_queue()
            self.assertTrue(b._quit_requested)
            self.assertEqual(b._quit_code, 3)
            self.assertEqual(b._quit_output, 'gone')
        finally:
            b.stop_workers()

    def test_select_calls_through(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.select(['a', 'b'])
            b.drain_main_queue()
            self.assertEqual(b._state.selected, {'a', 'b'})
        finally:
            b.stop_workers()


# --- run_external (headless) ----------------------------------------------


class TestRunExternalHeadless(unittest.TestCase):

    def test_echo_returns_zero(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            rc = ctx.run_external(['true'])
            self.assertEqual(rc, 0)
            # Headless: no terminal suspend/resume, but redraw still flagged.
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_failing_command_returns_nonzero(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            rc = ctx.run_external(['false'])
            self.assertNotEqual(rc, 0)
        finally:
            b.stop_workers()


# --- input (headless) -----------------------------------------------------


class TestInputHeadless(unittest.TestCase):

    def test_returns_default_in_headless(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.input('Name? ', default='alice'), 'alice')
        finally:
            b.stop_workers()

    def test_returns_empty_default_when_omitted(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertEqual(ctx.input('Name? '), '')
        finally:
            b.stop_workers()


# --- confirm (headless) ---------------------------------------------------


class TestConfirmHeadless(unittest.TestCase):

    def test_returns_false_in_headless(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertFalse(ctx.confirm('proceed?'))
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
