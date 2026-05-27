"""Unit tests for the interactive filter evaluator (`_recompute_filter_hidden`).

Covers the bottom-up pass over ``state._children``:

  * Empty/whitespace filter list -> no rows hidden, ``_filter_active`` False.
  * Single filter, leaf match -> match visible, non-matches hidden.
  * Single filter, deep match -> ancestors kept as scaffold; non-
    matching siblings hidden.
  * Multiple filters -> AND semantics across the stack.
  * Empty strings inside the filter list are skipped.
  * Un-cached ``has_children`` parents are optimistic (stay visible).
  * Case-insensitivity + tag matching ride on ``_search_text`` /
    ``_search_matches`` reuse.

See ``docs/superpowers/specs/2026-05-17-filter-design.md``.
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


def _state_with(children_map):
    """Build a State whose ``_children`` matches the given map."""
    s = State()
    for parent_id, kids in children_map.items():
        s._children[parent_id] = kids
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

    def test_matching_descendant_keeps_ancestor_visible(self):
        parent = Item(id='outer', has_children=True)
        child = Item(id='inner-foo')
        s = _state_with({None: [parent], 'outer': [child]})
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
        s = _state_with({
            None: [a, b],
            'a': [a_child],
            'b': [b_child],
        })
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(a._filter_hidden)        # scaffold for a-foo
        self.assertFalse(a_child._filter_hidden)
        self.assertTrue(b._filter_hidden)         # no matching descendant
        self.assertTrue(b_child._filter_hidden)

    def test_deeply_nested_match(self):
        # root -> mid -> leaf("foo"); only leaf matches but root + mid
        # both stay visible as scaffold.
        root = Item(id='root', has_children=True)
        mid = Item(id='mid', has_children=True)
        leaf = Item(id='leaf-foo')
        s = _state_with({
            None: [root],
            'root': [mid],
            'mid': [leaf],
        })
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(root._filter_hidden)
        self.assertFalse(mid._filter_hidden)
        self.assertFalse(leaf._filter_hidden)


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
        s = _state_with({None: [parent], 'parent': [c_both, c_one]})
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


class TestOptimisticPendingChildren(unittest.TestCase):

    def test_has_children_with_no_cache_stays_visible(self):
        # Parent claims has_children=True but ``_children['p']`` is
        # absent (children still loading). Parent itself doesn't match
        # the filter. With the optimistic rule it should stay visible
        # until the children stream in.
        parent = Item(id='p', has_children=True)
        s = _state_with({None: [parent]})
        _recompute_filter_hidden(s, ['foo'])
        self.assertFalse(parent._filter_hidden)

    def test_has_children_with_empty_cache_is_pessimistic(self):
        # has_children=True but ``_children['p'] = []`` (cached empty).
        # No descendants will arrive, so the optimism doesn't apply.
        parent = Item(id='p', has_children=True)
        s = _state_with({None: [parent], 'p': []})
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(parent._filter_hidden)

    def test_leaf_without_has_children_is_pessimistic(self):
        # Leaf row (has_children=False) and no match -> hidden.
        leaf = Item(id='x')
        s = _state_with({None: [leaf]})
        _recompute_filter_hidden(s, ['foo'])
        self.assertTrue(leaf._filter_hidden)


class TestScopedItems(unittest.TestCase):
    """Items under a scope (not directly under ``state.root_id``) are
    still flagged by the recompute pass.

    Mirrors ``browse-claude <session.jsonl>``: the recipe loads session
    children into ``_children[session_path]`` without ever populating
    ``_children[None]``. The evaluator must walk every cached parent's
    children, not just ``state.root_id``.
    """

    def test_items_under_scope_get_flagged(self):
        s = State(root_id=None)
        s.scope_stack = ['/path/session.jsonl']
        s._children['/path/session.jsonl'] = [
            Item(id='/path/session.jsonl#1', title='hello world'),
            Item(id='/path/session.jsonl#2', title='goodbye'),
        ]
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
        _recompute_filter_hidden(s, ['apple'])
        self.assertFalse(scope._filter_hidden)

    def test_non_scope_root_no_special_treatment(self):
        # An unrelated item with the same content shape gets normally
        # filtered when the scope is something else.
        scope = Item(id='scope-x', title='SCOPE', has_children=True)
        unrelated = Item(id='outer-y', title='YYY')
        s = _state_with({None: [scope, unrelated]})
        s.scope_stack = ['scope-x']
        # Filter that no row matches; unrelated is hidden, scope is
        # exempt.
        _recompute_filter_hidden(s, ['zzz-no-match'])
        self.assertFalse(scope._filter_hidden)
        self.assertTrue(unrelated._filter_hidden)


if __name__ == '__main__':
    unittest.main()
