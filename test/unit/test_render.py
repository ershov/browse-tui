"""Tests for the rendering layer in browse-tui.

The render module owns two kinds of code:

  * Pure helpers — ``format_item_segments`` (build the per-item segment
    list) and ``layout_panes`` (geometry math). These are unit-testable
    with no terminal in the loop.
  * Pane renderers — ``render_list``, ``render_preview``,
    ``render_separator``, plus the orchestration ``render_full`` /
    ``render_partial``. These write through ``020-terminal``'s
    ``write()`` / ``move()`` / ``set_style()`` and are exercised
    end-to-end in the layer-3 tmux tests (ticket #14). Phase 1 leaves
    them uncovered here on purpose — adding stub-write fixtures would
    over-fit the tests to current ANSI-byte layout.

This test file therefore covers the pure helpers only. The placeholder
``Item`` injection follows the same pattern used in ``test_visible_tree.py``.
"""

import io
import sys
import unittest

from test.unit import _loader
from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')

# The render module references ``Item`` for synthetic placeholder rows
# (mirrors the state module's pattern). Inject the real class so the
# default formatter can introspect.
_render.Item = _data.Item
# PaneCache is referenced by the four content renderers (#187) — they
# call ``browser._pane_cache.setdefault(name, PaneCache())``. Inject the
# state-layer type the same way Item is injected.
_render.PaneCache = _state.PaneCache

Item = _data.Item
format_item_segments = _render.format_item_segments
layout_panes = _render.layout_panes
render_separator = _render.render_separator
Rect = _render.Rect
point_in_rect = _render.point_in_rect
_TAG_STYLE = _render._TAG_STYLE


class _TermCapture:
    """Capture render writes as a list of (op, *args) tuples + a flat string.

    Injected into the loaded render module by replacing the ``move``,
    ``write``, ``set_style``, ``reset_style``, ``clear_line`` names with
    capturing stand-ins. ``flat`` accumulates the *content* writes only
    (no SGR / cursor-movement bytes) so tests can assert on the visible
    glyphs without coupling to ANSI escapes.
    """

    def __init__(self):
        self.events = []
        self.flat = []

    def install(self, mod):
        self._saved = {}
        for name in ('move', 'write', 'set_style', 'reset_style',
                     'clear_line', 'clear_columns', 'begin_row', 'end_row'):
            self._saved[name] = getattr(mod, name, None)
        mod.move = self._move
        mod.write = self._write
        mod.set_style = self._set_style
        mod.reset_style = self._reset_style
        mod.clear_line = self._clear_line
        mod.clear_columns = self._clear_columns
        # Row-shim stubs (#187): begin_row emits a move + clear-columns
        # equivalent to the pre-shim per-row prologue so existing tests
        # that count moves/clears still see the row-start activity.
        # end_row is a no-op — the real shim's cache-diff path is
        # exercised separately in test_terminal_row_shim.py.
        mod.begin_row = self._begin_row
        mod.end_row = self._end_row

    def restore(self, mod):
        for name, value in self._saved.items():
            if value is None:
                if hasattr(mod, name):
                    delattr(mod, name)
            else:
                setattr(mod, name, value)

    def _move(self, row, col):
        self.events.append(('move', row, col))

    def _write(self, s):
        self.events.append(('write', s))
        self.flat.append(s)

    def _set_style(self, **kwargs):
        self.events.append(('set_style', kwargs))

    def _reset_style(self):
        self.events.append(('reset_style',))

    def _clear_line(self):
        self.events.append(('clear_line',))

    def _clear_columns(self, row, left, right):
        self.events.append(('clear_columns', row, left, right))

    def _begin_row(self, pane_cache, rel_row, abs_row, left, right, *,
                   rightmost):
        # Mimic the row-prologue the renderer used to do explicitly:
        # clear the pane's column range, then move to (abs_row, left).
        # Tests that count clears/moves keep their assertions valid.
        self.events.append(('begin_row', rel_row, abs_row, left, right,
                            rightmost))
        self._clear_columns(abs_row, left, right)
        self._move(abs_row, left)

    def _end_row(self):
        self.events.append(('end_row',))


# --- _TAG_STYLE map ---------------------------------------------------------


class TestTagStyleMap(unittest.TestCase):
    """The tag_style → (fg, bold) map covers the spec's eight names + ''."""

    def test_required_keys_present(self):
        for k in ('green', 'red', 'yellow', 'gray', 'cyan',
                  'blue', 'magenta', 'dim', ''):
            self.assertIn(k, _TAG_STYLE, f'missing _TAG_STYLE key: {k!r}')

    def test_value_shape_is_fg_bold_tuple(self):
        for k, v in _TAG_STYLE.items():
            self.assertIsInstance(v, tuple, f'{k!r}: value is not a tuple')
            self.assertEqual(len(v), 2, f'{k!r}: value not a 2-tuple')
            fg, bold = v
            self.assertTrue(
                fg is None or isinstance(fg, int),
                f'{k!r}: fg must be int|None, got {type(fg).__name__}',
            )
            self.assertIsInstance(bold, bool, f'{k!r}: bold must be bool')

    def test_missing_key_falls_back_to_default(self):
        # Default style for unknown / unstyled tag is the '' entry. The
        # render code uses ``_TAG_STYLE.get(name, _TAG_STYLE[''])`` so a
        # bogus tag_style doesn't crash.
        default = _TAG_STYLE['']
        self.assertEqual(_TAG_STYLE.get('not-a-real-style', default), default)


# --- format_item_segments: default formatter --------------------------------


def _texts(segs):
    """Return just the text portions of a segment list (for shape asserts)."""
    return [s[0] for s in segs]


def _joined(segs):
    return ''.join(_texts(segs))


class TestFormatItemSegmentsDefault(unittest.TestCase):
    """Default item formatting: marker + indent + expand + [id] + [tag] + title."""

    def test_leaf_no_tag_no_selection(self):
        item = Item(id='a')
        segs = format_item_segments(item)
        joined = _joined(segs)
        # Selection marker (2 chars), expand-marker (2 chars for a leaf),
        # then title 'a'. Auto-suppression hides the id segment when
        # ``str(id) == title`` (the default for ``Item(id='a')``).
        self.assertIn('  ', joined[:2])      # selection marker
        self.assertIn('a', joined)
        # No '#' sigil — that's plan-tui-specific and was dropped.
        self.assertNotIn('#', joined)
        # No '[' — no tag.
        self.assertNotIn('[', joined)

    def test_leaf_id_visible_when_title_differs(self):
        # When title differs from id, the id segment is emitted (no '#').
        item = Item(id='a', title='Alpha')
        segs = format_item_segments(item)
        joined = _joined(segs)
        self.assertIn('a ', joined)
        self.assertIn('Alpha', joined)
        self.assertNotIn('#', joined)

    def test_show_ids_always_emits_id_even_when_equal_to_title(self):
        item = Item(id='a')  # title defaults to 'a'
        segs = format_item_segments(item, show_ids='always')
        joined = _joined(segs)
        # The id segment is present (and matches the title text); look
        # for the trailing ' ' separator that distinguishes it from the
        # title at end-of-string.
        self.assertIn('a a', joined)

    def test_show_ids_never_hides_id_even_when_different_from_title(self):
        item = Item(id='a', title='Alpha')
        segs = format_item_segments(item, show_ids='never')
        joined = _joined(segs)
        self.assertNotIn('a ', joined)
        self.assertIn('Alpha', joined)

    def test_collapsed_parent_uses_right_arrow(self):
        item = Item(id='a', has_children=True)
        segs = format_item_segments(item, expanded=False)
        self.assertIn('▶', _joined(segs))  # ▶

    def test_expanded_parent_uses_down_arrow(self):
        item = Item(id='a', has_children=True)
        segs = format_item_segments(item, expanded=True)
        self.assertIn('▼', _joined(segs))  # ▼

    def test_tag_segment_is_styled(self):
        item = Item(id='a', tag='running', tag_style='green')
        segs = format_item_segments(item)
        # Find the tag segment and confirm its fg matches the green entry.
        tag_segs = [s for s in segs if '[running]' in s[0]]
        self.assertEqual(len(tag_segs), 1, f'expected one tag seg, got {tag_segs!r}')
        text, fg, bold = tag_segs[0]
        self.assertEqual(fg, _TAG_STYLE['green'][0])
        self.assertEqual(bold, _TAG_STYLE['green'][1])

    def test_selected_emits_star_marker(self):
        item = Item(id='a')
        segs = format_item_segments(item, selected=True)
        # First segment text should start with '* '.
        self.assertTrue(
            segs[0][0].startswith('* '),
            f'first seg should be "* ", got {segs[0][0]!r}',
        )

    def test_indent_scales_with_depth(self):
        item = Item(id='a')
        d0 = format_item_segments(item, depth=0, base_depth=0)
        d2 = format_item_segments(item, depth=2, base_depth=0)
        # Depth 2 - depth 0 = 2 levels = 4 spaces of additional indent.
        self.assertEqual(len(_joined(d2)) - len(_joined(d0)), 4)


# --- format_item_segments: pending / scope_root kinds -----------------------


class TestFormatItemSegmentsKinds(unittest.TestCase):
    """Synthetic kinds short-circuit the default layout."""

    def test_pending_kind_renders_loading_glyph(self):
        item = Item(id='__pending__', title='⧗ loading…')
        segs = format_item_segments(item, kind='pending', depth=1)
        joined = _joined(segs)
        self.assertIn('⧗ loading', joined)
        # No selection star, no expand arrow on a pending row.
        self.assertNotIn('* ', joined)
        self.assertNotIn('▶', joined)
        self.assertNotIn('▼', joined)

    def test_scope_root_kind_is_bold_id_title(self):
        item = Item(id='proj', title='My Project')
        segs = format_item_segments(item, kind='scope_root')
        joined = _joined(segs)
        # No star marker, no expand arrow for the scope row. The id
        # is rendered (id != title) without the '#' sigil.
        self.assertNotIn('* ', joined)
        self.assertIn('proj ', joined)
        self.assertIn('My Project', joined)
        self.assertNotIn('#', joined)
        # At least one segment must be bold (signalling scope-root style).
        self.assertTrue(
            any(seg[2] for seg in segs),
            'scope_root row should have at least one bold segment',
        )

    def test_scope_root_auto_suppresses_id_when_equal_to_title(self):
        item = Item(id='proj')  # title defaults to 'proj'
        segs = format_item_segments(item, kind='scope_root')
        joined = _joined(segs)
        # Only the title is rendered; no leading id segment, no '#'.
        self.assertEqual(joined, 'proj')


# --- format_item override hook ---------------------------------------------


class TestFormatItemFormatHook(unittest.TestCase):
    """The user-supplied format_item hook short-circuits default formatting."""

    def test_hook_return_value_is_used_verbatim(self):
        called = []

        def hook(item, ctx):
            called.append((item, ctx))
            return [('CUSTOM', None, False)]

        item = Item(id='a', title='Alpha')
        segs = format_item_segments(item, format_item=hook)
        self.assertEqual(segs, [('CUSTOM', None, False)])
        # Default formatting did not run — no '#a' anywhere.
        self.assertEqual(_joined(segs), 'CUSTOM')

    def test_hook_receives_item_argument(self):
        captured = {}

        def hook(item, ctx):
            captured['item'] = item
            captured['ctx'] = ctx
            return [('x', None, False)]

        item = Item(id='a')
        format_item_segments(item, format_item=hook)
        self.assertIs(captured['item'], item)
        # ctx may be None in phase 1 — what matters is that the hook is
        # called with two positional args without raising. Phase-2 ticket
        # #11 wires the real Context object.
        self.assertIn('ctx', captured)


# --- layout_panes -----------------------------------------------------------


class TestPointInRect(unittest.TestCase):
    """``point_in_rect`` — inclusive-top, exclusive-right/bottom."""

    def test_point_in_rect_basic(self):
        r = Rect(left=10, top=5, right=20, bottom=15)
        # Inside, on top-left corner.
        self.assertTrue(point_in_rect(5, 10, r))
        # Inside, middle.
        self.assertTrue(point_in_rect(7, 12, r))
        # Just inside the bottom-right corner (exclusive).
        self.assertTrue(point_in_rect(14, 19, r))
        # Outside: above, below, left, right.
        self.assertFalse(point_in_rect(4, 12, r))
        self.assertFalse(point_in_rect(15, 12, r))   # bottom is exclusive
        self.assertFalse(point_in_rect(7, 9, r))
        self.assertFalse(point_in_rect(7, 20, r))    # right is exclusive

    def test_point_in_rect_none(self):
        # ``None`` rect is convenient for layout.get('children') style
        # callers — should always return False, not raise.
        self.assertFalse(point_in_rect(5, 5, None))

    def test_point_in_rect_zero_area(self):
        # Degenerate rect (left == right): nothing is inside.
        r = Rect(left=10, top=5, right=10, bottom=15)
        self.assertFalse(point_in_rect(7, 10, r))
        # Likewise for top == bottom.
        r2 = Rect(left=10, top=5, right=20, bottom=5)
        self.assertFalse(point_in_rect(5, 12, r2))


class TestLayoutPanes(unittest.TestCase):
    """Pane geometry: list+preview when show_preview, list-only otherwise.

    Convention: layout returns Rects (1-based with exclusive
    right/bottom). The preview Rect's first row IS its separator (in
    layout 'h'); user-visible content height is ``preview.height - 1``.
    The children Rect is ``None`` when the grid is hidden.
    """

    def test_two_pane_typical_terminal(self):
        layout = layout_panes(80, 24, show_preview=True)
        self.assertEqual(layout['cols'], 80)
        list_rect = layout['list']
        self.assertEqual(list_rect.top, 1)
        # 30% of 24 = 7.2 -> 7 (per the spec); leave wiggle room ±1.
        self.assertGreaterEqual(list_rect.height, 6)
        self.assertLessEqual(list_rect.height, 8)
        # No grid pane requested → children is None; info bar sits on
        # the preview separator.
        self.assertIsNone(layout['children'])
        info_bar = layout['info_bar']
        preview = layout['preview']
        self.assertEqual(info_bar.top, list_rect.bottom)
        self.assertEqual(preview.top, info_bar.top)
        # list + preview (incl. sep) = 24 rows total.
        self.assertEqual(list_rect.height + preview.height, 24)

    def test_one_pane_when_preview_hidden(self):
        layout = layout_panes(80, 24, show_preview=False)
        list_rect = layout['list']
        self.assertEqual(list_rect.top, 1)
        self.assertEqual(list_rect.height, 23)
        self.assertIsNone(layout['preview'])
        self.assertIsNone(layout['children'])
        # Info bar at the bottom row (24).
        self.assertEqual(layout['info_bar'].top, 24)

    def test_small_terminal_no_negative_heights(self):
        layout = layout_panes(40, 5, show_preview=True)
        # Whatever the math, heights must be non-negative and the panes
        # must fit inside ``rows``.
        list_rect = layout['list']
        preview = layout['preview']
        self.assertGreaterEqual(list_rect.height, 1)
        prev_h = preview.height if preview is not None else 0
        self.assertGreaterEqual(prev_h, 0)
        self.assertIsNone(layout['children'])
        self.assertLessEqual(list_rect.height + prev_h, 5)


class TestLayoutPanesListRatio(unittest.TestCase):
    """Custom ``list_ratio`` parameter — drives the resizable split."""

    def test_default_ratio_matches_legacy_30_percent(self):
        # Omitting list_ratio reproduces the historic 30% behaviour.
        legacy = layout_panes(80, 100, show_preview=True)
        explicit = layout_panes(80, 100, show_preview=True, list_ratio=0.30)
        self.assertEqual(legacy['list'].height, explicit['list'].height)

    def test_50_percent_ratio_splits_evenly(self):
        layout = layout_panes(80, 100, show_preview=True, list_ratio=0.50)
        list_rect = layout['list']
        preview = layout['preview']
        self.assertEqual(list_rect.height, 50)
        # Preview gets remainder including separator: 50 = sep(1) + 49 content.
        self.assertEqual(preview.height, 50)
        self.assertEqual(list_rect.height + preview.height, 100)

    def test_high_ratio_clamped_to_leave_preview_min(self):
        # 99% would give list=99 of 100 rows, preview=1 (separator only,
        # no content). The min-2 preview rule squeezes list down so 1
        # preview content row is always visible.
        layout = layout_panes(80, 100, show_preview=True, list_ratio=0.99)
        preview = layout['preview']
        self.assertGreaterEqual(preview.height, 2,
                                'preview must keep separator + 1 content')
        self.assertLessEqual(layout['list'].height, 98)

    def test_low_ratio_floors_list_at_one_row(self):
        layout = layout_panes(80, 100, show_preview=True, list_ratio=0.001)
        self.assertGreaterEqual(layout['list'].height, 1)

    def test_ratio_preserved_across_terminal_resizes(self):
        # Same ratio at different terminal heights → proportional list size.
        small = layout_panes(80, 50, show_preview=True, list_ratio=0.40)
        big = layout_panes(80, 100, show_preview=True, list_ratio=0.40)
        self.assertEqual(small['list'].height, 20)
        self.assertEqual(big['list'].height, 40)

    def test_tiny_terminal_degrades_gracefully(self):
        # Below the prev_min=2 threshold, layout shouldn't crash; it
        # falls back to "leave 1 row for the separator".
        layout = layout_panes(40, 2, show_preview=True, list_ratio=0.50)
        list_rect = layout['list']
        preview = layout['preview']
        prev_h = preview.height if preview is not None else 0
        self.assertGreaterEqual(list_rect.height, 1)
        self.assertLessEqual(list_rect.height + prev_h, 2)

    def test_with_children_grid_ratio_applies_to_total_rows(self):
        # Per model (a): list_ratio is list / (list+children+preview).
        # Children stays content-driven; preview absorbs the rest.
        layout = layout_panes(
            80, 100, show_preview=True, show_children_pane=True,
            children_rows_needed=5, list_ratio=0.30,
        )
        children = layout['children']
        list_rect = layout['list']
        preview = layout['preview']
        # Children grid: 1 sep + 5 content rows = 6 (capped at 25 by
        # 25% rule, so 6 is fine).
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 6)
        self.assertEqual(list_rect.height, 30)
        # Preview: 100 - 30 - 6 = 64.
        self.assertEqual(preview.height, 64)


class TestLayoutChildrenSubPane(unittest.TestCase):
    """Children sub-pane geometry across the four layouts.

    Per ticket #149 the children pane is capped at 25% of its sub-area
    along the relevant axis (height for h/m/pc, width for v). When the
    pane is hidden (``show_children_pane=False``) or there's nothing to
    show (``children_*_needed == 0``), the inner split must be omitted
    entirely — children Rect is None and sep_inner is None. When the
    sub-area can't accommodate children at minimum size, the layout
    must drop children gracefully rather than allocating a degenerate
    Rect or stealing space from the list/preview minimums.
    """

    # ----- layout 'h' --------------------------------------------------

    def test_h_no_children(self):
        # show_children=False: no inner split at all.
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        # show_children=True but children_rows_needed=0 (e.g. cursor on
        # a leaf with no cached children to display): also no split.
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_h_children_fits(self):
        # Need 3 rows (well under 25% of 40 = 10). Children pane height
        # = 1 separator + 3 content rows = 4.
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 1 + 3)

    def test_h_children_clamped_to_25pct(self):
        # Need 30 rows; 25% of 40 = 10 cap. Children pane is clamped to
        # 10 rows (incl. separator).
        layout = layout_panes(
            80, 40, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 10)

    def test_h_children_min_terminal(self):
        # rows<20 hard floor: children pane is dropped on tiny terminals
        # to keep the list+preview minimums sane.
        layout = layout_panes(
            80, 19, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_horizontal_children_unchanged(self):
        # Regression: in layout 'h', children sits between list and
        # preview — list.bottom == children.top, children.bottom ==
        # preview.top.
        layout = layout_panes(
            80, 30, split='h', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        list_rect = layout['list']
        children = layout['children']
        preview = layout['preview']
        self.assertIsNotNone(children)
        self.assertEqual(list_rect.bottom, children.top)
        self.assertEqual(children.bottom, preview.top)

    # ----- layout 'v' --------------------------------------------------
    #
    # Per #176 layout 'v' is a 3-COLUMN shape ``list | children |
    # preview`` where the children column occupies the FULL HEIGHT of
    # the body (above the info bar) and renders one child per row.
    # Sub-pane sizing is therefore width-based, capped at 25% of the
    # right-of-list area's width.

    def test_v_no_children(self):
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_v_children_fits(self):
        # Per #180: width is CONTENT-INDEPENDENT — always 25% of the
        # right area (with a max(8, ...) floor). cols=80, list_ratio=0.30
        # → list_w=24, sep_main=1 col, right area = 80 - 25 = 55.
        # desired = max(8, 55//4) = 13 regardless of children_cols_needed.
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=10,
        )
        children = layout['children']
        info_bar = layout['info_bar']
        self.assertIsNotNone(children)
        self.assertEqual(children.width, 13)
        self.assertEqual(children.top, 1)
        self.assertEqual(children.bottom, info_bar.top)

    def test_v_children_clamped_to_25pct(self):
        # Per #180: width is fixed at max(8, right_area_width // 4)
        # regardless of children_cols_needed. right_area = 55, so the
        # column is 13 cols wide whether the longest child is 10 or 100.
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
            children_cols_needed=100,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.width, 13)

    def test_v_children_width_is_content_independent(self):
        # Regression for #180: the children column width must not depend
        # on what's in cached children. Two layouts at the same terminal
        # size must produce IDENTICAL children rects regardless of the
        # children_cols_needed hint (short names vs. long names).
        short = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=3,
        )
        long_ = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=42,
        )
        self.assertIsNotNone(short['children'])
        self.assertIsNotNone(long_['children'])
        self.assertEqual(short['children'], long_['children'])
        self.assertEqual(short['sep_inner'], long_['sep_inner'])
        self.assertEqual(short['preview'], long_['preview'])

    def test_v_children_min_terminal(self):
        # Right area too narrow for children (sep_inner + children +
        # preview content) → drop children. cols=27, list_w=8 → right
        # area = 27 - 8 - 1 = 18; that's enough for children (cap=4).
        # Use a tighter terminal to force a fallback.
        # cols=10, list_w=3 → right area = 10 - 3 - 1 = 6, cap=1, but
        # max_w = right_area - 2 = 4 → children=1 col still fits.
        # To exhaust children: shrink right_area to <= 2 cols. cols=6,
        # list_w=1 → right area = 6 - 1 - 1 = 4 → still fits. The
        # `body_height < 1` branch falls back when terminal is 1 row
        # tall — but the helper degrades to layout 'h' for show_preview.
        # Simplest: tiny rows triggers a fallback to 'h' layout where
        # children is dropped since rows < 20.
        layout = layout_panes(
            80, 3, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
            children_cols_needed=10,
        )
        # rows=3 falls through to layout 'h' (body_height=2 still >= 1
        # so the v branch is taken; but the 'h' fallback path that
        # drops children when rows<20 is what we test in the more
        # extreme case below). For the current case, ensure nothing
        # crashed and the layout has SOME shape:
        self.assertIsNotNone(layout['list'])

        # Tiny terminal → should drop children entirely. rows<20 in 'h'
        # fallback drops the grid; in 'v' the body_height-based check
        # is satisfied at rows=3, so we test the right-area-too-narrow
        # path: cols too small to spare children + sep + preview cols.
        narrow = layout_panes(
            6, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
            children_cols_needed=10,
        )
        # cols=6, list_w=1, right_area=4. max_w = 4 - 2 = 2. Children
        # would be 2 cols wide — still kept. The fallback is for
        # right_area < 3 (need sep_inner + 1 col children + 1 col
        # preview minimum). Verify with cols=4.
        very_narrow = layout_panes(
            4, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
            children_cols_needed=10,
        )
        # cols=4, list_w=1, right_area=4-1-1=2 < 3 → children dropped.
        self.assertIsNone(very_narrow['children'])
        self.assertIsNone(very_narrow['sep_inner'])

    def test_vertical_children_is_full_height_column_between_list_and_preview(self):
        # Per #176 layout 'v' is a true 3-column shape: children sits
        # BETWEEN list and preview, with full body height (top of list
        # to top of info bar).
        layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=10,
        )
        list_rect = layout['list']
        sep_main = layout['sep_main']
        sep_inner = layout['sep_inner']
        children = layout['children']
        preview = layout['preview']
        info_bar = layout['info_bar']
        self.assertIsNotNone(children)
        self.assertIsNotNone(sep_inner)
        # Outer split: list | sep_main | children | sep_inner | preview.
        self.assertEqual(list_rect.right, sep_main.left)
        self.assertEqual(sep_main.right, children.left)
        self.assertEqual(children.right, sep_inner.left)
        self.assertEqual(sep_inner.right, preview.left)
        # Children spans the full body height (above the info bar).
        self.assertEqual(children.top, list_rect.top)
        self.assertEqual(children.bottom, info_bar.top)
        # Children is BETWEEN list and preview (left > list.right,
        # right < preview.left).
        self.assertGreater(children.left, list_rect.right)
        self.assertLess(children.right, preview.left)
        # Both inner separators run the full body height.
        self.assertEqual(sep_main.height, children.height)
        self.assertEqual(sep_inner.height, children.height)

    def test_vertical_distinct_from_preview_children(self):
        # Per #176 Alt-1 ('v') must differ from Alt-4 ('pc'). In 'v'
        # children is a full-height column; in 'pc' children sits above
        # the preview within the right column.
        v_layout = layout_panes(
            80, 30, split='v', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
            children_cols_needed=10,
        )
        pc_layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        v_children = v_layout['children']
        pc_children = pc_layout['children']
        v_info = v_layout['info_bar']
        pc_preview = pc_layout['preview']
        self.assertIsNotNone(v_children)
        self.assertIsNotNone(pc_children)
        # In 'v': children.bottom == info_bar.top (full body height).
        self.assertEqual(v_children.bottom, v_info.top)
        # In 'pc': children.bottom < preview.top (children stacks
        # above preview within the right area).
        self.assertLess(pc_children.bottom, pc_preview.top)

    # ----- layout 'm' --------------------------------------------------

    def test_m_no_children(self):
        layout = layout_panes(
            80, 30, split='m', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        layout = layout_panes(
            80, 30, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_m_children_fits(self):
        # body_height = rows - 1 (info bar) = 39. 25% = 9. Need 3 → 3.
        layout = layout_panes(
            80, 40, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 3)

    def test_m_children_clamped_to_25pct(self):
        # body_height=39, cap=floor(39/4)=9. Need 30 → clamped to 9.
        layout = layout_panes(
            80, 40, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 9)

    def test_m_children_min_terminal(self):
        # body_height < 3 → drop children. rows=3 → body_height=2.
        layout = layout_panes(
            80, 3, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_mixed_children_is_row_below_list(self):
        # In layout 'm' children sits inside the LEFT column, below the
        # list, sharing the column with the same horizontal extent.
        layout = layout_panes(
            80, 30, split='m', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        list_rect = layout['list']
        sep_inner = layout['sep_inner']
        children = layout['children']
        preview = layout['preview']
        self.assertIsNotNone(children)
        self.assertIsNotNone(sep_inner)
        # Same horizontal extent as list (left column).
        self.assertEqual(children.left, list_rect.left)
        self.assertEqual(children.right, list_rect.right)
        # Stacked below list, separated by sep_inner.
        self.assertEqual(list_rect.bottom, sep_inner.top)
        self.assertEqual(sep_inner.bottom, children.top)
        # Preview is to the right (NOT split horizontally itself).
        self.assertGreater(preview.left, children.right)
        self.assertEqual(preview.top, list_rect.top)

    # ----- layout 'pc' -------------------------------------------------

    def test_pc_no_children(self):
        layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=False, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

        layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=0,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_pc_children_fits(self):
        # body_height=39, cap=9. Need 3 → 3.
        layout = layout_panes(
            80, 40, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 3)

    def test_pc_children_clamped_to_25pct(self):
        # body_height=39, cap=9. Need 30 → clamped to 9.
        layout = layout_panes(
            80, 40, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=30,
        )
        children = layout['children']
        self.assertIsNotNone(children)
        self.assertEqual(children.height, 9)

    def test_pc_children_min_terminal(self):
        # body_height < 3 → drop children.
        layout = layout_panes(
            80, 3, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=2,
        )
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['sep_inner'])

    def test_preview_children_is_row_above_preview(self):
        # In layout 'pc' children sits inside the RIGHT column, ABOVE the
        # preview, sharing horizontal extent with preview.
        layout = layout_panes(
            80, 30, split='pc', show_preview=True,
            show_children_pane=True, children_rows_needed=3,
        )
        list_rect = layout['list']
        sep_inner = layout['sep_inner']
        children = layout['children']
        preview = layout['preview']
        self.assertIsNotNone(children)
        self.assertIsNotNone(sep_inner)
        # Same horizontal extent as preview (right column).
        self.assertEqual(children.left, preview.left)
        self.assertEqual(children.right, preview.right)
        # Children sits at the top of the right column.
        self.assertEqual(children.top, list_rect.top)
        # Stacked above preview via sep_inner.
        self.assertEqual(children.bottom, sep_inner.top)
        self.assertEqual(sep_inner.bottom, preview.top)
        # List spans the full body height on the left.
        self.assertGreater(children.left, list_rect.right)


class TestRenderSeparator(unittest.TestCase):
    """``render_separator`` draws plain horizontal / vertical pane dividers.

    The function writes through the terminal primitives in 050-render
    (``move``/``write``/``set_style``/``reset_style``); the test installs
    a :class:`_TermCapture` to inspect the emitted glyph stream without
    coupling to ANSI escape bytes.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)

    def tearDown(self):
        self.cap.restore(_render)

    def test_horizontal_fills_width_with_dash(self):
        rect = Rect(left=1, top=5, right=11, bottom=6)  # width=10, height=1
        render_separator(rect, orientation='h')
        flat = ''.join(self.cap.flat)
        self.assertEqual(flat, '─' * 10)
        # The cursor moved to (5, 1) before drawing.
        moves = [e for e in self.cap.events if e[0] == 'move']
        self.assertEqual(moves[0], ('move', 5, 1))

    def test_vertical_fills_height_with_bar(self):
        rect = Rect(left=8, top=2, right=9, bottom=6)  # width=1, height=4
        render_separator(rect, orientation='v')
        # Each row gets one '│' glyph.
        bars = [s for s in self.cap.flat if s == '│']
        self.assertEqual(len(bars), 4)
        # Each bar was preceded by a move to (row, 8).
        moves = [e for e in self.cap.events if e[0] == 'move']
        self.assertEqual(moves, [
            ('move', 2, 8), ('move', 3, 8),
            ('move', 4, 8), ('move', 5, 8),
        ])

    def test_orientation_inferred_from_shape(self):
        # height==1 → horizontal.
        h_rect = Rect(left=1, top=1, right=21, bottom=2)
        render_separator(h_rect)  # no explicit orientation
        flat = ''.join(self.cap.flat)
        self.assertIn('─', flat)
        self.assertNotIn('│', flat)

        self.cap.events.clear()
        self.cap.flat.clear()

        # width==1 → vertical.
        v_rect = Rect(left=5, top=1, right=6, bottom=11)
        render_separator(v_rect)
        flat = ''.join(self.cap.flat)
        self.assertIn('│', flat)
        self.assertNotIn('─', flat)

    def test_horizontal_with_content_centers_and_flanks(self):
        # width=20, content='HELLO' (5 chars). leftover=15, split 7/8.
        rect = Rect(left=1, top=1, right=21, bottom=2)
        render_separator(rect, orientation='h', content='HELLO')
        flat = ''.join(self.cap.flat)
        # Total visible width = 20, with '─' runs flanking 'HELLO'.
        self.assertEqual(len(flat), 20)
        self.assertIn('HELLO', flat)
        # Verify HELLO is roughly centred (position 7 with 7 leading dashes).
        idx = flat.index('HELLO')
        self.assertEqual(idx, 7)
        self.assertEqual(flat[:7], '─' * 7)
        self.assertEqual(flat[12:], '─' * 8)

    def test_horizontal_truncates_overlong_content(self):
        # width=10 → max content = 8. content='ABCDEFGHIJKL' (12) → 'ABCDEFGH'.
        rect = Rect(left=1, top=1, right=11, bottom=2)
        render_separator(rect, orientation='h', content='ABCDEFGHIJKL')
        flat = ''.join(self.cap.flat)
        self.assertEqual(len(flat), 10)
        self.assertIn('ABCDEFGH', flat)
        self.assertNotIn('IJKL', flat)

    def test_vertical_ignores_content(self):
        # Vertical separator shouldn't try to render content as text.
        rect = Rect(left=3, top=1, right=4, bottom=4)  # height=3
        render_separator(rect, orientation='v', content='IGNORED')
        flat = ''.join(self.cap.flat)
        # Only '│' glyphs should appear, no 'IGNORED' substring.
        self.assertNotIn('IGNORED', flat)
        self.assertEqual(flat.count('│'), 3)

    def test_zero_size_rect_is_a_noop(self):
        # width=0 — nothing should be drawn.
        rect = Rect(left=5, top=5, right=5, bottom=6)
        render_separator(rect, orientation='h')
        self.assertEqual(self.cap.flat, [])

    def test_none_rect_is_a_noop(self):
        render_separator(None, orientation='h')
        self.assertEqual(self.cap.flat, [])


class TestLayoutSeparatorRects(unittest.TestCase):
    """Layout v/m/pc emit non-None sep_main; layout 'h' keeps it folded.

    Per ticket #147 we explicitly preserve the layout-'h' folded model
    (sep_main / sep_inner are ``None`` because the children/preview pane's
    first row IS the separator) to avoid regressing the production
    rendering path. Layouts v/m/pc emit dedicated 1-col vertical
    separator Rects between the list-side and preview-side of the
    screen.
    """

    def test_layout_h_keeps_separators_folded(self):
        layout = layout_panes(80, 24, split='h', show_preview=True)
        self.assertIsNone(layout['sep_main'])
        self.assertIsNone(layout['sep_inner'])

    def test_layout_v_emits_vertical_sep_main(self):
        layout = layout_panes(80, 24, split='v', show_preview=True)
        sep = layout['sep_main']
        self.assertIsNotNone(sep)
        self.assertEqual(sep.width, 1)  # vertical bar
        # sep height spans the body (rows 1..23, info bar at row 24).
        self.assertEqual(sep.top, 1)

    def test_layout_m_emits_vertical_sep_main(self):
        layout = layout_panes(80, 24, split='m', show_preview=True)
        sep = layout['sep_main']
        self.assertIsNotNone(sep)
        self.assertEqual(sep.width, 1)

    def test_layout_pc_emits_vertical_sep_main(self):
        layout = layout_panes(80, 24, split='pc', show_preview=True)
        sep = layout['sep_main']
        self.assertIsNotNone(sep)
        self.assertEqual(sep.width, 1)

    def test_layout_v_with_children_emits_sep_inner(self):
        # Per #176 layout 'v' is a 3-column shape with children as a
        # full-height column between list and preview, so sep_inner is
        # a VERTICAL divider (width=1) running the full body height.
        layout = layout_panes(
            80, 30, split='v', show_preview=True, show_children_pane=True,
            children_rows_needed=5, children_cols_needed=10,
        )
        if layout['children'] is not None:
            sep_inner = layout['sep_inner']
            self.assertIsNotNone(sep_inner)
            self.assertEqual(sep_inner.width, 1)
            # Spans the full body height (matches sep_main).
            self.assertEqual(sep_inner.height, layout['sep_main'].height)
        else:
            self.assertIsNone(layout['sep_inner'])

    def test_layout_m_with_children_emits_horizontal_sep_inner(self):
        layout = layout_panes(
            120, 40, split='m', show_preview=True, show_children_pane=True,
            children_rows_needed=5,
        )
        if layout['children'] is not None:
            sep_inner = layout['sep_inner']
            self.assertIsNotNone(sep_inner)
            self.assertEqual(sep_inner.height, 1)  # horizontal divider

    def test_info_bar_full_width_in_all_layouts(self):
        for split in ('h', 'v', 'm', 'pc'):
            with self.subTest(split=split):
                layout = layout_panes(80, 24, split=split, show_preview=True)
                info_bar = layout['info_bar']
                self.assertIsNotNone(info_bar)
                # Full width: from col 1 to col 81 (exclusive right).
                self.assertEqual(info_bar.left, 1)
                self.assertEqual(info_bar.right, 81)
                self.assertEqual(info_bar.height, 1)


# --- _truncate_visible helper ----------------------------------------------


_truncate_visible = _render._truncate_visible


class TestTruncateVisible(unittest.TestCase):
    """SGR-aware truncation: counts only visible columns, preserves ESC[..m."""

    def test_plain_ascii_under_limit_is_unchanged(self):
        self.assertEqual(_truncate_visible('hello', 10), 'hello')

    def test_plain_ascii_over_limit_is_truncated(self):
        # Truncated and a final reset is appended so style can't bleed.
        out = _truncate_visible('hello world', 5)
        self.assertTrue(out.startswith('hello'))
        self.assertEqual(out, 'hello\033[0m')

    def test_max_cols_zero_returns_empty(self):
        self.assertEqual(_truncate_visible('abc', 0), '')

    def test_negative_max_cols_returns_empty(self):
        self.assertEqual(_truncate_visible('abc', -3), '')

    def test_empty_string(self):
        self.assertEqual(_truncate_visible('', 5), '')

    def test_sgr_escapes_dont_count_toward_width(self):
        # 'AB' + reset + 'CDE' has 5 visible chars; max_cols=5 should fit.
        s = '\033[31mAB\033[0mCDE'
        out = _truncate_visible(s, 5)
        # All visible chars present, escape preserved intact.
        self.assertIn('AB', out)
        self.assertIn('CDE', out)
        self.assertIn('\033[31m', out)
        self.assertIn('\033[0m', out)

    def test_truncation_inside_styled_run_preserves_escape(self):
        # 'ABCDE' wrapped in red. Truncate to 2 cols → keeps escape +
        # 'AB' + reset (so style doesn't leak).
        s = '\033[31mABCDE\033[0m'
        out = _truncate_visible(s, 2)
        # Original escape preserved (not split mid-bytes).
        self.assertIn('\033[31m', out)
        self.assertIn('AB', out)
        self.assertNotIn('CDE', out)
        # Final reset appended because truncation occurred.
        self.assertTrue(out.endswith('\033[0m'))

    def test_truncation_does_not_split_an_escape_sequence(self):
        # The string starts with an SGR escape; if truncate_visible
        # handed back '\\033[' alone, that would be a corrupt prefix.
        s = '\033[1;31mword'
        out = _truncate_visible(s, 1)
        # Either contains the full escape or skips it entirely; never
        # contains a half-cut escape (no '\\033[' without final 'm').
        self.assertNotEqual(out, '\033[')
        # We expect the escape preserved in front of one visible char.
        self.assertIn('\033[1;31m', out)
        self.assertIn('w', out)
        self.assertNotIn('ord', out)

    def test_no_trailing_reset_when_no_truncation(self):
        # If the string fits entirely, we should NOT append \033[0m
        # (it would be a spurious reset).
        s = 'hello'
        out = _truncate_visible(s, 10)
        self.assertEqual(out, 'hello')

    def test_truncation_at_reset_boundary(self):
        # Visible width is exactly max_cols and the string ends with a
        # reset escape — output should keep the reset.
        s = 'ABC\033[0m'
        out = _truncate_visible(s, 3)
        # All 3 visible chars and the reset preserved.
        self.assertIn('ABC', out)
        self.assertIn('\033[0m', out)


# --- clear_columns helper --------------------------------------------------


class TestClearColumns(unittest.TestCase):
    """``clear_columns(row, left, right)`` writes spaces only in [left, right)."""

    def setUp(self):
        # We test the helper through a minimal _render harness that
        # captures writes; the real ``clear_columns`` lives in
        # 020-terminal.py, but we recreate its behaviour in-process by
        # invoking through the captured renderer.
        self.cap = _TermCapture()

    def test_clear_columns_writes_correct_width(self):
        # Re-implement minimal clear_columns logic on top of the
        # capture: it should move(row, left) then write(' ' * width).
        events = []

        def move(r, c):
            events.append(('move', r, c))

        def write(s):
            events.append(('write', s))

        # Inline the function under test to verify the contract; the
        # production ``clear_columns`` lives in 020-terminal.py and is
        # tested by end-to-end runs.
        def clear_columns(row, left, right):
            width = right - left
            if width <= 0:
                return
            move(row, left)
            write(' ' * width)

        clear_columns(5, 10, 20)
        self.assertEqual(events, [('move', 5, 10), ('write', '          ')])

    def test_clear_columns_empty_range_is_noop(self):
        events = []

        def move(r, c):
            events.append(('move', r, c))

        def write(s):
            events.append(('write', s))

        def clear_columns(row, left, right):
            width = right - left
            if width <= 0:
                return
            move(row, left)
            write(' ' * width)

        clear_columns(5, 20, 20)   # right == left
        clear_columns(5, 20, 10)   # right < left
        self.assertEqual(events, [])


# --- render_list rect clipping ---------------------------------------------


class _MockState:
    """Just enough of ``State`` for render_list to traverse the visible list.

    ``visible_items`` is monkey-patched on the loaded render module to
    return a fixed list, sidestepping the full state machinery.
    """

    def __init__(self, visible, cursor=0, expanded=None, selected=None,
                 scope_stack=()):
        self._visible = visible
        self.cursor = cursor
        self.expanded = expanded or set()
        self.selected = selected or set()
        self.scope_stack = scope_stack
        self._preview = {}
        self._children = {}


class _AutoPaneCache(dict):
    """``_pane_cache`` stand-in that fails loud on un-reconciled lookups.

    After ticket #228 the per-pane renderers no longer self-create
    their cache entries — ``_reconcile_pane_caches`` (called from
    ``render_full`` / ``render_partial``) is the single dispatch site
    that runs ``cache.update_rect(rect)`` once per frame.

    Tests that call renderers directly (without going through the
    orchestrator) must seed the cache themselves — typically via the
    ``_reconcile`` helper on the test class. Forgetting that step used
    to surface as an opaque ``IndexError`` deep inside ``end_row`` (an
    auto-created cache has ``lines == []`` so ``lines[rel_row] = …``
    blows up). The loud ``KeyError`` here points the test author at
    the discipline they missed instead.
    """

    def __missing__(self, key):
        raise KeyError(
            f"PaneCache {key!r} not reconciled — call "
            f"_reconcile(browser, {key!r}, rect) (or run through "
            "_reconcile_pane_caches) before invoking the renderer."
        )


def _reconcile(browser, name, rect):
    """Mimic the orchestrator's per-frame ``_reconcile_pane_caches``.

    Tests that drive a renderer in isolation (without going through
    ``render_full`` / ``render_partial``) must seed the relevant
    cache via this helper before each paint, since post-#228 the
    renderers no longer self-create their entries.
    """
    cache = browser._pane_cache.setdefault(name, _state.PaneCache())
    cache.update_rect(rect)


class _MockBrowser:
    """Minimal Browser stand-in for render_list."""

    def __init__(self, state, **kwargs):
        self._state = state
        self._list_scroll = 0
        self._insert_mode = False
        self._insert_pos = 0
        self._insert_depth = 0
        self._insert_label = ''
        self._search_query = ''
        self._search_mode = False
        self._error_text = ''
        self._help_mode = False
        self._preview_scroll = 0
        self._needs_redraw = set()
        # Per-pane row cache used by the differential renderer (#187).
        # Tests that call renderers directly must reconcile the cache
        # via ``self._reconcile(...)`` before each paint; otherwise the
        # ``__missing__`` hook on ``_AutoPaneCache`` raises a pointed
        # ``KeyError`` instead of a confusing downstream ``IndexError``.
        self._pane_cache = _AutoPaneCache()
        self.show_ids = 'auto'
        self.show_preview = True
        self.show_children_pane = False
        self.list_ratio = 0.30
        self.format_item = None
        self.split = 'h'
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestRenderListRectClipping(unittest.TestCase):
    """render_list draws within the rect's column range, not the full row.

    Verifies the migration from ``(top, height, cols)`` to ``(rect)``:
    cursor moves are at ``rect.left``, content is clipped to
    ``rect.width``, and trailing columns are cleared via
    ``clear_columns`` so stale text from a wider render is wiped.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        # Stub out visible_items / VisibleEntry so we don't pull in the
        # whole state module.
        self._saved_visible = getattr(_render, 'visible_items', None)
        self._saved_ve = getattr(_render, 'VisibleEntry', None)
        self._saved_sm = getattr(_render, '_search_matches', None)
        self._saved_st = getattr(_render, '_search_text', None)
        _render._search_matches = lambda text, q: False
        _render._search_text = lambda item: item.title

    def tearDown(self):
        self.cap.restore(_render)
        if self._saved_visible is None:
            if hasattr(_render, 'visible_items'):
                delattr(_render, 'visible_items')
        else:
            _render.visible_items = self._saved_visible
        if self._saved_sm is None:
            if hasattr(_render, '_search_matches'):
                delattr(_render, '_search_matches')
        else:
            _render._search_matches = self._saved_sm
        if self._saved_st is None:
            if hasattr(_render, '_search_text'):
                delattr(_render, '_search_text')
        else:
            _render._search_text = self._saved_st

    def _make_browser_with_items(self, items):
        # Build a fake VisibleEntry-shaped object.
        class _Entry:
            def __init__(self, item, depth=0, kind='normal'):
                self.item = item
                self.depth = depth
                self.kind = kind

        visible = [_Entry(it) for it in items]
        state = _MockState(visible)
        _render.visible_items = lambda s: state._visible
        return _MockBrowser(state)

    def test_render_list_uses_rect_left_for_move(self):
        items = [Item(id='a'), Item(id='b')]
        browser = self._make_browser_with_items(items)
        # Pane offset to the right (column 41 onwards).
        rect = Rect(left=41, top=1, right=81, bottom=3)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        moves = [e for e in self.cap.events if e[0] == 'move']
        # Every move should be to column 41 (or further right within
        # the same pane, e.g. for the cursor highlight). The first move
        # for each rendered row is to column 41.
        self.assertTrue(moves)
        # First move per row in the per-row loop is at left=41.
        # We don't enforce all moves equal 41 (sub-helpers may move
        # within the row), but the row-start moves must be there.
        self.assertTrue(
            any(m == ('move', 1, 41) for m in moves),
            f'no move to row 1 col 41 in {moves[:8]!r}',
        )
        self.assertTrue(
            any(m == ('move', 2, 41) for m in moves),
            f'no move to row 2 col 41 in {moves[:8]!r}',
        )

    def test_render_list_clears_only_pane_columns(self):
        items = [Item(id='a')]
        browser = self._make_browser_with_items(items)
        rect = Rect(left=41, top=1, right=81, bottom=3)
        _reconcile(browser, 'list', rect)
        _render.render_list(browser, rect)
        clear_calls = [e for e in self.cap.events if e[0] == 'clear_columns']
        # We expect at least one clear_columns call per rendered row,
        # always within [41, 81).
        self.assertTrue(clear_calls,
                        'render_list must call clear_columns for each row')
        for ev in clear_calls:
            _, row, left, right = ev
            self.assertEqual(left, 41)
            self.assertEqual(right, 81)
        # And NO bare clear_line() calls (that would wipe other panes).
        self.assertFalse(any(e[0] == 'clear_line' for e in self.cap.events),
                         'render_list must not call clear_line (clobbers neighbors)')

    def test_render_list_zero_height_is_noop(self):
        items = [Item(id='a')]
        browser = self._make_browser_with_items(items)
        rect = Rect(left=1, top=5, right=80, bottom=5)  # height 0
        _render.render_list(browser, rect)
        # No moves, no writes — total no-op.
        self.assertEqual(self.cap.events, [])

    def test_render_list_none_rect_is_noop(self):
        items = [Item(id='a')]
        browser = self._make_browser_with_items(items)
        _render.render_list(browser, None)
        self.assertEqual(self.cap.events, [])


class TestRenderChildrenList(unittest.TestCase):
    """``render_children_list`` writes one child per row (Alt-1 vertical).

    Mirrors the structure of ``TestRenderListRectClipping``: stubs out
    ``visible_items`` so the renderer's cursor lookup finds a synthetic
    parent item, and inspects the captured terminal stream for the
    expected per-row glyphs.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        self._saved_visible = getattr(_render, 'visible_items', None)
        self._saved_render_info_bar = getattr(_render, 'render_info_bar', None)
        # Silence the info bar — it writes a flood of dashes that drown
        # the per-row content we want to assert on.
        _render.render_info_bar = lambda *a, **kw: None

    def tearDown(self):
        self.cap.restore(_render)
        if self._saved_visible is None:
            if hasattr(_render, 'visible_items'):
                delattr(_render, 'visible_items')
        else:
            _render.visible_items = self._saved_visible
        if self._saved_render_info_bar is None:
            if hasattr(_render, 'render_info_bar'):
                delattr(_render, 'render_info_bar')
        else:
            _render.render_info_bar = self._saved_render_info_bar

    def _browser_with_children(self, parent, children, *, has_header=False):
        class _Entry:
            def __init__(self, item, depth=0, kind='normal'):
                self.item = item
                self.depth = depth
                self.kind = kind
        visible = [_Entry(parent)]
        state = _MockState(visible, cursor=0)
        state._children = {parent.id: children}
        _render.visible_items = lambda s: state._visible
        return _MockBrowser(state, split='v', show_children_pane=True)

    def test_one_child_per_row(self):
        parent = Item(id='p', title='parent', has_children=True)
        children = [
            Item(id='c1', title='alpha'),
            Item(id='c2', title='beta'),
            Item(id='c3', title='gamma'),
        ]
        browser = self._browser_with_children(parent, children)
        rect = Rect(left=10, top=1, right=30, bottom=10)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)

        # Each child name must appear exactly once in the flat output.
        flat = ''.join(self.cap.flat)
        self.assertIn('alpha', flat)
        self.assertIn('beta', flat)
        self.assertIn('gamma', flat)

        # Row-start moves: one per content row at rect.left=10.
        moves_at_left = [
            e for e in self.cap.events
            if e[0] == 'move' and e[2] == 10
        ]
        # First three rows correspond to the three children; rows 4-9
        # are blank fillers that still get a move (the renderer moves
        # before clearing/blanking each row).
        # Just assert there are at least 3 moves with col=10, one per
        # row 1..3.
        rows = sorted({m[1] for m in moves_at_left})
        self.assertEqual(rows[:3], [1, 2, 3])

    def test_truncates_long_names_to_width(self):
        parent = Item(id='p', title='parent', has_children=True)
        children = [Item(id='c', title='X' * 200)]
        browser = self._browser_with_children(parent, children)
        # Width = 30 - 10 = 20 cols.
        rect = Rect(left=10, top=1, right=30, bottom=5)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)
        flat = ''.join(self.cap.flat)
        # The 'X' run was truncated — fewer than the original 200.
        self.assertLess(flat.count('X'), 200)

    def test_clears_pane_columns_only(self):
        parent = Item(id='p', title='parent', has_children=True)
        children = [Item(id='c', title='only')]
        browser = self._browser_with_children(parent, children)
        rect = Rect(left=10, top=1, right=30, bottom=5)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)
        clear_calls = [e for e in self.cap.events if e[0] == 'clear_columns']
        self.assertTrue(clear_calls)
        for _, row, left, right in clear_calls:
            self.assertEqual(left, 10)
            self.assertEqual(right, 30)
        self.assertFalse(any(e[0] == 'clear_line' for e in self.cap.events),
                         'render_children_list must not use clear_line')

    def test_pending_branch_shows_loading_hint(self):
        parent = Item(id='p', title='parent', has_children=True)
        # Children not yet cached (None, not []).
        class _Entry:
            def __init__(self, item, depth=0, kind='normal'):
                self.item = item
                self.depth = depth
                self.kind = kind
        visible = [_Entry(parent)]
        state = _MockState(visible, cursor=0)
        state._children = {}  # not cached
        _render.visible_items = lambda s: state._visible
        browser = _MockBrowser(state, split='v', show_children_pane=True)
        rect = Rect(left=10, top=1, right=30, bottom=5)
        _reconcile(browser, 'children', rect)
        _render.render_children_list(browser, rect, has_header=False)
        flat = ''.join(self.cap.flat)
        self.assertIn('loading', flat)


# --- render_partial separator regression (#180) -----------------------------


class TestRenderPartialRedrawsSeparators(unittest.TestCase):
    """Regression for #180: ``render_partial`` redraws ``sep_main`` /
    ``sep_inner`` whenever any pane is repainted.

    Cursor moves in the list pane flag ``{'list', 'children', 'preview'}``
    in ``_needs_redraw``. In Alt-1 vertical layout (``split='v'``) the
    children pane appears/disappears as the cursor crosses leaf/branch
    boundaries — when it appears, the layout's ``sep_inner`` column is
    new (the previous render didn't have one). If the partial redraw
    leaves separators alone, that column shows nothing where it should
    show ``│``. The fix paints both separators on any pane redraw.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        self._saved_visible = getattr(_render, 'visible_items', None)
        self._saved_render_info_bar = getattr(_render, 'render_info_bar', None)
        self._saved_layout_for = getattr(_render, '_layout_for', None)
        self._saved_render_list = getattr(_render, 'render_list', None)
        self._saved_render_children_list = getattr(
            _render, 'render_children_list', None)
        self._saved_render_preview = getattr(_render, 'render_preview', None)
        self._saved_flush = getattr(_render, 'flush', None)
        self._saved_begin_sync = getattr(_render, 'begin_sync', None)
        self._saved_end_sync = getattr(_render, 'end_sync', None)
        # Silence sub-renderers that aren't under test — we only want
        # to observe the separator draws.
        _render.render_info_bar = lambda *a, **kw: None
        _render.render_list = lambda *a, **kw: None
        _render.render_children_list = lambda *a, **kw: None
        _render.render_preview = lambda *a, **kw: None
        _render.flush = lambda: None
        # render_partial brackets its body with begin_sync / end_sync
        # (#186); when 050-render is loaded standalone these names
        # aren't present, so stub them out.
        _render.begin_sync = lambda: None
        _render.end_sync = lambda: None

    def tearDown(self):
        self.cap.restore(_render)
        for name, saved in (
            ('visible_items', self._saved_visible),
            ('render_info_bar', self._saved_render_info_bar),
            ('_layout_for', self._saved_layout_for),
            ('render_list', self._saved_render_list),
            ('render_children_list', self._saved_render_children_list),
            ('render_preview', self._saved_render_preview),
            ('flush', self._saved_flush),
            ('begin_sync', self._saved_begin_sync),
            ('end_sync', self._saved_end_sync),
        ):
            if saved is None:
                if hasattr(_render, name):
                    delattr(_render, name)
            else:
                setattr(_render, name, saved)

    def _stub_layout(self, *, with_children):
        """Stub ``_layout_for`` to return a v-split layout shape.

        ``with_children=True`` includes both ``sep_main`` and
        ``sep_inner`` (children pane present); ``False`` omits
        ``sep_inner`` (children pane absent — leaf cursor case).
        """
        body_top, body_bottom = 1, 39
        sep_main = Rect(left=70, top=body_top, right=71, bottom=body_bottom)
        list_rect = Rect(left=1, top=body_top, right=70, bottom=body_bottom)
        info_bar = Rect(left=1, top=39, right=241, bottom=40)
        if with_children:
            children_rect = Rect(left=72, top=body_top,
                                 right=92, bottom=body_bottom)
            sep_inner = Rect(left=92, top=body_top,
                             right=93, bottom=body_bottom)
            preview_rect = Rect(left=93, top=body_top,
                                right=241, bottom=body_bottom)
        else:
            children_rect = None
            sep_inner = None
            preview_rect = Rect(left=72, top=body_top,
                                right=241, bottom=body_bottom)
        layout = {
            'list': list_rect,
            'children': children_rect,
            'preview': preview_rect,
            'sep_main': sep_main,
            'sep_inner': sep_inner,
            'info_bar': info_bar,
            'cols': 240,
            'rows': 40,
        }
        _render._layout_for = lambda b: layout
        return layout

    def _make_browser(self, needs):
        state = _MockState(visible=[], cursor=0)
        browser = _MockBrowser(state, split='v', show_children_pane=True)
        browser._needs_redraw = set(needs)
        return browser

    def _separator_writes(self):
        """Extract ``│`` writes (the vertical-separator glyph)."""
        return [e for e in self.cap.events
                if e[0] == 'write' and e[1] == '│']

    def test_cursor_move_redraws_both_separators_when_present(self):
        """`{list, children, preview}` flags + children present → both
        sep_main and sep_inner are repainted.
        """
        layout = self._stub_layout(with_children=True)
        browser = self._make_browser({'list', 'children', 'preview'})
        _render.render_partial(browser)
        # Each separator paints ``│`` once per row of its rect.
        sep_main = layout['sep_main']
        sep_inner = layout['sep_inner']
        # Collect (row, col) of every '│' write — preceding 'move' event
        # gives the position.
        bars = []
        prev_pos = None
        for e in self.cap.events:
            if e[0] == 'move':
                prev_pos = (e[1], e[2])
            elif e[0] == 'write' and e[1] == '│':
                bars.append(prev_pos)
        # sep_main column: every body row from top..bottom-1.
        main_rows = sorted({r for (r, c) in bars if c == sep_main.left})
        inner_rows = sorted({r for (r, c) in bars if c == sep_inner.left})
        expected_rows = list(range(sep_main.top, sep_main.bottom))
        self.assertEqual(
            main_rows, expected_rows,
            f'sep_main missing rows; got {main_rows}, expected {expected_rows}',
        )
        self.assertEqual(
            inner_rows, expected_rows,
            f'sep_inner missing rows; got {inner_rows}, expected {expected_rows}',
        )

    def test_cursor_move_redraws_sep_main_when_no_children(self):
        """Leaf cursor → no sep_inner, but sep_main must still paint."""
        layout = self._stub_layout(with_children=False)
        browser = self._make_browser({'list', 'children', 'preview'})
        _render.render_partial(browser)
        sep_main = layout['sep_main']
        bars = []
        prev_pos = None
        for e in self.cap.events:
            if e[0] == 'move':
                prev_pos = (e[1], e[2])
            elif e[0] == 'write' and e[1] == '│':
                bars.append(prev_pos)
        main_rows = sorted({r for (r, c) in bars if c == sep_main.left})
        expected_rows = list(range(sep_main.top, sep_main.bottom))
        self.assertEqual(main_rows, expected_rows)
        # No separator draws at any other column.
        other_cols = {c for (r, c) in bars if c != sep_main.left}
        self.assertEqual(other_cols, set())

    def test_no_pane_redraw_leaves_separators_alone(self):
        """Empty needs set / 'info'-only redraw doesn't touch separators."""
        self._stub_layout(with_children=True)
        browser = self._make_browser(set())
        _render.render_partial(browser)
        # Empty needs → early return, no events at all.
        self.assertEqual(self._separator_writes(), [])


# --- BSU/ESU brackets + no \e[2J (#186) ------------------------------------


class TestSynchronizedOutputBrackets(unittest.TestCase):
    """``render_full`` / ``render_partial`` bracket their output with
    DEC mode 2026 begin/end synchronized output and never emit ``\\e[2J``.

    Pre-#186, ``render_full`` started with ``\\e[2J`` to clear the
    screen. The differential renderer in #185–#188 replaces that with
    a row-cache-aware repaint, so the blanket clear is gone. Both
    entry points now bracket their writes with ``\\e[?2026h`` (BSU)
    and ``\\e[?2026l`` (ESU) so terminals that support it swap in the
    new frame atomically.
    """

    def setUp(self):
        self.cap = _TermCapture()
        self.cap.install(_render)
        self._saved_layout_for = getattr(_render, '_layout_for', None)
        self._saved_render_list = getattr(_render, 'render_list', None)
        self._saved_render_children_grid = getattr(
            _render, 'render_children_grid', None)
        self._saved_render_children_list = getattr(
            _render, 'render_children_list', None)
        self._saved_render_preview = getattr(_render, 'render_preview', None)
        self._saved_render_separator = getattr(
            _render, 'render_separator', None)
        self._saved_render_info_bar = getattr(_render, 'render_info_bar', None)
        self._saved_flush = getattr(_render, 'flush', None)
        self._saved_begin_sync = getattr(_render, 'begin_sync', None)
        self._saved_end_sync = getattr(_render, 'end_sync', None)
        # Silence the inner renderers — we observe only the brackets.
        _render.render_list = lambda *a, **kw: None
        _render.render_children_grid = lambda *a, **kw: None
        _render.render_children_list = lambda *a, **kw: None
        _render.render_preview = lambda *a, **kw: None
        _render.render_separator = lambda *a, **kw: None
        _render.render_info_bar = lambda *a, **kw: None
        _render.flush = lambda: None
        # Real BSU/ESU bytes flow through the captured ``write`` so the
        # tests can assert on the escape sequences.
        _render.begin_sync = lambda: _render.write('\033[?2026h')
        _render.end_sync = lambda: _render.write('\033[?2026l')

    def tearDown(self):
        self.cap.restore(_render)
        for name, saved in (
            ('_layout_for', self._saved_layout_for),
            ('render_list', self._saved_render_list),
            ('render_children_grid', self._saved_render_children_grid),
            ('render_children_list', self._saved_render_children_list),
            ('render_preview', self._saved_render_preview),
            ('render_separator', self._saved_render_separator),
            ('render_info_bar', self._saved_render_info_bar),
            ('flush', self._saved_flush),
            ('begin_sync', self._saved_begin_sync),
            ('end_sync', self._saved_end_sync),
        ):
            if saved is None:
                if hasattr(_render, name):
                    delattr(_render, name)
            else:
                setattr(_render, name, saved)

    def _stub_layout(self):
        body_top, body_bottom = 1, 23
        layout = {
            'list': Rect(left=1, top=body_top, right=25, bottom=body_bottom),
            'children': None,
            'preview': Rect(left=25, top=body_top, right=81, bottom=body_bottom),
            'sep_main': None,
            'sep_inner': None,
            'info_bar': Rect(left=1, top=body_top, right=81, bottom=body_top + 1),
            'cols': 80,
            'rows': 24,
            'list_rightmost': False,
            'children_rightmost': False,
            'preview_rightmost': True,
            'info_bar_rightmost': True,
            'sep_main_rightmost': False,
            'sep_inner_rightmost': False,
        }
        _render._layout_for = lambda b: layout

    def _make_browser(self, needs=None):
        state = _MockState(visible=[], cursor=0)
        browser = _MockBrowser(state)
        browser._needs_redraw = set(needs or ())
        return browser

    def test_render_full_does_not_emit_2J(self):
        self._stub_layout()
        browser = self._make_browser()
        _render.render_full(browser)
        joined = ''.join(self.cap.flat)
        self.assertNotIn('\033[2J', joined,
                         'render_full must not blanket-clear the screen')

    def test_render_full_brackets_output_with_BSU_ESU(self):
        self._stub_layout()
        browser = self._make_browser()
        _render.render_full(browser)
        # First captured write is BSU; last is ESU.
        self.assertTrue(self.cap.flat, 'render_full produced no output')
        self.assertEqual(self.cap.flat[0], '\033[?2026h')
        self.assertEqual(self.cap.flat[-1], '\033[?2026l')

    def test_render_partial_brackets_output_with_BSU_ESU(self):
        self._stub_layout()
        browser = self._make_browser(needs={'list'})
        _render.render_partial(browser)
        self.assertTrue(self.cap.flat, 'render_partial produced no output')
        self.assertEqual(self.cap.flat[0], '\033[?2026h')
        self.assertEqual(self.cap.flat[-1], '\033[?2026l')

    def test_render_partial_empty_needs_is_silent(self):
        # Empty needs → render_partial returns before begin_sync; no BSU.
        self._stub_layout()
        browser = self._make_browser(needs=set())
        _render.render_partial(browser)
        joined = ''.join(self.cap.flat)
        self.assertNotIn('\033[?2026h', joined)
        self.assertNotIn('\033[?2026l', joined)


class TestSeparatorCacheZeroBytes(unittest.TestCase):
    """Separator + info-bar repaints with unchanged rect emit zero bytes.

    Wires the real ``begin_row`` / ``end_row`` shim from 020-terminal.py
    against a stdout capture, then paints a vertical separator and an
    info bar twice and verifies the second paint emits no bytes (the
    "skip separator redraw when layout unchanged" optimization promised
    by #188 — falls out automatically from the row-buffer cache).
    """

    def setUp(self):
        # Load the real terminal module and graft its shim onto _render
        # so render_separator / render_info_bar exercise the cache path.
        self._terminal = _loader.load(
            '_browse_tui_terminal_188', '020-terminal.py')
        self._saved = {}
        for name in ('move', 'write', 'set_style', 'reset_style',
                     'clear_line', 'clear_columns', 'begin_row', 'end_row'):
            self._saved[name] = getattr(_render, name, None)
            setattr(_render, name, getattr(self._terminal, name))
        # Stdout capture: real shim emits via sys.stdout.write on miss.
        self._orig_stdout = sys.stdout
        self._stdout = io.StringIO()
        sys.stdout = self._stdout
        # Defensive: clear shim capture state.
        self._terminal._row_capture_active = False
        self._terminal._row_buf = []
        self._terminal._row_meta = None

    def tearDown(self):
        sys.stdout = self._orig_stdout
        for name, value in self._saved.items():
            if value is None:
                if hasattr(_render, name):
                    delattr(_render, name)
            else:
                setattr(_render, name, value)

    def _drain(self):
        text = self._stdout.getvalue()
        self._stdout.truncate(0)
        self._stdout.seek(0)
        return text

    _reconcile = staticmethod(_reconcile)

    def test_vertical_separator_zero_bytes_on_second_paint(self):
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=20, top=2, right=21, bottom=8)  # height=6, width=1

        # First paint: cache empty → emits.
        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        first = self._drain()
        self.assertIn('│', first, 'first paint must emit the bar glyphs')

        # Cache populated for all 6 rows.
        cache = browser._pane_cache['sep_main']
        self.assertEqual(len(cache.lines), 6)
        for i, line in enumerate(cache.lines):
            self.assertIsNotNone(line, f'lines[{i}] should be cached')

        # Second paint with the same rect → zero bytes.
        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        second = self._drain()
        self.assertEqual(second, '',
                         f'second paint must emit nothing, got {second!r}')

    def test_horizontal_separator_zero_bytes_on_second_paint(self):
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=1, top=10, right=21, bottom=11)  # height=1, width=20

        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        self.assertIn('─', self._drain())
        cache = browser._pane_cache['sep_main']
        self.assertIsNotNone(cache.lines[0])

        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        self.assertEqual(self._drain(), '')

    def test_horizontal_separator_multi_row_paints_every_row(self):
        """Regression for #226: tall horizontal rect paints all rows.

        Pre-fix the cached horizontal path called ``begin_row`` only
        for ``rel_row=0``; rows 1..n-1 of the cache stayed ``None``,
        which both leaks the zero-byte invariant on subsequent paints
        and silently swallows any future multi-row caller's bar
        glyphs. The fix loops over ``rect.height`` and paints every
        row, mirroring the vertical-cached branch.
        """
        browser = _MockBrowser(_MockState([]))
        # height=2, width=10 — every row should carry ``─`` glyphs.
        rect = Rect(left=1, top=5, right=11, bottom=7)

        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        first = self._drain()
        self.assertEqual(
            first.count('─'), 20,
            f'both rows of a height=2 rect must emit 10 bar glyphs '
            f'each (20 total); got {first.count("─")} in {first!r}',
        )

        cache = browser._pane_cache['sep_main']
        self.assertEqual(len(cache.lines), 2)
        for i, line in enumerate(cache.lines):
            self.assertIsNotNone(
                line, f'cache.lines[{i}] must be populated after first paint',
            )

        # Second paint with the same rect → zero bytes, proving every
        # row is in the cache (otherwise rows 1..n-1 would re-emit).
        self._reconcile(browser, 'sep_main', rect)
        _render.render_separator(rect, cache_key='sep_main', browser=browser)
        self.assertEqual(
            self._drain(), '',
            'second paint of a multi-row horizontal separator must emit '
            'zero bytes (all rows must participate in the cache)',
        )

    def test_info_bar_zero_bytes_on_second_paint(self):
        browser = _MockBrowser(_MockState([]))

        # First paint at row 24, cols=80. ``render_info_bar`` builds an
        # implicit one-row rect spanning [1, cols+1).
        rect = Rect(left=1, top=24, right=81, bottom=25)
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Preview', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        first = self._drain()
        self.assertIn('Preview', first,
                      'first paint must emit the label')
        cache = browser._pane_cache['info_bar']
        self.assertIsNotNone(cache.lines[0],
                             'info_bar cache must be populated')

        # Second paint, same row/cols/label/state → zero bytes.
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Preview', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        self.assertEqual(self._drain(), '')

    def test_info_bar_no_redundant_clear_in_captured_row(self):
        """Cache-miss info-bar paint must not include `\\e[2K` in its row.

        Regression test for #225: ``render_info_bar`` used to call
        ``move(row, 1)`` + ``clear_line()`` unconditionally. Inside a
        ``begin_row`` capture those calls land in the row buffer, so
        the emitted bytes for a cache miss carried a redundant
        ``\\e[24;1H\\e[2K`` prefix — wasted bytes on every miss. The
        fix gates them behind ``if not use_cache:``.
        """
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=1, top=24, right=81, bottom=25)
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Preview', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        emitted = self._drain()
        self.assertIn('Preview', emitted,
                      'first paint must emit the label')
        # The leading position cue is end_row's `\e[24;1H`, not a
        # second cursor-move from inside the captured buffer.
        self.assertEqual(
            emitted.count('\033[24;1H'), 1,
            f'expected exactly one cursor-move to (24,1); got {emitted!r}',
        )
        self.assertNotIn(
            '\033[2K', emitted,
            f'captured row must not carry a redundant \\e[2K; got {emitted!r}',
        )

    def test_info_bar_relabel_overwrites_prior_cells_without_clear_line(self):
        """End-to-end: a different-label repaint replaces the prior cells.

        The info bar always fills to ``cols`` (label + ``─`` glyphs), so
        the captured visible_len is constant and ``end_row``'s shrink
        branch doesn't fire. The safety property here is simpler: the
        new buffer overwrites the same cells the old one occupied, so
        removing the captured ``\\e[2K`` is a pure-bytes win — no stale
        characters can survive.
        """
        browser = _MockBrowser(_MockState([]))
        rect = Rect(left=1, top=24, right=81, bottom=25)

        # First paint with a long pane label.
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'A Very Long Label', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        first = self._drain()
        self.assertIn('A Very Long Label', first)

        # Second paint with a different label — cache miss, but the new
        # buffer covers the same cells.
        self._reconcile(browser, 'info_bar', rect)
        _render.render_info_bar(
            24, 80, 'Hi', info=False, browser=browser,
            rightmost=True, manage_cache=True,
        )
        second = self._drain()
        self.assertIn('Hi', second)
        self.assertNotIn(
            'A Very Long Label', second,
            'relabel repaint must not contain the prior longer label',
        )
        # No stray \e[2K in the captured payload — the whole point of
        # the fix.
        self.assertNotIn(
            '\033[2K', second,
            f'captured row must not carry \\e[2K; got {second!r}',
        )
        # Cache stores the visible_len and it equals ``cols`` (the bar
        # always pads to full width), confirming end_row's shrink
        # branch can't surface a stale-cell window in the steady state.
        cache = browser._pane_cache['info_bar']
        stored_visible, _stored_buf = cache.lines[0]
        self.assertEqual(
            stored_visible, 80,
            f'info_bar visible_len should equal cols=80; got {stored_visible}',
        )

    def test_separator_rect_change_invalidates_cache(self):
        """Sanity check: changing the rect drops the zero-byte invariant.

        Confirms the cache-hit gate keys on rect (height) — a new height
        means the cache is reshaped and the next paint emits.
        """
        browser = _MockBrowser(_MockState([]))
        rect_a = Rect(left=20, top=2, right=21, bottom=8)
        rect_b = Rect(left=20, top=2, right=21, bottom=10)  # taller

        self._reconcile(browser, 'sep_main', rect_a)
        _render.render_separator(rect_a, cache_key='sep_main', browser=browser)
        self._drain()
        self._reconcile(browser, 'sep_main', rect_b)
        _render.render_separator(rect_b, cache_key='sep_main', browser=browser)
        self.assertNotEqual(self._drain(), '',
                            'rect change must force emission')


if __name__ == '__main__':
    unittest.main()
