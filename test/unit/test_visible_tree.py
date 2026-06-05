"""Tests for the visible-tree builder in browse-tui state layer.

`visible_items(state)` walks the (lazy) `_children` cache, honours the
`expanded` set, emits a placeholder row under expanded-but-not-yet-cached
parents, and caches its output until `_visible_dirty` is flipped on.

The state module loads independently from the data module (numbered files
are concatenated for the production single-file build). For tests we
inject the real `Item` class into the state module's globals so the
pending-placeholder row can be constructed; production builds resolve
`Item` naturally via name shadowing in the concatenated source.
"""

import unittest

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Inject Item into the state module so the placeholder Item can be built.
_state.Item = _data.Item

Item = _data.Item
State = _state.State
VisibleEntry = _state.VisibleEntry
visible_items = _state.visible_items
mark_visible_dirty = _state.mark_visible_dirty
cache_invalidate_subtree = _state.cache_invalidate_subtree
cache_invalidate_all = _state.cache_invalidate_all


# --- helpers --------------------------------------------------------------


def _state_factory(root_id=None, **kw):
    s = State(root_id=root_id)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _ids(rows):
    """Compact triple-tuple representation: (id, depth, kind)."""
    return [(r.item.id, r.depth, r.kind) for r in rows]


def _kid(id_, has_children=False):
    return Item(id=id_, has_children=has_children)


# --- tests ----------------------------------------------------------------


class TestVisibleAtRoot(unittest.TestCase):
    """At root scope, visible_items walks _children[root_id]."""

    def test_empty_root_yields_no_rows(self):
        s = _state_factory(root_id=None, _children={None: []})
        self.assertEqual(visible_items(s), [])

    def test_single_root_child_at_depth_zero(self):
        a = _kid('a')
        s = _state_factory(root_id=None, _children={None: [a]})
        self.assertEqual(_ids(visible_items(s)), [('a', 0, 'normal')])

    def test_two_root_children_in_insertion_order(self):
        a = _kid('a')
        b = _kid('b')
        s = _state_factory(root_id=None, _children={None: [a, b]})
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal'), ('b', 0, 'normal')],
        )


class TestExpansion(unittest.TestCase):
    """expanded set controls whether children are surfaced."""

    def test_collapsed_parent_hides_children(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a1]},
        )
        # 'a' not in expanded — only the parent is shown.
        self.assertEqual(_ids(visible_items(s)), [('a', 0, 'normal')])

    def test_expanded_parent_with_cached_children(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a1]},
            expanded={'a'},
        )
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal'), ('a1', 1, 'normal')],
        )

    def test_nested_expansion_grandchild_at_depth_two(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1', has_children=True)
        a1x = _kid('a1x')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a1], 'a1': [a1x]},
            expanded={'a', 'a1'},
        )
        self.assertEqual(
            _ids(visible_items(s)),
            [
                ('a', 0, 'normal'),
                ('a1', 1, 'normal'),
                ('a1x', 2, 'normal'),
            ],
        )


class TestEmptyChildrenCached(unittest.TestCase):
    """An empty cached children list yields no rows under that parent."""

    def test_empty_cached_no_placeholder(self):
        a = _kid('a', has_children=True)
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': []},  # explicitly empty
            expanded={'a'},
        )
        # Just the parent — no placeholder, no children.
        self.assertEqual(_ids(visible_items(s)), [('a', 0, 'normal')])


class TestPendingPlaceholder(unittest.TestCase):
    """Expanded-but-not-cached parents emit a single placeholder row."""

    def test_expanded_parent_not_in_cache_emits_placeholder(self):
        # Parent is expanded; renderer hasn't kicked the worker yet.
        a = _kid('a', has_children=True)
        s = _state_factory(
            root_id=None,
            _children={None: [a]},  # 'a' missing from cache
            expanded={'a'},
        )
        rows = visible_items(s)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].item.id, 'a')
        self.assertEqual(rows[0].depth, 0)
        self.assertEqual(rows[1].kind, 'pending')
        self.assertEqual(rows[1].depth, 1)

    def test_expanded_parent_in_pending_set_emits_placeholder(self):
        a = _kid('a', has_children=True)
        s = _state_factory(
            root_id=None,
            _children={None: [a]},
            expanded={'a'},
            _children_pending={'a'},
        )
        rows = visible_items(s)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].kind, 'pending')
        self.assertEqual(rows[1].depth, 1)

    def test_only_one_placeholder_under_a_pending_parent(self):
        a = _kid('a', has_children=True)
        s = _state_factory(
            root_id=None,
            _children={None: [a]},
            expanded={'a'},
        )
        rows = visible_items(s)
        pending_rows = [r for r in rows if r.kind == 'pending']
        self.assertEqual(len(pending_rows), 1)


class TestDirtyCaching(unittest.TestCase):
    """visible_items caches; mutation requires explicit dirty mark."""

    def test_uses_cache_when_not_dirty(self):
        a = _kid('a')
        s = _state_factory(root_id=None, _children={None: [a]})
        first = visible_items(s)
        # _visible_dirty was True initially; after build, it's False.
        self.assertFalse(s._visible_dirty)
        # Calling again returns the same cached list (identity).
        second = visible_items(s)
        self.assertIs(first, second)

    def test_mutating_expanded_does_not_auto_invalidate(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a1]},
        )
        first = visible_items(s)
        self.assertEqual(len(first), 1)
        # Caller forgot to mark dirty after expanding.
        s.expanded.add('a')
        cached = visible_items(s)
        self.assertIs(cached, first)  # same stale list

    def test_mark_dirty_forces_rebuild(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a1]},
        )
        visible_items(s)
        s.expanded.add('a')
        mark_visible_dirty(s)
        rebuilt = visible_items(s)
        self.assertEqual(
            _ids(rebuilt),
            [('a', 0, 'normal'), ('a1', 1, 'normal')],
        )


class TestRefreshInvalidation(unittest.TestCase):
    """cache_invalidate_* drops cache and marks dirty."""

    def test_invalidate_subtree_drops_one_parent(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        b = _kid('b')
        s = _state_factory(
            root_id=None,
            _children={None: [a, b], 'a': [a1]},
            expanded={'a'},
        )
        visible_items(s)
        cache_invalidate_subtree(s, 'a')
        self.assertNotIn('a', s._children)
        self.assertIn(None, s._children)  # other entries untouched
        self.assertTrue(s._visible_dirty)

    def test_invalidate_subtree_missing_key_is_safe(self):
        # Should not raise — invalidating a never-cached parent is fine.
        s = _state_factory(root_id=None, _children={None: []})
        cache_invalidate_subtree(s, 'never-cached')
        self.assertTrue(s._visible_dirty)

    def test_invalidate_all_clears_cache(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a1]},
            expanded={'a'},
        )
        visible_items(s)
        cache_invalidate_all(s)
        self.assertEqual(s._children, {})
        self.assertTrue(s._visible_dirty)


class TestInsertionOrder(unittest.TestCase):
    """Visible order matches _children list order."""

    def test_children_emitted_in_list_order(self):
        a = _kid('a', has_children=True)
        a1 = _kid('a1')
        a2 = _kid('a2')
        a3 = _kid('a3')
        s = _state_factory(
            root_id=None,
            _children={None: [a], 'a': [a3, a1, a2]},  # deliberately odd
            expanded={'a'},
        )
        rows = visible_items(s)
        self.assertEqual(
            [r.item.id for r in rows[1:]],
            ['a3', 'a1', 'a2'],
        )


class TestHiddenFlag(unittest.TestCase):
    """``hidden=True`` excludes a row and its subtree at render time."""

    def test_hidden_leaf_omitted(self):
        a = _kid('a')
        b = Item(id='b', hidden=True)
        c = _kid('c')
        s = _state_factory(root_id=None, _children={None: [a, b, c]})
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal'), ('c', 0, 'normal')],
        )

    def test_hidden_parent_hides_subtree(self):
        parent = Item(id='p', has_children=True, hidden=True)
        child = _kid('c')
        s = _state_factory(
            root_id=None,
            _children={None: [parent], 'p': [child]},
            expanded={'p'},
        )
        self.assertEqual(visible_items(s), [])

    def test_hidden_parent_with_visible_sibling(self):
        a = _kid('a')
        hidden_parent = Item(id='b', has_children=True, hidden=True)
        c = _kid('c')
        sub1 = _kid('b1')
        sub2 = _kid('b2')
        s = _state_factory(
            root_id=None,
            _children={None: [a, hidden_parent, c], 'b': [sub1, sub2]},
            expanded={'b'},
        )
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal'), ('c', 0, 'normal')],
        )

    def test_hidden_grandchild(self):
        # Hidden grandchild is omitted; siblings of grandchild emitted.
        parent = _kid('p', has_children=True)
        sub_visible = _kid('s1')
        sub_hidden = Item(id='s2', hidden=True)
        sub_visible2 = _kid('s3')
        s = _state_factory(
            root_id=None,
            _children={
                None: [parent],
                'p': [sub_visible, sub_hidden, sub_visible2],
            },
            expanded={'p'},
        )
        self.assertEqual(
            _ids(visible_items(s)),
            [
                ('p', 0, 'normal'),
                ('s1', 1, 'normal'),
                ('s3', 1, 'normal'),
            ],
        )

    def test_unhiding_parent_restores_child_state(self):
        # Per spec: render-only cascade. Child's own ``hidden`` is
        # preserved while the parent is hidden; flipping the parent
        # back doesn't disturb child state.
        parent = Item(id='p', has_children=True, hidden=True)
        child_visible = _kid('c1')
        child_hidden = Item(id='c2', hidden=True)
        s = _state_factory(
            root_id=None,
            _children={
                None: [parent],
                'p': [child_visible, child_hidden],
            },
            expanded={'p'},
        )
        # Initially hidden — nothing visible.
        self.assertEqual(visible_items(s), [])
        # Flip parent visible.
        parent.hidden = False
        mark_visible_dirty(s)
        # Child2 stays hidden; child1 reappears.
        self.assertEqual(
            _ids(visible_items(s)),
            [('p', 0, 'normal'), ('c1', 1, 'normal')],
        )

    def test_hidden_root_child_with_pending(self):
        # Hidden expandable + expanded → pending placeholder is not
        # emitted (the whole subtree is skipped).
        hidden_parent = Item(id='p', has_children=True, hidden=True)
        s = _state_factory(
            root_id=None,
            _children={None: [hidden_parent]},
            expanded={'p'},
        )
        # 'p' is expanded but the cache for 'p' is empty (no entry) —
        # would normally emit a pending placeholder under it. Hidden
        # ancestor skips the whole subtree.
        self.assertEqual(visible_items(s), [])


class TestFilterHiddenFlag(unittest.TestCase):
    """``_filter_hidden`` per-row flag drops the row from the visible list.

    Unlike ``hidden``, ``_filter_hidden`` is per-row (not a subtree
    cascade) — but the bottom-up evaluator only flags a row when its
    entire subtree fails, so the effect is equivalent. These tests
    exercise the renderer's skip logic with hand-set flags.
    """

    def test_inert_when_filter_active_false(self):
        # Flag set but ``_filter_active`` not set -> row still visible.
        a = _kid('a')
        a._filter_hidden = True
        s = _state_factory(root_id=None, _children={None: [a]})
        self.assertFalse(s._filter_active)
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal')],
        )

    def test_filter_active_drops_flagged_row(self):
        a = _kid('a')
        b = _kid('b')
        b._filter_hidden = True
        c = _kid('c')
        s = _state_factory(root_id=None, _children={None: [a, b, c]})
        s._filter_active = True
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal'), ('c', 0, 'normal')],
        )

    def test_filter_keeps_matching_descendant_when_parent_unflagged(self):
        # Parent ``_filter_hidden=False`` (scaffold), child
        # ``_filter_hidden=False`` too: both render.
        parent = _kid('p', has_children=True)
        child = _kid('c')
        s = _state_factory(
            root_id=None,
            _children={None: [parent], 'p': [child]},
            expanded={'p'},
        )
        s._filter_active = True
        self.assertEqual(
            _ids(visible_items(s)),
            [('p', 0, 'normal'), ('c', 1, 'normal')],
        )

    def test_hidden_takes_precedence_over_filter(self):
        # ``hidden=True`` short-circuits before the filter check, so a
        # filter-passing recipe-hidden row is still hidden.
        a = Item(id='a', hidden=True)
        # No filter flag — the row passes the filter but is hidden by
        # the recipe-owned ``hidden`` field.
        s = _state_factory(root_id=None, _children={None: [a]})
        s._filter_active = True
        self.assertEqual(visible_items(s), [])


class TestMetaKind(unittest.TestCase):
    """``meta=True`` yields ``kind='meta'`` and is always a leaf."""

    def test_meta_leaf_yields_meta_kind(self):
        a = _kid('a')
        sep = Item(id='sep', title='── divider ──', meta=True)
        b = _kid('b')
        s = _state_factory(root_id=None, _children={None: [a, sep, b]})
        self.assertEqual(
            _ids(visible_items(s)),
            [('a', 0, 'normal'), ('sep', 0, 'meta'), ('b', 0, 'normal')],
        )

    def test_meta_with_children_not_recursed(self):
        # A meta row carrying has_children + cached children is still a
        # leaf: its children are never emitted.
        sep = Item(id='sep', title='hdr', meta=True, has_children=True)
        child = _kid('hidden-child')
        s = _state_factory(
            root_id=None,
            _children={None: [sep], 'sep': [child]},
        )
        self.assertEqual(_ids(visible_items(s)), [('sep', 0, 'meta')])

    def test_meta_in_expanded_set_not_recursed(self):
        # Even forced into the expanded set, a meta row does not recurse
        # (meta short-circuits before the expansion check).
        sep = Item(id='sep', title='hdr', meta=True, has_children=True)
        child = _kid('hidden-child')
        s = _state_factory(
            root_id=None,
            _children={None: [sep], 'sep': [child]},
            expanded={'sep'},
        )
        self.assertEqual(_ids(visible_items(s)), [('sep', 0, 'meta')])

    def test_meta_expanded_emits_no_pending_placeholder(self):
        # Expanded + has_children but no cached children would normally
        # emit a 'pending' placeholder; a meta row suppresses recursion
        # entirely, so no placeholder appears either.
        sep = Item(id='sep', title='hdr', meta=True, has_children=True)
        s = _state_factory(
            root_id=None,
            _children={None: [sep]},  # 'sep' missing from cache
            expanded={'sep'},
        )
        self.assertEqual(_ids(visible_items(s)), [('sep', 0, 'meta')])

    def test_mixed_normal_meta_ordering_preserved(self):
        a = _kid('a')
        s1 = Item(id='s1', meta=True)
        b = _kid('b')
        s2 = Item(id='s2', meta=True)
        c = _kid('c')
        s = _state_factory(
            root_id=None,
            _children={None: [a, s1, b, s2, c]},
        )
        self.assertEqual(
            _ids(visible_items(s)),
            [
                ('a', 0, 'normal'),
                ('s1', 0, 'meta'),
                ('b', 0, 'normal'),
                ('s2', 0, 'meta'),
                ('c', 0, 'normal'),
            ],
        )

    def test_meta_nested_under_expanded_normal_parent(self):
        # A meta divider emitted among a normal parent's children gets
        # the right depth and kind, and the normal siblings still recurse.
        p = _kid('p', has_children=True)
        c1 = _kid('c1')
        sep = Item(id='sep', meta=True)
        c2 = _kid('c2')
        s = _state_factory(
            root_id=None,
            _children={None: [p], 'p': [c1, sep, c2]},
            expanded={'p'},
        )
        self.assertEqual(
            _ids(visible_items(s)),
            [
                ('p', 0, 'normal'),
                ('c1', 1, 'normal'),
                ('sep', 1, 'meta'),
                ('c2', 1, 'normal'),
            ],
        )


if __name__ == '__main__':
    unittest.main()
