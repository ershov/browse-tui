"""Tests for the public cell-accurate string helpers and the styles API.

Stage 1 of the columnar-lists design (spec sec D + Goal 4):

  * ``cell_width`` / ``cell_ljust`` / ``cell_rjust`` / ``cell_center`` /
    ``cell_trim`` / ``cell_fit`` — measure & format plain text in display
    *cells* (wide-char aware), reusing ``_char_width`` / ``_visible_len`` /
    ``_truncate_by_cells`` (no duplicated width logic).
  * ``style(name)`` / ``STYLE_NAMES`` / ``MARKER_FG`` / ``ID_FG`` /
    ``DIM_FG`` — the named + raw faces of the internal ``_TAG_STYLE`` map
    and the chrome palette constants.

These are pure, additive module-level functions (exported via the
``browse_tui`` alias); no terminal in the loop. The width primitives that
live in ``020-terminal.py`` are wired into the separately-loaded render
module the same way the other render tests do it.
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term_cells', '020-terminal.py')
_render = load('_browse_tui_render_cells', '050-render.py')

# Production builds get these via the concatenated namespace; the per-file
# test loader has to wire the width primitives into the render module.
_render._char_width = _term._char_width
_render._visible_len = _term._visible_len

cell_width = _render.cell_width
cell_ljust = _render.cell_ljust
cell_rjust = _render.cell_rjust
cell_center = _render.cell_center
cell_trim = _render.cell_trim
cell_fit = _render.cell_fit
style = _render.style
STYLE_NAMES = _render.STYLE_NAMES
MARKER_FG = _render.MARKER_FG
ID_FG = _render.ID_FG
DIM_FG = _render.DIM_FG

# A few wide (East Asian Fullwidth / Wide) characters — each is 2 cells.
WIDE = '你好'        # 你好  -> 4 cells
WIDE1 = '你'             # 你    -> 2 cells
ELLIPSIS = '…'          # … -> 1 cell


class TestCellWidth(unittest.TestCase):
    def test_ascii_counts_codepoints(self):
        self.assertEqual(cell_width('abc'), 3)

    def test_empty(self):
        self.assertEqual(cell_width(''), 0)

    def test_wide_chars_count_as_two(self):
        self.assertEqual(cell_width(WIDE), 4)
        self.assertEqual(cell_width(WIDE1), 2)

    def test_mixed(self):
        # 'a' (1) + 你 (2) + 'b' (1) = 4
        self.assertEqual(cell_width('a你b'), 4)

    def test_matches_visible_len(self):
        # cell_width is the public face of _visible_len.
        for s in ('', 'abc', WIDE, 'a你b', 'x' * 40):
            self.assertEqual(cell_width(s), _render._visible_len(s))


class TestCellPad(unittest.TestCase):
    def test_ljust_pads_right(self):
        self.assertEqual(cell_ljust('ab', 5), 'ab   ')
        self.assertEqual(cell_width(cell_ljust('ab', 5)), 5)

    def test_rjust_pads_left(self):
        self.assertEqual(cell_rjust('ab', 5), '   ab')
        self.assertEqual(cell_width(cell_rjust('ab', 5)), 5)

    def test_center_pads_both(self):
        # 1 left, 2 right — extra goes right, matching str.center.
        self.assertEqual(cell_center('ab', 5), ' ab  ')
        self.assertEqual(cell_width(cell_center('ab', 5)), 5)

    def test_center_even_padding(self):
        self.assertEqual(cell_center('ab', 6), '  ab  ')

    def test_unchanged_when_already_exact(self):
        self.assertEqual(cell_ljust('abc', 3), 'abc')
        self.assertEqual(cell_rjust('abc', 3), 'abc')
        self.assertEqual(cell_center('abc', 3), 'abc')

    def test_unchanged_when_wider(self):
        # No truncation — padding never shrinks.
        self.assertEqual(cell_ljust('abcdef', 3), 'abcdef')
        self.assertEqual(cell_rjust('abcdef', 3), 'abcdef')
        self.assertEqual(cell_center('abcdef', 3), 'abcdef')

    def test_wide_content_pads_by_cells(self):
        # 你好 is 4 cells; pad to 6 -> 2 spaces.
        self.assertEqual(cell_ljust(WIDE, 6), WIDE + '  ')
        self.assertEqual(cell_width(cell_ljust(WIDE, 6)), 6)
        self.assertEqual(cell_rjust(WIDE, 6), '  ' + WIDE)

    def test_custom_fill(self):
        self.assertEqual(cell_ljust('ab', 5, fill='.'), 'ab...')
        self.assertEqual(cell_rjust('ab', 5, fill='-'), '---ab')

    def test_wide_fill_rejected(self):
        # fill must be exactly one cell.
        with self.assertRaises(ValueError):
            cell_ljust('ab', 5, fill=WIDE1)
        with self.assertRaises(ValueError):
            cell_rjust('ab', 5, fill='')
        with self.assertRaises(ValueError):
            cell_center('ab', 5, fill='ab')

    def test_escape_bearing_fill_rejected(self):
        # An ANSI-bearing fill has visible width 1 but occupies several raw
        # cells when tiled verbatim — counting its ANSI-stripped width would
        # wrongly accept it and corrupt the column math. The guard counts
        # raw chars (_char_width_total, not cell_width), so it's rejected.
        ansi_fill = '\033[0m '   # 5 raw chars; visible width 1
        self.assertEqual(cell_width(ansi_fill), 1)
        for fn in (cell_ljust, cell_rjust, cell_center):
            with self.assertRaises(ValueError):
                fn('ab', 5, fill=ansi_fill)
        with self.assertRaises(ValueError):
            cell_fit('ab', 5, fill=ansi_fill)

    def test_nonpositive_width(self):
        # width <= 0 leaves a string that's already wider unchanged.
        self.assertEqual(cell_ljust('ab', 0), 'ab')
        self.assertEqual(cell_rjust('', 0), '')


class TestCellTrim(unittest.TestCase):
    def test_noop_when_fits(self):
        self.assertEqual(cell_trim('abc', 5), 'abc')
        self.assertEqual(cell_trim('abc', 3), 'abc')
        self.assertEqual(cell_trim('', 3), '')

    def test_end_placement(self):
        # 'abcdef' to 4 cells -> 3 content + '…'.
        self.assertEqual(cell_trim('abcdef', 4), 'abc' + ELLIPSIS)
        self.assertEqual(cell_width(cell_trim('abcdef', 4)), 4)

    def test_start_placement(self):
        self.assertEqual(cell_trim('abcdef', 4, where='start'),
                         ELLIPSIS + 'def')
        self.assertEqual(
            cell_width(cell_trim('abcdef', 4, where='start')), 4)

    def test_middle_placement(self):
        # 'abcdef' to 5 cells -> 4 content + '…'; head gets the extra cell.
        out = cell_trim('abcdef', 5, where='middle')
        self.assertEqual(out, 'ab' + ELLIPSIS + 'ef')
        self.assertEqual(cell_width(out), 5)

    def test_middle_odd_budget_head_heavy(self):
        # 'abcdefg' to 4 cells -> 3 content budget; head ceil(3/2)=2, tail 1.
        out = cell_trim('abcdefg', 4, where='middle')
        self.assertEqual(out, 'ab' + ELLIPSIS + 'g')
        self.assertEqual(cell_width(out), 4)

    def test_custom_ellipsis_three_dots(self):
        # '...' is 3 cells; budget 6 -> 3 content + '...'.
        out = cell_trim('abcdefgh', 6, ellipsis='...')
        self.assertEqual(out, 'abc...')
        self.assertEqual(cell_width(out), 6)

    def test_ellipsis_width_accounted_end(self):
        # The returned string never exceeds the requested width, and the
        # ellipsis is included within it (not appended past it).
        for w in range(2, 8):
            out = cell_trim('abcdefghij', w)
            self.assertLessEqual(cell_width(out), w)
            self.assertTrue(out.endswith(ELLIPSIS))

    def test_word_boundary_snaps_to_space(self):
        # Middle trim prefers a space near the cut so a word isn't split.
        s = 'hello world foo'
        out = cell_trim(s, 11, where='middle', word_boundary=True)
        self.assertEqual(cell_width(out), 11)
        # The head ends at a word boundary (no trailing partial word before …)
        head = out.split(ELLIPSIS)[0]
        self.assertTrue(head in ('hello ', 'hello'),
                        f'head not snapped to a word boundary: {head!r}')

    def test_word_boundary_falls_back_when_no_space(self):
        # No space near the cut -> behaves like the non-word-boundary trim.
        s = 'abcdefghij'
        plain = cell_trim(s, 5, where='middle')
        wb = cell_trim(s, 5, where='middle', word_boundary=True)
        self.assertEqual(wb, plain)

    def test_wide_chars_no_overshoot_end(self):
        # 你好你好 (8 cells) trimmed to 5: '…' is 1 cell, content budget 4.
        # Two wide chars fit exactly (4 cells); never overshoot 5.
        s = WIDE + WIDE
        out = cell_trim(s, 5)
        self.assertLessEqual(cell_width(out), 5)
        self.assertTrue(out.endswith(ELLIPSIS))

    def test_wide_chars_no_overshoot_straddle(self):
        # Content budget 3 cells, but chars are 2 wide -> only 1 fits (2
        # cells); must not emit a 3rd cell of a wide char.
        s = WIDE + WIDE          # 8 cells
        out = cell_trim(s, 4)    # budget 3 for content + 1 for …
        self.assertLessEqual(cell_width(out), 4)

    def test_wide_chars_no_overshoot_start(self):
        s = WIDE + WIDE
        out = cell_trim(s, 5, where='start')
        self.assertLessEqual(cell_width(out), 5)
        self.assertTrue(out.startswith(ELLIPSIS))

    def test_ellipsis_wider_than_width(self):
        # width smaller than the ellipsis itself: never overshoot width.
        out = cell_trim('abcdef', 2, ellipsis='...')
        self.assertLessEqual(cell_width(out), 2)


class TestCellFit(unittest.TestCase):
    def test_pads_when_short_left(self):
        self.assertEqual(cell_fit('ab', 5), 'ab   ')

    def test_pads_when_short_right(self):
        self.assertEqual(cell_fit('ab', 5, justify='right'), '   ab')

    def test_pads_when_short_center(self):
        self.assertEqual(cell_fit('ab', 5, justify='center'), ' ab  ')

    def test_trims_when_long(self):
        self.assertEqual(cell_fit('abcdef', 4), 'abc' + ELLIPSIS)

    def test_always_exact_width_across_justify_and_trim(self):
        cases = ['', 'a', 'abc', 'abcdefghij', WIDE, WIDE + WIDE,
                 'a你b好c']
        for s in cases:
            for w in range(0, 9):
                for justify in ('left', 'right', 'center'):
                    for trim in ('end', 'start', 'middle'):
                        out = cell_fit(s, w, justify=justify, trim=trim)
                        self.assertEqual(
                            cell_width(out), w,
                            f's={s!r} w={w} justify={justify} trim={trim} '
                            f'-> {out!r} ({cell_width(out)} cells)')

    def test_exact_when_already_fits(self):
        self.assertEqual(cell_fit('abc', 3), 'abc')

    def test_custom_fill_and_ellipsis(self):
        self.assertEqual(cell_fit('ab', 5, fill='.'), 'ab...')
        self.assertEqual(cell_fit('abcdefgh', 6, ellipsis='...'), 'abc...')


class TestStylesAPI(unittest.TestCase):
    def test_style_names_frozenset(self):
        self.assertIsInstance(STYLE_NAMES, frozenset)

    def test_style_names_cover_vocabulary(self):
        for name in ('green', 'red', 'yellow', 'gray', 'cyan',
                     'blue', 'magenta', 'dim', ''):
            self.assertIn(name, STYLE_NAMES)

    def test_style_resolves_to_raw_pair(self):
        # style(name) returns the same (fg, bold) tuple tag rendering uses.
        for name in STYLE_NAMES:
            self.assertEqual(style(name), _render._TAG_STYLE[name])

    def test_style_known_values(self):
        self.assertEqual(style('green'), (2, False))
        self.assertEqual(style('red'), (1, True))
        self.assertEqual(style('dim'), (242, False))

    def test_style_unknown_is_plain(self):
        self.assertEqual(style('not-a-style'), (None, False))
        self.assertEqual(style(''), (None, False))

    def test_style_returns_tuple_shape(self):
        fg, bold = style('cyan')
        self.assertEqual((fg, bold), (6, False))

    def test_palette_constants_match_internal(self):
        self.assertEqual(MARKER_FG, _render._MARKER_COLOR)
        self.assertEqual(ID_FG, _render._ID_COLOR)
        self.assertEqual(DIM_FG, _render._PENDING_FG)

    def test_palette_constant_values(self):
        self.assertEqual(MARKER_FG, 4)
        self.assertEqual(ID_FG, 3)
        self.assertEqual(DIM_FG, 242)

    def test_dim_fg_matches_dim_style(self):
        # DIM_FG is documented as the 'dim' named style's fg.
        self.assertEqual(DIM_FG, style('dim')[0])


if __name__ == '__main__':
    unittest.main()
