"""Tests for scope-stack mechanics in browse-tui state layer.

The scope stack lets the user "drill into" an item, treating it as the
new root of the visible tree. Per-scope expanded sets are memoised in
`_expanded_by_scope` so leaving and re-entering a scope restores its
expansion state. See the design spec, section "Public API → Browser".
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Inject Item — see test_visible_tree.py for explanation.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

# The actions module references ``visible_items``, ``mark_visible_dirty``,
# ``scope_into``, ``scope_out`` from globals — production builds get them
# via concatenation; the test loader injects them by hand.
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode
_actions.scope_into = _state.scope_into
_actions.scope_out = _state.scope_out
_actions.current_scope = _state.current_scope

Item = _data.Item
State = _state.State
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
visible_items = _state.visible_items
scope_into = _state.scope_into
scope_out = _state.scope_out
current_scope = _state.current_scope
dispatch_key = _actions.dispatch_key


def _ctx_for(browser):
    """Build a real Context for the given browser (matches test_actions)."""
    _context = load('_browse_tui_context', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


def _make_browser(**kw):
    """Build a headless Browser; tests call stop_workers in tearDown."""
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


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
        # Fresh scope — expanded contains only the scope id itself
        # (auto-added by scope_into so the scope row paints expanded).
        self.assertEqual(s.expanded, {'A'})
        s.expanded.add('z')
        scope_out(s)
        # Restored to root's set; 'z' / 'A' did not bleed across.
        self.assertEqual(s.expanded, {'x', 'y'})

    def test_revisiting_scope_restores_its_expanded(self):
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        s.expanded.add('z')
        scope_out(s)
        scope_into(s, 'A')
        # Restored set includes the previously-added 'z' AND the scope
        # id itself ('A', auto-added on every scope_into).
        self.assertEqual(s.expanded, {'A', 'z'})


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
        # Scope into 'A': scope row at depth 0 (normal kind) + children.
        scope_into(s, 'A')
        rows = visible_items(s)
        self.assertEqual(
            [(r.item.id, r.depth, r.kind) for r in rows],
            [
                ('A', 0, 'normal'),
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


class TestScopeIntoAutoExpands(unittest.TestCase):
    """scope_into auto-adds the scope id to the expanded set so the
    scope row renders with the ▼ glyph by default (the scope row is
    now a normal row at depth 0 — see scope-root unification design)."""

    def test_scope_into_adds_id_to_expanded(self):
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        self.assertIn('A', s.expanded)

    def test_scope_into_does_not_leak_into_memoised_set(self):
        # The auto-add lives only in the active expanded set, not in
        # the memoised entry of the scope we just left. scope_out
        # writes the active set back, so any explicit changes the
        # user makes while in scope are still preserved across
        # round-trips.
        s = State(root_id='__ROOT__')
        s.expanded = {'x'}
        scope_into(s, 'A')
        # 'A' is in the new active set...
        self.assertEqual(s.expanded, {'A'})
        # ...but not in the memoised root entry, so popping back
        # restores {'x'} without 'A' bleeding in.
        scope_out(s)
        self.assertEqual(s.expanded, {'x'})


class TestScopeOutAutoExpands(unittest.TestCase):
    """scope_out mirrors scope_into's auto-expand: when we land in a
    still-scoped state, the new current scope id is added to
    ``state.expanded`` so the scope row paints expanded by default.
    Without this, popping into a scope whose memoised expanded set
    doesn't include itself (recipe-pushed deep stack, never previously
    entered via scope_into) would hide the popped row's siblings."""

    def test_scope_out_into_nested_scope_auto_expands(self):
        s = State(root_id='__ROOT__')
        # Simulate a recipe-pushed deep stack: never went through
        # scope_into so neither scope's memoised set carries the
        # scope id.
        s.scope_stack = ['outer', 'inner']
        s._expanded_by_scope = {}  # nothing memoised
        s.expanded = set()
        # Pop the inner scope. Now we're scoped to 'outer'.
        popped = scope_out(s)
        self.assertEqual(popped, 'inner')
        self.assertEqual(s.scope_stack, ['outer'])
        # 'outer' was auto-added to expanded so the scope row paints
        # expanded and 'inner' appears as a child below.
        self.assertIn('outer', s.expanded)

    def test_scope_out_to_root_does_not_auto_expand(self):
        # No scope after popping → nothing to auto-expand.
        s = State(root_id='__ROOT__')
        scope_into(s, 'A')
        scope_out(s)
        # At root: expanded is whatever root's memoised set was
        # (empty in this case), no auto-add fires.
        self.assertEqual(s.expanded, set())
        self.assertEqual(s.scope_stack, [])


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


# --- dispatch path: alt-down / alt-up -------------------------------------


class TestAltDownDispatch(unittest.TestCase):
    """Dispatching 'alt-down' pushes scope when cursor is on a branch."""

    def test_alt_down_pushes_scope(self):
        b = _make_browser()
        try:
            b._state._children[None] = [
                Item(id='A', has_children=True),
                Item(id='B'),
            ]
            b._state._children['A'] = [Item(id='a1'), Item(id='a2')]
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-down'))
            # ``ctx.expand`` posts; drain so the in-flight expand resolves
            # against the cached children.
            b.drain_main_queue()
            self.assertEqual(b._state.scope_stack, ['A'])
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_alt_down_on_leaf_is_noop(self):
        b = _make_browser()
        try:
            b._state._children[None] = [Item(id='A'), Item(id='B')]
            ctx = _ctx_for(b)
            # Cursor on 'A' (a leaf — has_children defaults to False).
            self.assertTrue(dispatch_key(b, ctx, 'alt-down'))
            self.assertEqual(b._state.scope_stack, [])
        finally:
            b.stop_workers()

    def test_alt_down_at_no_cursor_is_noop(self):
        # Empty visible list — cursor resolves to None; the gate
        # 'cursor' on the alt-down Action should swallow the dispatch
        # silently with no scope change and no error.
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            # dispatch returns True (gated) but does not run the handler.
            dispatch_key(b, ctx, 'alt-down')
            self.assertEqual(b._state.scope_stack, [])
        finally:
            b.stop_workers()


class TestAltUpDispatch(unittest.TestCase):
    """Dispatching 'alt-up' pops scope and re-positions the cursor."""

    def test_alt_up_pops_scope_and_places_cursor_on_popped_id(self):
        b = _make_browser()
        try:
            b._state._children[None] = [
                Item(id='A', has_children=True),
                Item(id='B'),
            ]
            b._state._children['A'] = [Item(id='a1')]
            scope_into(b._state, 'A')
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-up'))
            self.assertEqual(b._state.scope_stack, [])
            # Cursor lands on 'A' in the now-current root view.
            vis = visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A')
        finally:
            b.stop_workers()

    def test_alt_up_at_root_is_noop(self):
        b = _make_browser()
        try:
            b._state._children[None] = [Item(id='A'), Item(id='B')]
            b._state.cursor = 1
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'alt-up')
            self.assertEqual(b._state.scope_stack, [])
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_alt_up_lazy_fetches_uncached_new_scope(self):
        # Recipe pre-pushed a multi-level scope stack without ever
        # navigating through the lower levels — so the new top after
        # scope_up has no cached children. _scope_up must queue a
        # fetch so the visible list populates instead of stranding.
        b = _make_browser()
        try:
            # 'parent' has no cached children. Only 'A' (the current
            # scope) and root are cached.
            b._state._children[None] = [Item(id='parent', has_children=True)]
            b._state.scope_stack[:] = ['parent', 'A']
            b._state._children['A'] = [Item(id='a1')]
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'alt-up')
            # Scope popped to 'parent'.
            self.assertEqual(b._state.scope_stack, ['parent'])
            # 'parent' got queued for a fetch.
            self.assertIn('parent', b._state._children_pending)
            # Cursor land on the popped id is deferred via cursor_to —
            # a posted callable that runs *after* _handle_one_key's
            # trailing _reanchor_cursor (which would otherwise clobber
            # any anchor set inline here). The post queue carries one
            # _do_cursor_to closure waiting to run.
            self.assertFalse(
                b._main_queue.empty(),
                'expected a posted cursor_to(popped) on the main queue',
            )
        finally:
            b.stop_workers()

    def test_alt_up_does_not_re_fetch_cached_scope(self):
        # When the new top scope has cached children (normal
        # navigation case — user drilled in earlier so its children
        # were already fetched), scope_up does NOT queue a redundant
        # fetch.
        b = _make_browser()
        try:
            b._state._children[None] = [Item(id='A', has_children=True)]
            b._state._children['A'] = [Item(id='a1')]
            scope_into(b._state, 'A')
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'alt-up')
            self.assertEqual(b._state.scope_stack, [])
            # Root was already cached — no fetch queued.
            self.assertNotIn(None, b._state._children_pending)
        finally:
            b.stop_workers()


class TestPerScopeExpandedRoundTrip(unittest.TestCase):
    """Per-scope expanded sets survive scope-out/scope-in cycles."""

    def test_per_scope_expanded_preserved_round_trip(self):
        b = _make_browser()
        try:
            b._state._children[None] = [
                Item(id='A', has_children=True),
                Item(id='X', has_children=True),
            ]
            b._state._children['A'] = [Item(id='a1', has_children=True)]
            b._state._children['a1'] = [Item(id='a1a')]
            b._state._children['X'] = [Item(id='x1')]
            ctx = _ctx_for(b)
            # At root: expand X (a sibling of A whose children are cached).
            b._state.expanded.add('X')
            # Cursor is on 'A' (index 0 in the visible list).
            self.assertEqual(b._state.cursor, 0)
            # Drill into A.
            self.assertTrue(dispatch_key(b, ctx, 'alt-down'))
            b.drain_main_queue()
            self.assertEqual(b._state.scope_stack, ['A'])
            # Inside A: expand a1.
            b._state.expanded.add('a1')
            # Pop back to root.
            self.assertTrue(dispatch_key(b, ctx, 'alt-up'))
            self.assertEqual(b._state.scope_stack, [])
            # Root's expanded set is preserved (X was set there originally,
            # a1 was set in A's scope and must NOT bleed).
            self.assertEqual(b._state.expanded, {'X'})
            # Cursor sits on A in the now-current view.
            vis = visible_items(b._state)
            self.assertEqual(vis[b._state.cursor].item.id, 'A')
            # Drill into A again.
            self.assertTrue(dispatch_key(b, ctx, 'alt-down'))
            b.drain_main_queue()
            # A's expanded set is restored to what we set last time
            # ({'a1'}), plus the scope id 'A' itself (auto-added by
            # scope_into so the scope row paints expanded).
            self.assertEqual(b._state.expanded, {'A', 'a1'})
        finally:
            b.stop_workers()


# --- scope crumb rendering ------------------------------------------------


class TestScopeCrumbText(unittest.TestCase):
    """The plain-text scope-crumb helper used by the renderer."""

    def setUp(self):
        # Lazy-load render module + Item injection (mirrors test_render).
        self._render = load('_browse_tui_render2', '050-render.py')
        self._render.Item = Item
        self._render.Mode = _state.Mode

    def test_unscoped_returns_empty_string(self):
        b = _make_browser()
        try:
            self.assertEqual(self._render._scope_crumb_text(b), '')
        finally:
            b.stop_workers()

    def test_one_level_scope_renders_single_segment(self):
        b = _make_browser()
        try:
            scope_into(b._state, 'A')
            crumb = self._render._scope_crumb_text(b)
            self.assertIn('▸ A', crumb)
            # Leading and trailing single space — see helper docstring.
            self.assertTrue(crumb.startswith(' '))
            self.assertTrue(crumb.endswith(' '))
        finally:
            b.stop_workers()

    def test_nested_scope_renders_multiple_segments(self):
        b = _make_browser()
        try:
            scope_into(b._state, 'A')
            scope_into(b._state, 'B')
            crumb = self._render._scope_crumb_text(b)
            self.assertIn('▸ A', crumb)
            self.assertIn('▸ B', crumb)
            # Order: A before B.
            self.assertLess(crumb.index('A'), crumb.index('B'))
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
