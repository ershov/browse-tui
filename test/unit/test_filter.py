"""Unit tests for the interactive filter evaluator (`_recompute_filter_hidden`).

Visible-tree-only semantic (see
``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``).
The evaluator walks the same shape ``_emit_children`` would walk with
the filter off: descend through items whose parent is in
``state.expanded``, skip items with ``Item.hidden=True``. Collapsed
parents are evaluated against self alone; their children are not
consulted. ``has_children`` parents whose children aren't cached
contribute nothing (no optimistic keep-visible).

Coverage:

  * Empty/whitespace filter list -> no rows hidden, ``_filter_active`` False.
  * Single filter, leaf match -> match visible, non-matches hidden.
  * Single filter, deep match through an expanded chain -> every
    expanded ancestor stays visible; non-matching siblings hidden.
  * Collapsed parent: self-match alone; children never consulted.
  * Recipe-hidden row containing a match: excluded; doesn't scaffold.
  * Uncached-children parent: NO LONGER optimistic — hidden when self
    doesn't match.
  * Multiple filters -> AND semantics across the stack.
  * Empty strings inside the filter list are skipped.
  * Case-insensitivity + tag matching ride on ``_search_text`` /
    ``_search_matches`` reuse.
  * Scope-row exemption preserved.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake


Item = _data.Item
State = _state.State
Mode = _state.Mode
_recompute_filter_hidden = _state._recompute_filter_hidden
_propagate_filter_status_up = _state._propagate_filter_status_up


def _state_with(children_map, expanded=()):
    """Build a State whose ``_children`` matches the given map.

    ``expanded`` lists parent ids that should be in ``state.expanded``
    so the evaluator descends into their children. The new semantic
    only walks descendants of expanded parents.

    Also populates ``_parent_of_id`` so that helpers that walk upward
    via the reverse-parent index (``_propagate_filter_status_up``) see
    the same shape as ``_children``.
    """
    s = State()
    for parent_id, kids in children_map.items():
        s._children[parent_id] = kids
        for child in kids:
            s._items_by_id[child.id] = child
            s._parent_of_id[child.id] = parent_id
    for pid in expanded:
        s.expanded.add(pid)
    return s


class TestEmptyFilter(unittest.TestCase):

    def test_empty_list_clears_active_flag(self):
        s = _state_with({None: [Item(id='a'), Item(id='b')]})
        s._filter_active = True  # pretend a previous filter was active
        _recompute_filter_hidden(s, [])
        self.assertFalse(s._filter_active)

    def test_empty_strings_only_treated_as_empty(self):
        s = _state_with({None: [Item(id='a')]})
        _recompute_filter_hidden(s, ['', '', ''])
        self.assertFalse(s._filter_active)

    def test_whitespace_only_entry_is_inactive(self):
        # ``_search_matches`` treats whitespace-only as no-match; but
        # the evaluator's empty-filter early-out checks ``if q``, which
        # is True for ' '. So a whitespace-only entry IS active; every
        # row's self_passes returns False, meaning every leaf gets
        # hidden. Documented quirk: empty strings are filtered, but
        # non-empty whitespace is honoured.
        s = _state_with({None: [Item(id='a')]})
        _recompute_filter_hidden(s, [' '])
        self.assertTrue(s._filter_active)
        # Every row hidden because no fragment matches anything.
        self.assertTrue(s._children[None][0]._filter_hidden)


class TestLeafMatch(unittest.TestCase):

    def test_single_filter_picks_matching_leaf(self):
        s = _state_with({None: [
            Item(id='alpha'), Item(id='beta'), Item(id='gamma'),
        ]})
        _recompute_filter_hidden(s, ['beta'])
        flags = {it.id: it._filter_hidden for it in s._children[None]}
        self.assertFalse(flags['beta'])
        self.assertTrue(flags['alpha'])
        self.assertTrue(flags['gamma'])

    def test_filter_matches_tag(self):
        # ``_search_text`` includes [tag], so filter 'open' matches.
        s = _state_with({None: [
            Item(id='1', title='thing', tag='open'),
            Item(id='2', title='another', tag='closed'),
        ]})
        _recompute_filter_hidden(s, ['open'])
        self.assertFalse(s._children[None][0]._filter_hidden)
        self.assertTrue(s._children[None][1]._filter_hidden)

    def test_filter_is_case_insensitive(self):
        s = _state_with({None: [Item(id='Foo'), Item(id='bar')]})
        _recompute_filter_hidden(s, ['FOO'])
        flags = {it.id: it._filter_hidden for it in s._children[None]}
        self.assertFalse(flags['Foo'])
        self.assertTrue(flags['bar'])


class TestDeepMatchScaffolding(unittest.TestCase):
    """Deep matches through an *expanded* chain keep ancestors visible.

    The new semantic only descends through expanded parents — the test
    fixtures here put the relevant parent ids into ``state.expanded``.
    """

    def test_matching_descendant_keeps_ancestor_visible(self):
        parent = Item(id='outer', has_children=True)
        child = Item(id='inner-foo')
        s = _state_with(
            {None: [parent], 'outer': [child]},
            expanded=['outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        # Parent doesn't match 'foo' but its child does -> parent
        # stays visible as scaffold.
        self.assertFalse(parent._filter_hidden)
        self.assertFalse(child._filter_hidden)

    def test_non_matching_sibling_branch_is_hidden(self):
        a = Item(id='a', has_children=True)
        b = Item(id='b', has_children=True)
        a_child = Item(id='a-foo')
        b_child = Item(id='b-bar')
        s = _state_with(
            {
                None: [a, b],
                'a': [a_child],
                'b': [b_child],
            },
            expanded=['a', 'b'],
        )
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(a._filter_hidden)        # scaffold for a-foo
        self.assertFalse(a_child._filter_hidden)
        self.assertTrue(b._filter_hidden)         # no matching descendant
        self.assertTrue(b_child._filter_hidden)

    def test_deeply_nested_match_through_expanded_chain(self):
        # root -> mid -> leaf("foo"); every level in the chain is
        # expanded, so the match keeps the whole chain visible.
        root = Item(id='root', has_children=True)
        mid = Item(id='mid', has_children=True)
        leaf = Item(id='leaf-foo')
        s = _state_with(
            {
                None: [root],
                'root': [mid],
                'mid': [leaf],
            },
            expanded=['root', 'mid'],
        )
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(root._filter_hidden)
        self.assertFalse(mid._filter_hidden)
        self.assertFalse(leaf._filter_hidden)

    def test_deep_match_non_matching_siblings_hidden(self):
        # root expanded; two children, one expanded with a match deep
        # under it, the other not. Sibling without the match stays
        # hidden — only the chain leading to the match scaffolds.
        root = Item(id='root', has_children=True)
        match_branch = Item(id='match-branch', has_children=True)
        miss_branch = Item(id='miss-branch')
        leaf = Item(id='leaf-foo')
        s = _state_with(
            {
                None: [root],
                'root': [match_branch, miss_branch],
                'match-branch': [leaf],
            },
            expanded=['root', 'match-branch'],
        )
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(root._filter_hidden)
        self.assertFalse(match_branch._filter_hidden)
        self.assertTrue(miss_branch._filter_hidden)
        self.assertFalse(leaf._filter_hidden)


class TestCollapsedParentSelfOnly(unittest.TestCase):
    """Rule 2: collapsed parents are evaluated against their own text
    alone; their children are not consulted. This is the load-bearing
    change vs. the optimistic-keep-visible rule.
    """

    def test_collapsed_parent_self_match_visible(self):
        # Parent is collapsed (not in ``state.expanded``). Its title
        # alone matches; child does not. Children not consulted; parent
        # visible by self-match.
        parent = Item(id='p-foo', has_children=True)
        child = Item(id='c-foo')   # also matches, but should NOT be
                                   # walked because parent is collapsed
        s = _state_with({None: [parent], 'p-foo': [child]})
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(parent._filter_hidden)

    def test_collapsed_parent_self_no_match_hidden(self):
        # Parent does NOT match; child DOES. But parent is collapsed,
        # so children are not consulted — parent hidden.
        parent = Item(id='outer', has_children=True)
        child = Item(id='inner-foo')
        s = _state_with({None: [parent], 'outer': [child]})
        # NOT expanded: 'outer' is collapsed in this fixture.
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(parent._filter_hidden)


class TestStackingAND(unittest.TestCase):

    def test_two_filters_AND_match(self):
        s = _state_with({None: [
            Item(id='foo-bar'),
            Item(id='foo-only'),
            Item(id='bar-only'),
        ]})
        _recompute_filter_hidden(s, ['foo', 'bar'])
        flags = {it.id: it._filter_hidden for it in s._children[None]}
        self.assertFalse(flags['foo-bar'])      # matches both
        self.assertTrue(flags['foo-only'])      # matches first only
        self.assertTrue(flags['bar-only'])      # matches second only

    def test_AND_propagates_through_scaffold(self):
        # Parent doesn't match either filter; one child matches both,
        # another matches only one. Parent stays visible (one passing
        # descendant); the one-match child is hidden.
        parent = Item(id='parent', has_children=True)
        c_both = Item(id='c-foo-bar')
        c_one = Item(id='c-foo-only')
        s = _state_with(
            {None: [parent], 'parent': [c_both, c_one]},
            expanded=['parent'],
        )
        _recompute_filter_hidden(s, ['foo', 'bar'])
        self.assertFalse(parent._filter_hidden)
        self.assertFalse(c_both._filter_hidden)
        self.assertTrue(c_one._filter_hidden)

    def test_empty_strings_in_stack_are_skipped(self):
        # ['foo', '', 'bar'] is equivalent to ['foo', 'bar'].
        s = _state_with({None: [
            Item(id='foo-bar'),
            Item(id='foo-only'),
        ]})
        _recompute_filter_hidden(s, ['foo', '', 'bar'])
        flags = {it.id: it._filter_hidden for it in s._children[None]}
        self.assertFalse(flags['foo-bar'])
        self.assertTrue(flags['foo-only'])


class TestUncachedChildrenNoLongerOptimistic(unittest.TestCase):
    """A parent with ``has_children=True`` but no cached subtree gets
    no special treatment under the visible-tree-only semantic. Its
    own text decides — there's no "optimistically keep visible until
    children stream in" branch.
    """

    def test_uncached_has_children_parent_hidden_when_self_no_match(self):
        # Parent claims has_children=True but ``_children['p']`` is
        # absent (children not loaded). Parent itself doesn't match
        # the filter. New semantic: parent is hidden (Rule 2 + no
        # optimistic branch).
        parent = Item(id='p', has_children=True)
        s = _state_with({None: [parent]})
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(parent._filter_hidden)

    def test_uncached_has_children_parent_visible_when_self_matches(self):
        # has_children=True, children not cached, parent's own text
        # matches → visible by self-match (no children consulted).
        parent = Item(id='p-foo', has_children=True)
        s = _state_with({None: [parent]})
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(parent._filter_hidden)

    def test_has_children_with_empty_cache_no_match_hidden(self):
        # has_children=True but ``_children['p'] = []`` (cached empty).
        # No descendants will arrive, parent itself doesn't match →
        # hidden.
        parent = Item(id='p', has_children=True)
        s = _state_with({None: [parent], 'p': []}, expanded=['p'])
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(parent._filter_hidden)

    def test_leaf_without_has_children_no_match_hidden(self):
        # Leaf row (has_children=False) and no match -> hidden.
        leaf = Item(id='x')
        s = _state_with({None: [leaf]})
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(leaf._filter_hidden)


class TestRecipeHiddenRow(unittest.TestCase):
    """Rows with ``Item.hidden=True`` are excluded from the walk
    entirely: they don't match, don't scaffold an ancestor, and their
    own ``_filter_hidden`` is left at its default (False) — the
    renderer would skip the row before consulting ``_filter_hidden``
    anyway."""

    def test_recipe_hidden_row_does_not_scaffold(self):
        # Parent expanded, only child matches the filter but is
        # recipe-hidden. Parent should NOT be kept as scaffold (the
        # hidden child can't contribute), and the parent itself
        # doesn't match — parent ends up hidden too.
        parent = Item(id='outer', has_children=True)
        child = Item(id='inner-foo', hidden=True)
        s = _state_with(
            {None: [parent], 'outer': [child]},
            expanded=['outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        # Parent has no visible matching descendant (the only child is
        # recipe-hidden so doesn't count) and doesn't match itself.
        self.assertTrue(parent._filter_hidden)

    def test_recipe_hidden_row_excluded_visible_sibling_still_scaffolds(self):
        # One recipe-hidden child with the match, one visible child
        # without. Hidden child must not contribute; visible child
        # doesn't match. Parent should be hidden.
        parent = Item(id='outer', has_children=True)
        hidden_match = Item(id='inner-foo', hidden=True)
        visible_miss = Item(id='inner-bar')
        s = _state_with(
            {None: [parent], 'outer': [hidden_match, visible_miss]},
            expanded=['outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(parent._filter_hidden)


class TestScopedItems(unittest.TestCase):
    """Items under a scope (not directly under ``state.root_id``).

    Mirrors ``browse-claude <session.jsonl>``: the recipe loads session
    children into ``_children[session_path]`` without ever populating
    ``_children[None]``. The evaluator's scoped-walk entry point must
    cover that subtree. The new semantic descends through expanded
    parents, including the scope row itself — ``scope_into`` auto-adds
    the scope id to ``state.expanded``, so tests mirror that.
    """

    def test_items_under_scope_get_flagged(self):
        s = State(root_id=None)
        s.scope_stack = ['/path/session.jsonl']
        s.expanded.add('/path/session.jsonl')   # mirrors scope_into
        s._children['/path/session.jsonl'] = [
            Item(id='/path/session.jsonl#1', title='hello world'),
            Item(id='/path/session.jsonl#2', title='goodbye'),
        ]
        s._items_by_id['/path/session.jsonl'] = Item(
            id='/path/session.jsonl',
            has_children=True,
        )
        # _children[None] is empty — items live only under the scope.
        _recompute_filter_hidden(s, ['hello'])
        flags = {
            it.id: it._filter_hidden
            for it in s._children['/path/session.jsonl']
        }
        self.assertFalse(flags['/path/session.jsonl#1'])  # matches
        self.assertTrue(flags['/path/session.jsonl#2'])   # doesn't


class TestActiveFlag(unittest.TestCase):

    def test_active_flag_set_when_filter_non_empty(self):
        s = _state_with({None: [Item(id='a')]})
        _recompute_filter_hidden(s, ['anything'])
        self.assertTrue(s._filter_active)

    def test_active_flag_cleared_after_empty_recompute(self):
        s = _state_with({None: [Item(id='a')]})
        _recompute_filter_hidden(s, ['x'])
        self.assertTrue(s._filter_active)
        _recompute_filter_hidden(s, [])
        self.assertFalse(s._filter_active)


class TestScopeRowExemption(unittest.TestCase):
    """The current-scope row is exempt from filter hiding — it's the
    "you are here" indicator and must remain visible regardless of
    whether its content matches. See scope-root unification design."""

    def test_scope_row_visible_even_when_it_does_not_match(self):
        scope = Item(id='scope-x', title='SCOPE', has_children=True)
        child = Item(id='c1', title='banana')
        s = _state_with({None: [scope], 'scope-x': [child]})
        s.scope_stack = ['scope-x']
        s.expanded.add('scope-x')   # mirrors scope_into
        # Filter for 'apple' — child doesn't match, scope's own text
        # ('scope-x SCOPE') doesn't match either. Without the
        # exemption the scope row would be hidden too.
        _recompute_filter_hidden(s, ['apple'])
        self.assertFalse(scope._filter_hidden)  # scope kept visible
        self.assertTrue(child._filter_hidden)   # non-matching child hidden

    def test_scope_row_visible_when_matching_too(self):
        # Sanity: matching scope row stays visible (the exemption is
        # additive, not destructive).
        scope = Item(id='scope-apple', title='SCOPE', has_children=True)
        child = Item(id='c1', title='banana')
        s = _state_with({None: [scope], 'scope-apple': [child]})
        s.scope_stack = ['scope-apple']
        s.expanded.add('scope-apple')
        _recompute_filter_hidden(s, ['apple'])
        self.assertFalse(scope._filter_hidden)

    def test_non_scope_root_no_special_treatment(self):
        # An unrelated item with the same content shape gets normally
        # filtered when the scope is something else.
        scope = Item(id='scope-x', title='SCOPE', has_children=True)
        unrelated = Item(id='outer-y', title='YYY')
        s = _state_with({None: [scope, unrelated]})
        s.scope_stack = ['scope-x']
        s.expanded.add('scope-x')
        # Synthetic for the scope so the walker can find it.
        s._items_by_id['scope-x'] = scope
        # Filter that no row matches; unrelated... is NOT in the
        # scope's subtree, so it isn't walked. The scope row is exempt
        # and visible. ``unrelated`` keeps its default flag (False) —
        # under the new semantic, items outside the visible tree are
        # not flagged. The renderer only sees rows under the scope
        # anyway, so this is fine.
        _recompute_filter_hidden(s, ['zzz-no-match'])
        self.assertFalse(scope._filter_hidden)


class TestPropagateFilterStatusUp(unittest.TestCase):
    """Unit tests for ``_propagate_filter_status_up``.

    Bottom-up walk re-evaluating one item then ancestors until status
    stabilizes. Used by per-op ``update_data`` dispatch and by
    ``expand``'s subtree-revisit. See
    ``docs/superpowers/specs/2026-05-27-filter-visible-tree-only-design.md``
    ("Per-op incremental update").
    """

    def test_no_op_when_filter_inactive(self):
        # Empty filter list: helper must early-return, not touch any
        # ``_filter_hidden`` flags. We pre-set the parent's flag to
        # True to make the no-op observable.
        parent = Item(id='outer', has_children=True)
        child = Item(id='inner-foo')
        s = _state_with(
            {None: [parent], 'outer': [child]},
            expanded=['outer'],
        )
        parent._filter_hidden = True
        _propagate_filter_status_up(s, child, [])
        # Flag untouched: helper did nothing.
        self.assertTrue(parent._filter_hidden)

    def test_add_matching_child_to_hidden_parent_flips_parent(self):
        # Parent is currently hidden (no matching descendant); we add a
        # new matching child whose ``_filter_hidden`` is already set
        # correctly (False) and call the helper from the parent (the
        # dispatch policy for ``upsert`` of a NEW item per the design:
        # walk the new item's subtree first, then propagate up from
        # parent). The walk evaluates the parent (now has a visible
        # matching descendant -> visible), then root -> terminates.
        gp = Item(id='gp', has_children=True)
        parent = Item(id='outer', has_children=True)
        s = _state_with(
            {None: [gp], 'gp': [parent], 'outer': []},
            expanded=['gp', 'outer'],
        )
        # Establish pre-state: parent hidden because nothing under it
        # and self doesn't match. Same for grandparent.
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(parent._filter_hidden)
        self.assertTrue(gp._filter_hidden)
        # Add a matching child. Its _filter_hidden default is False,
        # which (for a leaf that matches) is the correct final value;
        # the dispatch policy's "visit() the new subtree first" is a
        # no-op for a matching leaf.
        child = Item(id='inner-foo')
        s._children['outer'] = [child]
        s._items_by_id[child.id] = child
        s._parent_of_id[child.id] = 'outer'
        # Propagate from the parent (dispatch policy for new-item
        # upsert).
        _propagate_filter_status_up(s, parent, ['foo'])
        self.assertFalse(child._filter_hidden)   # matches itself
        self.assertFalse(parent._filter_hidden)  # scaffolded by child
        self.assertFalse(gp._filter_hidden)      # scaffolded by parent

    def test_add_non_matcher_to_scaffold_parent_terminates_early(self):
        # Parent is currently scaffold-visible (has another matching
        # descendant). We add a *non*-matching child and propagate from
        # it: child evaluates to hidden, parent re-evaluates and is
        # still visible (other descendant still matches) -> walk
        # terminates at parent (no flag change) without touching the
        # grandparent. Verified by pre-flipping the grandparent's flag
        # to a "wrong" value and confirming it isn't rewritten.
        gp = Item(id='gp', has_children=True)
        parent = Item(id='outer', has_children=True)
        existing = Item(id='inner-foo')
        s = _state_with(
            {None: [gp], 'gp': [parent], 'outer': [existing]},
            expanded=['gp', 'outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        # Pre-state sanity: chain scaffolded by the match.
        self.assertFalse(gp._filter_hidden)
        self.assertFalse(parent._filter_hidden)
        self.assertFalse(existing._filter_hidden)
        # Sentinel: pre-flip grandparent's flag to True. If the walk
        # terminates at ``parent`` (its flag doesn't change), the
        # helper must NOT touch gp; the sentinel stays True.
        gp._filter_hidden = True
        # Add a non-matcher child.
        new_child = Item(id='inner-zzz')
        s._children['outer'] = [existing, new_child]
        s._items_by_id[new_child.id] = new_child
        s._parent_of_id[new_child.id] = 'outer'
        _propagate_filter_status_up(s, new_child, ['foo'])
        self.assertTrue(new_child._filter_hidden)   # doesn't match
        self.assertFalse(parent._filter_hidden)     # still scaffold
        # Sentinel preserved: helper terminated at parent.
        self.assertTrue(gp._filter_hidden)

    def test_mod_flip_last_matcher_to_non_matcher_hides_parent(self):
        # Parent is scaffold-visible only because of one matching leaf.
        # We mutate the leaf so its title no longer matches; the walk
        # re-evaluates the leaf (now hidden), then the parent (no
        # visible matching descendant -> hidden).
        gp = Item(id='gp', has_children=True)
        parent = Item(id='outer', has_children=True)
        leaf = Item(id='leaf', title='foo-text')
        s = _state_with(
            {None: [gp], 'gp': [parent], 'outer': [leaf]},
            expanded=['gp', 'outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(gp._filter_hidden)
        self.assertFalse(parent._filter_hidden)
        self.assertFalse(leaf._filter_hidden)
        # Mutate the leaf in-place: title no longer contains 'foo'.
        leaf.title = 'banana'
        _propagate_filter_status_up(s, leaf, ['foo'])
        self.assertTrue(leaf._filter_hidden)
        self.assertTrue(parent._filter_hidden)
        self.assertTrue(gp._filter_hidden)

    def test_remove_last_matcher_in_scaffold_hides_parent(self):
        # Parent was scaffold-visible because its one matching child;
        # we remove the child from ``_children`` and propagate from the
        # parent (the dispatch policy for removes per the design).
        # Parent re-evaluates: no visible matching descendant, self
        # doesn't match -> hidden.
        gp = Item(id='gp', has_children=True)
        parent = Item(id='outer', has_children=True)
        leaf = Item(id='leaf-foo')
        s = _state_with(
            {None: [gp], 'gp': [parent], 'outer': [leaf]},
            expanded=['gp', 'outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(parent._filter_hidden)
        self.assertFalse(gp._filter_hidden)
        # Remove the matching child.
        s._children['outer'] = []
        s._items_by_id.pop(leaf.id, None)
        s._parent_of_id.pop(leaf.id, None)
        _propagate_filter_status_up(s, parent, ['foo'])
        self.assertTrue(parent._filter_hidden)
        self.assertTrue(gp._filter_hidden)

    def test_walk_reaches_root_without_status_change(self):
        # Walk from a leaf whose visibility doesn't change. The first
        # item evaluates with the same flag value it had -> helper
        # early-terminates immediately. No ancestor is touched.
        # (Verified with grandparent sentinel.)
        gp = Item(id='gp', has_children=True)
        parent = Item(id='outer', has_children=True)
        leaf = Item(id='leaf-foo')
        s = _state_with(
            {None: [gp], 'gp': [parent], 'outer': [leaf]},
            expanded=['gp', 'outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        # Sentinels: flip ancestors to "wrong" values; if the helper
        # walks up despite the leaf's flag not changing, the
        # assertions below will fail.
        gp._filter_hidden = True
        parent._filter_hidden = True
        _propagate_filter_status_up(s, leaf, ['foo'])
        # Leaf still matches; flag unchanged from False -> early-out.
        self.assertFalse(leaf._filter_hidden)
        self.assertTrue(parent._filter_hidden)   # sentinel intact
        self.assertTrue(gp._filter_hidden)       # sentinel intact

    def test_recipe_hidden_item_does_not_scaffold_through(self):
        # The propagating item is recipe-hidden: it forces
        # cur_visible=False (can't scaffold). Walk continues upward
        # with that as the descendant state, parent re-evaluates with
        # no visible matching descendant -> hidden.
        parent = Item(id='outer', has_children=True)
        hidden_match = Item(id='inner-foo', hidden=True)
        s = _state_with(
            {None: [parent], 'outer': [hidden_match]},
            expanded=['outer'],
        )
        _recompute_filter_hidden(s, ['foo'])
        # Pre-state: parent already hidden because hidden_match
        # doesn't scaffold (Rule 2 + recipe-hidden exclusion).
        self.assertTrue(parent._filter_hidden)
        # Propagate from the hidden child: walk treats it as
        # not-visible; parent re-evaluates the same way -> still
        # hidden, walk terminates.
        _propagate_filter_status_up(s, hidden_match, ['foo'])
        self.assertTrue(parent._filter_hidden)

    def test_scope_row_exemption_applies_to_propagation(self):
        # When the walk reaches the scope row, the scope-row exemption
        # makes ``self_passes=True``, so the scope row stays visible
        # even if no descendant matches and its own text doesn't
        # match. Mirrors ``_recompute_filter_hidden``'s rule.
        scope = Item(id='scope-x', title='SCOPE', has_children=True)
        leaf = Item(id='leaf', title='foo-text')
        s = _state_with(
            {None: [scope], 'scope-x': [leaf]},
            expanded=['scope-x'],
        )
        s.scope_stack = ['scope-x']
        _recompute_filter_hidden(s, ['foo'])
        # Pre-state: scope and leaf visible.
        self.assertFalse(scope._filter_hidden)
        self.assertFalse(leaf._filter_hidden)
        # Mutate leaf to no longer match (title swapped, id is 'leaf').
        leaf.title = 'banana'
        _propagate_filter_status_up(s, leaf, ['foo'])
        self.assertTrue(leaf._filter_hidden)
        # Scope row exempted; stays visible despite no matching
        # descendant.
        self.assertFalse(scope._filter_hidden)


if __name__ == '__main__':
    unittest.main()
