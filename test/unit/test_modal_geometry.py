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
    """``_modal_caps`` — 80% width minus the two outer-margin columns,
    rows-4 height."""

    def test_basic_80x24(self):
        # 0.8 * 80 = 64, minus the 2 reserved outer-margin columns -> 62.
        self.assertEqual(_modal_caps(80, 24), (62, 20))

    def test_width_floors_fractional(self):
        # 0.8 * 100 = 80.0 -> 78 after the -2 margin reserve; 0.8 * 99 =
        # 79.2 -> floored to 79 -> 77 after the reserve.
        self.assertEqual(_modal_caps(100, 30), (78, 26))
        self.assertEqual(_modal_caps(99, 30), (77, 26))

    def test_width_reserves_two_margin_columns(self):
        # The cap is exactly two less than the bare 80% width — the room
        # for the left + right outer-margin columns (#1043).
        self.assertEqual(_modal_caps(80, 24)[0], int(0.8 * 80) - 2)

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
    """``placement='anchor'`` — vertical drops below the anchor row (flips
    above on bottom overflow), horizontal is CENTERED on screen (#1040).

    The anchor's COLUMN no longer drives the horizontal position: a
    keyboard-triggered context menu anchors at the list pane's left edge,
    and using that column hugged the screen left. The box is now centered
    horizontally — the same ``left`` the ``'center'`` branch computes —
    while the anchor's ROW still drives the vertical placement.
    """

    def _centered_left(self, cols, w):
        """The ``left`` a centered frame of width ``w`` lands at."""
        return _modal_place(cols, 24, w, 6,
                            placement='center', anchor=None).left

    def test_horizontal_is_centered_not_anchor_column(self):
        # Anchor column (10) is well left of center on an 80-col screen;
        # the box must center horizontally, NOT sit at left == 10.
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(5, 10))
        self.assertNotEqual(r.left, 10)               # not the anchor column
        self.assertEqual(r.left, self._centered_left(80, 20))  # centered X
        # Vertical still drops just below the anchor row (5 -> top 6).
        self.assertEqual(r.top, 6)

    def test_left_edge_anchor_still_centers(self):
        # The motivating case (#1040): a keyboard trigger anchors at the
        # pane's LEFT edge (col 1). The menu must NOT hug the left — it
        # centers on screen, dropping below the anchor row.
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(5, 1))
        self.assertGreater(r.left, 1)                 # does not hug the left
        self.assertEqual(r.left, self._centered_left(80, 20))  # centered X
        self.assertEqual(r.top, 6)                    # below the anchor row

    def test_centered_x_independent_of_anchor_column(self):
        # The horizontal position depends only on the screen + frame width,
        # never on the anchor column — far-left and far-right anchors at the
        # same row land at the identical (centered) left.
        left_anchor = _modal_place(80, 24, 20, 6,
                                   placement='anchor', anchor=(5, 1))
        right_anchor = _modal_place(80, 24, 20, 6,
                                    placement='anchor', anchor=(5, 78))
        self.assertEqual(left_anchor.left, right_anchor.left)
        self.assertEqual(left_anchor.left, self._centered_left(80, 20))

    def test_flip_above_when_bottom_overflows(self):
        # Anchor near the bottom (row 22) with a 6-row frame: below would
        # start at row 23 and occupy 23..28 > 24. Flip above: bottom just
        # above the anchor -> top = 22 - 6 = 16, occupies 16..21. (X stays
        # centered, unaffected by the vertical flip.)
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(22, 10))
        self.assertEqual(r.top, 16)
        self.assertEqual(r.bottom, 22)
        self.assertEqual(r.left, self._centered_left(80, 20))  # centered X

    def test_clamp_top_to_one(self):
        # 80x10 screen, 8-row frame, anchor at row 7: below (top=8) would
        # overflow (8..15 > 10), so it flips above to top = 7 - 8 = -1 —
        # off the top of the screen. Clamp to top=1.
        r = _modal_place(80, 10, 20, 8, placement='anchor', anchor=(7, 10))
        self.assertEqual(r.top, 1)

    def test_anchor_top_left_corner(self):
        # Anchor at (1, 1): vertical drops below the anchor row -> top 2.
        # Horizontal centers on screen (NOT the anchor column 1), so the
        # box matches the centered ``left`` and keeps column 1 free for the
        # left outer margin (#1043).
        r = _modal_place(80, 24, 20, 6, placement='anchor', anchor=(1, 1))
        self.assertEqual(r.top, 2)                    # below the anchor row
        self.assertEqual(r.left, self._centered_left(80, 20))  # centered X
        self.assertGreaterEqual(r.left, 2)            # left margin on-screen

    def test_centered_x_matches_center_branch_exactly(self):
        # The anchored horizontal placement is byte-for-byte the centered
        # branch's ``left`` across screen widths (the stated #1040 intent),
        # while the anchored vertical comes from the anchor row.
        for cols in (40, 80, 81, 120):
            with self.subTest(cols=cols):
                anchored = _modal_place(cols, 24, 20, 6,
                                        placement='anchor', anchor=(5, 3))
                centered = _modal_place(cols, 24, 20, 6,
                                        placement='center', anchor=None)
                self.assertEqual(anchored.left, centered.left)
                self.assertEqual(anchored.right, centered.right)


# --- Outer-margin room (#1043) -------------------------------------------


class TestPlaceMarginRoom(unittest.TestCase):
    """The geometry reserves the #1043 outer-margin columns.

    For any frame sized through ``_modal_caps`` on a non-tiny screen the
    frame width is at most ``cols - 2``, and ``_modal_place`` then keeps
    ``frame.left >= 2`` and ``frame.right <= cols`` — so column
    ``frame.left - 1`` and column ``frame.right`` (the margins) are both
    on-screen. Checked across screen sizes and both placements, with the
    frame pinned at the width cap (the binding case).
    """

    def _frame_at_cap(self, cols, rows):
        """Frame ``Rect`` for a content area pinned at the width cap."""
        max_w, _max_h = _modal_caps(cols, rows)
        fw, fh = _frame_size(max_w, 4)
        return fw, fh

    def test_capped_frame_fits_with_margins_centered(self):
        for cols in (20, 21, 40, 80, 81, 120, 200):
            with self.subTest(cols=cols):
                fw, fh = self._frame_at_cap(cols, 24)
                self.assertLessEqual(fw, cols - 2)
                r = _modal_place(cols, 24, fw, fh,
                                 placement='center', anchor=None)
                self.assertGreaterEqual(r.left - 1, 1)   # left margin on-screen
                self.assertLessEqual(r.right, cols)      # right margin on-screen

    def test_capped_frame_fits_with_margins_anchored(self):
        # Anchored placement now centers horizontally (#1040), so a
        # cap-width frame leaves both outer-margin columns on-screen for any
        # anchor column — checked here at a far-right / bottom anchor cell.
        for cols in (20, 40, 80, 120):
            with self.subTest(cols=cols):
                fw, fh = self._frame_at_cap(cols, 24)
                r = _modal_place(cols, 24, fw, fh,
                                 placement='anchor', anchor=(20, cols))
                self.assertGreaterEqual(r.left - 1, 1)   # left margin on-screen
                self.assertLessEqual(r.right, cols)      # right margin on-screen

    def test_narrow_frame_keeps_left_margin_column(self):
        # A narrow anchored frame centers horizontally (#1040) regardless of
        # the anchor column, so it never sits in column 1 — column 1 stays
        # free for the left outer margin even when the anchor is at col 1.
        r = _modal_place(80, 24, 10, 5, placement='anchor', anchor=(3, 1))
        self.assertGreaterEqual(r.left, 2)

    def test_full_width_frame_omits_margins(self):
        # A frame that spans the screen width (w == cols) has no room for
        # margins: the clamp falls back to left >= 1 (column 1), so the box
        # keeps the screen edge and _paint omits that side's margin.
        r = _modal_place(40, 24, 40, 6, placement='center', anchor=None)
        self.assertEqual(r.left, 1)
        self.assertEqual(r.right, 41)   # right edge at the screen edge


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
