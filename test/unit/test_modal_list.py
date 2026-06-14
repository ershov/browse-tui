"""Tests for ``ListContent`` — the modal selection list (ticket #972).

``ListContent`` backs both ``ctx.pick`` (``filter=True``, centered) and
``ctx.menu`` (``filter=False``, anchored). It implements the modal content
protocol (``title`` / ``measure`` / ``draw_row`` / ``handle_key``) consumed
by ``run_modal``.

Most tests drive the class DIRECTLY — construct a ``ListContent``, call
``measure`` to fix its geometry, then exercise ``draw_row`` / ``handle_key``;
the full ``run_modal`` loop isn't needed to assert the content's own
behavior. One integration test drives it through ``run_modal`` with an
injected ``_read_key`` to confirm the wiring (selected string returned).

No TTY: ``draw_row`` emits through the modal module's ``write`` / ``set_style``
/ ``reset_style`` (which it shares with 050-render's segment writers after
the loader wiring), captured into a buffer — the same approach the engine
tests use. ``Rect`` / ``PaneCache`` / cell helpers / the preview pipeline
live in other numbered files and are wired in the way the concatenated build
shares a namespace.
"""

import io
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term_modallist', '020-terminal.py')
_state = load('_browse_tui_state_modallist', '040-state.py')
_render = load('_browse_tui_render_modallist', '050-render.py')
_modal = load('_browse_tui_modal_list', '055-modal.py')

# 050-render references a handful of names by bare identifier that live in
# 020-terminal in the concatenated build's shared namespace — wire them so
# the segment writers / collapse helper resolve under the isolated load.
_render._char_width = _term._char_width
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render._visible_len = _term._visible_len
_render.write = _term.write
_render.set_style = _term.set_style
_render.reset_style = _term.reset_style

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
_modal._truncate_by_cells = _render._truncate_by_cells
# ``ListContent`` reuses 050-render's cell helpers + the selection-rendering
# machinery (the SAME functions ``render_list`` uses for the cursor row).
_modal.cell_width = _render.cell_width
_modal.cell_trim = _render.cell_trim
_modal.cell_ljust = _render.cell_ljust
_modal._collapse_visible = _render._collapse_visible
_modal._write_highlighted = _render._write_highlighted
_modal._write_segments = _render._write_segments
import time as _time  # noqa: E402  (after the loader wiring block)
_modal.time = _time


ListContent = _modal.ListContent
run_modal = _modal.run_modal
Rect = _render.Rect
PaneCache = _state.PaneCache


# --- Output capture --------------------------------------------------------
#
# ``draw_row`` writes through the modal module's ``write`` (== ``_term.write``
# after wiring) and the segment writers route there too. Pointing
# ``_term._tty_writer`` at a StringIO captures the emitted bytes. Outside an
# active ``begin_row`` capture the writes go straight to the writer, which is
# exactly what we want for a direct ``draw_row`` call.


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


# --- measure ---------------------------------------------------------------


class TestMeasure(unittest.TestCase):
    def test_width_is_widest_option(self):
        c = ListContent(['ab', 'abcdefghij', 'xyz'], filter=False)
        w, h = c.measure(100, 100)
        self.assertEqual(w, 10)   # widest option 'abcdefghij'
        self.assertEqual(h, 3)    # no chrome in menu mode

    def test_width_floor_eight(self):
        # Short options floor to 8 so the box doesn't become a sliver.
        c = ListContent(['a', 'bb', 'c'], filter=False)
        w, _ = c.measure(100, 100)
        self.assertEqual(w, 8)

    def test_width_clamped_to_max_w(self):
        c = ListContent(['x' * 50], filter=False)
        w, _ = c.measure(20, 100)
        self.assertEqual(w, 20)

    def test_width_wide_glyphs_counted_as_two_cells(self):
        # A 3-glyph CJK option is 6 cells wide — wider than the 8 floor only
        # because cells, not code points, drive the width.
        c = ListContent(['東京都'], filter=False)   # 3 wide glyphs = 6 cells
        w, _ = c.measure(100, 100)
        self.assertEqual(w, 8)    # 6 < 8 floor
        c2 = ListContent(['東京都市'], filter=False)  # 4 glyphs = 8 cells
        self.assertEqual(c2.measure(100, 100)[0], 8)
        c3 = ListContent(['東京都市圏'], filter=False)  # 5 glyphs = 10 cells
        self.assertEqual(c3.measure(100, 100)[0], 10)

    def test_height_filter_adds_chrome(self):
        # filter=True adds 2 rows (prompt + separator).
        c = ListContent(['a', 'b', 'c'], filter=True)
        _, h = c.measure(100, 100)
        self.assertEqual(h, 5)    # 3 options + 2 chrome

    def test_height_menu_no_chrome(self):
        c = ListContent(['a', 'b', 'c'], filter=False)
        _, h = c.measure(100, 100)
        self.assertEqual(h, 3)

    def test_height_clamped_to_max_h(self):
        c = ListContent([str(i) for i in range(50)], filter=False)
        _, h = c.measure(100, 10)
        self.assertEqual(h, 10)

    def test_rows_visible_excludes_chrome(self):
        c = ListContent([str(i) for i in range(50)], filter=True)
        c.measure(100, 10)        # h capped at 10
        self.assertEqual(c._rows_visible, 8)   # 10 - 2 chrome
        c2 = ListContent([str(i) for i in range(50)], filter=False)
        c2.measure(100, 10)
        self.assertEqual(c2._rows_visible, 10)


# --- draw_row layout (filter vs menu) --------------------------------------


class TestDrawLayout(unittest.TestCase):
    def test_filter_row0_is_prompt(self):
        c = ListContent(['alpha', 'beta'], filter=True)
        c.measure(20, 20)
        c.filter_query = 'al'
        out = _draw(c, 0, 12)
        self.assertEqual(_plain(out), '> al'.ljust(12))

    def test_filter_row1_is_separator(self):
        c = ListContent(['alpha', 'beta'], filter=True)
        c.measure(20, 20)
        out = _draw(c, 1, 10)
        self.assertEqual(_plain(out), '─' * 10)

    def test_filter_options_start_at_row2(self):
        c = ListContent(['alpha', 'beta'], filter=True)
        c.measure(20, 20)
        # Row 2 is the first option.
        self.assertIn('alpha', _plain(_draw(c, 2, 12)))
        self.assertIn('beta', _plain(_draw(c, 3, 12)))

    def test_menu_options_start_at_row0(self):
        # filter=False: no prompt/separator; row 0 IS the first option.
        c = ListContent(['Open', 'Rename', 'Delete'], filter=False)
        c.measure(20, 20)
        self.assertIn('Open', _plain(_draw(c, 0, 12)))
        self.assertIn('Rename', _plain(_draw(c, 1, 12)))
        self.assertIn('Delete', _plain(_draw(c, 2, 12)))

    def test_option_row_padded_to_width(self):
        c = ListContent(['hi'], filter=False)
        c.measure(20, 20)
        out = _draw(c, 0, 15)
        self.assertEqual(_visible(out), 15)

    def test_option_cell_trimmed_no_wrap(self):
        # A too-long option is single-line: cell-trimmed to width, never
        # wrapped onto a second row.
        c = ListContent(['x' * 40], filter=False)
        c.measure(8, 20)          # width capped at 8
        out = _draw(c, 0, 8)
        self.assertEqual(_visible(out), 8)

    def test_row_beyond_filtered_list_blank_filled(self):
        # The area has more option rows than the (filtered) list — extra
        # rows blank-fill to width rather than indexing past the end.
        c = ListContent(['only'], filter=False)
        c.measure(20, 20)
        out = _draw(c, 5, 10)     # row 5 maps past the single option
        self.assertEqual(_plain(out), ' ' * 10)


# --- filtering -------------------------------------------------------------


class TestFiltering(unittest.TestCase):
    def test_filter_narrows_visible_list(self):
        c = ListContent(['open', 'in-progress', 'closed', 'wontfix'],
                        filter=True)
        c.measure(40, 20)
        self.assertEqual(len(c._filtered()), 4)
        c.handle_key('i')        # query 'i'
        self.assertEqual(c.filter_query, 'i')
        # 'i' appears in 'in-progress', 'wontfix' — substring, case-insens.
        self.assertEqual(c._filtered(), ['in-progress', 'wontfix'])

    def test_filter_case_insensitive(self):
        c = ListContent(['Open', 'CLOSED'], filter=True)
        c.measure(40, 20)
        c.handle_key('o')
        self.assertEqual(c._filtered(), ['Open', 'CLOSED'])  # both contain o/O

    def test_filter_preserves_order(self):
        c = ListContent(['ab', 'xb', 'cb'], filter=True)
        c.measure(40, 20)
        c.handle_key('b')
        self.assertEqual(c._filtered(), ['ab', 'xb', 'cb'])

    def test_backspace_widens_list(self):
        c = ListContent(['open', 'closed'], filter=True)
        c.measure(40, 20)
        c.handle_key('z')        # narrows to nothing matching? 'z' not in any
        self.assertEqual(c._filtered(), [])
        c.handle_key('backspace')
        self.assertEqual(c.filter_query, '')
        self.assertEqual(len(c._filtered()), 2)

    def test_space_appends_to_query(self):
        c = ListContent(['a b', 'cd'], filter=True)
        c.measure(40, 20)
        c.handle_key('a')
        c.handle_key('space')
        self.assertEqual(c.filter_query, 'a ')
        self.assertEqual(c._filtered(), ['a b'])

    def test_cursor_clamped_as_filter_narrows(self):
        c = ListContent(['open', 'in-progress', 'closed', 'wontfix'],
                        filter=True)
        c.measure(40, 20)
        c.cursor = 3              # on 'wontfix'
        c.handle_key('o')        # narrows to options containing 'o'
        # 'o' in open/in-progress/closed/wontfix -> all four still match.
        c.handle_key('p')        # 'op' -> only 'open'? in-progress has 'p'..
        # query 'op': substring 'op' present only in 'open'.
        self.assertEqual(c._filtered(), ['open'])
        self.assertLess(c.cursor, len(c._filtered()))   # clamped into range

    def test_menu_mode_ignores_printable_keys(self):
        # filter=False: typing does NOT build a query (no filtering).
        c = ListContent(['Open', 'Rename'], filter=False)
        c.measure(40, 20)
        done, _ = c.handle_key('o')
        self.assertFalse(done)
        self.assertEqual(c.filter_query, '')
        self.assertEqual(c._filtered(), ['Open', 'Rename'])


# --- selection movement (wrap, home/end) -----------------------------------


class TestSelectionMovement(unittest.TestCase):
    def _menu(self):
        c = ListContent(['a', 'b', 'c'], filter=False)
        c.measure(40, 20)
        return c

    def test_down_advances(self):
        c = self._menu()
        c.handle_key('down')
        self.assertEqual(c.cursor, 1)

    def test_down_wraps_at_end(self):
        c = self._menu()
        c.cursor = 2              # last
        c.handle_key('down')
        self.assertEqual(c.cursor, 0)   # wrapped to first

    def test_up_wraps_at_start(self):
        c = self._menu()
        c.cursor = 0
        c.handle_key('up')
        self.assertEqual(c.cursor, 2)   # wrapped to last

    def test_ctrl_n_ctrl_p_are_down_up(self):
        c = self._menu()
        c.handle_key('ctrl-n')
        self.assertEqual(c.cursor, 1)
        c.handle_key('ctrl-p')
        self.assertEqual(c.cursor, 0)

    def test_home_first_end_last(self):
        c = self._menu()
        c.handle_key('end')
        self.assertEqual(c.cursor, 2)
        c.handle_key('home')
        self.assertEqual(c.cursor, 0)

    def test_move_on_empty_filtered_list_is_noop(self):
        c = ListContent(['open'], filter=True)
        c.measure(40, 20)
        c.handle_key('z')        # filters to nothing
        self.assertEqual(c._filtered(), [])
        # Moving with an empty list must not crash or move off zero.
        for k in ('down', 'up', 'ctrl-n', 'ctrl-p', 'end'):
            done, _ = c.handle_key(k)
            self.assertFalse(done)
            self.assertEqual(c.cursor, 0)


# --- windowing (keep the cursor visible) -----------------------------------


class TestWindowing(unittest.TestCase):
    def _drawn_options(self, c, width):
        """The plain option strings the engine would draw, top to bottom."""
        out = []
        for r in range(c._rows_visible):
            row = r + c._chrome
            text = _plain(_draw(c, row, width)).rstrip()
            out.append(text)
        return out

    def test_window_scrolls_down_to_keep_cursor_visible(self):
        # 20 options, only 5 rows visible. Walk the cursor to the bottom;
        # the drawn window must always include the selected option.
        opts = [f'opt{i:02d}' for i in range(20)]
        c = ListContent(opts, filter=False)
        c.measure(40, 5)          # 5 option rows (no chrome)
        self.assertEqual(c._rows_visible, 5)
        for _ in range(12):       # move cursor to index 12
            c.handle_key('down')
        self.assertEqual(c.cursor, 12)
        shown = self._drawn_options(c, 40)
        self.assertIn('opt12', shown)             # selected option is drawn
        self.assertEqual(len(shown), 5)           # exactly the window
        # Cursor near the bottom of the window after scrolling down.
        self.assertEqual(c._scroll + (c.cursor - c._scroll), 12)

    def test_window_scrolls_up_to_keep_cursor_visible(self):
        opts = [f'opt{i:02d}' for i in range(20)]
        c = ListContent(opts, filter=False)
        c.measure(40, 5)
        # Jump to the end then walk back up past the top of the window.
        c.handle_key('end')       # cursor 19, window at the bottom
        self.assertIn('opt19', self._drawn_options(c, 40))
        for _ in range(17):       # back up to index 2
            c.handle_key('up')
        self.assertEqual(c.cursor, 2)
        shown = self._drawn_options(c, 40)
        self.assertIn('opt02', shown)
        self.assertEqual(len(shown), 5)

    def test_wrap_down_jumps_window_to_top(self):
        # Wrapping from the last option to the first must scroll the window
        # back so the first option (the new selection) is visible.
        opts = [f'opt{i:02d}' for i in range(20)]
        c = ListContent(opts, filter=False)
        c.measure(40, 5)
        c.handle_key('end')       # cursor 19
        c.handle_key('down')      # wraps to 0
        self.assertEqual(c.cursor, 0)
        shown = self._drawn_options(c, 40)
        self.assertIn('opt00', shown)
        self.assertEqual(c._scroll, 0)

    def test_no_scroll_when_all_fit(self):
        opts = ['a', 'b', 'c']
        c = ListContent(opts, filter=False)
        c.measure(40, 10)         # 10 rows >> 3 options
        c.handle_key('end')
        self.assertEqual(c._scroll, 0)   # never scrolls when everything fits

    def test_scroll_clamped_when_filter_shrinks_list(self):
        # Scroll down a long list, then filter to a short list: the window
        # must not be parked past the (now short) list's end.
        opts = [f'item{i:02d}' for i in range(20)]
        c = ListContent(opts, filter=True)
        c.measure(40, 7)          # 5 option rows (7 - 2 chrome)
        for _ in range(15):
            c.handle_key('down')  # scroll well down
        self.assertGreater(c._scroll, 0)
        c.handle_key('1')         # query '1' still matches many (item1x, *1*)
        c.handle_key('9')         # 'item19' is the only one with '19'
        self.assertEqual(c._filtered(), ['item19'])
        self.assertEqual(c._scroll, 0)   # clamped back to the start


# --- selection rendering: ANSI passthrough vs plain reverse video ----------


class TestSelectionRendering(unittest.TestCase):
    """The selected option is plain reverse video (embedded SGR stripped);
    unselected option rows render their embedded ANSI normally — the list
    pane's rule, reused via ``_collapse_visible`` / ``_write_segments``."""

    def test_unselected_passes_embedded_ansi_through(self):
        # A red-colored option, NOT selected — the SGR survives in the
        # emitted bytes.
        red = '\033[31mDANGER\033[m'
        c = ListContent([red, 'safe'], filter=False)
        c.measure(20, 20)
        c.cursor = 1              # select 'safe', so row 0 is unselected
        out = _draw(c, 0, 12)
        self.assertIn('\033[31m', out)            # red SGR passed through
        self.assertEqual(_plain(out).rstrip(), 'DANGER')

    def test_selected_strips_embedded_ansi_to_plain_reverse(self):
        # The SAME red option, now SELECTED — the embedded color is stripped
        # and the row is drawn in reverse video instead.
        red = '\033[31mDANGER\033[m'
        c = ListContent([red, 'safe'], filter=False)
        c.measure(20, 20)
        c.cursor = 0              # select the red option
        out = _draw(c, 0, 12)
        self.assertNotIn('\033[31m', out)         # embedded color stripped
        self.assertIn('7', out)                   # reverse-video SGR param
        self.assertEqual(_plain(out).rstrip(), 'DANGER')

    def test_selected_reverse_pads_full_width(self):
        # The reverse highlight extends across the full inner width (a
        # stripe, not a tag-shaped patch).
        c = ListContent(['hi', 'bye'], filter=False)
        c.measure(20, 20)
        c.cursor = 0
        out = _draw(c, 0, 15)
        self.assertEqual(_visible(out), 15)
        # Reverse style is present (param 7).
        self.assertIn('7', out)


# --- enter -----------------------------------------------------------------


class TestEnter(unittest.TestCase):
    def test_enter_returns_selected_string(self):
        c = ListContent(['a', 'b', 'c'], filter=False)
        c.measure(40, 20)
        c.handle_key('down')      # cursor -> 1 ('b')
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'b')

    def test_enter_returns_selected_from_filtered_list(self):
        c = ListContent(['open', 'in-progress', 'closed'], filter=True)
        c.measure(40, 20)
        c.handle_key('i')         # filter -> ['in-progress'] ('i' in others?)
        # 'i' substring: 'in-progress' yes, 'open' no, 'closed' no.
        self.assertEqual(c._filtered(), ['in-progress'])
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'in-progress')

    def test_enter_on_empty_filtered_list_is_noop(self):
        c = ListContent(['open', 'closed'], filter=True)
        c.measure(40, 20)
        c.handle_key('z')         # no option contains 'z'
        self.assertEqual(c._filtered(), [])
        done, result = c.handle_key('enter')
        self.assertFalse(done)    # no-op
        self.assertIsNone(result)


# --- other keys ignored ----------------------------------------------------


class TestIgnoredKeys(unittest.TestCase):
    def test_unrecognized_key_ignored(self):
        c = ListContent(['a', 'b'], filter=False)
        c.measure(40, 20)
        for k in ('tab', 'ctrl-x', 'f1', 'left', 'right'):
            done, result = c.handle_key(k)
            self.assertFalse(done)
            self.assertIsNone(result)
        self.assertEqual(c.cursor, 0)   # nothing moved


# --- integration through run_modal -----------------------------------------


class TestThroughRunModal(unittest.TestCase):
    """Drive ListContent through the real engine with a scripted key stream
    to confirm the protocol wiring (the chosen string comes back)."""

    def test_pick_returns_chosen_option(self):
        b = _FakeBrowser()
        c = ListContent(['open', 'in-progress', 'closed'], filter=True)
        # Type 'cl' -> filters to ['closed'], then enter.
        keys = ['c', 'l', 'enter']
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(keys))
        self.assertEqual(res, 'closed')

    def test_menu_returns_chosen_option(self):
        b = _FakeBrowser()
        c = ListContent(['Open', 'Rename', 'Delete'], filter=False)
        # down -> 'Rename', enter.
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, anchor=(5, 10), placement='anchor',
                            _read_key=_scripted(['down', 'enter']))
        self.assertEqual(res, 'Rename')

    def test_esc_cancels_to_none(self):
        b = _FakeBrowser()
        c = ListContent(['a', 'b'], filter=True)
        with _FixedTermSize(80, 24), _Capture():
            res = run_modal(b, c, _read_key=_scripted(['esc']))
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
