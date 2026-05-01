"""Tests for the action layer (070-actions.py).

The action layer wires keybindings to handlers. ``Action`` is a tiny
dataclass; ``default_actions`` enumerates the built-in bindings;
``build_keymap`` merges the defaults with user-supplied ``Browser.actions``
(user wins on duplicates); ``dispatch_key`` does the runtime lookup,
including search-mode interception and ``on_enter`` semantics.

Tests load ``020-terminal``, ``030-data``, ``040-state``, and
``070-actions`` independently and inject the cross-module names the
production single-file build resolves via concatenation.
"""

import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_render = load('_browse_tui_render', '050-render.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Wire up cross-module names — production builds get them via
# concatenation; the test loader needs them by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_render.Item = _data.Item
_render.VisibleEntry = _state.VisibleEntry

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.current_scope = _state.current_scope
_actions._search_find = _state._search_find
_actions._search_jump_nearest = _state._search_jump_nearest
# term_size / layout_panes are deliberately NOT wired at module scope:
# the existing TestPreviewScrollActions cases verify the headless
# fallback path. The TestPageSizesTrackTerminalHeight cases below
# patch the symbols on _actions inside try/finally to exercise the
# wired path.


Item = _data.Item
Browser = _state.Browser
Action = _actions.Action
default_actions = _actions.default_actions
build_keymap = _actions.build_keymap
dispatch_key = _actions.dispatch_key
_gate_passes = _actions._gate_passes


class _FakeContext:
    """Stand-in for Context — the gate tests only need a minimal surface.

    Real Context lives in 060-context.py; loading it would force us to
    inject more state-module symbols. The dispatcher only reads
    ``cursor``, ``selected``, ``targets``, and ``_browser`` — exactly
    what we provide here. The dispatch tests themselves use a real
    Context (loaded below).
    """

    def __init__(self, cursor=None, selected=None, browser=None):
        self.cursor = cursor
        self.selected = selected if selected is not None else []
        self.targets = list(self.selected) if self.selected else (
            [self.cursor] if self.cursor else []
        )
        self._browser = browser


def _make_browser(**kw):
    """Build a headless Browser; tests call stop_workers in tearDown."""
    kw.setdefault('_headless', True)
    return Browser(**kw)


def _ctx_for(browser):
    """Build a real Context for the given browser.

    The Context module imports ``visible_items`` from its globals;
    inject it before constructing.
    """
    _context = load('_browse_tui_context', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


# --- Action dataclass -----------------------------------------------------


class TestActionDataclass(unittest.TestCase):

    def test_construction_with_defaults(self):
        a = Action('e')
        self.assertEqual(a.key, 'e')
        self.assertEqual(a.label, '')
        self.assertIsNone(a.handler)
        self.assertEqual(a.requires, 'none')

    def test_construction_with_explicit_fields(self):
        h = lambda ctx: None
        a = Action('ctrl-r', label='Reload', handler=h, requires='cursor')
        self.assertEqual(a.key, 'ctrl-r')
        self.assertEqual(a.label, 'Reload')
        self.assertIs(a.handler, h)
        self.assertEqual(a.requires, 'cursor')


# --- gate -----------------------------------------------------------------


class TestGate(unittest.TestCase):

    def test_none_always_passes(self):
        a = Action('x', requires='none')
        ctx = _FakeContext(cursor=None)
        self.assertTrue(_gate_passes(a, ctx))

    def test_cursor_requires_non_none(self):
        a = Action('x', requires='cursor')
        self.assertFalse(_gate_passes(a, _FakeContext(cursor=None)))
        self.assertTrue(_gate_passes(a, _FakeContext(cursor=Item(id='a'))))

    def test_selection_requires_non_empty(self):
        a = Action('x', requires='selection')
        self.assertFalse(_gate_passes(a, _FakeContext(selected=[])))
        self.assertTrue(
            _gate_passes(a, _FakeContext(selected=[Item(id='a')]))
        )

    def test_targets_passes_when_selection_or_cursor(self):
        a = Action('x', requires='targets')
        # No selection, no cursor → fails.
        self.assertFalse(_gate_passes(a, _FakeContext()))
        # Cursor only → passes.
        self.assertTrue(_gate_passes(a, _FakeContext(cursor=Item(id='a'))))
        # Selection only → passes.
        self.assertTrue(
            _gate_passes(a, _FakeContext(selected=[Item(id='b')]))
        )


# --- default_actions ------------------------------------------------------


class TestDefaultActions(unittest.TestCase):

    def test_returns_a_list_of_action_objects(self):
        acts = default_actions()
        self.assertGreater(len(acts), 0)
        for a in acts:
            self.assertIsInstance(a, Action)

    def test_required_keys_present(self):
        keys = {a.key for a in default_actions()}
        for required in ('j', 'k', 'down', 'up', 'home', 'end',
                         'pgdn', 'pgup', 'left', 'right',
                         'ctrl-r', 'ctrl-l', 'ctrl-p',
                         '?', 'f1', '/', 'q', 'esc', 'ctrl-c'):
            self.assertIn(required, keys, f'missing default key: {required!r}')

    def test_returns_fresh_list(self):
        # Mutating one return must not affect a subsequent call.
        a = default_actions()
        a.append(Action('zz'))
        b = default_actions()
        self.assertNotIn('zz', {x.key for x in b})


# --- build_keymap ---------------------------------------------------------


class TestBuildKeymap(unittest.TestCase):

    def test_defaults_populate_keymap(self):
        b = _make_browser()
        try:
            km = build_keymap(b)
            self.assertIn('j', km)
            self.assertIn('ctrl-r', km)
            self.assertIn('q', km)
        finally:
            b.stop_workers()

    def test_custom_action_overrides_default_for_same_key(self):
        sentinel = lambda ctx: None
        custom = Action('q', handler=sentinel, label='custom-quit')
        b = _make_browser(actions=[custom])
        try:
            km = build_keymap(b)
            self.assertIs(km['q'].handler, sentinel)
            self.assertEqual(km['q'].label, 'custom-quit')
        finally:
            b.stop_workers()

    def test_multiple_custom_actions_all_present(self):
        a1 = Action('e', handler=lambda ctx: None, label='Edit')
        a2 = Action('v', handler=lambda ctx: None, label='View')
        b = _make_browser(actions=[a1, a2])
        try:
            km = build_keymap(b)
            self.assertIs(km['e'], a1)
            self.assertIs(km['v'], a2)
            # Defaults still present.
            self.assertIn('j', km)
        finally:
            b.stop_workers()


# --- dispatch nav ---------------------------------------------------------


class TestDispatchNav(unittest.TestCase):

    def _browser_with_three_root_children(self):
        b = _make_browser()
        b._state._children[None] = [Item(id='A'), Item(id='B'), Item(id='C')]
        return b

    def test_j_moves_cursor_down(self):
        b = self._browser_with_three_root_children()
        try:
            ctx = _ctx_for(b)
            self.assertEqual(b._state.cursor, 0)
            self.assertTrue(dispatch_key(b, ctx, 'j'))
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_k_moves_cursor_up(self):
        b = self._browser_with_three_root_children()
        b._state.cursor = 2
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'k'))
            self.assertEqual(b._state.cursor, 1)
        finally:
            b.stop_workers()

    def test_g_jumps_to_first(self):
        b = self._browser_with_three_root_children()
        b._state.cursor = 2
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'g'))
            self.assertEqual(b._state.cursor, 0)
        finally:
            b.stop_workers()

    def test_G_jumps_to_last(self):
        b = self._browser_with_three_root_children()
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'G'))
            self.assertEqual(b._state.cursor, 2)
        finally:
            b.stop_workers()


# --- search dispatch ------------------------------------------------------


class TestDispatchSearchStart(unittest.TestCase):

    def test_slash_enters_search_mode(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            self.assertFalse(b._search_mode)
            self.assertTrue(dispatch_key(b, ctx, '/'))
            self.assertTrue(b._search_mode)
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()


class TestDispatchInSearchMode(unittest.TestCase):

    def test_typing_extends_query(self):
        b = _make_browser()
        b._search_mode = True
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'a'))
            self.assertTrue(dispatch_key(b, ctx, 'b'))
            self.assertEqual(b._search_query, 'ab')
        finally:
            b.stop_workers()

    def test_esc_exits_search_mode(self):
        b = _make_browser()
        b._search_mode = True
        b._search_query = 'foo'
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'esc'))
            self.assertFalse(b._search_mode)
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()

    def test_backspace_removes_last_char(self):
        b = _make_browser()
        b._search_mode = True
        b._search_query = 'foo'
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'backspace'))
            self.assertEqual(b._search_query, 'fo')
        finally:
            b.stop_workers()


# --- on_enter dispatch ----------------------------------------------------


class TestDispatchEnterPrintExit(unittest.TestCase):

    def test_print_format_id_emits_cursor_id(self):
        b = _make_browser(print_format='{id}')
        b._state._children[None] = [Item(id='A'), Item(id='B')]
        b._state.cursor = 1
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'enter'))
            b.drain_main_queue()
            self.assertTrue(b._quit_requested)
            self.assertEqual(b._quit_code, 0)
            self.assertTrue(b._quit_output.endswith('\n'))
            self.assertIn('B', b._quit_output)
        finally:
            b.stop_workers()

    def test_no_targets_no_quit(self):
        # Empty visible list → no cursor → no quit triggered.
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'enter')
            b.drain_main_queue()
            self.assertFalse(b._quit_requested)
        finally:
            b.stop_workers()


class TestDispatchEnterActionMode(unittest.TestCase):

    def test_action_redirect_invokes_target_handler(self):
        called = []
        custom = Action('e', handler=lambda c: called.append('e'))
        b = _make_browser(on_enter='action:e', actions=[custom])
        b._state._children[None] = [Item(id='A')]
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'enter'))
            self.assertEqual(called, ['e'])
        finally:
            b.stop_workers()


class TestDispatchEnterNoop(unittest.TestCase):

    def test_noop_does_nothing_observable(self):
        b = _make_browser(on_enter='noop')
        b._state._children[None] = [Item(id='A')]
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'enter'))
            # No quit, no output.
            b.drain_main_queue()
            self.assertFalse(b._quit_requested)
        finally:
            b.stop_workers()

    def test_callable_on_enter_invoked(self):
        seen = []
        def cb(ctx):
            seen.append(ctx)
        b = _make_browser(on_enter=cb)
        b._state._children[None] = [Item(id='A')]
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'enter'))
            self.assertEqual(len(seen), 1)
            self.assertIs(seen[0], ctx)
        finally:
            b.stop_workers()


# --- unbound key ----------------------------------------------------------


class TestDispatchUnboundKey(unittest.TestCase):

    def test_unknown_key_returns_false(self):
        b = _make_browser()
        b._state._children[None] = [Item(id='A')]
        try:
            ctx = _ctx_for(b)
            cursor_before = b._state.cursor
            self.assertFalse(dispatch_key(b, ctx, 'alt-q'))
            # State unchanged.
            self.assertEqual(b._state.cursor, cursor_before)
        finally:
            b.stop_workers()


# --- add_action -----------------------------------------------------------


class TestAddAction(unittest.TestCase):

    def test_add_action_registers_new_key(self):
        b = _make_browser()
        try:
            self.assertEqual(b.actions, [])
            a = Action('e', handler=lambda c: None, label='Edit')
            b.add_action(a)
            self.assertIn(a, b.actions)
        finally:
            b.stop_workers()

    def test_add_action_replaces_existing_for_same_key(self):
        first = Action('e', handler=lambda c: None, label='first')
        second = Action('e', handler=lambda c: None, label='second')
        b = _make_browser(actions=[first])
        try:
            b.add_action(second)
            # Only 'second' should remain for key 'e'.
            es = [a for a in b.actions if a.key == 'e']
            self.assertEqual(len(es), 1)
            self.assertEqual(es[0].label, 'second')
        finally:
            b.stop_workers()


# --- recursive expand/collapse + preview scroll ---------------------------


class TestExpandCollapseRecursive(unittest.TestCase):
    """alt-right / alt-left expand-or-collapse all sibling subtrees."""

    def _tree(self):
        # A two-level tree:
        #   root: [A (kids), B (kids), C (leaf)]
        #   A: [A1 (kids), A2]; A1: [A1a]
        #   B: [B1]
        b = _make_browser()
        s = b._state
        s._children[None] = [
            Item(id='A', has_children=True),
            Item(id='B', has_children=True),
            Item(id='C'),
        ]
        s._children['A'] = [
            Item(id='A1', has_children=True),
            Item(id='A2'),
        ]
        s._children['A1'] = [Item(id='A1a')]
        s._children['B'] = [Item(id='B1')]
        return b

    def test_expand_recursive_expands_all_branch_siblings(self):
        b = self._tree()
        try:
            ctx = _ctx_for(b)
            # Start with cursor on A — its parent is the root.
            self.assertTrue(dispatch_key(b, ctx, 'alt-right'))
            # Branches A, A1, B should all be in expanded; leaves C, A2,
            # A1a, B1 must not.
            self.assertIn('A', b._state.expanded)
            self.assertIn('A1', b._state.expanded)
            self.assertIn('B', b._state.expanded)
            self.assertNotIn('C', b._state.expanded)
            self.assertNotIn('A2', b._state.expanded)
            self.assertNotIn('A1a', b._state.expanded)
        finally:
            b.stop_workers()

    def test_collapse_recursive_drops_all_descendants(self):
        b = self._tree()
        # Pre-expand the tree so we have something to collapse.
        b._state.expanded = {'A', 'A1', 'B'}
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-left'))
            # Everything below the (root) parent collapses.
            self.assertEqual(b._state.expanded, set())
        finally:
            b.stop_workers()


class TestPreviewScrollActions(unittest.TestCase):
    """shift-up/down + alt-pgup/pgdn drive ``_preview_scroll``."""

    def test_shift_down_increments_scroll(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            self.assertEqual(b._preview_scroll, 0)
            dispatch_key(b, ctx, 'shift-down')
            self.assertEqual(b._preview_scroll, 1)
            dispatch_key(b, ctx, 'shift-down')
            self.assertEqual(b._preview_scroll, 2)
        finally:
            b.stop_workers()

    def test_shift_up_decrements_clamped(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._preview_scroll = 2
            dispatch_key(b, ctx, 'shift-up')
            self.assertEqual(b._preview_scroll, 1)
            dispatch_key(b, ctx, 'shift-up')
            self.assertEqual(b._preview_scroll, 0)
            # Already 0 — clamps; doesn't go negative.
            dispatch_key(b, ctx, 'shift-up')
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()

    def test_alt_pgdn_jumps_by_page(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'alt-pgdn')
            # Headless fallback page size is _DEFAULT_PAGE_ROWS (20) —
            # term_size / layout_panes aren't wired in this test module
            # so _preview_pane_height returns the documented default.
            self.assertEqual(b._preview_scroll, 20)
        finally:
            b.stop_workers()

    def test_alt_pgup_jumps_by_page_clamped(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._preview_scroll = 7
            dispatch_key(b, ctx, 'alt-pgup')
            # Clamped at 0.
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()


# --- ticket #75: page sizes track terminal height -------------------------


class TestPageSizesTrackTerminalHeight(unittest.TestCase):
    """``_nav_pgdn`` / ``_nav_pgup`` / ``_preview_page_*`` track viewport.

    The page jump used to be a hard-coded 10 rows; ticket #75 wires it to
    the actual list-pane / preview-pane height returned by
    ``layout_panes``. Headless contexts where ``term_size`` raises (or
    isn't wired) fall back to the documented default
    ``_DEFAULT_PAGE_ROWS`` (= 20).
    """

    def _patch_term(self, cols, rows):
        """Patch term_size / layout_panes onto _actions; return restorer."""
        prev_ts = getattr(_actions, 'term_size', None)
        prev_lp = getattr(_actions, 'layout_panes', None)
        _actions.term_size = lambda: (cols, rows)
        _actions.layout_panes = _render.layout_panes

        def restore():
            if prev_ts is None:
                if hasattr(_actions, 'term_size'):
                    del _actions.term_size
            else:
                _actions.term_size = prev_ts
            if prev_lp is None:
                if hasattr(_actions, 'layout_panes'):
                    del _actions.layout_panes
            else:
                _actions.layout_panes = prev_lp

        return restore

    def test_list_pane_height_matches_layout_at_24_rows(self):
        """Helper returns the same list_height that layout_panes reports."""
        b = _make_browser()
        restore = self._patch_term(80, 24)
        try:
            expected = _render.layout_panes(
                80, 24,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            self.assertEqual(_actions._list_pane_height(b), expected)
        finally:
            restore()
            b.stop_workers()

    def test_list_pane_height_matches_layout_at_60_rows(self):
        """Larger terminals → larger page size."""
        b = _make_browser()
        restore = self._patch_term(120, 60)
        try:
            expected = _render.layout_panes(
                120, 60,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            self.assertEqual(_actions._list_pane_height(b), expected)
            # And it should genuinely be bigger than the 24-row case.
            small = _render.layout_panes(
                80, 24,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            self.assertGreater(expected, small)
        finally:
            restore()
            b.stop_workers()

    def test_headless_falls_back_to_default(self):
        """No term_size wired → returns _DEFAULT_PAGE_ROWS (20)."""
        b = _make_browser()
        # Make sure no patched term_size leaks in from a prior test.
        prev_ts = getattr(_actions, 'term_size', None)
        if prev_ts is not None:
            del _actions.term_size
        try:
            self.assertEqual(
                _actions._list_pane_height(b),
                _actions._DEFAULT_PAGE_ROWS,
            )
            self.assertEqual(
                _actions._preview_pane_height(b),
                _actions._DEFAULT_PAGE_ROWS,
            )
        finally:
            if prev_ts is not None:
                _actions.term_size = prev_ts
            b.stop_workers()

    def test_headless_term_size_raises_falls_back(self):
        """term_size wired but raising OSError → fallback path."""
        b = _make_browser()

        def _raise():
            raise OSError('not a tty')

        prev_ts = getattr(_actions, 'term_size', None)
        prev_lp = getattr(_actions, 'layout_panes', None)
        _actions.term_size = _raise
        _actions.layout_panes = _render.layout_panes
        try:
            self.assertEqual(
                _actions._list_pane_height(b),
                _actions._DEFAULT_PAGE_ROWS,
            )
        finally:
            if prev_ts is None:
                del _actions.term_size
            else:
                _actions.term_size = prev_ts
            if prev_lp is None:
                del _actions.layout_panes
            else:
                _actions.layout_panes = prev_lp
            b.stop_workers()

    def test_pgdn_moves_cursor_by_list_pane_height(self):
        """``pgdn`` advances cursor by the wired list_height (not 10)."""
        b = _make_browser()
        # Populate enough items that the page jump can't be clamped to
        # the end of the list.
        b._state._children[None] = [Item(id=f'i{i}') for i in range(100)]
        restore = self._patch_term(80, 24)
        try:
            ctx = _ctx_for(b)
            expected_page = _render.layout_panes(
                80, 24,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            self.assertEqual(b._state.cursor, 0)
            dispatch_key(b, ctx, 'pgdn')
            self.assertEqual(b._state.cursor, expected_page)
            # The whole point of the ticket: this is NOT the old hard-
            # coded 10. (At 24 rows, list_height is ~7, so the assertion
            # below catches a regression where the literal 10 sneaks
            # back in.)
            self.assertNotEqual(b._state.cursor, 10)
        finally:
            restore()
            b.stop_workers()

    def test_pgdn_uses_bigger_page_at_taller_terminal(self):
        """Resizing the terminal taller → ``pgdn`` jumps further."""
        b = _make_browser()
        b._state._children[None] = [Item(id=f'i{i}') for i in range(200)]

        # Small terminal first.
        restore = self._patch_term(80, 24)
        try:
            ctx = _ctx_for(b)
            small_page = _render.layout_panes(
                80, 24,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            dispatch_key(b, ctx, 'pgdn')
            small_landing = b._state.cursor
            self.assertEqual(small_landing, small_page)
        finally:
            restore()

        # Reset cursor; bigger terminal.
        b._state.cursor = 0
        restore = self._patch_term(80, 60)
        try:
            ctx = _ctx_for(b)
            big_page = _render.layout_panes(
                80, 60,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            dispatch_key(b, ctx, 'pgdn')
            self.assertEqual(b._state.cursor, big_page)
            self.assertGreater(big_page, small_page)
        finally:
            restore()
            b.stop_workers()

    def test_pgup_moves_cursor_by_list_pane_height(self):
        """Symmetric: ``pgup`` retreats cursor by list_height."""
        b = _make_browser()
        b._state._children[None] = [Item(id=f'i{i}') for i in range(100)]
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            expected_page = _render.layout_panes(
                80, 40,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list_height']
            b._state.cursor = 50
            dispatch_key(b, ctx, 'pgup')
            self.assertEqual(b._state.cursor, 50 - expected_page)
        finally:
            restore()
            b.stop_workers()

    def test_alt_pgdn_uses_preview_pane_height(self):
        """``alt-pgdn`` advances preview_scroll by preview pane content rows."""
        b = _make_browser()
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                80, 40,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )
            expected = layout['prev_height'] - 1  # excludes separator
            dispatch_key(b, ctx, 'alt-pgdn')
            self.assertEqual(b._preview_scroll, expected)
        finally:
            restore()
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
