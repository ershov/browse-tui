"""Tests for the modal dialog placement & sizing helpers (ticket #969).

These are the pure geometry helpers in ``055-modal.py`` — caps, frame
size, the tiny-terminal predicate, and the placement math (centering +
anchored). No terminal in the loop, so they're fully unit-testable.

``Rect`` lives in ``050-render.py`` (1-based, exclusive right/bottom);
the modal module references it by bare name, so the isolated test load
wires it in the same way the other render tests wire their cross-module
references.
"""

import unittest

from test.unit._loader import load


_render = load('_browse_tui_render_modal', '050-render.py')
_modal = load('_browse_tui_modal', '055-modal.py')

# The modal module uses ``Rect`` (defined in 050-render) by bare name.
# Production builds get it via the concatenated namespace; the per-file
# test loader has to wire it in.
_modal.Rect = _render.Rect

Rect = _render.Rect
_modal_caps = _modal._modal_caps
_frame_size = _modal._frame_size
_modal_is_tiny = _modal._modal_is_tiny
_modal_place = _modal._modal_place


# --- Caps -----------------------------------------------------------------


class TestModalCaps(unittest.TestCase):
    """``_modal_caps`` — 80% width, rows-4 height."""

    def test_basic_80x24(self):
        self.assertEqual(_modal_caps(80, 24), (64, 20))

    def test_width_floors_fractional(self):
        # 0.8 * 100 = 80.0; 0.8 * 99 = 79.2 -> floored to 79.
        self.assertEqual(_modal_caps(100, 30), (80, 26))
        self.assertEqual(_modal_caps(99, 30), (79, 26))

    def test_height_is_rows_minus_four(self):
        self.assertEqual(_modal_caps(80, 50)[1], 46)


# --- Frame size -----------------------------------------------------------


class TestFrameSize(unittest.TestCase):
    """``_frame_size`` adds border + inner pad: +4 wide, +2 tall."""

    def test_adds_border_and_pad(self):
        self.assertEqual(_frame_size(10, 5), (14, 7))

    def test_zero_content(self):
        self.assertEqual(_frame_size(0, 0), (4, 2))

    def test_one_by_one(self):
        self.assertEqual(_frame_size(1, 1), (5, 3))


# --- Tiny-terminal predicate ---------------------------------------------


class TestModalIsTiny(unittest.TestCase):
    """``_modal_is_tiny`` — cols < 20 or rows < 8."""

    def test_normal_is_not_tiny(self):
        self.assertFalse(_modal_is_tiny(80, 24))

    def test_just_at_threshold_is_not_tiny(self):
        # Exactly the minimums are NOT tiny (strict <).
        self.assertFalse(_modal_is_tiny(20, 8))

    def test_too_few_cols_is_tiny(self):
        self.assertTrue(_modal_is_tiny(19, 24))

    def test_too_few_rows_is_tiny(self):
        self.assertTrue(_modal_is_tiny(80, 7))

    def test_constants(self):
        self.assertEqual(_modal._MODAL_MIN_COLS, 20)
        self.assertEqual(_modal._MODAL_MIN_ROWS, 8)


# --- Centered placement ---------------------------------------------------


class TestPlaceCenter(unittest.TestCase):
    """``placement='center'`` centers the frame on the screen."""

    def test_even_leftover(self):
        # 80x24 screen, 40x10 frame -> 40 cols / 14 rows leftover, both
        # even. left = 1 + 40//2 = 21; top = 1 + 14//2 = 8.
        r = _modal_place(80, 24, 40, 10, placement='center', anchor=None)
        self.assertEqual(r, Rect(21, 8, 61, 18))

    def test_odd_leftover(self):
        # 81x25 screen, 40x10 frame -> 41 cols / 15 rows leftover, both
        # odd. Floor-division biases the extra cell to the right/bottom.
        # left = 1 + 41//2 = 21; top = 1 + 15//2 = 8.
        r = _modal_place(81, 25, 40, 10, placement='center', anchor=None)
        self.assertEqual(r, Rect(21, 8, 61, 18))
        self.assertEqual(r.width, 40)
        self.assertEqual(r.height, 10)

    def test_exact_fit(self):
        # Frame exactly fills the screen.
        r = _modal_place(40, 10, 40, 10, placement='center', anchor=None)
        self.assertEqual(r, Rect(1, 1, 41, 11))

    def test_frame_wider_than_screen_clamps_to_left(self):
        # A frame wider than the screen can't be centered with left>=1;
        # clamp the left edge to 1.
        r = _modal_place(40, 24, 100, 10, placement='center', anchor=None)
        self.assertEqual(r.left, 1)
        self.assertEqual(r.top, 8)


# --- Anchored placement ---------------------------------------------------


class TestPlaceAnchor(unittest.TestCase):
    """``placement='anchor'`` — top-left just below the anchor cell,
    flipping above / shifting left / clamping as needed."""

    def test_below_anchor(self):
        # Anchor at row 5, col 10 on a roomy screen: top-left at
        # (6, 10), no overflow.
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(5, 10))
        self.assertEqual(r, Rect(10, 6, 30, 12))

    def test_flip_above_when_bottom_overflows(self):
        # Anchor near the bottom (row 22) with a 6-row frame: below would
        # start at row 23 and occupy 23..28 > 24. Flip above: bottom just
        # above the anchor -> top = 22 - 6 = 16, occupies 16..21.
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(22, 10))
        self.assertEqual(r.top, 16)
        self.assertEqual(r.bottom, 22)

    def test_shift_left_when_right_overflows(self):
        # Anchor at col 70 with a 20-wide frame: left=70 occupies 70..89 >
        # 80. Shift left so the right edge fits: left = 80 - 20 + 1 = 61.
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(5, 70))
        self.assertEqual(r.left, 61)
        self.assertEqual(r.right, 81)

    def test_clamp_left_to_one(self):
        # Anchor at col 1 is fine (left=1); a frame wider than the screen
        # still clamps to left=1 rather than going negative.
        r = _modal_place(40, 24, 100, 6, placement='anchor', anchor=(5, 1))
        self.assertEqual(r.left, 1)

    def test_clamp_top_to_one(self):
        # 80x10 screen, 8-row frame, anchor at row 7: below (top=8) would
        # overflow (8..15 > 10), so it flips above to top = 7 - 8 = -1 —
        # off the top of the screen. Clamp to top=1.
        r = _modal_place(80, 10, 20, 8, placement='anchor', anchor=(7, 10))
        self.assertEqual(r.top, 1)

    def test_anchor_top_left_corner(self):
        # Anchor at (1, 1): below the anchor row -> top-left (2, 1).
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(1, 1))
        self.assertEqual(r, Rect(1, 2, 21, 8))

    def test_flip_and_shift_together(self):
        # Bottom-right corner anchor forces BOTH a flip-above and a
        # shift-left.
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(22, 70))
        self.assertEqual(r.left, 61)   # shifted left
        self.assertEqual(r.top, 16)    # flipped above


# --- Tiny terminal -> full screen ----------------------------------------


class TestPlaceTiny(unittest.TestCase):
    """On a tiny terminal the frame becomes the whole screen, regardless
    of placement / anchor."""

    def test_tiny_center_is_full_screen(self):
        r = _modal_place(15, 24, 10, 5, placement='center', anchor=None)
        self.assertEqual(r, Rect(1, 1, 16, 25))

    def test_tiny_anchor_is_full_screen(self):
        r = _modal_place(80, 5, 10, 3, placement='anchor', anchor=(2, 2))
        self.assertEqual(r, Rect(1, 1, 81, 6))

    def test_tiny_full_screen_dimensions(self):
        # Rect spans columns 1..cols and rows 1..rows.
        r = _modal_place(10, 4, 99, 99, placement='center', anchor=None)
        self.assertEqual(r.width, 10)
        self.assertEqual(r.height, 4)


if __name__ == '__main__':
    unittest.main()
