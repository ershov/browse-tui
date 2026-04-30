"""Tests for scope-stack mechanics in browse-tui state layer.

The scope stack lets the user "drill into" an item, treating it as the
new root of the visible tree. Per-scope expanded sets are memoised in
`_expanded_by_scope` so leaving and re-entering a scope restores its
expansion state. See the design spec, section "Public API → Browser".
"""

import unittest

from test.unit._loader import load

_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Inject Item — see test_visible_tree.py for explanation.
_state.Item = _data.Item

Item = _data.Item
State = _state.State
visible_items = _state.visible_items
scope_into = _state.scope_into
scope_out = _state.scope_out
current_scope = _state.current_scope


def _kid(id_, has_children=False):
    return Item(id=id_, has_children=has_children)


class TestScopeBasics(unittest.TestCase):
    """current_scope and scope_into/scope_out single-step behaviour."""

    def test_empty_stack_uses_root_id(self):
        s = State(root_id='__ROOT__')
        self.assertEqual(current_scope(s), '__ROOT__')

    def test_scope_into_pushes(self):
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        self.assertEqual(current_scope(s), 'A')
        self.assertEqual(s.scope_stack, ['A'])

    def test_scope_out_pops_and_returns_id(self):
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        popped = scope_out(s)
        self.assertEqual(popped, 'A')
        self.assertEqual(current_scope(s), '__ROOT__')
        self.assertEqual(s.scope_stack, [])


class TestScopeInOutBalance(unittest.TestCase):
    """Nested scopes round-trip cleanly."""

    def test_double_in_double_out(self):
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        scope_into(s, 'B')
        self.assertEqual(s.scope_stack, ['A', 'B'])
        self.assertEqual(current_scope(s), 'B')
        self.assertEqual(scope_out(s), 'B')
        self.assertEqual(current_scope(s), 'A')
        self.assertEqual(scope_out(s), 'A')
        self.assertEqual(s.scope_stack, [])
        self.assertEqual(current_scope(s), '__ROOT__')


class TestExpandedPreserved(unittest.TestCase):
    """Per-scope expanded sets are memoised in _expanded_by_scope."""

    def test_expanded_swapped_per_scope(self):
        s = State(root_id='__ROOT__')
        s.expanded = {'x', 'y'}
        scope_into(s, 'A')
        # Fresh scope — expanded is empty (no prior memo).
        self.assertEqual(s.expanded, set())
        s.expanded.add('z')
        scope_out(s)
        # Restored to root's set; 'z' did not bleed across.
        self.assertEqual(s.expanded, {'x', 'y'})

    def test_revisiting_scope_restores_its_expanded(self):
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        s.expanded.add('z')
        scope_out(s)
        scope_into(s, 'A')
        self.assertEqual(s.expanded, {'z'})


class TestVisibleReflectsScope(unittest.TestCase):
    """visible_items shows the scope item as the depth-0 row."""

    def test_root_then_scoped_view(self):
        a = _kid('A', has_children=True)
        a1 = _kid('A1')
        a2 = _kid('A2')
        s = State(
            root_id='__ROOT__',
            _children={'__ROOT__': [a], 'A': [a1, a2]},
        )
        # At root: only 'A' is shown.
        rows = visible_items(s)
        self.assertEqual(
            [(r.item.id, r.depth, r.kind) for r in rows],
            [('A', 0, 'normal')],
        )
        # Scope into 'A': scope_root row + children.
        scope_into(s, 'A')
        rows = visible_items(s)
        self.assertEqual(
            [(r.item.id, r.depth, r.kind) for r in rows],
            [
                ('A', 0, 'scope_root'),
                ('A1', 1, 'normal'),
                ('A2', 1, 'normal'),
            ],
        )


class TestScopeOutAtRootIsNoOp(unittest.TestCase):
    """scope_out from empty stack returns None and leaves state alone."""

    def test_scope_out_when_already_at_root(self):
        s = State(root_id='__ROOT__')
        s.expanded = {'x'}
        self.assertIsNone(scope_out(s))
        self.assertEqual(s.scope_stack, [])
        self.assertEqual(current_scope(s), '__ROOT__')
        self.assertEqual(s.expanded, {'x'})


class TestScopeMarksDirty(unittest.TestCase):
    """Both scope_into and scope_out flip _visible_dirty back on."""

    def test_scope_into_marks_dirty(self):
        s = State(root_id='__ROOT__', _children={'__ROOT__': []})
        visible_items(s)
        self.assertFalse(s._visible_dirty)
        scope_into(s, 'A')
        self.assertTrue(s._visible_dirty)

    def test_scope_out_marks_dirty(self):
        s = State(root_id='__ROOT__', _children={'__ROOT__': []})
        scope_into(s, 'A')
        visible_items(s)
        self.assertFalse(s._visible_dirty)
        scope_out(s)
        self.assertTrue(s._visible_dirty)


if __name__ == '__main__':
    unittest.main()
