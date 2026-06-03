"""Tests for the public collapse_all / expand_subtree API."""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_context = load('_browse_tui_context', '060-context.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_context.visible_items = _state.visible_items

Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context


def _seed_tree(b):
    """Build:  a (branch) -> a1 (branch) -> a1x (leaf)
                          -> a2 (leaf)
               b (branch) -> b1 (leaf)
    """
    b.update_data([
        ('upsert', 'a', None, {'has_children': True}),
        ('upsert', 'b', None, {'has_children': True}),
        ('upsert', 'a1', 'a', {'has_children': True}),
        ('upsert', 'a2', 'a', {}),
        ('upsert', 'a1x', 'a1', {}),
        ('upsert', 'b1', 'b', {}),
    ])
    b.drain_main_queue()


class TestCollapseAll(unittest.TestCase):

    def test_clears_expanded_set(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.expanded.update({'a', 'a1', 'b'})
        b.collapse_all()
        b.drain_main_queue()
        self.assertEqual(b._state.expanded, set())

    def test_already_collapsed_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        # Nothing expanded — call must not raise.
        b.collapse_all()
        b.drain_main_queue()
        self.assertEqual(b._state.expanded, set())

    def test_visible_list_reflects_collapse(self):
        # 'a' expanded → a1 and a2 are visible. After collapse_all,
        # only top-level rows remain visible.
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.expanded.add('a')
        _state.mark_visible_dirty(b._state)
        before = [e.item.id for e in _state.visible_items(b._state)
                  if e.kind == 'normal']
        self.assertIn('a1', before)
        b.collapse_all()
        b.drain_main_queue()
        after = [e.item.id for e in _state.visible_items(b._state)
                 if e.kind == 'normal']
        self.assertNotIn('a1', after)
        self.assertIn('a', after)

    def test_scope_stack_unchanged(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.scope_stack = ['a']
        b.collapse_all()
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, ['a'])


class TestCollapse(unittest.TestCase):

    def test_removes_id_from_expanded(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.expanded.update({'a', 'b'})
        b.collapse('a')
        b.drain_main_queue()
        self.assertNotIn('a', b._state.expanded)
        # Sibling expansion is untouched.
        self.assertIn('b', b._state.expanded)

    def test_already_collapsed_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        # 'a' is not in the expanded set — collapsing must not raise.
        b.collapse('a')
        b.drain_main_queue()
        self.assertNotIn('a', b._state.expanded)

    def test_visible_list_folds_subtree(self):
        # 'a' expanded → a1 and a2 are visible. After collapse('a'),
        # those child rows fold away while 'a' itself stays visible.
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.expanded.add('a')
        _state.mark_visible_dirty(b._state)
        before = [e.item.id for e in _state.visible_items(b._state)
                  if e.kind == 'normal']
        self.assertIn('a1', before)
        b.collapse('a')
        b.drain_main_queue()
        after = [e.item.id for e in _state.visible_items(b._state)
                 if e.kind == 'normal']
        self.assertNotIn('a1', after)
        self.assertIn('a', after)


class TestExpandSubtree(unittest.TestCase):

    def test_adds_id_and_descendants(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b.expand_subtree('a')
        b.drain_main_queue()
        # 'a' is added; 'a1' is added (branch); 'a2' is leaf, not added
        # (the helper only adds parents); 'a1x' is leaf too.
        self.assertIn('a', b._state.expanded)
        self.assertIn('a1', b._state.expanded)
        self.assertNotIn('a2', b._state.expanded)
        self.assertNotIn('a1x', b._state.expanded)

    def test_does_not_touch_other_branches(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b.expand_subtree('a')
        b.drain_main_queue()
        self.assertNotIn('b', b._state.expanded)
        self.assertNotIn('b1', b._state.expanded)

    def test_uncached_branch_not_recursed(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        # Remove 'a1' from the children cache to simulate an
        # un-fetched branch.
        b._state._children.pop('a1', None)
        b.expand_subtree('a')
        b.drain_main_queue()
        # 'a' is added; 'a1' is added too (it's in 'a's children
        # list — a branch). But the helper doesn't recurse into 'a1'
        # because its children aren't cached.
        self.assertIn('a', b._state.expanded)
        self.assertIn('a1', b._state.expanded)

    def test_idempotent(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b.expand_subtree('a')
        b.drain_main_queue()
        before = set(b._state.expanded)
        b.expand_subtree('a')
        b.drain_main_queue()
        self.assertEqual(b._state.expanded, before)


def _seed_boundary_tree(b, *, boundary):
    """Build:  a (branch) -> bnd (branch) -> bnd_kid (branch) -> bnd_leaf
                                          -> bnd_kid2 (leaf)

    ``bnd``'s subtree is fully cached (every level's children list is in
    ``_children``). When ``boundary=True`` is set on ``bnd``, a recursive
    expand of the ancestor ``a`` must reach ``bnd`` but stop there — even
    though ``bnd_kid`` is a cached branch. ``boundary=False`` is the
    control: ``bnd_kid`` then expands like any other cached branch.
    """
    b.update_data([
        ('upsert', 'a', None, {'has_children': True}),
        ('upsert', 'bnd', 'a', {'has_children': True, 'boundary': boundary}),
        ('upsert', 'bnd_kid', 'bnd', {'has_children': True}),
        ('upsert', 'bnd_kid2', 'bnd', {}),
        ('upsert', 'bnd_leaf', 'bnd_kid', {}),
    ])
    b.drain_main_queue()


class TestExpandSubtreeBoundary(unittest.TestCase):
    """``expand_subtree`` treats a ``boundary`` node as a leaf.

    Expand *to* a boundary node (it joins ``state.expanded``) but never
    *through* it — its children are not recursively expanded even when
    they are already cached.
    """

    def test_boundary_node_expanded_but_children_not(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_boundary_tree(b, boundary=True)
        # Sanity: bnd's subtree is genuinely cached (the "even when
        # cached" case the flag exists to cover).
        self.assertIn('bnd_kid', b._state._children)
        b.expand_subtree('a')
        b.drain_main_queue()
        # Expanded TO the boundary node: 'a' and 'bnd' are added.
        self.assertIn('a', b._state.expanded)
        self.assertIn('bnd', b._state.expanded)
        # NOT through it: the cached branch below 'bnd' stays collapsed.
        self.assertNotIn('bnd_kid', b._state.expanded)

    def test_non_boundary_cached_node_is_expanded(self):
        # Control: identical shape, but 'bnd' is not a boundary, so the
        # cached branch below it IS expanded recursively.
        b = Browser(BrowserConfig(_headless=True))
        _seed_boundary_tree(b, boundary=False)
        b.expand_subtree('a')
        b.drain_main_queue()
        self.assertIn('a', b._state.expanded)
        self.assertIn('bnd', b._state.expanded)
        self.assertIn('bnd_kid', b._state.expanded)

    def test_expand_subtree_on_boundary_node_itself(self):
        # Invoking expand_subtree directly on a boundary node still
        # expands the node itself (you asked to open it) but does not
        # walk through into its cached descendants.
        b = Browser(BrowserConfig(_headless=True))
        _seed_boundary_tree(b, boundary=True)
        b.expand_subtree('bnd')
        b.drain_main_queue()
        self.assertIn('bnd', b._state.expanded)
        self.assertNotIn('bnd_kid', b._state.expanded)


class TestContextPassthroughs(unittest.TestCase):

    def test_collapse_all(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.expanded.add('a')
        ctx = Context(b)
        ctx.collapse_all()
        b.drain_main_queue()
        self.assertEqual(b._state.expanded, set())

    def test_expand_subtree(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        ctx = Context(b)
        ctx.expand_subtree('a')
        b.drain_main_queue()
        self.assertIn('a', b._state.expanded)
        self.assertIn('a1', b._state.expanded)

    def test_collapse(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        b._state.expanded.add('a')
        ctx = Context(b)
        ctx.collapse('a')
        b.drain_main_queue()
        self.assertNotIn('a', b._state.expanded)

    def test_collapse_already_collapsed_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed_tree(b)
        ctx = Context(b)
        # Nothing expanded — pass-through must not raise.
        ctx.collapse('a')
        b.drain_main_queue()
        self.assertNotIn('a', b._state.expanded)


if __name__ == '__main__':
    unittest.main()
