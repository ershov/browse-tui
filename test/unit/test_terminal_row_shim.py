"""Tests for the row-buffer shim primitives in 020-terminal.py.

The shim lets renderers stay nearly unchanged while their per-row writes
get diffed against a per-pane line cache. ``begin_row`` redirects
``write()`` (and indirect callers ``set_style`` / ``move`` / etc.) into
``_row_buf``; ``end_row`` compares the captured bytes against
``pane_cache.lines[rel_row]`` and emits only on a cache miss with the
appropriate padding (or ``\\e[K``) when content is shorter than what's
already on screen.

These tests stub stdout to capture emitted bytes and use a minimal
duck-typed ``PaneCache`` stand-in (the real type lives in 040-state.py
and is added by ticket #186).
"""

import io
import sys
import types
import unittest

from test.unit._loader import load


_terminal = load('_browse_tui_terminal', '020-terminal.py')


def _rect(left, top, right, bottom):
    """Minimal Rect stand-in matching 050-render.Rect's attrs.

    The shim only ever does ``==`` / ``!=`` on rects, plus reads
    ``right - left`` for pane width via the caller (we pass left/right
    explicitly to begin_row, so the shim doesn't need width itself).
    Using SimpleNamespace keeps the test independent of 050-render.
    """
    return types.SimpleNamespace(
        left=left, top=top, right=right, bottom=bottom,
        width=right - left, height=bottom - top,
    )


def _make_cache(rect, height, prev_rect=None):
    """Build a duck-typed PaneCache: ``rect``, ``prev_rect``, ``lines``."""
    return types.SimpleNamespace(
        rect=rect,
        prev_rect=prev_rect,
        lines=[None] * height,
    )


class _StdoutCapture:
    """Replace ``sys.stdout`` with a StringIO during a test.

    The shim writes to stdout via ``sys.stdout.write`` for direct emits
    (when capture is inactive) and via the same path on cache miss after
    ``end_row`` resets the flag. Capturing stdout lets tests assert on
    the exact byte stream emitted.
    """

    def __enter__(self):
        self._orig = sys.stdout
        self.buf = io.StringIO()
        sys.stdout = self.buf
        return self

    def __exit__(self, *args):
        sys.stdout = self._orig

    @property
    def text(self):
        return self.buf.getvalue()


class VisibleLenTests(unittest.TestCase):
    """``_visible_len`` ignores SGR sequences."""

    def test_plain_text(self):
        self.assertEqual(_terminal._visible_len('hello'), 5)

    def test_empty(self):
        self.assertEqual(_terminal._visible_len(''), 0)

    def test_skips_sgr(self):
        # \033[1mhello\033[0m → visible='hello' → 5
        self.assertEqual(
            _terminal._visible_len('\033[1mhello\033[0m'), 5)

    def test_multiple_sgr_runs(self):
        s = '\033[31ma\033[0m\033[1mbc\033[0md'
        self.assertEqual(_terminal._visible_len(s), 4)

    def test_cjk_wide_chars_count_as_two_cells(self):
        # Two CJK ideographs render as 2 cells each → 4 columns.
        self.assertEqual(_terminal._visible_len('東京'), 4)

    def test_mixed_ascii_and_wide(self):
        # 'a' (1) + '東' (2) + 'b' (1) → 4 columns.
        self.assertEqual(_terminal._visible_len('a東b'), 4)

    def test_ascii_no_regression(self):
        self.assertEqual(_terminal._visible_len('ascii'), 5)

    def test_sgr_stripped_around_wide_char(self):
        # SGR stripped, single wide char counts as 2 cells.
        self.assertEqual(_terminal._visible_len('\033[31m東\033[m'), 2)

    def test_steady_state_shrink_with_wide_chars_pads_columns(self):
        """End-to-end: shrinking past a wide-char row pads display columns,
        not code points. Without the fix, '東京' would count as 2 instead
        of 4 and trailing ghost cells would remain on a non-rightmost pane.
        """
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        # Paint 1: '東京' = 4 columns (2 code points), first paint, no pad.
        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('東京')
            _terminal.end_row()
        # Cache stores column count, not code-point count.
        self.assertEqual(cache.lines[0], (4, '東京'))
        cache.prev_rect = cache.rect

        # Paint 2: 'hi' = 2 columns; pad must be 4 - 2 = 2.
        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hi')
            _terminal.end_row()

        expected = '\033[1;1Hhi\033[m' + ' ' * 2
        self.assertEqual(cap.text, expected)
        self.assertEqual(cache.lines[0], (4, 'hi'))


class RowShimTests(unittest.TestCase):
    """begin_row / end_row capture, diff, and emit semantics."""

    def setUp(self):
        # Defensive: if a previous test left capture state dirty, reset.
        _terminal._row_capture_active = False
        _terminal._row_buf = []
        _terminal._row_meta = None

    def test_first_paint_emits_move_content_reset_no_pad(self):
        """prev_rect=None → emit move + content + \\e[m, NO padding."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, rel_row=0, abs_row=1, left=1, right=41,
                                rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()

        self.assertEqual(cap.text, '\033[1;1Hhello\033[m')
        # Cache stores (visible_len, bytes).
        self.assertEqual(cache.lines[0], (5, 'hello'))
        # Capture state reset.
        self.assertFalse(_terminal._row_capture_active)
        self.assertEqual(_terminal._row_buf, [])
        self.assertIsNone(_terminal._row_meta)

    def test_same_content_second_call_is_cache_hit(self):
        """Same rect, same content, prev_rect != None → emit nothing."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        # First paint.
        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()

        # Steady state: a real renderer's commit step would set
        # prev_rect = rect after a full paint. Simulate that here.
        cache.prev_rect = cache.rect

        # Second paint, same content.
        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()

        self.assertEqual(cap.text, '')
        # Cache unchanged.
        self.assertEqual(cache.lines[0], (5, 'hello'))

    def test_different_content_second_call_emits(self):
        """Same rect, different content → emit move + new content + reset."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()
        cache.prev_rect = cache.rect

        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('world')
            _terminal.end_row()

        # Same length, no padding, just move + content + reset.
        self.assertEqual(cap.text, '\033[1;1Hworld\033[m')
        self.assertEqual(cache.lines[0], (5, 'world'))

    def test_rect_changed_pads_to_pane_width_non_rightmost(self):
        """prev_rect != rect, content shorter than pane → pad with spaces."""
        old_rect = _rect(1, 1, 21, 25)
        new_rect = _rect(1, 1, 41, 25)   # wider pane than before
        cache = _make_cache(new_rect, height=10, prev_rect=old_rect)
        # Pre-existing line entry from a previous rect (cache contents
        # are typically discarded on rect change by the higher layer,
        # but the shim should pad based on new pane width regardless).
        cache.lines[0] = (5, 'hello')

        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hi')
            _terminal.end_row()

        # pane_width = 41 - 1 = 40; visible('hi') = 2; pad = 38.
        expected = '\033[1;1Hhi\033[m' + ' ' * 38
        self.assertEqual(cap.text, expected)
        self.assertEqual(cache.lines[0], (40, 'hi'))

    def test_rect_changed_uses_clear_line_when_rightmost(self):
        """prev_rect != rect, rightmost=True → use \\e[K instead of pad."""
        old_rect = _rect(1, 1, 21, 25)
        new_rect = _rect(1, 1, 41, 25)
        cache = _make_cache(new_rect, height=10, prev_rect=old_rect)

        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=True)
            _terminal.write('hi')
            _terminal.end_row()

        self.assertEqual(cap.text, '\033[1;1Hhi\033[m\033[K')
        # pane width = 40
        self.assertEqual(cache.lines[0], (40, 'hi'))

    def test_steady_state_shorter_content_pads_to_cached_len(self):
        """Same rect, new visible_len < cached → pad to cached length."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        # Paint 1: long content, prev_rect=None (first paint, no pad).
        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello world')
            _terminal.end_row()
        cache.prev_rect = cache.rect

        # Paint 2: shorter content.
        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hi')
            _terminal.end_row()

        # Cached visible = 11, new visible = 2, pad = 9 spaces.
        expected = '\033[1;1Hhi\033[m' + ' ' * 9
        self.assertEqual(cap.text, expected)
        # Cache stores cached_visible (length displayed on screen).
        self.assertEqual(cache.lines[0], (11, 'hi'))

    def test_steady_state_shorter_content_rightmost_uses_clear_line(self):
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=True)
            _terminal.write('hello world')
            _terminal.end_row()
        cache.prev_rect = cache.rect

        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=True)
            _terminal.write('hi')
            _terminal.end_row()

        self.assertEqual(cap.text, '\033[1;1Hhi\033[m\033[K')
        # Stored visible_len reflects new content (rest is blank via \e[K).
        self.assertEqual(cache.lines[0], (2, 'hi'))

    def test_visible_len_skips_sgr_in_cache(self):
        """SGR sequences don't count toward visible length in the cache."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('\033[1mhello\033[0m')
            _terminal.end_row()

        # visible_len is 5, NOT len('\033[1mhello\033[0m') == 13.
        stored_visible, stored_bytes = cache.lines[0]
        self.assertEqual(stored_visible, 5)
        self.assertEqual(stored_bytes, '\033[1mhello\033[0m')

    def test_capture_state_resets_on_cache_hit(self):
        """After a cache hit (no emit), capture flag must be reset."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()
        cache.prev_rect = cache.rect

        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()  # cache hit, emits nothing

        self.assertFalse(_terminal._row_capture_active)
        self.assertEqual(_terminal._row_buf, [])
        self.assertIsNone(_terminal._row_meta)

        # And we can begin again right after.
        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('again')
            _terminal.end_row()
        # 'again' is 5 visible cells, same as cached 'hello' (5), no pad.
        self.assertEqual(cap.text, '\033[1;1Hagain\033[m')

    def test_capture_state_resets_on_cache_miss(self):
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture():
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.write('hello')
            _terminal.end_row()

        self.assertFalse(_terminal._row_capture_active)
        self.assertEqual(_terminal._row_buf, [])
        self.assertIsNone(_terminal._row_meta)

    def test_nesting_raises(self):
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        try:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            with self.assertRaises(RuntimeError):
                _terminal.begin_row(cache, 1, 2, 1, 41, rightmost=False)
        finally:
            # Clean up so other tests aren't poisoned.
            _terminal._row_capture_active = False
            _terminal._row_buf = []
            _terminal._row_meta = None

    def test_end_row_without_begin_raises(self):
        with self.assertRaises(RuntimeError):
            _terminal.end_row()

    def test_indirect_writes_via_helpers_are_captured(self):
        """``set_style`` / ``move`` / ``clear_line`` go through ``write``."""
        rect = _rect(1, 1, 41, 25)
        cache = _make_cache(rect, height=10, prev_rect=None)

        with _StdoutCapture() as cap:
            _terminal.begin_row(cache, 0, 1, 1, 41, rightmost=False)
            _terminal.set_style(fg=2)
            _terminal.write('ok')
            _terminal.reset_style()
            _terminal.end_row()

        # Captured bytes should include set_style + content + reset_style,
        # surrounded by the move + final reset emitted by end_row.
        # set_style(fg=2) → '\033[0;38;5;2m'; reset_style → '\033[0m'.
        self.assertIn('\033[1;1H', cap.text)
        self.assertIn('\033[0;38;5;2m', cap.text)
        self.assertIn('ok', cap.text)
        self.assertIn('\033[0m', cap.text)
        self.assertTrue(cap.text.endswith('\033[m'))
        # Visible length of 'ok' is 2 — SGR shouldn't count.
        stored_visible, _ = cache.lines[0]
        self.assertEqual(stored_visible, 2)


class SyncTests(unittest.TestCase):
    """begin_sync / end_sync emit DEC mode 2026 enable/disable."""

    def setUp(self):
        _terminal._row_capture_active = False
        _terminal._row_buf = []
        _terminal._row_meta = None

    def test_begin_sync_emits_2026h(self):
        with _StdoutCapture() as cap:
            _terminal.begin_sync()
        self.assertEqual(cap.text, '\033[?2026h')

    def test_end_sync_emits_2026l(self):
        with _StdoutCapture() as cap:
            _terminal.end_sync()
        self.assertEqual(cap.text, '\033[?2026l')


# Make _visible_len accessible at module top-level (tests reference it).
_visible_len = _terminal._visible_len


if __name__ == '__main__':
    unittest.main()
