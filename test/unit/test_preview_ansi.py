"""Unit tests for ``_wrap_preview_line`` / ``_tokenise_line`` (ticket #242).

Wrap-aware SGR re-emit walker for the preview pane. The walker:

  * Tokenises one logical preview line via a single ``_ANSI_CSI_RE``
    pass into alternating ``('text', s)`` / ``('csi', s)`` tokens.
  * Emits self-contained visual rows: any active SGR is re-emitted at
    the start of each wrapped row, and a trailing ``\\e[m`` closes the
    row iff it contains any SGR.
  * Plain rows (no SGR active, no SGR in the cut) emit identical bytes
    whether ``ansi_on`` is True or False — preserves the per-pane row
    cache hit invariants.
  * Three-tier ASCII fast path: whole-token ASCII, per-cut ASCII,
    char-by-char column fit only when wide chars are in the cut.
  * ``drop_sgr=True`` strips SGR even in ANSI mode (search-highlight
    path: highlight wins).
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term_pa', '020-terminal.py')
_render = load('_browse_tui_render_pa', '050-render.py')

# Cross-wiring: 050-render.py references _ANSI_CSI_RE / SgrState /
# _char_width from 020-terminal at module scope (the concatenated build
# has them in one namespace). Inject them like the other test files do.
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render.SgrState = _term.SgrState
_render._char_width = _term._char_width


_tokenise_line = _render._tokenise_line
_wrap_preview_line = _render._wrap_preview_line


class TestTokeniseLine(unittest.TestCase):

    def test_plain_text_one_token(self):
        self.assertEqual(
            list(_tokenise_line('hello world')),
            [('text', 'hello world')],
        )

    def test_empty_line_no_tokens(self):
        self.assertEqual(list(_tokenise_line('')), [])

    def test_single_sgr(self):
        self.assertEqual(
            list(_tokenise_line('\033[31mred\033[m')),
            [('csi', '\033[31m'), ('text', 'red'), ('csi', '\033[m')],
        )

    def test_leading_text_then_csi(self):
        self.assertEqual(
            list(_tokenise_line('abc\033[1mX')),
            [('text', 'abc'), ('csi', '\033[1m'), ('text', 'X')],
        )

    def test_non_sgr_csi_tokenised(self):
        # \e[2J is erase-screen — still a CSI, walker decides what to do.
        self.assertEqual(
            list(_tokenise_line('a\033[2Jb')),
            [('text', 'a'), ('csi', '\033[2J'), ('text', 'b')],
        )


class TestWrapPlainAscii(unittest.TestCase):
    """Plain ASCII path — must match the pre-existing slice-based wrap."""

    def _slice_wrap(self, line, width):
        out = []
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
        return out

    def test_no_wrap_needed(self):
        self.assertEqual(
            _wrap_preview_line('hello', 10, ansi_on=False),
            ['hello'],
        )

    def test_exact_width_no_wrap(self):
        # Width-12 line in a 12-col rect → exactly one row.
        self.assertEqual(
            _wrap_preview_line('abcdefghijkl', 12, ansi_on=False),
            ['abcdefghijkl'],
        )

    def test_wrap_two_rows(self):
        self.assertEqual(
            _wrap_preview_line('hello world', 5, ansi_on=False),
            ['hello', ' worl', 'd'],
        )

    def test_matches_slice_path_regression(self):
        # Random-ish content; equivalent to the old naive slice wrap.
        line = 'the quick brown fox jumps over the lazy dog'
        for width in (3, 5, 7, 10, 17, 80):
            self.assertEqual(
                _wrap_preview_line(line, width, ansi_on=False),
                self._slice_wrap(line, width),
            )

    def test_empty_line_yields_one_empty_row(self):
        self.assertEqual(_wrap_preview_line('', 10, ansi_on=False), [''])

    def test_plain_rows_identical_with_or_without_ansi(self):
        # A line with NO escapes must emit byte-identical rows whether
        # ansi_on is True or False — preserves cache-hit invariants.
        line = 'the quick brown fox'
        for w in (3, 5, 7, 80):
            self.assertEqual(
                _wrap_preview_line(line, w, ansi_on=True),
                _wrap_preview_line(line, w, ansi_on=False),
            )


class TestWrapColoured(unittest.TestCase):

    def test_coloured_line_carries_sgr_across_wraps(self):
        # "RRRRRRRR" in red at width 3 → three rows; each carries
        # \e[31m at the start and \e[m at the end.
        rows = _wrap_preview_line('\033[31mRRRRRRRR', 3, ansi_on=True)
        self.assertEqual(rows, [
            '\033[31mRRR\033[m',
            '\033[31mRRR\033[m',
            '\033[31mRR\033[m',
        ])

    def test_reset_midline_drops_colour_in_subsequent_wraps(self):
        # Red for 3 cols, reset, then 5 plain cols. With width=4 we wrap
        # mid-uncoloured run; the second row must NOT carry red.
        rows = _wrap_preview_line(
            '\033[31mRED\033[mPLAINX', 4, ansi_on=True)
        # Row 1: \e[31mRED + (text 'P' to fill width=4). The reset is
        # within row 1, so row 1 contains both \e[31m and \e[m — we
        # only emit ONE trailing \e[m (the row's terminator).
        self.assertEqual(rows[0], '\033[31mRED\033[mP')
        # Row 2: plain 'LAIN' (4 cols, no SGR active) → no SGR markers.
        self.assertEqual(rows[1], 'LAIN')
        # Row 3: plain 'X' (no SGR) → no SGR markers.
        self.assertEqual(rows[2], 'X')

    def test_active_sgr_reemitted_after_wrap(self):
        # SGR opens before any text; wraps must re-open the same SGR.
        rows = _wrap_preview_line(
            '\033[1;33mABCDEF', 3, ansi_on=True)
        self.assertEqual(rows, [
            '\033[1;33mABC\033[m',
            '\033[1;33mDEF\033[m',
        ])


class TestWrapCsiHandling(unittest.TestCase):

    def test_non_sgr_csi_stripped(self):
        # \e[2J (erase screen) is a CSI but not SGR. It must be
        # dropped, and the surrounding 'a' / 'b' wrap normally.
        self.assertEqual(
            _wrap_preview_line('a\033[2Jb', 10, ansi_on=True),
            ['ab'],
        )

    def test_non_sgr_csi_does_not_count_toward_width(self):
        # 8 visible chars + \e[2J in the middle, width 5 → wraps at 5.
        rows = _wrap_preview_line('abcd\033[2Jefgh', 5, ansi_on=True)
        self.assertEqual(rows, ['abcde', 'fgh'])

    def test_ansi_off_strips_all_csi(self):
        # ansi_on=False → all CSI gone, output indistinguishable
        # from feeding a pre-stripped string through.
        self.assertEqual(
            _wrap_preview_line('\033[31mRED\033[mTAIL', 4, ansi_on=False),
            ['REDT', 'AIL'],
        )

    def test_drop_sgr_strips_sgr_in_ansi_mode(self):
        # drop_sgr=True overrides ansi_on for SGR specifically (search-
        # highlight path: highlight wins). Non-SGR CSI is also dropped
        # (always is). Output equivalent to ansi_on=False here.
        self.assertEqual(
            _wrap_preview_line(
                '\033[31mRED\033[mTAIL', 4, ansi_on=True, drop_sgr=True),
            ['REDT', 'AIL'],
        )

    def test_drop_sgr_plain_rows_byte_identical(self):
        # With drop_sgr=True, rows must be byte-identical to ansi_on=False.
        line = '\033[1;31mhello \033[mworld'
        self.assertEqual(
            _wrap_preview_line(line, 4, ansi_on=True, drop_sgr=True),
            _wrap_preview_line(line, 4, ansi_on=False),
        )


class TestWrapFastPaths(unittest.TestCase):
    """Coverage for the three-tier ASCII fast path."""

    def test_whole_token_ascii_fast_path(self):
        # Pure ASCII token — all cuts use fast path 1.
        self.assertEqual(
            _wrap_preview_line('abcdefghij', 4, ansi_on=False),
            ['abcd', 'efgh', 'ij'],
        )

    def test_per_cut_ascii_fast_path(self):
        # Mixed token with wide chars at the END. Early cuts are
        # ASCII-only — fast path 2. The cut that lands on the wide
        # chars falls to the slow path.
        # 東京 is 2 wide chars (each 2 cols → 4 cols total). Width 5,
        # so first cut is "hello" (ASCII fast), then " 東" lands
        # 1 ASCII space + 1 wide char = 3 cols (slow path under-fill
        # by 2 because the next char 京 would push to 5).
        line = 'hello 東京tail'
        rows = _wrap_preview_line(line, 5, ansi_on=False)
        # First row: 'hello' (5 cols, fast path).
        self.assertEqual(rows[0], 'hello')
        # The remaining text is ' 東京tail' = 1+2+2+4 = 9 cols.
        # Second row's avail=5, ' 東' = 3 cols, '京' = +2 = 5 cols → fits.
        # So row 2 = ' 東京', row 3 = 'tail'.
        self.assertEqual(rows, ['hello', ' 東京', 'tail'])

    def test_slow_path_under_fill(self):
        # avail=5, "a東b" cut → 'a' (1) + '東' (2) = 3, 'b' would be 4,
        # but the spec example says "emits 4 cols, leaves 1 col on
        # the floor" when forced to slow-path with avail=5.
        # With "a東b" alone at width 5: row should be 'a東b' (4 cols)
        # and end with one row only, not wrapped.
        rows = _wrap_preview_line('a東b', 5, ansi_on=False)
        self.assertEqual(rows, ['a東b'])

    def test_slow_path_wraps_when_wide_char_doesnt_fit(self):
        # avail=4, "a東b東c": 'a'(1)+'東'(3) → '東' fits (taken=3),
        # 'b' → taken=4 fits, '東' → taken=6 > 4 → stop. Row=a東b.
        # Next row: avail=4, '東c': 東(2)+c(3) → fits. Row=東c.
        rows = _wrap_preview_line('a東b東c', 4, ansi_on=False)
        self.assertEqual(rows, ['a東b', '東c'])


class TestWrapWideChars(unittest.TestCase):

    def test_single_cjk_no_room_in_avail(self):
        # avail=1, single wide char wouldn't fit (needs 2 cols).
        # First row should pack what fits, wide char lands on next row.
        # 'aa東' at width 2: 'aa' (2 cols) → row done. Then '東' (2 cols).
        rows = _wrap_preview_line('aa東', 2, ansi_on=False)
        self.assertEqual(rows, ['aa', '東'])

    def test_cjk_under_fill_floor(self):
        # avail=3, 'a東b': 'a'(1)+'東'(3) fits, 'b'(4) doesn't → row 'a東'.
        # Row 2: 'b' (1 col).
        rows = _wrap_preview_line('a東b', 3, ansi_on=False)
        self.assertEqual(rows, ['a東', 'b'])

    def test_cjk_run_wraps_two_per_row_at_width_4(self):
        # 4 CJK chars at width 4 → two per row.
        rows = _wrap_preview_line('東京大阪', 4, ansi_on=False)
        self.assertEqual(rows, ['東京', '大阪'])

    def test_single_wide_char_into_width_1_takes_anyway(self):
        # Pathological: width=1 can't fit any wide char. The walker
        # emits one wide char per row anyway (slow path j==i guard).
        rows = _wrap_preview_line('東京', 1, ansi_on=False)
        self.assertEqual(rows, ['東', '京'])


if __name__ == '__main__':
    unittest.main()
