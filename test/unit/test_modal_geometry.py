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
    """``placement='anchor'`` WITHOUT ``bounds`` — vertical drops below the
    anchor row (flips above on bottom overflow), horizontal is CENTERED on
    screen (#1040).

    The anchor's COLUMN does not drive the horizontal position: a
    keyboard-triggered context menu anchors at the list pane's left edge,
    and using that column hugged the screen left. With no ``bounds`` the box
    centers horizontally — the same ``left`` the ``'center'`` branch computes —
    while the anchor's ROW drives the vertical placement. (#1051 then clamps
    that centered TARGET into the list-pane span when ``bounds`` is supplied;
    those cases live in :class:`TestPlaceAnchorBounds` below. With no bound the
    target is used as-is, so these remain the unchanged full-screen centering.)
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


# --- Anchored placement: lean toward center, clamp to list bounds (#1051) ---


class TestPlaceAnchorBounds(unittest.TestCase):
    """``placement='anchor', bounds=(L, R)`` — lean the menu X toward screen
    center but keep its painted FOOTPRINT within the list pane span ``[L, R]``.

    #1051 REVISES #1040's full-screen centering: an anchored context menu
    targets the screen-centered ``left`` (the lean), then CLAMPS that target so
    the footprint — the box plus the two #1043 outer-margin columns, spanning
    ``[left - 1, left + w]`` — fits within the inclusive 1-based list-pane span
    ``[L, R]``. The vertical placement (drop / sticky side) is untouched.

    The footprint's right EDGE is the right MARGIN column at ``left + w``
    (= ``Rect.right``, exclusive of the box, the cell ``_paint`` overdraws just
    outside the border); the box's right BORDER is the column before it,
    ``left + w - 1``. The assertions below speak in terms of that footprint
    edge — the column the ticket's worked example pins at ``R``.
    """

    @staticmethod
    def _footprint(rect):
        """Inclusive 1-based ``(first, last)`` columns the footprint covers.

        The box owns ``[rect.left, rect.right - 1]``; the #1043 margins add one
        column on each side, so the footprint spans
        ``[rect.left - 1, rect.right]``.
        """
        return (rect.left - 1, rect.right)

    def test_pane_left_of_center_pins_footprint_right_edge_to_R(self):
        # THE worked example. List pane [1, 40] on a 300-col screen: the pane
        # lies entirely left of screen center (~150), so the centered target is
        # far to the right of the bound and the clamp pins the footprint's RIGHT
        # edge to R = 40 (NOT centered at ~150). Asserts the INTENT — the
        # footprint's right margin lands exactly at R — not a magic number.
        L, R = 1, 40
        w = 20
        r = _modal_place(300, 24, w, 6, placement='anchor',
                         anchor=(5, 1), bounds=(L, R))
        first, last = self._footprint(r)
        self.assertEqual(last, R)                 # right margin column at R = 40
        self.assertEqual(r.right - 1, R - 1)      # box's right border just inside
        self.assertGreaterEqual(first, L)         # left margin not before L
        # And it is NOT the full-screen centered placement (#1040 would do):
        centered = _modal_place(300, 24, w, 6, placement='center', anchor=None)
        self.assertNotEqual(r.left, centered.left)
        self.assertLess(r.right, centered.left)   # pinned far left of center

    def test_footprint_stays_within_bounds_when_pane_left_of_center(self):
        # General invariant for a left-of-center pane across pane widths and
        # box widths: the whole footprint [left-1, left+w] sits inside [L, R].
        for R in (30, 40, 60):
            for w in (10, 20, 24):
                with self.subTest(R=R, w=w):
                    L = 1
                    if R - w < L + 1:
                        continue   # wider-than-bound case covered separately
                    r = _modal_place(300, 24, w, 6, placement='anchor',
                                     anchor=(5, 1), bounds=(L, R))
                    first, last = self._footprint(r)
                    self.assertGreaterEqual(first, L)
                    self.assertLessEqual(last, R)

    def test_pane_straddling_center_keeps_centered_target(self):
        # When the screen-centered target already fits within the bound the
        # clamp is a no-op: the box stays at the centered ``left`` (the lean
        # wins). Bound [50, 250] on a 300-col screen brackets center (~150), so
        # a 20-wide box centered at left = 1 + (300-20)//2 = 141 fits with both
        # margins inside the bound and is used unchanged.
        w = 20
        r = _modal_place(300, 24, w, 6, placement='anchor',
                         anchor=(5, 60), bounds=(50, 250))
        centered = _modal_place(300, 24, w, 6, placement='center', anchor=None)
        self.assertEqual(r.left, centered.left)   # centered target untouched
        first, last = self._footprint(r)
        self.assertGreaterEqual(first, 50)
        self.assertLessEqual(last, 250)

    def test_pane_right_of_center_pins_footprint_left_edge_to_L(self):
        # Mirror of the worked example: a pane entirely RIGHT of center clamps
        # the centered target up to the bound's LEFT edge, landing the
        # footprint's left margin at L.
        L, R = 250, 290
        w = 20
        r = _modal_place(300, 24, w, 6, placement='anchor',
                         anchor=(5, 250), bounds=(L, R))
        first, last = self._footprint(r)
        self.assertEqual(first, L)                # left margin column at L
        self.assertLessEqual(last, R)

    def test_wider_than_offcenter_bound_centers_on_bound_midpoint(self):
        # #1103: a menu wider than its bound can't sit inside it, so instead of
        # drifting to SCREEN center (the old fallback) it centers on the BOUND's
        # midpoint — staying over the parent selection. Off-center bound
        # [200, 230] (midpoint 215, right of screen center ~150) on a 300-col
        # screen, box w=60: wider than the bound, so it centers on 215, NOT at
        # the unbounded screen-centered ~121.
        L, R = 200, 230
        w, cols = 60, 300
        self.assertLess(R - w, L + 1)             # precondition: wider than bound
        r = _modal_place(cols, 24, w, 6, placement='anchor',
                         anchor=(5, 210), bounds=(L, R))
        box_mid = r.left + w / 2
        self.assertAlmostEqual(box_mid, (L + R) / 2, delta=1)  # centered on bound
        # And NOT the unbounded (screen-centered) placement.
        unbounded = _modal_place(cols, 24, w, 6, placement='anchor',
                                 anchor=(5, 210), bounds=None)
        self.assertNotEqual(r.left, unbounded.left)
        self.assertGreater(r.left, unbounded.left)     # pulled right toward bound
        self.assertLessEqual(r.right, cols)            # still on-screen

    def test_wider_than_bound_clamps_to_screen_when_midpoint_off_edge(self):
        # When centering on the bound midpoint would push the box off a screen
        # edge, the on-screen clamp still applies. Narrow bound [1, 20]
        # (midpoint 10) with a 30-wide box on an 80-col screen: midpoint-center
        # would be left = 10 - 15 = -5, clamped to the left margin (left = 2).
        L, R = 1, 20
        w, cols = 30, 80
        self.assertLess(R - w, L + 1)             # precondition: wider than bound
        r = _modal_place(cols, 24, w, 6, placement='anchor',
                         anchor=(5, 1), bounds=(L, R))
        self.assertEqual(r.left, 2)               # clamped: left margin on-screen
        self.assertGreaterEqual(r.left - 1, 1)    # left margin on-screen
        self.assertLessEqual(r.right, cols)       # right edge on-screen

    def test_wider_than_bound_clamps_to_right_edge(self):
        # Mirror: a narrow bound near the RIGHT edge whose midpoint-centered box
        # would overflow the right edge clamps back so its right margin lands at
        # the screen's last column. Bound [70, 80] (midpoint 75) with a 30-wide
        # box on an 80-col screen: midpoint-center = 75 - 15 = 60, box spans
        # [60, 90) — right margin at col 90 > 80 — so it clamps to left = 50
        # (cols - w), right margin column = 80.
        L, R = 70, 80
        w, cols = 30, 80
        self.assertLess(R - w, L + 1)             # precondition: wider than bound
        r = _modal_place(cols, 24, w, 6, placement='anchor',
                         anchor=(5, 75), bounds=(L, R))
        self.assertEqual(r.left, cols - w)        # clamped: right margin at cols
        self.assertEqual(r.right, cols)           # box right border at last col

    def test_full_screen_bound_matches_unbounded(self):
        # ``bounds=(1, cols)`` is exactly the full-screen default: the #1043
        # margin clamp is just this clamp with the screen as the bound, so an
        # explicit full-screen bound is byte-for-byte the ``bounds=None`` path.
        for cols in (40, 80, 120, 300):
            with self.subTest(cols=cols):
                with_bound = _modal_place(cols, 24, 20, 6, placement='anchor',
                                          anchor=(5, 3), bounds=(1, cols))
                no_bound = _modal_place(cols, 24, 20, 6, placement='anchor',
                                        anchor=(5, 3), bounds=None)
                self.assertEqual(with_bound, no_bound)

    def test_bounds_do_not_affect_vertical(self):
        # #1051 is X-only: the vertical drop/flip is identical with and without
        # a bound (the bound only enters the horizontal clamp).
        bounded = _modal_place(300, 24, 20, 6, placement='anchor',
                               anchor=(5, 1), bounds=(1, 40))
        unbounded = _modal_place(300, 24, 20, 6, placement='anchor',
                                 anchor=(5, 1), bounds=None)
        self.assertEqual(bounded.top, unbounded.top)
        self.assertEqual(bounded.bottom, unbounded.bottom)

    def test_center_placement_ignores_bounds(self):
        # A centered modal never leans/clamps to a list pane — passing a bound
        # (defensive; ``ctx`` never does for centered dialogs) the centered box
        # FITS within must not move it. (The footprint is the box + 2 margin
        # columns = 42 wide; a bound it fits inside, e.g. [100, 200], leaves the
        # screen-centered target untouched via the fits-within-bound branch.)
        base = _modal_place(300, 24, 40, 10, placement='center', anchor=None)
        withb = _modal_place(300, 24, 40, 10, placement='center',
                             anchor=None, bounds=(100, 200))
        self.assertEqual(withb, base)


# --- Anchored placement: forced SIDE for chained submenus (#1041) --------


class TestPlaceAnchorSide(unittest.TestCase):
    """``placement='anchor', side=…`` — keep a chained submenu on the side the
    first menu picked instead of independently flipping.

    A context menu can chain (a chosen entry re-opens ``ctx.menu``). The FIRST
    menu decides above/below from its own height; ``_measure_frame`` stores
    that ``side`` and forces EVERY later menu in the chain onto it. The point
    of forcing is the tall-submenu case: a submenu too tall for the chosen
    side must SHIFT to fit (clamped onto the screen, overlapping the subject
    row if need be) rather than flip to the opposite side and read as a
    disjoint box. ``side=None`` keeps today's below-if-fits-else-above
    decision (the first menu / any standalone anchored use).
    """

    # -- side=None: unchanged below-if-fits-else-above (regression guard) ---

    def test_side_none_matches_legacy_decision_across_rows(self):
        # With no forced side the anchored vertical is exactly today's rule:
        # below (top = row + 1) while the box fits, flipping above
        # (top = row - h) only once below would overflow the bottom. Sweep
        # the anchor row down an 80x24 screen with a 6-row frame and assert
        # the boundary lands where the overflow predicate flips, NOT at a
        # hard-coded row.
        rows, h = 24, 6
        for row in range(1, rows + 1):
            with self.subTest(row=row):
                r = _modal_place(80, rows, 20, h,
                                 placement='anchor', anchor=(row, 10))
                fits_below = (row + 1) + h - 1 <= rows
                if fits_below:
                    self.assertEqual(r.top, row + 1, 'should drop below')
                else:
                    # Flipped above (then clamped to >= 1 near the top).
                    self.assertEqual(r.top, max(1, row - h),
                                     'should flip above')

    def test_side_none_default_argument(self):
        # ``side`` defaults to None, so the existing keyword-free call sites
        # keep their behavior — below for an anchor with room beneath it.
        explicit = _modal_place(80, 24, 20, 6,
                                placement='anchor', anchor=(5, 10),
                                side=None)
        default = _modal_place(80, 24, 20, 6,
                               placement='anchor', anchor=(5, 10))
        self.assertEqual(explicit, default)
        self.assertEqual(default.top, 6)             # below the anchor row

    # -- side='below': forced below, shift (never flip) when too tall ------

    def test_below_when_it_fits_is_just_below_anchor(self):
        # A small forced-below menu sits one row under the anchor — same as
        # the fresh decision when it fits.
        r = _modal_place(80, 24, 20, 6, placement='anchor',
                         anchor=(5, 10), side='below')
        self.assertEqual(r.top, 6)
        self.assertEqual(r.bottom, 12)

    def test_tall_below_submenu_clamps_instead_of_flipping(self):
        # THE motivating #1041 case. Anchor at row 5; the chain's side is
        # 'below'. A TALL submenu (18 rows) dropped below would occupy
        # 6..23 — fits here, so first take a frame that DOESN'T fit below to
        # force the shift: 20-row frame below row 5 would be 6..25 > 24.
        # With side='below' it must NOT flip above (top would be 5 - 20 =
        # -15); instead it clamps DOWN onto the screen: top = rows - h + 1 =
        # 24 - 20 + 1 = 5, occupying 5..24. So it stays anchored to the
        # SAME (below) side and merely shifted up to fit, overlapping the
        # subject row — exactly the "reads as going down a level" intent.
        rows, h = 24, 20
        r = _modal_place(80, rows, 20, h, placement='anchor',
                         anchor=(5, 10), side='below')
        # Did NOT flip to the above position (which would be row - h = -15,
        # clamped to 1 — top == 1). It stayed below-anchored and clamped to
        # the bottom of the screen instead.
        flipped_above_top = 1
        self.assertNotEqual(r.top, flipped_above_top,
                            'tall below submenu must not flip above')
        self.assertEqual(r.top, rows - h + 1)        # clamped to fit
        self.assertEqual(r.bottom, rows + 1)         # extends to screen bottom
        # Contrast: the FRESH decision for the same tall frame WOULD flip
        # above (and clamp to top 1). Forcing 'below' is what differs.
        fresh = _modal_place(80, rows, 20, h, placement='anchor',
                             anchor=(5, 10), side=None)
        self.assertEqual(fresh.top, 1)               # fresh flips above
        self.assertNotEqual(r.top, fresh.top)        # forced-below diverges

    # -- side='above': forced above, shift (never flip) when too tall ------

    def test_above_when_it_fits_sits_just_above_anchor(self):
        # Forced above with room: bottom just above the anchor row.
        r = _modal_place(80, 24, 20, 6, placement='anchor',
                         anchor=(15, 10), side='above')
        self.assertEqual(r.top, 9)                   # 15 - 6
        self.assertEqual(r.bottom, 15)               # last row just above 15

    def test_tall_above_submenu_clamps_to_top_instead_of_flipping(self):
        # Mirror of the below case. Anchor near the top (row 4); chain side
        # 'above'. A tall submenu above row 4 would start at 4 - 18 = -14,
        # off the top. With side='above' it must NOT flip below; it clamps
        # to top = 1 and extends downward, overlapping the subject row.
        rows, h = 24, 18
        r = _modal_place(80, rows, 20, h, placement='anchor',
                         anchor=(4, 10), side='above')
        self.assertEqual(r.top, 1)                   # clamped to the top edge
        # NOT the below position (top = row + 1 = 5).
        self.assertNotEqual(r.top, 5, 'tall above submenu must not flip below')

    # -- composition with #1040 (centered X) and #1043 (margin) ------------

    def test_forced_side_keeps_centered_x(self):
        # The forced vertical side never disturbs the centered-X (#1040):
        # left matches the centered branch for both sides, independent of the
        # anchor column.
        centered_left = _modal_place(80, 24, 20, 6,
                                     placement='center', anchor=None).left
        for side in ('below', 'above'):
            with self.subTest(side=side):
                r = _modal_place(80, 24, 20, 6, placement='anchor',
                                 anchor=(12, 1), side=side)
                self.assertEqual(r.left, centered_left)

    def test_forced_side_keeps_outer_margin_columns(self):
        # A cap-width forced-side frame still leaves both #1043 margin
        # columns on-screen (left >= 2, right <= cols) — the horizontal
        # clamp is unchanged by the side parameter.
        cols = 80
        max_w, _max_h = _modal_caps(cols, 24)
        fw, fh = _frame_size(max_w, 4)
        for side in ('below', 'above'):
            with self.subTest(side=side):
                r = _modal_place(cols, 24, fw, fh, placement='anchor',
                                 anchor=(12, cols), side=side)
                self.assertGreaterEqual(r.left - 1, 1)
                self.assertLessEqual(r.right, cols)

    # -- side is meaningless for centered placement ------------------------

    def test_side_ignored_for_center_placement(self):
        # A centered dialog never consults ``side`` — passing one must not
        # change the centered position.
        base = _modal_place(80, 24, 40, 10, placement='center', anchor=None)
        for side in ('below', 'above'):
            with self.subTest(side=side):
                r = _modal_place(80, 24, 40, 10,
                                 placement='center', anchor=None, side=side)
                self.assertEqual(r, base)


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
