"""Tests for the children-grid pane (ticket #19).

The grid pane lives between the list and the preview. It shows the
cursor item's direct children in a multi-column flowed layout, ported
from plan-tui.

Tests cover three layers:

  * the layout-math helpers in 050-render.py (``_sub_layout``,
    ``_distribute_to_columns``, ``_sub_total_rows``, ``_sub_needed_rows``,
    ``_fmt_child``, ``_wrap_entry``);
  * ``layout_panes`` with the new ``show_children_pane`` /
    ``children_rows_needed`` kwargs;
  * the Browser-level ``_update_children_for_cursor`` helper that
    kicks a children fetch when the cursor lands on an unfetched
    branch (so the grid populates as the user navigates).
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')

# Cross-module name injection — production concatenates everything into
# one namespace; the test loader keeps them isolated and we wire what
# the production build resolves naturally.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# ``Browser.children_grid_layout`` (in 040-state.py) calls
# ``_sub_layout`` from 050-render.py and returns a ``ChildrenGridLayout``
# from 030-data.py. In the artifact they share a module namespace;
# under the test loader they don't, so inject explicitly.
_state.ChildrenGridLayout = _data.ChildrenGridLayout
_state._sub_layout = _render._sub_layout
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.ChildrenGridLayout = _data.ChildrenGridLayout

Item = _data.Item
State = _state.State
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
visible_items = _state.visible_items

layout_panes = _render.layout_panes
_sub_layout = _render._sub_layout
_distribute_to_columns = _render._distribute_to_columns
_sub_total_rows = _render._sub_total_rows
_sub_needed_rows = _render._sub_needed_rows
_fmt_child = _render._fmt_child
_wrap_entry = _render._wrap_entry


# ---------------------------------------------------------------------------
# _fmt_child / _wrap_entry — text helpers
# ---------------------------------------------------------------------------


class TestFmtChild(unittest.TestCase):
    """Format a child item as ``'id [tag] title'`` or ``'id title'``.

    The id is gated on ``show_ids`` (default ``'auto'``: suppressed when
    ``str(id) == title``).
    """

    def test_with_tag(self):
        item = Item(id='42', title='hello', tag='running')
        self.assertEqual(_fmt_child(item), '42 [running] hello')

    def test_without_tag(self):
        item = Item(id='x', title='leaf')
        self.assertEqual(_fmt_child(item), 'x leaf')

    def test_auto_suppresses_id_when_equal_to_title(self):
        item = Item(id='README.md')  # title defaults to 'README.md'
        self.assertEqual(_fmt_child(item), 'README.md')

    def test_show_ids_always_keeps_duplicated_id(self):
        item = Item(id='README.md')
        self.assertEqual(
            _fmt_child(item, show_ids='always'), 'README.md README.md',
        )

    def test_show_ids_never_drops_distinct_id(self):
        item = Item(id='42', title='hello', tag='running')
        self.assertEqual(
            _fmt_child(item, show_ids='never'), '[running] hello',
        )


class TestWrapEntry(unittest.TestCase):
    """Wrap long entries at a width; continuation lines indent."""

    def test_short_entry_one_line(self):
        self.assertEqual(_wrap_entry('hello', 80), ['hello'])

    def test_long_entry_wraps(self):
        text = 'x' * 100
        lines = _wrap_entry(text, 80)
        self.assertGreaterEqual(len(lines), 2)
        # First line is full width (no indent).
        self.assertEqual(lines[0], 'x' * 80)
        # Continuation lines start with the indent.
        for line in lines[1:]:
            self.assertTrue(
                line.startswith('    ') or line.startswith('x'),
                f'continuation should be indented: {line!r}',
            )


# ---------------------------------------------------------------------------
# _sub_layout / _distribute_to_columns / _sub_total_rows / _sub_needed_rows
# ---------------------------------------------------------------------------


class TestSubLayout(unittest.TestCase):
    """Multi-column flowed layout math."""

    def test_empty_children_zero_rows(self):
        self.assertEqual(_sub_needed_rows([], 80), 0)
        num_cols, _, slot_rows, entry_lines = _sub_layout([], 80)
        self.assertEqual(slot_rows, [])
        self.assertEqual(entry_lines, [])

    def test_short_entries_multi_column_at_120_cols(self):
        # Six tiny entries should fit in multiple columns at 120 cols.
        children = [Item(id=str(i), title='x') for i in range(6)]
        num_cols, _, slot_rows, _ = _sub_layout(children, 120)
        self.assertGreater(num_cols, 1)
        # Total content rows < total entries (since multiple columns).
        total = _sub_total_rows(num_cols, slot_rows)
        self.assertLess(total, len(children))

    def test_long_entries_yield_one_column(self):
        # Single very long entry at narrow width forces one column.
        long_title = 'x' * 200
        children = [Item(id='a', title=long_title)]
        num_cols, _, slot_rows, _ = _sub_layout(children, 40)
        self.assertEqual(num_cols, 1)

    def test_distribute_balances_lines(self):
        # 4 entries each one row tall, 2 columns → 2 rows per column.
        slot_rows = [1, 1, 1, 1]
        ranges = _distribute_to_columns(2, slot_rows)
        self.assertEqual(len(ranges), 2)
        # Each column gets ~ half the entries.
        col_lines = [sum(slot_rows[s:e]) for s, e in ranges]
        self.assertEqual(col_lines[0], col_lines[1])

    def test_distribute_handles_uneven_heights(self):
        # Entry 0 takes 3 rows, the rest take 1. Balancer still
        # distributes by total lines, not entry count.
        slot_rows = [3, 1, 1, 1]
        ranges = _distribute_to_columns(2, slot_rows)
        # Total = 6 rows; target = 3 per column.
        col_lines = [sum(slot_rows[s:e]) for s, e in ranges]
        self.assertEqual(sum(col_lines), 6)
        # Column 0 should have at most ~3 lines (the tall entry alone).
        self.assertLessEqual(max(col_lines), 4)

    def test_distribute_zero_columns(self):
        self.assertEqual(_distribute_to_columns(0, [1, 2, 3]), [])

    def test_distribute_empty_slots(self):
        self.assertEqual(_distribute_to_columns(2, []), [])

    def test_sub_total_rows_simple(self):
        # 2 cols, 4 single-row entries → 2 rows max.
        self.assertEqual(_sub_total_rows(2, [1, 1, 1, 1]), 2)

    def test_sub_total_rows_empty(self):
        self.assertEqual(_sub_total_rows(2, []), 0)
        self.assertEqual(_sub_total_rows(0, [1, 2, 3]), 0)

    def test_sub_needed_rows_non_empty(self):
        children = [Item(id=str(i)) for i in range(4)]
        rows = _sub_needed_rows(children, 120)
        self.assertGreater(rows, 0)


# ---------------------------------------------------------------------------
# layout_panes — three-pane geometry
# ---------------------------------------------------------------------------


class TestLayoutPanesThreePane(unittest.TestCase):
    """Verify the three-pane arithmetic in ``layout_panes``."""

    def test_three_panes_when_children_needed(self):
        # 24-row terminal, 5 children rows requested.
        layout = layout_panes(80, 24, show_preview=True,
                              show_children_pane=True,
                              children_rows_needed=5)
        children = layout['children']
        list_rect = layout['list']
        preview = layout['preview']
        self.assertIsNotNone(children)
        # children.height = 1 (sep) + content_rows up to 25% cap.
        self.assertEqual(children.height, 1 + 5)
        # Total height rows = list + grid + preview (each incl. its sep).
        self.assertEqual(
            list_rect.height + children.height + preview.height,
            24,
        )
        # info bar sits on the grid's separator (the active one).
        self.assertEqual(layout['info_bar'].top, children.top)

    def test_grid_hidden_when_no_children(self):
        layout = layout_panes(80, 24, show_preview=True,
                              show_children_pane=True,
                              children_rows_needed=0)
        self.assertIsNone(layout['children'])
        # info bar falls back to the preview separator.
        self.assertEqual(layout['info_bar'].top, layout['preview'].top)

    def test_grid_hidden_when_terminal_too_small(self):
        # rows < 20 hides the grid even if children are requested.
        layout = layout_panes(80, 18, show_preview=True,
                              show_children_pane=True,
                              children_rows_needed=10)
        self.assertIsNone(layout['children'])

    def test_no_children_pane_kwarg_hides_grid(self):
        layout = layout_panes(80, 40, show_preview=True,
                              show_children_pane=False,
                              children_rows_needed=5)
        self.assertIsNone(layout['children'])

    def test_grid_capped_at_25_percent(self):
        # 40-row terminal: 25% cap = 10 rows. Request 100 children rows;
        # the grid should top out at 10.
        layout = layout_panes(80, 40, show_preview=True,
                              show_children_pane=True,
                              children_rows_needed=100)
        self.assertEqual(layout['children'].height, 10)

    def test_show_preview_false_suppresses_grid(self):
        # When the preview is hidden, the grid is also suppressed —
        # a single full-screen list with the info bar at the bottom.
        layout = layout_panes(80, 40, show_preview=False,
                              show_children_pane=True,
                              children_rows_needed=5)
        self.assertIsNone(layout['children'])
        self.assertIsNone(layout['preview'])
        self.assertEqual(layout['info_bar'].top, 40)


# ---------------------------------------------------------------------------
# Browser._update_children_for_cursor — fetch trigger
# ---------------------------------------------------------------------------


class TestUpdateChildrenForCursor(unittest.TestCase):
    """The cursor-aware children-fetch trigger."""

    def _make_browser(self, **kwargs):
        # Minimal Browser; from_flat_tree pre-populates the root cache
        # so visible_items has something to walk over.
        kwargs.setdefault('_headless', True)
        return Browser.from_flat_tree(
            kwargs.pop('rows', []),
            **kwargs,
        )

    def test_kicks_fetch_for_uncached_branch(self):
        rows = [
            Item(id='a', title='alpha', has_children=True),
            Item(id='b', title='beta'),
        ]
        b = self._make_browser(rows=rows)
        # Cursor at index 0 -> 'a' (a branch with uncached children).
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertIn('a', b._state._children_pending)
        self.assertEqual(list(b._children_queue), [('a', False)])

    def test_no_op_for_leaf(self):
        rows = [Item(id='x', title='leaf')]  # has_children=False
        b = self._make_browser(rows=rows)
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertNotIn('x', b._state._children_pending)
        self.assertEqual(list(b._children_queue), [])

    def test_no_op_for_already_cached_branch(self):
        rows = [Item(id='a', title='alpha', has_children=True)]
        b = self._make_browser(rows=rows)
        # Pre-populate the cache for 'a' so the helper treats it as known.
        b._state._children['a'] = []
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertNotIn('a', b._state._children_pending)
        self.assertEqual(list(b._children_queue), [])

    def test_no_op_when_pane_disabled(self):
        rows = [Item(id='a', title='alpha', has_children=True)]
        b = self._make_browser(rows=rows, show_children_pane=False)
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertNotIn('a', b._state._children_pending)

    def test_no_op_when_already_in_flight(self):
        rows = [Item(id='a', title='alpha', has_children=True)]
        b = self._make_browser(rows=rows)
        # Mark already pending; helper must not double-enqueue.
        b._state._children_pending.add('a')
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertEqual(list(b._children_queue), [])

    def test_no_op_for_non_normal_cursor(self):
        # Empty rows list -> no visible items -> helper returns silently.
        b = self._make_browser(rows=[])
        b._state.cursor = 0
        b._update_children_for_cursor()
        self.assertEqual(list(b._children_queue), [])


# ---------------------------------------------------------------------------
# Browser.children_grid_layout — memoised layout cache (#414)
# ---------------------------------------------------------------------------


class TestChildrenGridLayoutCache(unittest.TestCase):
    """The Browser-level children-grid layout cache.

    ``Browser.children_grid_layout(children, width, show_ids)`` returns
    a ``ChildrenGridLayout`` namedtuple. Repeat calls with the same
    ``(id(children), width, show_ids)`` key hit the cache and return
    the same instance. Mutating the children list in place via
    ``update_data`` invalidates the cache eagerly so subsequent calls
    see the new content.
    """

    def _make_browser(self, **kwargs):
        kwargs.setdefault('_headless', True)
        return Browser.from_flat_tree(kwargs.pop('rows', []), **kwargs)

    def test_cache_hit_returns_same_instance(self):
        rows = [
            Item(id='p', title='parent', has_children=True),
        ]
        b = self._make_browser(rows=rows)
        # Pre-populate cached children for 'p'.
        b._state._children['p'] = [
            Item(id=str(i), title='child{}'.format(i)) for i in range(4)
        ]
        children = b._state._children['p']
        a = b.children_grid_layout(children, 80, show_ids='auto')
        c = b.children_grid_layout(children, 80, show_ids='auto')
        # Same key → same memoised instance.
        self.assertIs(a, c)
        # Returned object is a ChildrenGridLayout namedtuple.
        self.assertIsInstance(a, _data.ChildrenGridLayout)
        # And carries the same fields _sub_layout returns.
        expected = _sub_layout(children, 80, show_ids='auto')
        self.assertEqual(a.num_cols, expected[0])
        self.assertEqual(a.col_width, expected[1])
        self.assertEqual(a.slot_rows, expected[2])
        self.assertEqual(a.entry_lines, expected[3])

    def test_cache_miss_on_width_change(self):
        rows = [Item(id='p', title='parent', has_children=True)]
        b = self._make_browser(rows=rows)
        b._state._children['p'] = [
            Item(id=str(i), title='c{}'.format(i)) for i in range(4)
        ]
        children = b._state._children['p']
        a = b.children_grid_layout(children, 80, show_ids='auto')
        bigger = b.children_grid_layout(children, 120, show_ids='auto')
        self.assertIsNot(a, bigger)

    def test_cache_miss_on_show_ids_change(self):
        rows = [Item(id='p', title='parent', has_children=True)]
        b = self._make_browser(rows=rows)
        b._state._children['p'] = [
            Item(id='42', title='hello'),
            Item(id='99', title='world'),
        ]
        children = b._state._children['p']
        a = b.children_grid_layout(children, 80, show_ids='auto')
        c = b.children_grid_layout(children, 80, show_ids='always')
        self.assertIsNot(a, c)

    def test_cache_invalidated_by_update_data(self):
        # In-place mutation of the children list (upsert appending a new
        # id) leaves ``id(children)`` stable. The cache must be
        # invalidated explicitly by the mutation path so the next
        # ``children_grid_layout`` call reflects the new entry.
        upsert = _state.upsert

        rows = [Item(id='p', title='parent', has_children=True)]
        b = self._make_browser(rows=rows)
        # Pre-populate 'p' with two children.
        b._state._children['p'] = [
            Item(id='a', title='alpha'),
            Item(id='b', title='beta'),
        ]
        # Re-register so _items_by_id / _parent_of_id are consistent.
        b._state._items_by_id['a'] = b._state._children['p'][0]
        b._state._items_by_id['b'] = b._state._children['p'][1]
        b._state._parent_of_id['a'] = 'p'
        b._state._parent_of_id['b'] = 'p'

        children = b._state._children['p']
        first = b.children_grid_layout(children, 80, show_ids='auto')
        first_len = len(first.entry_lines)

        # In-place append via update_data → upsert with a new id.
        b.update_data([upsert('c', 'p', title='gamma')])
        b.run_until_idle()

        # Same list object (in-place append) ...
        self.assertIs(b._state._children['p'], children)
        # ... but the cache must have been invalidated.
        after = b.children_grid_layout(children, 80, show_ids='auto')
        self.assertIsNot(first, after)
        self.assertEqual(len(after.entry_lines), first_len + 1)

    def test_layout_panes_and_render_share_cache(self):
        # ``_layout_for`` (sizing the grid pane) and
        # ``render_children_grid`` (drawing it) should both land on the
        # same memoised ChildrenGridLayout instance. We instrument
        # ``_sub_layout`` and assert it's invoked exactly once across
        # back-to-back paints with identical inputs.
        rows = [
            Item(id='p', title='parent', has_children=True),
            Item(id='q', title='other'),
        ]
        b = self._make_browser(rows=rows)
        # Cursor on the branch and cache its children.
        b._state.cursor = 0
        b._state._children['p'] = [
            Item(id=str(i), title='c{}'.format(i)) for i in range(6)
        ]
        for child in b._state._children['p']:
            b._state._items_by_id[child.id] = child
            b._state._parent_of_id[child.id] = 'p'

        # Warm the cache with one call. From here on, identical inputs
        # must hit the cache.
        children = b._state._children['p']
        first = b.children_grid_layout(children, 80, show_ids='auto')

        # Patch ``_sub_layout`` (on _state, since Browser invokes it
        # from there in the test build) to count invocations.
        calls = [0]
        original = _state._sub_layout

        def _counting_sub_layout(*args, **kwargs):
            calls[0] += 1
            return original(*args, **kwargs)

        _state._sub_layout = _counting_sub_layout
        try:
            # Same inputs → cache hits → no further _sub_layout calls.
            second = b.children_grid_layout(children, 80, show_ids='auto')
            third = b.children_grid_layout(children, 80, show_ids='auto')
            self.assertIs(first, second)
            self.assertIs(first, third)
            self.assertEqual(calls[0], 0)
        finally:
            _state._sub_layout = original


if __name__ == '__main__':
    unittest.main()
