"""Pure-function tests for ``auto_insert_depth`` and ``resolve_insert``.

These ports of plan-tui's ``_auto_insert_depth`` / ``_resolve_insert``
(plan-source/src-tui/070-main.py:50-96) decide where the placement
marker lives and what ``(relation, dest_id)`` it resolves to on confirm.
The algorithm has many subtle cases — we cover them exhaustively here so
phase-2 callers can rely on the contract without re-deriving it.

Difference from plan-tui: outdent uses the visible-list ordering to
reach the ancestor at the target depth (rather than parent metadata on
items), so we test deep-tree outdent paths carefully to make sure the
walk finds the right row.
"""

import unittest

from test.unit._loader import load


_data = load('_browse_tui_data', '030-data.py')
_term = load('_browse_tui_term', '020-terminal.py')
_state = load('_browse_tui_state', '040-state.py')

# Match the cross-module wiring the concatenated build performs.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake


Item = _data.Item
VisibleEntry = _state.VisibleEntry
auto_insert_depth = _state.auto_insert_depth
resolve_insert = _state.resolve_insert


def _entry(id_, depth, kind='normal', has_children=False):
    """Shorthand: build a VisibleEntry around an Item with the given id."""
    return VisibleEntry(
        item=Item(id=id_, has_children=has_children),
        depth=depth,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# auto_insert_depth
# ---------------------------------------------------------------------------


class TestAutoInsertDepth(unittest.TestCase):

    def test_empty_list_returns_zero(self):
        self.assertEqual(auto_insert_depth(0, []), 0)
        self.assertEqual(auto_insert_depth(5, []), 0)

    def test_position_zero_returns_first_row_depth(self):
        vis = [_entry('a', 0), _entry('b', 0)]
        self.assertEqual(auto_insert_depth(0, vis), 0)

    def test_position_zero_returns_first_row_depth_nonzero(self):
        # First row at depth 1 (e.g. inside a scope).
        vis = [_entry('a', 1), _entry('b', 1)]
        self.assertEqual(auto_insert_depth(0, vis), 1)

    def test_between_two_same_depth_siblings(self):
        # gap between two siblings at depth 0 → depth 0 (sibling-after).
        vis = [_entry('a', 0), _entry('b', 0)]
        self.assertEqual(auto_insert_depth(1, vis), 0)

    def test_between_parent_and_first_child_dives_in(self):
        # gap between a depth-0 parent and its depth-1 child returns 1
        # so the marker "lands inside" the parent's subtree by default.
        vis = [_entry('p', 0, has_children=True), _entry('c', 1)]
        self.assertEqual(auto_insert_depth(1, vis), 1)

    def test_at_end_returns_last_row_depth(self):
        vis = [_entry('a', 0), _entry('b', 0)]
        self.assertEqual(auto_insert_depth(2, vis), 0)

    def test_at_end_with_deep_last_row(self):
        # End-of-list under a deep child: marker stays at the child's depth.
        vis = [
            _entry('p', 0, has_children=True),
            _entry('c', 1, has_children=True),
            _entry('gc', 2),
        ]
        self.assertEqual(auto_insert_depth(3, vis), 2)

    def test_above_below_same_depth_returns_that_depth(self):
        vis = [_entry('a', 1), _entry('b', 1), _entry('c', 1)]
        self.assertEqual(auto_insert_depth(2, vis), 1)

    def test_below_shallower_returns_above_depth(self):
        # Above is at depth 2, below is at depth 0 (subtree just ended).
        # auto_depth should stick with the above's depth.
        vis = [_entry('p', 0, has_children=True),
               _entry('c', 1, has_children=True),
               _entry('gc', 2),
               _entry('next', 0)]
        self.assertEqual(auto_insert_depth(3, vis), 2)


# ---------------------------------------------------------------------------
# resolve_insert
# ---------------------------------------------------------------------------


class TestResolveInsertEmpty(unittest.TestCase):

    def test_empty_list_returns_none_none(self):
        self.assertEqual(resolve_insert(0, 0, []), (None, None))
        self.assertEqual(resolve_insert(1, 0, []), (None, None))

    def test_pos_zero_returns_none_none(self):
        # gap 0 has no row above — invalid.
        vis = [_entry('a', 0)]
        self.assertEqual(resolve_insert(0, 0, vis), (None, None))


class TestResolveInsertSibling(unittest.TestCase):

    def test_after_first_at_root(self):
        vis = [_entry('a', 0), _entry('b', 0)]
        self.assertEqual(resolve_insert(1, 0, vis), ('after', 'a'))

    def test_after_last_at_root(self):
        vis = [_entry('a', 0), _entry('b', 0)]
        self.assertEqual(resolve_insert(2, 0, vis), ('after', 'b'))

    def test_after_sibling_inside_subtree(self):
        # parent expanded with two children; gap between children is
        # ('after', first_child) at depth 1.
        vis = [
            _entry('p', 0, has_children=True),
            _entry('c1', 1),
            _entry('c2', 1),
        ]
        self.assertEqual(resolve_insert(2, 1, vis), ('after', 'c1'))


class TestResolveInsertChild(unittest.TestCase):

    def test_first_child_of_above(self):
        # depth > above.depth → ('first', above.id).
        vis = [_entry('p', 0, has_children=True)]
        self.assertEqual(resolve_insert(1, 1, vis), ('first', 'p'))

    def test_first_child_of_branch_with_existing_children(self):
        # parent has cached children; user picks "first child" by setting
        # depth to parent.depth + 1 with marker right after the parent row.
        vis = [
            _entry('p', 0, has_children=True),
            _entry('c1', 1),
        ]
        self.assertEqual(resolve_insert(1, 1, vis), ('first', 'p'))

    def test_first_child_of_grandchild(self):
        # Going deeper than above.depth + 1 still resolves to ('first',
        # above) per plan-tui (any depth strictly greater than above
        # uses "first"). We don't bound the depth here.
        vis = [_entry('p', 0, has_children=True)]
        self.assertEqual(resolve_insert(1, 5, vis), ('first', 'p'))


class TestResolveInsertOutdent(unittest.TestCase):

    def test_outdent_to_root_after_subtree(self):
        # Marker at gap after the last grandchild; depth=0 outdent should
        # find the root parent and resolve to ('after', root_parent).
        vis = [
            _entry('p', 0, has_children=True),
            _entry('c', 1),
        ]
        # gap 2 (end), depth 0 → walk back: vis[1] depth=1 > 0, skip;
        # vis[0] depth=0 == 0 → ('after', 'p').
        self.assertEqual(resolve_insert(2, 0, vis), ('after', 'p'))

    def test_outdent_to_intermediate_depth(self):
        # Three-level tree; outdent from grandchild back to depth 1.
        vis = [
            _entry('p', 0, has_children=True),
            _entry('c1', 1, has_children=True),
            _entry('gc', 2),
        ]
        # gap 3 (end), depth 1 → walk back: vis[2] d=2 skip; vis[1] d=1 ✓
        self.assertEqual(resolve_insert(3, 1, vis), ('after', 'c1'))

    def test_outdent_no_ancestor_returns_none(self):
        # Outdent depth deeper than any earlier row's depth — invalid.
        vis = [_entry('a', 1), _entry('b', 1)]
        # gap 2, depth 0 → walk back: vis[1] d=1 > 0, vis[0] d=1 > 0,
        # never find depth 0; result is (None, None).
        self.assertEqual(resolve_insert(2, 0, vis), (None, None))

    def test_outdent_with_multiple_subtrees_finds_correct_ancestor(self):
        vis = [
            _entry('p1', 0, has_children=True),
            _entry('c1', 1),
            _entry('p2', 0, has_children=True),  # second root
            _entry('c2', 1),
        ]
        # gap 4 (end), depth 0 → walk back: vis[3] d=1 skip; vis[2] d=0 ✓
        # so we resolve to the *second* root, p2 (not p1). The walk only
        # reaches the first ancestor at the target depth, which is the
        # most recent — by tree DFS this is the closest enclosing one.
        self.assertEqual(resolve_insert(4, 0, vis), ('after', 'p2'))


class TestResolveInsertScopeRoot(unittest.TestCase):

    def test_above_scope_root_with_below_returns_before(self):
        vis = [
            _entry('S', 0, kind='scope_root'),
            _entry('a', 1),
        ]
        # gap 1, depth 1 → above is scope_root; below is 'a' → ('before', 'a').
        self.assertEqual(resolve_insert(1, 1, vis), ('before', 'a'))

    def test_above_scope_root_with_no_below_returns_none(self):
        vis = [_entry('S', 0, kind='scope_root')]
        # gap 1 (end), no below row → (None, None).
        self.assertEqual(resolve_insert(1, 0, vis), (None, None))

    def test_outdent_walking_past_scope_root_returns_none(self):
        # Don't allow outdent to land on the scope_root as an ancestor.
        vis = [
            _entry('S', 0, kind='scope_root'),
            _entry('a', 1, has_children=True),
            _entry('b', 2),
        ]
        # gap 3 (end), depth 0 → walk back: vis[2] d=2 skip; vis[1] d=1
        # skip; vis[0] d=0 but kind='scope_root' → reject → (None, None).
        self.assertEqual(resolve_insert(3, 0, vis), (None, None))


class TestResolveInsertDeepTree(unittest.TestCase):
    """A bigger tree exercise to lock down the contract end-to-end."""

    def setUp(self):
        # Tree structure (depths in parens):
        #   p1 (0) [has_children]
        #     c1 (1)
        #     c2 (1) [has_children]
        #       gc1 (2)
        #       gc2 (2)
        #     c3 (1)
        #   p2 (0)
        self.vis = [
            _entry('p1', 0, has_children=True),
            _entry('c1', 1),
            _entry('c2', 1, has_children=True),
            _entry('gc1', 2),
            _entry('gc2', 2),
            _entry('c3', 1),
            _entry('p2', 0),
        ]

    def test_after_first_root(self):
        # gap 1, depth 0 → ('after', 'p1').
        self.assertEqual(resolve_insert(1, 0, self.vis), ('after', 'p1'))

    def test_first_child_of_first_root(self):
        # gap 1, depth 1 → ('first', 'p1').
        self.assertEqual(resolve_insert(1, 1, self.vis), ('first', 'p1'))

    def test_after_first_grandchild(self):
        # gap 4 (between gc1 and gc2), depth 2 → ('after', 'gc1').
        self.assertEqual(resolve_insert(4, 2, self.vis), ('after', 'gc1'))

    def test_first_child_of_first_grandchild(self):
        # gap 4, depth 3 → ('first', 'gc1').
        self.assertEqual(resolve_insert(4, 3, self.vis), ('first', 'gc1'))

    def test_outdent_after_grandchildren_to_sibling_of_c2(self):
        # gap 5 (between gc2 and c3), depth 1 → walk back: gc2 d=2 skip;
        # c2 d=1 ✓ → ('after', 'c2').
        self.assertEqual(resolve_insert(5, 1, self.vis), ('after', 'c2'))

    def test_outdent_to_root_at_end_of_subtree(self):
        # gap 6 (just before p2), depth 0 → walk back: c3 d=1 skip;
        # c2 d=1 skip; c1 d=1 skip; p1 d=0 ✓ → ('after', 'p1').
        self.assertEqual(resolve_insert(6, 0, self.vis), ('after', 'p1'))

    def test_first_child_at_end_of_full_list(self):
        # gap 7 (end), depth 1 with above=p2 → depth > above.depth so
        # this is ('first', 'p2').
        self.assertEqual(resolve_insert(7, 1, self.vis), ('first', 'p2'))


if __name__ == '__main__':
    unittest.main()
