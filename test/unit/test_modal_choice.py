"""Tests for ``ChoiceContent`` — the modal message + button row (ticket #973).

``ChoiceContent`` backs both ``ctx.confirm`` (multi-button) and ``ctx.alert``
(a single ``&OK`` button). It implements the modal content protocol
(``title`` / ``measure`` / ``draw_row`` / ``handle_key``) consumed by
``run_modal``.

Tests drive the class DIRECTLY — construct a ``ChoiceContent``, call
``measure`` to fix its geometry, then exercise ``draw_row`` / ``handle_key``;
the full ``run_modal`` loop isn't needed to assert the content's own
behavior. One integration test drives it through ``run_modal`` with an
injected ``_read_key`` to confirm the wiring (the chosen resolved label
comes back).

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


_term = load('_browse_tui_term_modalchoice', '020-terminal.py')
_state = load('_browse_tui_state_modalchoice', '040-state.py')
_render = load('_browse_tui_render_modalchoice', '050-render.py')
_modal = load('_browse_tui_modal_choice', '055-modal.py')

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
# ``ChoiceContent`` reuses 050-render's cell helpers + the preview ANSI
# pipeline (the SAME functions the preview pane uses for its body text).
_modal.cell_width = _render.cell_width
_modal._truncate_by_cells = _render._truncate_by_cells
_modal._truncate_visible = _render._truncate_visible
_modal._sanitize_preview = _render._sanitize_preview
_modal._wrap_preview_line = _render._wrap_preview_line
_modal._write_segments = _render._write_segments
import time as _time  # noqa: E402  (after the loader wiring block)
_modal.time = _time


ChoiceContent = _modal.ChoiceContent
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
    def test_empty_buttons_raises_value_error(self):
        # An empty button tuple is a programming error, not a user condition.
        with self.assertRaises(ValueError):
            ChoiceContent('hi', [])

    def test_title_stored(self):
        c = ChoiceContent('msg', ['&OK'], title='Confirm')
        self.assertEqual(c.title, 'Confirm')

    def test_title_defaults_none(self):
        c = ChoiceContent('msg', ['&OK'])
        self.assertIsNone(c.title)

    def test_first_button_focused_initially(self):
        c = ChoiceContent('msg', ['&Yes', '&No'])
        self.assertEqual(c.focus, 0)


# --- & hotkey parsing ------------------------------------------------------


class TestHotkeyParsing(unittest.TestCase):
    """``&X`` → display ``X`` with hotkey ``X``; ``&&`` → literal ``&`` (no
    hotkey); no ``&`` → no hotkey. The RETURNED value is the label with the
    markers resolved."""

    def _buttons(self, c):
        return c._buttons

    def test_amp_marks_hotkey_and_resolves_label(self):
        c = ChoiceContent('m', ['&Yes'])
        b = self._buttons(c)[0]
        self.assertEqual(b.display, 'Yes')
        self.assertEqual(b.value, 'Yes')      # resolved return value
        self.assertEqual(b.hotkey, 'y')       # lowercased
        self.assertEqual(b.hot_index, 0)      # 'Y' is at display index 0

    def test_amp_mid_label(self):
        c = ChoiceContent('m', ['O&K'])
        b = self._buttons(c)[0]
        self.assertEqual(b.display, 'OK')
        self.assertEqual(b.value, 'OK')       # resolved return value
        self.assertEqual(b.hotkey, 'k')
        self.assertEqual(b.hot_index, 1)

    def test_double_amp_is_literal_not_hotkey(self):
        c = ChoiceContent('m', ['a&&b'])
        b = self._buttons(c)[0]
        self.assertEqual(b.display, 'a&b')    # literal ampersand
        self.assertEqual(b.value, 'a&b')      # resolved return value
        self.assertIsNone(b.hotkey)           # && is NOT a hotkey
        self.assertIsNone(b.hot_index)

    def test_no_amp_no_hotkey(self):
        c = ChoiceContent('m', ['Plain'])
        b = self._buttons(c)[0]
        self.assertEqual(b.display, 'Plain')
        self.assertEqual(b.value, 'Plain')    # resolved return value
        self.assertIsNone(b.hotkey)

    def test_first_amp_marked_char_wins(self):
        # Two &-marked chars: the first wins as the hotkey.
        c = ChoiceContent('m', ['&Save &As'])
        b = self._buttons(c)[0]
        self.assertEqual(b.display, 'Save As')
        self.assertEqual(b.hotkey, 's')
        self.assertEqual(b.hot_index, 0)


# --- measure ---------------------------------------------------------------


class TestMeasure(unittest.TestCase):
    def test_height_is_body_plus_spacer_plus_buttons(self):
        # One short body line -> 1 body + 1 spacer + 1 button row = 3.
        c = ChoiceContent('hello', ['&OK'])
        _, h = c.measure(60, 24)
        self.assertEqual(h, 3)

    def test_height_counts_wrapped_body_lines(self):
        # A 25-char body char-wraps into 3 visual rows at width 10
        # -> 3 body + 1 spacer + 1 button row = 5. (The preview wrap is
        # char-based, not word-based.)
        c = ChoiceContent('a' * 25, ['&OK'])
        _, h = c.measure(10, 24)
        self.assertEqual(c._body_lines_shown, 3)   # 10 + 10 + 5 cells
        self.assertEqual(h, 5)

    def test_width_at_least_button_row(self):
        # A short body but several buttons: width is driven by the button row.
        c = ChoiceContent('hi', ['&Yes', '&No', '&Cancel'])
        w, _ = c.measure(80, 24)
        # Button cells: '[ Yes ]' (7) + ' ' + '[ No ]' (6) + ' ' + '[ Cancel ]'
        # (10) = 7 + 1 + 6 + 1 + 10 = 25.
        self.assertEqual(w, 25)

    def test_width_driven_by_body_when_wider(self):
        c = ChoiceContent('a fairly long message line here', ['&OK'])
        w, _ = c.measure(80, 24)
        # Body line is 31 cells, wider than the single '[ OK ]' (6) button row.
        self.assertEqual(w, 31)

    def test_width_clamped_to_max_w(self):
        c = ChoiceContent('x' * 200, ['&OK'])
        w, _ = c.measure(30, 24)
        self.assertEqual(w, 30)

    def test_height_clamped_to_max_h_clips_body(self):
        # 10 body lines but only room for a few rows: height clamps to max_h
        # and the body is clipped to (max_h - spacer - buttons) lines.
        body = '\n'.join(f'line{i}' for i in range(10))
        c = ChoiceContent(body, ['&OK'])
        _, h = c.measure(40, 6)
        self.assertEqual(h, 6)
        self.assertEqual(c._body_lines_shown, 4)   # 6 - 1 spacer - 1 buttons

    def test_width_wide_glyphs_counted_as_cells(self):
        # A 3-glyph CJK body is 6 cells; button row '[ OK ]' is 6 — tie, 6.
        c = ChoiceContent('東京都', ['&OK'])
        w, _ = c.measure(80, 24)
        self.assertEqual(w, 6)


# --- draw_row layout -------------------------------------------------------


class TestDrawLayout(unittest.TestCase):
    def test_body_on_top_rows(self):
        c = ChoiceContent('hello world', ['&OK'])
        c.measure(40, 24)
        self.assertIn('hello world', _plain(_draw(c, 0, 38)))

    def test_spacer_row_blank(self):
        c = ChoiceContent('hello', ['&OK'])
        c.measure(40, 24)        # h=3: row0 body, row1 spacer, row2 buttons
        self.assertEqual(_plain(_draw(c, 1, 30)), ' ' * 30)

    def test_button_row_has_buttons(self):
        c = ChoiceContent('hello', ['&Yes', '&No'])
        c.measure(40, 24)        # h=3: buttons on row 2
        out = _plain(_draw(c, 2, 36))
        self.assertIn('[ Yes ]', out)
        self.assertIn('[ No ]', out)

    def test_every_row_padded_to_width(self):
        c = ChoiceContent('hi', ['&Yes', '&No'])
        c.measure(40, 24)
        for row in range(3):
            self.assertEqual(_visible(_draw(c, row, 36)), 36,
                             f'row {row} not padded to width')

    def test_leftover_rows_blank_filled(self):
        # Asking for a row past the content area blank-fills to width.
        c = ChoiceContent('hi', ['&OK'])
        c.measure(40, 24)        # h=3
        self.assertEqual(_plain(_draw(c, 5, 20)), ' ' * 20)

    def test_button_row_centered(self):
        # The single button is centered: padding on both sides.
        c = ChoiceContent('hi', ['&OK'])
        c.measure(40, 24)
        out = _plain(_draw(c, 2, 30))
        self.assertEqual(_visible(_draw(c, 2, 30)), 30)
        stripped = out.rstrip()
        # Leading pad before the '[' is roughly half the slack.
        lead = len(out) - len(out.lstrip())
        self.assertGreater(lead, 0)
        self.assertIn('[ OK ]', stripped)

    def test_button_row_clamps_when_wider_than_width(self):
        # The button row's cell count (25) exceeds the drawn width: it MUST
        # clamp to exactly ``width`` visible cells, never overflow (``end_row``
        # only pads short rows, it never truncates long ones, so an overflow
        # would push the frame's right border out of its column).
        c = ChoiceContent('hi', ('&Yes', '&No', '&Cancel'))
        c.measure(40, 24)
        self.assertEqual(c._button_row_cells, 25)
        button_row = c._h - 1
        for narrow in (1, 5, 12, 20, 24):     # all below the 25-cell row
            out = _draw(c, button_row, narrow)
            self.assertEqual(_visible(out), narrow,
                             f'button row overflowed at width {narrow}')
            # No SGR may dangle past the row.
            self.assertTrue(out.endswith('\033[0m') or '\033[' not in out,
                            f'unterminated SGR at width {narrow}')

    def test_single_button_clamps_when_wider_than_width(self):
        # The alert (single-button) case has the same overflow risk: '[ OK ]'
        # is 6 cells, so a width below 6 must still clamp exactly.
        c = ChoiceContent('hi', ('&OK',))
        c.measure(40, 24)
        self.assertEqual(c._button_row_cells, 6)
        button_row = c._h - 1
        for narrow in (1, 3, 5):
            out = _draw(c, button_row, narrow)
            self.assertEqual(_visible(out), narrow,
                             f'single-button row overflowed at width {narrow}')


# --- body text: wrap, clip marker, ANSI passthrough ------------------------


class TestBodyText(unittest.TestCase):
    def test_body_wraps_to_width(self):
        # Each drawn body row fits within the content width.
        c = ChoiceContent('alpha beta gamma delta epsilon', ['&OK'])
        c.measure(12, 24)
        for r in range(c._body_lines_shown):
            self.assertLessEqual(_visible(_draw(c, r, c._w)), c._w)

    def test_body_clip_marks_last_visible_row(self):
        # More body lines than fit: the last visible body row ends with '…'.
        body = '\n'.join(f'line{i}' for i in range(10))
        c = ChoiceContent(body, ['&OK'])
        c.measure(40, 6)             # body clipped to 4 rows
        self.assertEqual(c._body_lines_shown, 4)
        last = _plain(_draw(c, 3, c._w))   # last visible body row
        self.assertTrue(last.rstrip().endswith('…'),
                        f'expected clip marker, got {last!r}')

    def test_no_clip_marker_when_body_fits(self):
        c = ChoiceContent('one\ntwo', ['&OK'])
        c.measure(40, 24)            # plenty of room, nothing clipped
        for r in range(c._body_lines_shown):
            self.assertNotIn('…', _plain(_draw(c, r, c._w)))

    def test_embedded_ansi_preserved_on_unclipped_line(self):
        # A colored body line keeps its SGR in the emitted bytes.
        c = ChoiceContent('\033[31mDANGER\033[m here', ['&OK'])
        c.measure(40, 24)
        out = _draw(c, 0, c._w)
        self.assertIn('\033[31m', out)
        self.assertIn('DANGER', _plain(out))

    def test_non_sgr_control_neutralized(self):
        # A raw cursor-move CSI in the body is dropped (only SGR survives).
        c = ChoiceContent('a\033[2Jb', ['&OK'])
        c.measure(40, 24)
        out = _draw(c, 0, c._w)
        self.assertNotIn('\033[2J', out)
        self.assertIn('ab', _plain(out))

    def test_clipped_row_padded_to_width(self):
        body = '\n'.join(f'a longish line number {i}' for i in range(10))
        c = ChoiceContent(body, ['&OK'])
        c.measure(40, 6)
        self.assertEqual(_visible(_draw(c, c._body_lines_shown - 1, c._w)),
                         c._w)


# --- focus movement (wrapping) ---------------------------------------------


class TestFocusMovement(unittest.TestCase):
    def _three(self):
        c = ChoiceContent('m', ['&Yes', '&No', '&Cancel'])
        c.measure(60, 24)
        return c

    def test_right_advances(self):
        c = self._three()
        c.handle_key('right')
        self.assertEqual(c.focus, 1)

    def test_right_wraps(self):
        c = self._three()
        c.focus = 2
        c.handle_key('right')
        self.assertEqual(c.focus, 0)

    def test_left_wraps(self):
        c = self._three()
        c.focus = 0
        c.handle_key('left')
        self.assertEqual(c.focus, 2)

    def test_tab_moves_forward_like_right(self):
        c = self._three()
        c.handle_key('tab')
        self.assertEqual(c.focus, 1)
        c.focus = 2
        c.handle_key('tab')
        self.assertEqual(c.focus, 0)   # wraps

    def test_focused_button_reverse_video(self):
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        out = _draw(c, 2, 36)
        # Reverse-video SGR param (7) is present for the focused button.
        self.assertIn('7', out)

    def test_hotkey_char_underlined(self):
        c = ChoiceContent('m', ['&Yes'])
        c.measure(40, 24)
        out = _draw(c, 2, 36)
        # Underline SGR param (4) appears for the hotkey char.
        self.assertIn('\033[', out)
        self.assertTrue(any('4' in seq for seq in
                            _term._ANSI_CSI_RE.findall(out)),
                        'expected an underline SGR for the hotkey char')


# --- enter / hotkeys / space -----------------------------------------------


class TestActivation(unittest.TestCase):
    def test_enter_returns_focused_resolved_label(self):
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        c.handle_key('right')        # focus -> 'No'
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'No')      # resolved label, '&' stripped

    def test_enter_default_focus_is_first(self):
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'Yes')

    def test_hotkey_activates_button_immediately(self):
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        done, result = c.handle_key('n')    # 'No' hotkey, focus still on Yes
        self.assertTrue(done)
        self.assertEqual(result, 'No')

    def test_hotkey_case_insensitive(self):
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        done, result = c.handle_key('N')    # uppercase
        self.assertTrue(done)
        self.assertEqual(result, 'No')

    def test_resolved_label_strips_markers(self):
        c = ChoiceContent('m', ['O&K', 'a&&b'])
        c.measure(40, 24)
        done, result = c.handle_key('k')
        self.assertEqual(result, 'OK')
        c2 = ChoiceContent('m', ['O&K', 'a&&b'])
        c2.measure(40, 24)
        c2.handle_key('right')              # focus the 'a&b' button
        done, result = c2.handle_key('enter')
        self.assertEqual(result, 'a&b')     # literal '&' kept, marker resolved

    def test_double_amp_char_is_not_a_hotkey(self):
        # The literal '&' in 'a&&b' must not act as a hotkey.
        c = ChoiceContent('m', ['&Yes', 'a&&b'])
        c.measure(40, 24)
        done, result = c.handle_key('&')
        self.assertFalse(done)              # '&' ignored, not an activator
        self.assertIsNone(result)

    def test_single_button_space_activates(self):
        c = ChoiceContent('m', ['&OK'])
        c.measure(40, 24)
        done, result = c.handle_key('space')
        self.assertTrue(done)
        self.assertEqual(result, 'OK')

    def test_multi_button_space_does_not_activate(self):
        # space only activates the single-button (alert) case.
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        done, result = c.handle_key('space')
        self.assertFalse(done)

    def test_unrecognized_key_ignored(self):
        c = ChoiceContent('m', ['&Yes', '&No'])
        c.measure(40, 24)
        for k in ('up', 'down', 'home', 'ctrl-x', 'f1'):
            done, result = c.handle_key(k)
            self.assertFalse(done)
            self.assertIsNone(result)
        self.assertEqual(c.focus, 0)


# --- (display, value) tuple buttons: value mapping (ticket #998) ------------


class TestValueMapping(unittest.TestCase):
    """A button may be a ``(display, value)`` tuple: the display half is parsed
    for the ``&`` hotkey convention EXACTLY as a bare string is, but activating
    the button returns the tuple's ``value`` (any type) rather than the resolved
    display. A bare string keeps today's behavior — it returns its own resolved
    display, so ``b.value == b.display``."""

    def test_bare_string_value_is_resolved_display(self):
        # Backward compat: a bare string's value is its resolved display.
        c = ChoiceContent('m', ['&Yes', 'O&K', 'a&&b', 'Plain'])
        b = c._buttons
        self.assertEqual([x.value for x in b], ['Yes', 'OK', 'a&b', 'Plain'])
        # ...and a bare string's value is exactly its resolved display.
        self.assertEqual([x.value for x in b], [x.display for x in b])

    def test_tuple_parses_display_for_hotkey(self):
        # The display half is parsed identically to a bare string; the value
        # half is NOT scanned for ``&``.
        c = ChoiceContent('m', [('&Yes', 1)])
        b = c._buttons[0]
        self.assertEqual(b.display, 'Yes')
        self.assertEqual(b.hotkey, 'y')
        self.assertEqual(b.hot_index, 0)
        self.assertEqual(b.value, 1)          # the tuple's value, verbatim

    def test_tuple_value_not_parsed_for_ampersand(self):
        # A ``&`` living in the VALUE must survive untouched — only the display
        # half goes through the hotkey parser.
        c = ChoiceContent('m', [('&Save', 'a&&b')])
        b = c._buttons[0]
        self.assertEqual(b.display, 'Save')
        self.assertEqual(b.value, 'a&&b')     # value kept verbatim, not 'a&b'

    def test_enter_returns_tuple_value(self):
        c = ChoiceContent('m', [('&Yes', True), ('&No', False)])
        c.measure(40, 24)
        done, result = c.handle_key('enter')   # focus on first button
        self.assertTrue(done)
        self.assertIs(result, True)
        c2 = ChoiceContent('m', [('&Yes', True), ('&No', False)])
        c2.measure(40, 24)
        c2.handle_key('right')                 # focus -> second button
        done, result = c2.handle_key('enter')
        self.assertTrue(done)
        self.assertIs(result, False)

    def test_hotkey_returns_tuple_value(self):
        c = ChoiceContent('m', [('&Yes', True), ('&No', False)])
        c.measure(40, 24)
        done, result = c.handle_key('n')       # 'No' hotkey, focus still Yes
        self.assertTrue(done)
        self.assertIs(result, False)

    def test_single_button_space_returns_tuple_value(self):
        sentinel = object()
        c = ChoiceContent('m', [('&OK', sentinel)])
        c.measure(40, 24)
        done, result = c.handle_key('space')
        self.assertTrue(done)
        self.assertIs(result, sentinel)

    def test_mixed_string_and_tuple_sequence(self):
        # A sequence may freely mix bare strings and tuples; each returns the
        # right thing (string -> resolved display, tuple -> its value).
        c = ChoiceContent('m', ['&Yes', ('&No', 0), '&Maybe'])
        c.measure(40, 24)
        done, result = c.handle_key('y')       # bare string -> resolved display
        self.assertEqual(result, 'Yes')
        c2 = ChoiceContent('m', ['&Yes', ('&No', 0), '&Maybe'])
        c2.measure(40, 24)
        done, result = c2.handle_key('n')      # tuple -> its value (the int 0)
        self.assertIs(result, 0)
        c3 = ChoiceContent('m', ['&Yes', ('&No', 0), '&Maybe'])
        c3.measure(40, 24)
        done, result = c3.handle_key('m')      # bare string -> resolved display
        self.assertEqual(result, 'Maybe')

    def test_double_amp_literal_unaffected_in_tuple_display(self):
        # ``&&`` in a tuple's display still resolves to a literal '&' and is
        # NOT a hotkey; the value half is independent.
        c = ChoiceContent('m', [('a&&b', 99)])
        c.measure(40, 24)
        b = c._buttons[0]
        self.assertEqual(b.display, 'a&b')
        self.assertIsNone(b.hotkey)
        done, result = c.handle_key('enter')
        self.assertIs(result, 99)
        # The literal '&' still doesn't activate.
        c2 = ChoiceContent('m', [('a&&b', 99)])
        c2.measure(40, 24)
        done2, result2 = c2.handle_key('&')
        self.assertFalse(done2)
        self.assertIsNone(result2)

    def test_non_string_values_round_trip(self):
        # bool / int / arbitrary object all come back unchanged via enter.
        marker = object()
        cases = [True, 0, 42, marker, None, ['a', 'b'], {'k': 'v'}]
        for val in cases:
            c = ChoiceContent('m', [('&Go', val)])
            c.measure(40, 24)
            done, result = c.handle_key('enter')
            self.assertTrue(done)
            self.assertIs(result, val)


# --- integration through run_modal -----------------------------------------


class TestThroughRunModal(unittest.TestCase):
    """Drive ChoiceContent through the real engine with a scripted key stream
    to confirm the protocol wiring (the chosen resolved label comes back)."""

    def test_confirm_returns_chosen_label(self):
        b = _FakeBrowser()
        c = ChoiceContent('Delete 3 items?', ['&Yes', '&No'])
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['right', 'enter']))
        self.assertEqual(res, 'No')

    def test_hotkey_through_engine(self):
        b = _FakeBrowser()
        c = ChoiceContent('Delete 3 items?', ['&Yes', '&No'])
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['y']))
        self.assertEqual(res, 'Yes')

    def test_alert_space_through_engine(self):
        b = _FakeBrowser()
        c = ChoiceContent('Saved.', ['&OK'])
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['space']))
        self.assertEqual(res, 'OK')

    def test_esc_cancels_to_none(self):
        b = _FakeBrowser()
        c = ChoiceContent('Delete?', ['&Yes', '&No'])
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['esc']))
        self.assertIsNone(res)

    def test_tuple_value_through_engine(self):
        # A (display, value) tuple's value comes back through the full loop.
        b = _FakeBrowser()
        c = ChoiceContent('Delete?', [('&Yes', True), ('&No', False)])
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['right', 'enter']))
        self.assertIs(res, False)


if __name__ == '__main__':
    unittest.main()
