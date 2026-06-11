"""Tests for ctx.pick — the fzf-style sub-picker (ticket #20).

The picker is a sub-flow on Context: filter prompt on the info bar,
filtered list of options overlaid in the preview-pane area, returns
the chosen string or None.

Headless Browsers short-circuit to None — that's the documented
contract for unit tests that don't drive a key stream. To exercise
the actual loop logic we use the ``_read_key`` injection seam on
``_pick_on_info_bar`` to feed a deterministic key sequence and stub
the terminal write helpers to no-ops so the picker doesn't try to
talk to a real tty.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_context = load('_browse_tui_context', '060-context.py')

# Wire up cross-module references the way the concatenated build does.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode

_context.visible_items = _state.visible_items
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
_context.layout_panes = _render.layout_panes


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context
_pick_on_info_bar = _context._pick_on_info_bar


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


# --- Headless contract ----------------------------------------------------


class TestPickHeadless(unittest.TestCase):
    """Headless mode returns None immediately — no key reads."""

    def test_pick_in_headless_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.pick('Status', ['open', 'in-progress', 'done']))
        finally:
            b.stop_workers()

    def test_pick_in_headless_with_empty_options(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.pick('Status', []))
        finally:
            b.stop_workers()

    def test_pick_in_headless_does_not_consume_keys(self):
        # Sanity: even if read_key were stubbed to crash, pick() must
        # not call it in headless mode. We replace it temporarily.
        original = _context.read_key
        _context.read_key = lambda: (_ for _ in ()).throw(
            AssertionError('read_key called in headless mode'))
        try:
            b = _make_browser()
            try:
                ctx = Context(b)
                self.assertIsNone(ctx.pick('Status', ['a', 'b']))
            finally:
                b.stop_workers()
        finally:
            _context.read_key = original


# --- Loop logic via _pick_on_info_bar with stubbed terminal --------------
#
# ``_pick_on_info_bar`` does its own drawing, so to drive it under unit
# tests we replace the terminal write helpers (move, clear_line,
# set_style, reset_style, write, flush, term_size, layout_panes) with
# no-ops that don't touch real stdout. The ``_read_key=...`` parameter
# feeds a scripted key sequence.


class _StubTerminal:
    """Context manager that replaces 060-context's terminal helpers
    with no-ops for the duration of the block. ``term_size`` returns a
    fixed (cols, rows) so layout_panes resolves stable geometry.
    """

    def __init__(self, cols=80, rows=24):
        self._cols = cols
        self._rows = rows
        self._saved = {}

    def __enter__(self):
        names = ('move', 'clear_line', 'clear_columns', 'set_style',
                 'reset_style', 'write', 'flush')
        for n in names:
            self._saved[n] = getattr(_context, n)
            setattr(_context, n, lambda *a, **kw: None)
        self._saved['term_size'] = _context.term_size
        _context.term_size = lambda: (self._cols, self._rows)
        # layout_panes stays as the real render-layer function — it's
        # a pure helper that just computes geometry.
        return self

    def __exit__(self, *_):
        for n, v in self._saved.items():
            setattr(_context, n, v)


def _scripted(keys):
    """Return a callable that yields successive items from ``keys``."""
    it = iter(keys)
    return lambda: next(it)


def _make_picker_browser():
    """Browser configured for pick-loop tests.

    ``_headless=False`` so the headless short-circuit doesn't kick in;
    the StubTerminal context manager prevents any actual tty output.
    The Browser still needs ``stop_workers`` called by the test.
    """
    return Browser(BrowserConfig(_headless=False))


class TestPickLoop(unittest.TestCase):

    def test_typing_filters_options(self):
        # Type "op" then enter — first match should be 'open'.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status',
                    ['open', 'in-progress', 'done', 'wontfix'],
                    _read_key=_scripted(['o', 'p', 'enter']),
                )
            self.assertEqual(result, 'open')
        finally:
            b.stop_workers()

    def test_down_arrow_moves_cursor(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b', 'c'],
                    _read_key=_scripted(['down', 'enter']),
                )
            self.assertEqual(result, 'b')
        finally:
            b.stop_workers()

    def test_up_arrow_wraps_to_bottom(self):
        # Up from cursor 0 wraps modulo len(visible) → last item.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b', 'c'],
                    _read_key=_scripted(['up', 'enter']),
                )
            self.assertEqual(result, 'c')
        finally:
            b.stop_workers()

    def test_esc_cancels(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b'],
                    _read_key=_scripted(['esc']),
                )
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_ctrl_c_cancels(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b'],
                    _read_key=_scripted(['ctrl-c']),
                )
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_enter_with_no_matches_does_not_return(self):
        # 'z' filters everything out; subsequent enter is a no-op; esc
        # cancels.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b'],
                    _read_key=_scripted(['z', 'enter', 'esc']),
                )
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_backspace_edits_filter(self):
        # Filter goes "op" → "o"; first match for "o" is 'open'.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['open', 'in-progress', 'done'],
                    _read_key=_scripted(['o', 'p', 'backspace', 'enter']),
                )
            self.assertEqual(result, 'open')
        finally:
            b.stop_workers()

    def test_home_jumps_to_top(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b', 'c'],
                    _read_key=_scripted(['down', 'down', 'home', 'enter']),
                )
            self.assertEqual(result, 'a')
        finally:
            b.stop_workers()

    def test_end_jumps_to_bottom(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b', 'c'],
                    _read_key=_scripted(['end', 'enter']),
                )
            self.assertEqual(result, 'c')
        finally:
            b.stop_workers()

    def test_ctrl_n_and_ctrl_p_navigate(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['a', 'b', 'c'],
                    _read_key=_scripted(['ctrl-n', 'ctrl-n', 'ctrl-p', 'enter']),
                )
            self.assertEqual(result, 'b')
        finally:
            b.stop_workers()

    def test_filter_is_case_insensitive(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['Open', 'in-Progress'],
                    _read_key=_scripted(['O', 'P', 'enter']),
                )
            self.assertEqual(result, 'Open')
        finally:
            b.stop_workers()

    def test_returns_first_match_when_filter_narrows_to_one(self):
        # Filter "fix" only matches 'wontfix' — selecting it on enter
        # confirms the cursor lands on the sole remaining option.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['open', 'in-progress', 'done', 'wontfix'],
                    _read_key=_scripted(['f', 'i', 'x', 'enter']),
                )
            self.assertEqual(result, 'wontfix')
        finally:
            b.stop_workers()

    def test_select_picks_filtered_match_after_typing(self):
        # Filter 'n' matches 'open' / 'in-progress' / 'done' / 'wontfix'
        # (every option contains 'n'); press down to advance from 'open'
        # to 'in-progress', enter to select.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                result = _pick_on_info_bar(
                    b, 'Status', ['open', 'in-progress', 'done', 'wontfix'],
                    _read_key=_scripted(['n', 'down', 'enter']),
                )
            self.assertEqual(result, 'in-progress')
        finally:
            b.stop_workers()

    def test_marks_redraw_on_select(self):
        # Confirm exiting the picker flags a full repaint on the
        # Browser so the main loop restores the screen.
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                _pick_on_info_bar(
                    b, 'Status', ['a'],
                    _read_key=_scripted(['enter']),
                )
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_marks_redraw_on_cancel(self):
        b = _make_picker_browser()
        try:
            with _StubTerminal():
                _pick_on_info_bar(
                    b, 'Status', ['a'],
                    _read_key=_scripted(['esc']),
                )
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
