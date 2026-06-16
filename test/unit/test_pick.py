"""Tests for ``ctx.pick`` — now backed by the modal selection list.

``ctx.pick`` opens a centered, filtered modal (``ListContent`` driven by
``run_modal``); the old ``_pick_on_info_bar`` info-bar/preview overlay is
gone. The exhaustive ``ListContent`` behavior (filtering, windowing,
selection rendering, key handling) is covered in ``test_modal_list.py``;
this file pins the ``ctx.pick`` wiring:

  * the headless short-circuit (returns ``None`` without driving a key
    stream), including the empty-options and no-key-consumption contracts;
  * ``modal_pick``'s empty-options short-circuit (returns ``None`` WITHOUT
    opening a dialog — asserted by a ``run_modal`` that would explode if
    called);
  * that ``ctx.pick`` builds the right content + placement (a captured
    ``run_modal`` records ``(content, placement)``);
  * a couple of end-to-end loop drives through ``run_modal`` with an
    injected ``_read_key`` stream (the seam still lives on ``run_modal``).

No TTY: ``run_modal``'s drawing is captured into a StringIO via the
terminal layer's ``_tty_writer`` (the same approach the other modal tests
use); ``term_size`` is stubbed to a fixed geometry.
"""

import io
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_modal = load('_browse_tui_modal', '055-modal.py')
_context = load('_browse_tui_context', '060-context.py')

# Cross-module references the concatenated build resolves by shared namespace.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode

# 050-render's segment writers / cell helpers reach a few names that live in
# 020-terminal in the shared namespace.
_render._char_width = _term._char_width
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render._visible_len = _term._visible_len
_render.write = _term.write
_render.set_style = _term.set_style
_render.reset_style = _term.reset_style

# 055-modal's bare-name dependencies (terminal layer + render helpers).
_modal.Rect = _render.Rect
_modal.PaneCache = _state.PaneCache
_modal.write = _term.write
_modal.move = _term.move
_modal.set_style = _term.set_style
_modal.reset_style = _term.reset_style
_modal.read_key = _term.read_key
_modal.term_size = _term.term_size
_modal.begin_row = _term.begin_row
_modal.end_row = _term.end_row
_modal.begin_sync = _term.begin_sync
_modal.end_sync = _term.end_sync
_modal.flush = _term.flush
_modal.input_ready = _term.input_ready
_modal._truncate_by_cells = _render._truncate_by_cells
_modal.cell_width = _render.cell_width
_modal.cell_trim = _render.cell_trim
_modal.cell_ljust = _render.cell_ljust
_modal._collapse_visible = _render._collapse_visible
_modal._write_highlighted = _render._write_highlighted
_modal._write_segments = _render._write_segments
import time as _time  # noqa: E402  (after the loader wiring block)
_modal.time = _time

# 060-context's bare-name dependencies. The modal wrappers + engine live in
# 055-modal in the concatenated build; ctx.pick / ctx.menu reach them and the
# render-layer geometry helpers by bare name.
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
_context.modal_pick = _modal.modal_pick
_context.modal_menu = _modal.modal_menu
_context.modal_confirm = _modal.modal_confirm
_context.modal_input = _modal.modal_input
_context.modal_alert = _modal.modal_alert
_context.run_modal = _modal.run_modal


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context
ListContent = _modal.ListContent
run_modal = _modal.run_modal
modal_pick = _modal.modal_pick


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


def _scripted(keys):
    """Zero-arg callable yielding successive keys (the ``_read_key`` seam)."""
    it = iter(keys)
    return lambda: next(it)


class _Capture:
    """Capture run_modal's emitted bytes (no TTY); fixes a stable geometry."""

    def __init__(self, cols=80, rows=24):
        self._sz = (cols, rows)

    def __enter__(self):
        self._orig_writer = _term._tty_writer
        self._orig_size = _modal.term_size
        self.buf = io.StringIO()
        _term._tty_writer = self.buf
        _term._row_capture_active = False
        _term._row_buf = []
        _term._row_meta = None
        _modal.term_size = lambda: self._sz
        return self

    def __exit__(self, *_):
        _term._tty_writer = self._orig_writer
        _modal.term_size = self._orig_size


# --- Headless contract ----------------------------------------------------


class TestPickHeadless(unittest.TestCase):
    """Headless mode returns None immediately — no key reads, no dialog."""

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

    def test_pick_in_headless_does_not_open_modal(self):
        # Even if run_modal were to fire, the headless guard must return
        # first — so a run_modal that explodes proves it's never reached.
        original = _context.run_modal
        _context.run_modal = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError('run_modal called in headless mode'))
        try:
            b = _make_browser()
            try:
                ctx = Context(b)
                self.assertIsNone(ctx.pick('Status', ['a', 'b']))
            finally:
                b.stop_workers()
        finally:
            _context.run_modal = original


# --- modal_pick wiring (empty short-circuit, content construction) --------


class TestModalPickWiring(unittest.TestCase):

    def test_empty_options_returns_none_without_opening(self):
        # The empty-collection short-circuit lives in modal_pick: it must
        # return None WITHOUT calling run_modal. A run_modal that explodes
        # proves the dialog is never opened.
        original = _modal.run_modal
        _modal.run_modal = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError('run_modal opened for empty options'))
        try:
            self.assertIsNone(modal_pick(object(), 'Status', []))
        finally:
            _modal.run_modal = original

    def test_builds_filtered_centered_list_content(self):
        # ctx.pick should construct a filtered ListContent titled with the
        # label and place it centered. Capture run_modal's (content,
        # placement) without driving the loop.
        captured = {}

        def _fake_run_modal(browser, content, *, placement='center',
                            anchor=None, delay_interaction=False,
                            _read_key=None):
            captured['content'] = content
            captured['placement'] = placement
            captured['anchor'] = anchor
            return 'sentinel'

        original = _modal.run_modal
        _modal.run_modal = _fake_run_modal
        try:
            b = _make_browser(_headless=False)
            try:
                ctx = Context(b)
                result = ctx.pick('Status', ['open', 'done'])
            finally:
                b.stop_workers()
        finally:
            _modal.run_modal = original

        self.assertEqual(result, 'sentinel')
        content = captured['content']
        self.assertIsInstance(content, ListContent)
        self.assertTrue(content._filter)             # filter row present
        self.assertEqual(content.title, 'Status')    # label becomes the title
        # Bare strings normalize to ``(display, value)`` pairs == ``(s, s)``.
        self.assertEqual(content._options,
                         [('open', 'open'), ('done', 'done')])
        self.assertEqual(captured['placement'], 'center')
        self.assertIsNone(captured['anchor'])


# --- end-to-end through run_modal (injected key stream) -------------------


class TestPickLoop(unittest.TestCase):
    """Drive modal_pick through the real engine with a scripted key stream."""

    def test_typing_filters_then_enter_selects(self):
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                result = modal_pick(
                    b, 'Status',
                    ['open', 'in-progress', 'done', 'wontfix'],
                    _read_key=_scripted(['o', 'p', 'enter']),
                )
            self.assertEqual(result, 'open')
        finally:
            b.stop_workers()

    def test_down_then_enter_selects_second(self):
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                result = modal_pick(
                    b, 'Status', ['a', 'b', 'c'],
                    _read_key=_scripted(['down', 'enter']),
                )
            self.assertEqual(result, 'b')
        finally:
            b.stop_workers()

    def test_esc_cancels_to_none(self):
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                result = modal_pick(
                    b, 'Status', ['a', 'b'],
                    _read_key=_scripted(['esc']),
                )
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_ctrl_c_cancels_to_none(self):
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                result = modal_pick(
                    b, 'Status', ['a', 'b'],
                    _read_key=_scripted(['ctrl-c']),
                )
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_enter_with_no_matches_is_noop_then_esc(self):
        # 'z' filters everything out; enter is a no-op; esc cancels.
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                result = modal_pick(
                    b, 'Status', ['a', 'b'],
                    _read_key=_scripted(['z', 'enter', 'esc']),
                )
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_tuple_option_value_through_engine(self):
        # A (display, value) option's VALUE comes back through the full loop;
        # the filter still matches on the display half.
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                result = modal_pick(
                    b, 'Status',
                    [('Open', 1), ('In progress', 2), ('Closed', 3)],
                    # 'gr' matches only 'In progress' (display), then enter.
                    _read_key=_scripted(['g', 'r', 'enter']),
                )
            self.assertEqual(result, 2)
        finally:
            b.stop_workers()

    def test_marks_redraw_on_close(self):
        # Closing the modal poisons pane caches and flags a full repaint so
        # the main loop restores the screen.
        b = _make_browser(_headless=False)
        try:
            with _Capture():
                modal_pick(
                    b, 'Status', ['a'],
                    _read_key=_scripted(['enter']),
                )
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
