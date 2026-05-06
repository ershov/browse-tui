"""Tests for Browser-level state on the state module (040-state.py).

Currently focused on the ``split`` attribute introduced in ticket #146:
clamp helper, constructor param, and ``set_split`` redraw side effect.
Other Browser-level state (list_ratio, expanded, …) is exercised
indirectly by the renderer / actions tests; this module is the natural
home for narrow state-bit tests that would otherwise need a full UI
fixture.
"""

import unittest

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Inject Item so any state helper that needs it (e.g. the pending
# placeholder) keeps working when tests use the loader.
_state.Item = _data.Item

Browser = _state.Browser
_clamp_split = _state._clamp_split
_VALID_SPLITS = _state._VALID_SPLITS


class TestClampSplit(unittest.TestCase):
    """``_clamp_split`` is the input gate for both __init__ and set_split."""

    def test_valid_codes_pass_through(self):
        for code in _VALID_SPLITS:
            self.assertEqual(_clamp_split(code), code)

    def test_unknown_code_defaults_to_h(self):
        self.assertEqual(_clamp_split('zz'), 'h')
        self.assertEqual(_clamp_split(''), 'h')

    def test_auto_shorthand_resolves_via_term_size(self):
        # 'auto'/'a' resolve against term_size: <230 cols → 'h', else 'v'.
        # In headless tests term_size falls back to 80, so 'a'/'auto' → 'h'.
        self.assertEqual(_clamp_split('a'), 'h')
        self.assertEqual(_clamp_split('auto'), 'h')
        self.assertEqual(_clamp_split('Auto'), 'h')

    def test_none_defaults_to_h(self):
        self.assertEqual(_clamp_split(None), 'h')

    def test_non_string_defaults_to_h(self):
        self.assertEqual(_clamp_split(42), 'h')
        self.assertEqual(_clamp_split(['v']), 'h')
        self.assertEqual(_clamp_split(object()), 'h')


class TestBrowserSplitConstructor(unittest.TestCase):
    """Browser.__init__ accepts ``split=`` and stores it (clamped)."""

    def test_default_split_resolves_auto(self):
        # Default is 'auto' which resolves via term_size; in headless
        # 80-col tests that yields 'h'.
        b = Browser(_headless=True)
        self.assertEqual(b.split, 'h')

    def test_all_valid_splits_stick(self):
        for code in _VALID_SPLITS:
            b = Browser(split=code, _headless=True)
            self.assertEqual(b.split, code)

    def test_invalid_split_defaults_to_h(self):
        for bad in ('zz', '', 'horizontal'):
            b = Browser(split=bad, _headless=True)
            self.assertEqual(b.split, 'h')

    def test_none_split_defaults_to_h(self):
        b = Browser(split=None, _headless=True)
        self.assertEqual(b.split, 'h')

    def test_non_string_split_defaults_to_h(self):
        b = Browser(split=42, _headless=True)
        self.assertEqual(b.split, 'h')


class TestBrowserSetSplit(unittest.TestCase):
    """``set_split`` clamps + flags the screen for redraw."""

    def test_set_split_stores_valid_value(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_split('v')
        self.assertEqual(b.split, 'v')

    def test_set_split_marks_full_redraw(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_split('m')
        self.assertIn('all', b._needs_redraw)

    def test_set_split_clamps_invalid(self):
        b = Browser(split='v', _headless=True)
        b.set_split('garbage')
        self.assertEqual(b.split, 'h')

    def test_set_split_clamps_none(self):
        b = Browser(split='v', _headless=True)
        b.set_split(None)
        self.assertEqual(b.split, 'h')

    def test_set_split_each_valid_round_trip(self):
        b = Browser(_headless=True)
        for code in _VALID_SPLITS:
            b.set_split(code)
            self.assertEqual(b.split, code)


# --- PaneCache (#186) ------------------------------------------------------


PaneCache = _state.PaneCache


class _FakeRect:
    """Minimal Rect stand-in for the cache tests.

    PaneCache uses duck-typing for ``rect`` / ``prev_rect``: it only
    reads ``.height`` to size the line buffer and uses ``__eq__`` to
    detect geometry changes. A NamedTuple-style helper is enough.
    """

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def height(self):
        return self.bottom - self.top

    def __eq__(self, other):
        if not isinstance(other, _FakeRect):
            return NotImplemented
        return (self.left, self.top, self.right, self.bottom) == \
               (other.left, other.top, other.right, other.bottom)

    def __hash__(self):
        return hash((self.left, self.top, self.right, self.bottom))


class TestPaneCache(unittest.TestCase):
    """``PaneCache`` is the per-pane row buffer for the differential
    renderer (#185–#188). #186 introduces it; #187/#188 wire it into
    each pane renderer."""

    def test_default_state(self):
        c = PaneCache()
        self.assertIsNone(c.rect)
        self.assertIsNone(c.prev_rect)
        self.assertEqual(c.lines, [])

    def test_invalidate_rotates_rect_and_sizes_lines(self):
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)  # height=24
        c.invalidate(r1)
        self.assertEqual(c.rect, r1)
        self.assertIsNone(c.prev_rect)
        self.assertEqual(len(c.lines), 24)
        self.assertTrue(all(line is None for line in c.lines))

        r2 = _FakeRect(1, 1, 81, 30)  # height=29
        c.invalidate(r2)
        self.assertEqual(c.rect, r2)
        self.assertEqual(c.prev_rect, r1)
        self.assertEqual(len(c.lines), 29)

    def test_ensure_is_idempotent_when_geometry_unchanged(self):
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        c.ensure(r1)
        # Mark a slot as painted; ensure() with the same rect must
        # not clobber the buffer.
        c.lines[0] = (10, 'hello world')
        c.ensure(_FakeRect(1, 1, 81, 25))
        self.assertEqual(c.lines[0], (10, 'hello world'))

    def test_ensure_invalidates_on_geometry_change(self):
        c = PaneCache()
        c.ensure(_FakeRect(1, 1, 81, 25))
        c.lines[0] = (10, 'hello world')
        new_rect = _FakeRect(1, 1, 81, 30)
        c.ensure(new_rect)
        # New geometry resets the buffer.
        self.assertEqual(c.rect, new_rect)
        self.assertEqual(len(c.lines), 29)
        self.assertTrue(all(line is None for line in c.lines))

    def test_second_ensure_with_same_rect_rolls_prev_rect_forward(self):
        """The second ensure() with the same rect should roll prev_rect
        forward so subsequent paints take ``end_row``'s steady-state
        branch instead of the "first paint after rect change" branch.

        The renderers call ensure() once per paint. After invalidate(),
        prev_rect lags behind rect by one paint (None on first ever
        paint, or the prior rect after a resize). end_row consumes that
        lag once; ensure must then advance prev_rect on the next paint
        so stale-cell padding kicks in for steady-state writes.
        """
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        # First ensure — invalidate path: rect=r1, prev_rect=None.
        c.ensure(r1)
        self.assertEqual(c.rect, r1)
        self.assertIsNone(c.prev_rect)
        # Second ensure with the same rect — same-rect branch should roll
        # prev_rect forward to match rect (steady-state regime).
        c.ensure(_FakeRect(1, 1, 81, 25))
        self.assertEqual(c.rect, r1)
        self.assertEqual(c.prev_rect, r1)
        # Third ensure — already in steady state; nothing changes.
        c.ensure(_FakeRect(1, 1, 81, 25))
        self.assertEqual(c.rect, r1)
        self.assertEqual(c.prev_rect, r1)

    def test_ensure_after_resize_rolls_prev_rect_on_next_paint(self):
        """After a resize, the first paint with the new rect runs in
        "rect changed" regime (prev_rect = old rect != rect). The
        second paint with the same new rect must roll prev_rect to
        new rect so steady-state padding applies thereafter.
        """
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        r2 = _FakeRect(1, 1, 81, 30)
        # Establish steady state at r1.
        c.ensure(r1)
        c.ensure(r1)
        self.assertEqual(c.prev_rect, r1)
        # Resize: prev_rect rotates to old r1, rect becomes r2.
        c.ensure(r2)
        self.assertEqual(c.rect, r2)
        self.assertEqual(c.prev_rect, r1)
        # Second paint at r2: prev_rect rolls forward to r2.
        c.ensure(_FakeRect(1, 1, 81, 30))
        self.assertEqual(c.rect, r2)
        self.assertEqual(c.prev_rect, r2)


class TestBrowserPaneCacheInit(unittest.TestCase):
    """Browser.__init__ initialises ``_pane_cache`` to an empty dict."""

    def test_pane_cache_starts_empty(self):
        b = Browser(_headless=True)
        self.assertEqual(b._pane_cache, {})
        self.assertIsInstance(b._pane_cache, dict)


if __name__ == '__main__':
    unittest.main()
