"""Tests for ``InputContent`` — the modal single-line text entry (ticket #974).

``InputContent`` backs ``ctx.prompt``: a wrapped prompt above a one-row entry
field showing the edit buffer (its tail, with a visible cursor, when it
overflows). It implements the modal content protocol (``title`` / ``measure``
/ ``draw_row`` / ``handle_key``) consumed by ``run_modal``.

Tests drive the class DIRECTLY — construct an ``InputContent``, call
``measure`` to fix its geometry, then exercise ``draw_row`` / ``handle_key``;
the full ``run_modal`` loop isn't needed to assert the content's own
behavior. One integration test drives it through ``run_modal`` with an
injected ``_read_key`` to confirm the wiring (the typed buffer comes back).

No TTY: ``draw_row`` emits through the modal module's ``write`` / ``set_style``
/ ``reset_style`` (shared with 050-render's segment writers after the loader
wiring), captured into a buffer — the same approach the other modal tests
use. ``Rect`` / ``PaneCache`` / cell helpers / the preview pipeline live in
other numbered files and are wired in the way the concatenated build shares a
namespace.
"""

import io
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term_modalinput', '020-terminal.py')
_state = load('_browse_tui_state_modalinput', '040-state.py')
_render = load('_browse_tui_render_modalinput', '050-render.py')
_modal = load('_browse_tui_modal_input', '055-modal.py')

# 050-render references a handful of names by bare identifier that live in
# 020-terminal in the concatenated build's shared namespace — wire them so
# the segment writers / preview pipeline resolve under the isolated load.
_render._char_width = _term._char_width
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render._visible_len = _term._visible_len
_render.write = _term.write
_render.set_style = _term.set_style
_render.reset_style = _term.reset_style
# The preview wrap pipeline (``_wrap_preview_line``) drives an ``SgrState``,
# which lives in 020-terminal in the concatenated build's shared namespace.
_render.SgrState = _term.SgrState

# Wire the modal module's bare-name cross-references the same way.
_modal.Rect = _render.Rect
_modal.PaneCache = _state.PaneCache
_modal.write = _term.write
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
# ``InputContent`` reuses 050-render's cell helpers + the preview ANSI
# pipeline (the SAME functions the preview pane uses for its body text). The
# cell-aware suffix trim (``_suffix_by_cells``) composes the field's tail.
_modal.cell_width = _render.cell_width
_modal._suffix_by_cells = _render._suffix_by_cells
_modal._sanitize_preview = _render._sanitize_preview
_modal._wrap_preview_line = _render._wrap_preview_line
_modal._write_segments = _render._write_segments
import time as _time  # noqa: E402  (after the loader wiring block)
_modal.time = _time


InputContent = _modal.InputContent
run_modal = _modal.run_modal
Rect = _render.Rect
PaneCache = _state.PaneCache


# --- Output capture --------------------------------------------------------
#
# ``draw_row`` writes through the modal module's ``write`` (== ``_term.write``
# after wiring) and the segment writers route there too. Pointing
# ``_term._tty_writer`` at a StringIO captures the emitted bytes.


class _Capture:
    def __enter__(self):
        self._orig = _term._tty_writer
        self.buf = io.StringIO()
        _term._tty_writer = self.buf
        _term._row_capture_active = False
        _term._row_buf = []
        _term._row_meta = None
        return self

    def __exit__(self, *_):
        _term._tty_writer = self._orig

    @property
    def text(self):
        return self.buf.getvalue()


def _draw(content, row, width):
    """Capture a single ``draw_row`` call's emitted bytes."""
    with _Capture() as cap:
        content.draw_row(row, width)
    return cap.text


def _scripted(keys):
    """Zero-arg callable yielding successive keys (the picker's seam)."""
    it = iter(keys)
    return lambda: next(it)


def _visible(text):
    """Visible cell count of captured bytes (SGR/CSI stripped, wide-aware)."""
    return _term._visible_len(text)


def _plain(text):
    """Strip CSI escapes from captured bytes, leaving visible characters."""
    return _term._ANSI_CSI_RE.sub('', text)


# --- Fake browser (only what the integration test's run_modal touches) -----


class _FakeBrowser:
    def __init__(self):
        self._modal_open = False
        self._pane_cache = {}
        self._needs_redraw = set()
        self._out_stream_live = False
        self._out_dead = False
        self._out_buf = bytearray()
        self._stdin_live = False


class _FixedTermSize:
    def __init__(self, cols=80, rows=24):
        self._sz = (cols, rows)

    def __enter__(self):
        self._orig = _modal.term_size
        _modal.term_size = lambda: self._sz
        return self

    def __exit__(self, *_):
        _modal.term_size = self._orig


# --- construction ----------------------------------------------------------


class TestConstruction(unittest.TestCase):
    def test_title_is_none(self):
        # An input dialog has no title (the prompt text carries the label).
        c = InputContent('Name?')
        self.assertIsNone(c.title)

    def test_buffer_empty_by_default(self):
        c = InputContent('Name?')
        self.assertEqual(c.buffer, '')

    def test_default_prefills_buffer(self):
        c = InputContent('Name?', default='alice')
        self.assertEqual(c.buffer, 'alice')


# --- measure ---------------------------------------------------------------


class TestMeasure(unittest.TestCase):
    def test_height_is_prompt_lines_plus_field(self):
        # One short prompt line -> 1 prompt + 1 field row = 2.
        c = InputContent('Name?')
        _, h = c.measure(60, 24)
        self.assertEqual(h, 2)

    def test_height_counts_wrapped_prompt_lines(self):
        # A 25-char prompt char-wraps into 3 visual rows at width 16
        # -> 3 prompt + 1 field row = 4. (Preview wrap is char-based.)
        c = InputContent('p' * 40)
        _, h = c.measure(16, 24)
        self.assertEqual(len(c._prompt_lines), 3)   # 16 + 16 + 8 cells
        self.assertEqual(h, 4)

    def test_width_floor_applied(self):
        # A tiny prompt + no default still gets the small floor so there's
        # room to type.
        c = InputContent('x')
        w, _ = c.measure(60, 24)
        self.assertEqual(w, 16)

    def test_width_driven_by_prompt_when_wider_than_floor(self):
        c = InputContent('a fairly long prompt line here')
        w, _ = c.measure(80, 24)
        self.assertEqual(w, 30)         # 30 cells, wider than the 16 floor

    def test_width_driven_by_default_when_wider(self):
        # A long default widens the box past a short prompt so the field can
        # show it.
        c = InputContent('id', default='x' * 25)
        w, _ = c.measure(80, 24)
        self.assertEqual(w, 25)

    def test_width_clamped_to_max_w(self):
        c = InputContent('p' * 200)
        w, _ = c.measure(30, 24)
        self.assertEqual(w, 30)

    def test_height_clamped_to_max_h(self):
        # Many prompt lines but a tight height cap: height clamps to max_h
        # (the field still lands on the last row — see draw tests).
        c = InputContent('\n'.join(f'line{i}' for i in range(10)))
        _, h = c.measure(40, 4)
        self.assertEqual(h, 4)

    def test_width_wide_glyphs_counted_as_cells(self):
        # A 10-glyph CJK default is 20 cells, wider than the 16 floor.
        c = InputContent('id', default='東' * 10)
        w, _ = c.measure(80, 24)
        self.assertEqual(w, 20)


# --- draw_row layout -------------------------------------------------------


class TestDrawLayout(unittest.TestCase):
    def test_prompt_on_top_rows(self):
        c = InputContent('Enter name')
        c.measure(40, 24)               # h=2: row0 prompt, row1 field
        self.assertIn('Enter name', _plain(_draw(c, 0, c._w)))

    def test_field_on_last_row(self):
        c = InputContent('Name?', default='bob')
        c.measure(40, 24)
        out = _plain(_draw(c, c._h - 1, c._w))
        self.assertIn('bob', out)

    def test_every_row_padded_to_width(self):
        c = InputContent('Enter a fairly long value', default='seed')
        c.measure(20, 24)
        for row in range(c._h):
            self.assertEqual(_visible(_draw(c, row, c._w)), c._w,
                             f'row {row} not padded to width')

    def test_field_has_cursor_cell(self):
        # The field carries a reverse-video (SGR param 7) cursor cell.
        c = InputContent('Name?', default='bob')
        c.measure(40, 24)
        out = _draw(c, c._h - 1, c._w)
        self.assertTrue(any('7' in seq for seq in
                            _term._ANSI_CSI_RE.findall(out)),
                        'expected a reverse-video SGR for the cursor cell')
        # The cursor's SGR must be reset before the trailing pad — no SGR may
        # dangle past the row into the frame border. After stripping all CSI
        # the row is exactly ``width`` plain cells, and the reset closes the
        # reverse run (the last escape in the row is a reset).
        self.assertNotIn('\033[', out.split('\033[0m')[-1])

    def test_empty_buffer_field_is_just_cursor_then_pad(self):
        # With nothing typed, the field is the cursor cell + padding, exactly
        # ``width`` cells, no buffer text before it.
        c = InputContent('Name?')
        c.measure(40, 24)
        out = _draw(c, c._h - 1, c._w)
        self.assertEqual(_visible(out), c._w)
        self.assertEqual(_plain(out), ' ' * c._w)   # cursor space + pad


# --- field tail-trim on overflow -------------------------------------------


class TestFieldOverflow(unittest.TestCase):
    def test_field_shows_tail_not_head_when_overflowing(self):
        # A buffer far wider than the field shows its END (the tail), with the
        # cursor after it — never the start.
        c = InputContent('p')                 # short prompt -> width 16 floor
        c.measure(40, 24)
        c.buffer = 'abcdefghij' + 'Z' * 40    # 50 chars, field is 16 wide
        out = _draw(c, c._h - 1, c._w)
        self.assertEqual(_visible(out), c._w)
        plain = _plain(out)
        # The end of the buffer is on screen; the head ('abcdefghij') is not.
        self.assertIn('ZZZ', plain)
        self.assertNotIn('abcdef', plain)
        # The very last buffer char ('Z') is visible, the cursor sits after it.
        self.assertEqual(plain.rstrip(' ')[-1], 'Z')

    def test_field_exactly_width_across_buffer_lengths(self):
        # Whatever the buffer length, the field row is exactly ``width`` cells
        # (tail trim leaves room for the cursor cell, then pads).
        c = InputContent('p')
        c.measure(40, 24)
        for n in (0, 1, 5, 15, 16, 17, 100):
            c.buffer = 'q' * n
            out = _draw(c, c._h - 1, c._w)
            self.assertEqual(_visible(out), c._w,
                             f'field not exactly width for buffer len {n}')

    def test_field_wide_glyph_tail_padded_to_width(self):
        # A buffer of wide (2-cell) glyphs may leave the tail one cell short of
        # ``width - 1`` when a glyph straddles the budget; the pad makes the
        # row exactly ``width`` regardless.
        c = InputContent('p')
        c.measure(40, 24)
        c.buffer = '東' * 30
        out = _draw(c, c._h - 1, c._w)
        self.assertEqual(_visible(out), c._w)


# --- key handling ----------------------------------------------------------


class TestKeyHandling(unittest.TestCase):
    def test_printable_appends(self):
        c = InputContent('p')
        for k in 'abc':
            done, _ = c.handle_key(k)
            self.assertFalse(done)
        self.assertEqual(c.buffer, 'abc')

    def test_space_appends(self):
        c = InputContent('p', default='a')
        c.handle_key('space')
        c.handle_key('b')
        self.assertEqual(c.buffer, 'a b')

    def test_backspace_deletes_last_char(self):
        c = InputContent('p', default='abc')
        c.handle_key('backspace')
        self.assertEqual(c.buffer, 'ab')

    def test_backspace_on_empty_is_noop(self):
        c = InputContent('p')                 # empty buffer
        done, result = c.handle_key('backspace')
        self.assertFalse(done)
        self.assertEqual(c.buffer, '')

    def test_enter_returns_buffer(self):
        c = InputContent('p', default='hello')
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'hello')

    def test_enter_returns_empty_string_not_none(self):
        # An empty buffer is a VALID return; None comes only from engine
        # cancel.
        c = InputContent('p')
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, '')
        self.assertIsNotNone(result)

    def test_default_prefill_enter_returns_it(self):
        c = InputContent('p', default='preset')
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'preset')

    def test_append_then_enter(self):
        c = InputContent('p')
        for k in 'hi':
            c.handle_key(k)
        c.handle_key('space')
        for k in 'yo':
            c.handle_key(k)
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'hi yo')

    def test_navigation_keys_ignored_no_cursor_movement(self):
        # End-only editing: arrows / home / end / etc. don't move or mutate.
        c = InputContent('p', default='abc')
        for k in ('left', 'right', 'home', 'end', 'up', 'down', 'tab', 'f1',
                  'ctrl-a'):
            done, result = c.handle_key(k)
            self.assertFalse(done)
            self.assertIsNone(result)
        self.assertEqual(c.buffer, 'abc')   # unchanged


# --- integration through run_modal -----------------------------------------


class TestThroughRunModal(unittest.TestCase):
    """Drive InputContent through the real engine with a scripted key stream
    to confirm the protocol wiring (the typed buffer string comes back)."""

    def test_typed_value_returned(self):
        b = _FakeBrowser()
        c = InputContent('Name?')
        keys = ['a', 'b', 'space', 'c', 'enter']
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(keys))
        self.assertEqual(res, 'ab c')

    def test_default_returned_on_immediate_enter(self):
        b = _FakeBrowser()
        c = InputContent('Name?', default='alice')
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['enter']))
        self.assertEqual(res, 'alice')

    def test_empty_buffer_returns_empty_string(self):
        b = _FakeBrowser()
        c = InputContent('Name?')
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['enter']))
        self.assertEqual(res, '')

    def test_backspace_then_enter_through_engine(self):
        b = _FakeBrowser()
        c = InputContent('Name?', default='abc')
        keys = ['backspace', 'backspace', 'X', 'enter']
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(keys))
        self.assertEqual(res, 'aX')

    def test_esc_cancels_to_none(self):
        # Cancel returns None even with a pre-filled default — None is the
        # engine's cancel signal, distinct from an empty-string return.
        b = _FakeBrowser()
        c = InputContent('Name?', default='alice')
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['esc']))
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
