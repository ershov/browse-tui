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

# ``modal_menu`` references ``CONTEXT_MENU_TRIGGER_KEYS`` — defined in
# 070-actions (the OPEN path's source of truth) and resolved by concatenation
# in the single-file build. The isolated load doesn't pull 070, so mirror the
# literal here for the menu close-gesture tests (#1039).
_modal.CONTEXT_MENU_TRIGGER_KEYS = frozenset({'\\', 'f1'})


ListContent = _modal.ListContent
run_modal = _modal.run_modal
modal_menu = _modal.modal_menu
modal_pick = _modal.modal_pick
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
        # ``_filtered`` returns (display, value) pairs; bare strings are (s, s).
        self.assertEqual(c._filtered(),
                         [('in-progress', 'in-progress'),
                          ('wontfix', 'wontfix')])

    def test_filter_case_insensitive(self):
        c = ListContent(['Open', 'CLOSED'], filter=True)
        c.measure(40, 20)
        c.handle_key('o')
        # both contain o/O
        self.assertEqual(c._filtered(),
                         [('Open', 'Open'), ('CLOSED', 'CLOSED')])

    def test_filter_preserves_order(self):
        c = ListContent(['ab', 'xb', 'cb'], filter=True)
        c.measure(40, 20)
        c.handle_key('b')
        self.assertEqual(c._filtered(),
                         [('ab', 'ab'), ('xb', 'xb'), ('cb', 'cb')])

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
        self.assertEqual(c._filtered(), [('a b', 'a b')])

    def test_cursor_clamped_as_filter_narrows(self):
        c = ListContent(['open', 'in-progress', 'closed', 'wontfix'],
                        filter=True)
        c.measure(40, 20)
        c.cursor = 3              # on 'wontfix'
        c.handle_key('o')        # narrows to options containing 'o'
        # 'o' in open/in-progress/closed/wontfix -> all four still match.
        c.handle_key('p')        # 'op' -> only 'open'? in-progress has 'p'..
        # query 'op': substring 'op' present only in 'open'.
        self.assertEqual(c._filtered(), [('open', 'open')])
        self.assertLess(c.cursor, len(c._filtered()))   # clamped into range

    def test_menu_mode_ignores_printable_keys(self):
        # filter=False: typing does NOT build a query (no filtering).
        c = ListContent(['Open', 'Rename'], filter=False)
        c.measure(40, 20)
        done, _ = c.handle_key('o')
        self.assertFalse(done)
        self.assertEqual(c.filter_query, '')
        self.assertEqual(c._filtered(),
                         [('Open', 'Open'), ('Rename', 'Rename')])


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


# --- type-ahead (filter=False menus only) ----------------------------------


class TestTypeAhead(unittest.TestCase):
    """In a NO-FILTER menu (``filter=False``) a single printable char is
    single-letter type-ahead: it jumps the selection to the NEXT option whose
    visible display starts with that char (case-insensitive), cycling forward
    from the current selection and wrapping. No match → no move. (Filter mode
    instead types into the query — covered in ``TestFiltering`` and the
    regression test at the end of this class.)
    """

    def _menu(self, options):
        c = ListContent(options, filter=False)
        c.measure(40, 20)
        return c

    def test_letter_jumps_to_next_match(self):
        c = self._menu(['Open', 'Rename', 'Save', 'Delete'])
        done, result = c.handle_key('s')
        self.assertFalse(done)            # type-ahead never closes the menu
        self.assertIsNone(result)
        self.assertEqual(c.cursor, 2)     # 'Save'

    def test_repeated_letter_cycles_through_matches(self):
        # Three options start with 'S'; pressing 's' walks them in order.
        c = self._menu(['Save', 'Open', 'Send', 'Sync', 'Quit'])
        self.assertEqual(c.cursor, 0)     # starts on 'Save'
        c.handle_key('s')                 # next match after 'Save' -> 'Send'
        self.assertEqual(c.cursor, 2)
        c.handle_key('s')                 # -> 'Sync'
        self.assertEqual(c.cursor, 3)
        c.handle_key('s')                 # wraps back to 'Save'
        self.assertEqual(c.cursor, 0)

    def test_cycle_wraps_to_match_before_current(self):
        # The only 'A' match sits before the current selection — the forward
        # search wraps around to find it.
        c = self._menu(['Apple', 'Open', 'Rename'])
        c.handle_key('end')               # cursor -> 2 ('Rename'), last
        self.assertEqual(c.cursor, 2)
        c.handle_key('a')                 # wraps forward to 'Apple' at 0
        self.assertEqual(c.cursor, 0)

    def test_case_insensitive(self):
        c = self._menu(['Open', 'Save'])
        c.handle_key('S')                 # uppercase matches 'Save'
        self.assertEqual(c.cursor, 1)
        c.cursor = 0
        c.handle_key('s')                 # lowercase matches the same item
        self.assertEqual(c.cursor, 1)

    def test_no_match_is_noop(self):
        c = self._menu(['Open', 'Rename', 'Delete'])
        c.handle_key('down')              # cursor -> 1 ('Rename')
        done, result = c.handle_key('z')  # nothing starts with 'z'
        self.assertFalse(done)
        self.assertIsNone(result)
        self.assertEqual(c.cursor, 1)     # unchanged

    def test_single_match_on_current_stays_put(self):
        # Only one option starts with 'O' and it IS the current selection;
        # the forward-then-wrap search lands back on it (no spurious move).
        c = self._menu(['Open', 'Rename', 'Delete'])
        self.assertEqual(c.cursor, 0)     # 'Open'
        c.handle_key('o')
        self.assertEqual(c.cursor, 0)

    def test_matches_display_half_of_tuple_not_value(self):
        # The match is on the DISPLAY half; a value starting with the char
        # must NOT make the option match.
        c = self._menu([('Open', 'zzz'), ('Save', 'aaa')])
        c.handle_key('z')                 # no DISPLAY starts with 'z'
        self.assertEqual(c.cursor, 0)     # no move
        c.handle_key('s')                 # display 'Save' matches
        self.assertEqual(c.cursor, 1)

    def test_matches_visible_text_ignoring_embedded_sgr(self):
        # An option's display carries embedded SGR; type-ahead matches the
        # VISIBLE first char ('D'), not the leading escape bytes.
        red_delete = '\033[31mDelete\033[m'
        c = self._menu(['Open', red_delete])
        c.handle_key('d')
        self.assertEqual(c.cursor, 1)

    def test_matches_first_non_space_char(self):
        # Leading whitespace in the display is skipped — the first VISIBLE
        # char drives the match.
        c = self._menu(['Open', '  Save'])
        c.handle_key('s')
        self.assertEqual(c.cursor, 1)

    def test_typeahead_scrolls_window_to_match(self):
        # Type-ahead must keep the landed-on option inside the visible window.
        opts = [f'opt{i:02d}' for i in range(20)] + ['zebra']
        c = ListContent(opts, filter=False)
        c.measure(40, 5)                  # only 5 rows visible
        self.assertEqual(c._rows_visible, 5)
        c.handle_key('z')                 # jump to 'zebra' at index 20
        self.assertEqual(c.cursor, 20)
        # The window scrolled so the cursor is within [_scroll, _scroll+rows).
        self.assertTrue(c._scroll <= c.cursor < c._scroll + c._rows_visible)

    def test_filter_true_printable_still_extends_query(self):
        # REGRESSION: in filter mode (the picker) a printable char must STILL
        # type into the filter query — type-ahead is menu-only and must NOT
        # leak into pick behavior.
        c = ListContent(['Save', 'Send', 'Open'], filter=True)
        c.measure(40, 20)
        c.handle_key('s')
        self.assertEqual(c.filter_query, 's')   # query extended, not a jump
        self.assertEqual(c._filtered(),
                         [('Save', 'Save'), ('Send', 'Send')])
        c.handle_key('e')                       # 'se' narrows to 'Send'
        self.assertEqual(c.filter_query, 'se')
        self.assertEqual(c._filtered(), [('Send', 'Send')])


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
        self.assertEqual(c._filtered(), [('item19', 'item19')])
        self.assertEqual(c._scroll, 0)   # clamped back to the start


# --- selected_row_offset: the #1101 modal-anchor advance accessor ----------


class TestSelectedRowOffset(unittest.TestCase):
    """``selected_row_offset()`` — the content row the engine anchors the next
    modal to (#1101). It is the selected option's row WITHIN the content area:
    the filter chrome plus the in-window position (``cursor - scroll``) — the
    same row ``draw_row`` highlights. ``None`` when the filtered list is empty.
    """

    def test_menu_offset_is_cursor_no_chrome(self):
        # filter=False: no chrome, so the offset is the cursor (everything
        # fits → scroll 0).
        c = ListContent(['a', 'b', 'c'], filter=False)
        c.measure(40, 20)
        self.assertEqual(c.selected_row_offset(), 0)
        c.handle_key('down')
        self.assertEqual(c.selected_row_offset(), 1)
        c.handle_key('end')
        self.assertEqual(c.selected_row_offset(), 2)

    def test_filter_offset_includes_chrome(self):
        # filter=True: the prompt + separator add 2 rows of chrome, so the
        # offset is cursor + 2 (everything fits → scroll 0).
        c = ListContent(['alpha', 'beta', 'gamma'], filter=True)
        c.measure(40, 20)
        self.assertEqual(c.selected_row_offset(), 2)       # row 0 selected
        c.handle_key('down')
        self.assertEqual(c.selected_row_offset(), 3)       # row 1 → 1 + 2

    def test_offset_is_in_window_position_when_scrolled(self):
        # When the list scrolls, the offset is the cursor's position WITHIN the
        # visible window (cursor - scroll), not its absolute index — exactly
        # the on-screen row the highlight lands on.
        opts = [f'opt{i:02d}' for i in range(20)]
        c = ListContent(opts, filter=False)
        c.measure(40, 5)            # 5 option rows, no chrome
        for _ in range(12):
            c.handle_key('down')    # cursor 12, window scrolled down
        self.assertEqual(c.cursor, 12)
        self.assertGreater(c._scroll, 0)
        self.assertEqual(c.selected_row_offset(), c.cursor - c._scroll)
        # And it stays within the visible window.
        self.assertTrue(0 <= c.selected_row_offset() < c._rows_visible)

    def test_offset_none_when_filtered_empty(self):
        # A query matching nothing → no selection → None (the slot is left
        # unchanged by the engine).
        c = ListContent(['open', 'closed'], filter=True)
        c.measure(40, 20)
        c.handle_key('z')           # no option contains 'z'
        self.assertEqual(c._filtered(), [])
        self.assertIsNone(c.selected_row_offset())


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
        self.assertEqual(c._filtered(), [('in-progress', 'in-progress')])
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


# --- (display, value) tuple options: value mapping (ticket #999) ------------


class TestValueMapping(unittest.TestCase):
    """An option may be a ``(display, value)`` 2-tuple: the DISPLAY half is
    shown and filtered on, but ``enter`` returns the tuple's ``value`` (any
    type). A bare string keeps today's behavior — its value IS the string
    verbatim (no ``&`` hotkey convention for lists), so the pair is ``(s, s)``.
    """

    def test_bare_string_normalizes_to_self_pair(self):
        # Backward compat: a bare string becomes ``(s, s)`` — display == value.
        c = ListContent(['open', 'closed'], filter=False)
        self.assertEqual(c._options, [('open', 'open'), ('closed', 'closed')])

    def test_tuple_keeps_display_and_value_separate(self):
        c = ListContent([('Open', 1), ('Closed', 2)], filter=False)
        self.assertEqual(c._options, [('Open', 1), ('Closed', 2)])

    def test_enter_returns_tuple_value(self):
        c = ListContent([('Open', 1), ('Closed', 2)], filter=False)
        c.measure(40, 20)
        c.handle_key('down')          # cursor -> ('Closed', 2)
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 2)   # the value, not the display

    def test_enter_returns_bare_string_itself(self):
        c = ListContent(['open', 'closed'], filter=False)
        c.measure(40, 20)
        c.handle_key('down')          # cursor -> 'closed'
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'closed')

    def test_filter_matches_display_not_value(self):
        # The query matches the DISPLAY half; a substring living only in the
        # value must NOT make the option match.
        c = ListContent([('Open', 'xyzzy'), ('Closed', 'abc')], filter=True)
        c.measure(40, 20)
        c.handle_key('x')             # 'x' is in the value 'xyzzy', not display
        self.assertEqual(c._filtered(), [])   # no display contains 'x'
        c.handle_key('backspace')
        c.handle_key('o')             # 'o' is in both displays (Open/Closed)
        self.assertEqual(c._filtered(), [('Open', 'xyzzy'), ('Closed', 'abc')])

    def test_filter_then_enter_returns_value(self):
        c = ListContent([('Open', 1), ('In progress', 2), ('Closed', 3)],
                        filter=True)
        c.measure(40, 20)
        c.handle_key('g')             # 'g' substring present only in 'progress'
        c.handle_key('r')             # 'gr' -> 'In progress' alone
        self.assertEqual(c._filtered(), [('In progress', 2)])
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 2)

    def test_mixed_string_and_tuple_sequence(self):
        # A sequence may freely mix bare strings and tuples; each returns the
        # right thing (string -> itself, tuple -> its value).
        c = ListContent(['plain', ('Display', 99), 'other'], filter=False)
        c.measure(40, 20)
        self.assertEqual(c._options,
                         [('plain', 'plain'), ('Display', 99),
                          ('other', 'other')])
        done, result = c.handle_key('enter')   # cursor 0 -> 'plain'
        self.assertEqual(result, 'plain')
        c2 = ListContent(['plain', ('Display', 99), 'other'], filter=False)
        c2.measure(40, 20)
        c2.handle_key('down')                   # cursor 1 -> ('Display', 99)
        done, result = c2.handle_key('enter')
        self.assertIs(result, 99)               # the tuple's value, verbatim

    def test_menu_tuple_round_trips(self):
        # filter=False (menu): a tuple option's value comes back on enter.
        c = ListContent([('Rename…', 'rename'), ('Delete', 'delete')],
                        filter=False)
        c.measure(40, 20)
        c.handle_key('down')          # cursor -> ('Delete', 'delete')
        done, result = c.handle_key('enter')
        self.assertTrue(done)
        self.assertEqual(result, 'delete')

    def test_non_string_values_round_trip(self):
        # bool / int / arbitrary object all come back unchanged via enter.
        marker = object()
        for val in (True, 0, 42, marker, None, ['a', 'b'], {'k': 'v'}):
            c = ListContent([('Go', val)], filter=False)
            c.measure(40, 20)
            done, result = c.handle_key('enter')
            self.assertTrue(done)
            self.assertIs(result, val)

    def test_display_is_rendered_not_value(self):
        # The drawn row shows the DISPLAY half, never the value.
        c = ListContent([('Pretty', 'ugly-value')], filter=False)
        c.measure(40, 20)
        out = _plain(_draw(c, 0, 20))
        self.assertIn('Pretty', out)
        self.assertNotIn('ugly-value', out)

    def test_measure_width_from_display(self):
        # Width comes from the DISPLAY half's cell width, not the value's.
        c = ListContent([('ab', 'a-very-long-value-string')], filter=False)
        w, _ = c.measure(100, 100)
        self.assertEqual(w, 8)        # display 'ab' floors to 8; value ignored


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


class TestMenuCloseGesture(unittest.TestCase):
    """A repeated context-menu trigger (``\\`` / F1 / right-click) closes an
    open ``ctx.menu`` (#1039). Driven through the real ``modal_menu`` so the
    wiring of the centralized trigger set is exercised end-to-end. The CRITICAL
    interaction with #1042 type-ahead: ``\\`` is printable, so the close check
    must beat type-ahead — feeding ``\\`` returns None, NOT a type-ahead jump."""

    _ITEMS = ['Open', 'Rename', 'Delete']

    def test_backslash_closes_menu(self):
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_menu(b, self._ITEMS, anchor=(5, 10),
                             _read_key=_scripted(['\\']))
        self.assertIsNone(res)

    def test_f1_closes_menu(self):
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_menu(b, self._ITEMS, anchor=(5, 10),
                             _read_key=_scripted(['f1']))
        self.assertIsNone(res)

    def test_right_click_closes_menu(self):
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_menu(b, self._ITEMS, anchor=(5, 10),
                             _read_key=_scripted(['right-click:9:9']))
        self.assertIsNone(res)

    def test_backslash_does_not_typeahead(self):
        # If '\' were treated as a printable (type-ahead) it would NOT close,
        # so 'enter' next would return a selection. Prove the opposite: '\'
        # closes immediately and 'enter' is never consumed (returns None).
        b = _FakeBrowser()
        sentinel = object()

        def _src():
            for k in ('\\',):
                yield k
            # The menu must have closed on '\'; if it didn't, the loop would
            # ask for another key — surface that as a clear failure.
            raise AssertionError('menu did not close on "\\" (type-ahead?)')
            yield sentinel  # pragma: no cover

        gen = _src()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_menu(b, self._ITEMS, anchor=(5, 10),
                             _read_key=lambda: next(gen))
        self.assertIsNone(res)

    def test_normal_letter_still_typeaheads_not_close(self):
        # A non-trigger printable letter is type-ahead (does NOT close): 'd'
        # jumps to 'Delete', then enter selects it.
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_menu(b, self._ITEMS, anchor=(5, 10),
                             _read_key=_scripted(['d', 'enter']))
        self.assertEqual(res, 'Delete')

    def test_enter_still_selects(self):
        # The close gesture doesn't disturb normal selection: enter returns
        # the focused item.
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_menu(b, self._ITEMS, anchor=(5, 10),
                             _read_key=_scripted(['down', 'enter']))
        self.assertEqual(res, 'Rename')


class TestPickCloseGesture(unittest.TestCase):
    """``ctx.pick`` (``modal_pick``, ``filter=True``) is dismissed by the
    context-menu trigger (#1063) — but RESPECTING text entry: F1 and right-click
    (never text) cancel, while ``\\`` stays a literal filter character (it must
    NOT close, since the filter row is text)."""

    def test_backslash_extends_filter_not_close(self):
        # Items chosen so a '\' in the query filters to exactly one, which
        # 'enter' then selects — proving '\' extended the filter (a close
        # would have returned None instead). This is the text-respect case.
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_pick(b, 'Pick', ['a/b', r'a\b', 'cc'],
                             _read_key=_scripted(['\\', 'enter']))
        self.assertEqual(res, r'a\b')

    def test_f1_closes_pick(self):
        # F1 is never text → it cancels the pick (returns None) before content.
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_pick(b, 'Pick', ['one', 'two'],
                             _read_key=_scripted(['f1']))
        self.assertIsNone(res)

    def test_right_click_closes_pick(self):
        # A right-click is never text → it cancels the pick (returns None).
        b = _FakeBrowser()
        with _FixedTermSize(80, 24), _Capture():
            res = modal_pick(b, 'Pick', ['one', 'two'],
                             _read_key=_scripted(['right-click:9:9']))
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
