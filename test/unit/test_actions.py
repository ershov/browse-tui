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


class TestPreviewResetOnCursorMove(unittest.TestCase):
    """Cursor moves reset ``_preview_scroll`` and dismiss ``_help_mode``.

    The contract: each cursor move begins a fresh preview view. Stale
    scroll offset from the previous item must not bleed in, and a
    help overlay opened with ``?`` must dismiss as soon as the user
    navigates — they expect the new item's preview, not stale state.
    """

    def _three_root_items(self):
        b = _make_browser()
        b._state._children[None] = [Item(id='A'), Item(id='B'), Item(id='C')]
        # Prime _preview_cursor_id by pretending the preview pipeline
        # has already settled on the cursor row (mirrors the main loop's
        # initial _update_preview_for_cursor call before the user
        # touches the keyboard).
        b._update_preview_for_cursor()
        return b

    def test_cursor_move_resets_preview_scroll(self):
        b = self._three_root_items()
        try:
            ctx = _ctx_for(b)
            b._preview_scroll = 5
            dispatch_key(b, ctx, 'j')
            b._update_preview_for_cursor()
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()

    def test_cursor_move_dismisses_help_mode(self):
        b = self._three_root_items()
        try:
            ctx = _ctx_for(b)
            b._help_mode = True
            dispatch_key(b, ctx, 'j')
            b._update_preview_for_cursor()
            self.assertFalse(b._help_mode)
        finally:
            b.stop_workers()

    def test_no_movement_preserves_scroll_and_help(self):
        """Same cursor item across calls → no reset (idempotent re-fire)."""
        b = self._three_root_items()
        try:
            b._preview_scroll = 4
            b._help_mode = True
            b._update_preview_for_cursor()
            # Cursor didn't change between primer call and this one.
            self.assertEqual(b._preview_scroll, 4)
            self.assertTrue(b._help_mode)
        finally:
            b.stop_workers()

    def test_cursor_move_marks_preview_dirty(self):
        b = self._three_root_items()
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            dispatch_key(b, ctx, 'j')
            # ``j`` itself adds 'preview'; clear and re-fire the helper
            # to confirm the helper alone dirties the preview when the
            # cursor identity changes.
            b._needs_redraw.clear()
            b._state.cursor = 2
            b._update_preview_for_cursor()
            self.assertIn('preview', b._needs_redraw)
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


# --- ticket #117: mouse support ------------------------------------------


class TestMouseDispatch(unittest.TestCase):
    """Mouse click + wheel events route to the pane under the cursor.

    The terminal layer decodes SGR sequences to ``mouse-click:R:C`` /
    ``scroll-up:R:C`` / ``scroll-down:R:C`` strings; the dispatcher
    parses those and looks up the pane via ``layout_panes``. These
    tests patch ``term_size`` + ``layout_panes`` onto ``_actions`` to
    control the geometry, then drive ``dispatch_key`` directly.
    """

    def _patch_term(self, cols, rows):
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

    def _browser_with_n_items(self, n):
        b = _make_browser()
        b._state._children[None] = [Item(id=f'I{i}') for i in range(n)]
        return b

    # ---- click ----------------------------------------------------------

    def test_click_on_list_row_moves_cursor(self):
        b = self._browser_with_n_items(10)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            # Click on the third row of the list (list_top + 2).
            target_row = layout['list_top'] + 2
            dispatch_key(b, ctx, f'mouse-click:{target_row}:5')
            self.assertEqual(b._state.cursor, 2)
        finally:
            restore()
            b.stop_workers()

    def test_click_respects_list_scroll(self):
        """Click row reflects scroll offset: clicking row 1 with scroll=4 → idx 4."""
        b = self._browser_with_n_items(20)
        restore = self._patch_term(80, 40)
        try:
            b._list_scroll = 4
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            target_row = layout['list_top']  # first visible row
            dispatch_key(b, ctx, f'mouse-click:{target_row}:5')
            self.assertEqual(b._state.cursor, 4)
        finally:
            restore()
            b.stop_workers()

    def test_click_past_end_is_noop(self):
        """Click on a row beyond the visible items leaves cursor unchanged."""
        b = self._browser_with_n_items(3)
        restore = self._patch_term(80, 40)
        try:
            b._state.cursor = 1
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            # Click well past the end.
            dispatch_key(b, ctx, f'mouse-click:{layout["list_top"] + 8}:5')
            self.assertEqual(b._state.cursor, 1)
        finally:
            restore()
            b.stop_workers()

    def test_click_on_preview_dismisses_help_mode(self):
        b = self._browser_with_n_items(3)
        restore = self._patch_term(80, 40)
        try:
            b._help_mode = True
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            target_row = layout['prev_top'] + 2  # inside preview content
            dispatch_key(b, ctx, f'mouse-click:{target_row}:5')
            self.assertFalse(b._help_mode)
        finally:
            restore()
            b.stop_workers()

    def test_click_on_separator_is_noop(self):
        b = self._browser_with_n_items(3)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=False)
            cursor_before = b._state.cursor
            scroll_before = b._list_scroll
            # Preview separator row (sub_height==0 → info_row == prev_top).
            dispatch_key(b, ctx, f'mouse-click:{layout["prev_top"]}:5')
            self.assertEqual(b._state.cursor, cursor_before)
            self.assertEqual(b._list_scroll, scroll_before)
        finally:
            restore()
            b.stop_workers()

    # ---- wheel: list pane ----------------------------------------------

    def test_wheel_down_on_list_advances_scroll_not_cursor(self):
        b = self._browser_with_n_items(50)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            cursor_before = b._state.cursor
            target_row = layout['list_top'] + 2
            dispatch_key(b, ctx, f'scroll-down:{target_row}:5')
            self.assertEqual(b._list_scroll, 3)
            self.assertEqual(b._state.cursor, cursor_before)
        finally:
            restore()
            b.stop_workers()

    def test_wheel_up_on_list_decreases_scroll(self):
        b = self._browser_with_n_items(50)
        restore = self._patch_term(80, 40)
        try:
            b._list_scroll = 10
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            target_row = layout['list_top'] + 2
            dispatch_key(b, ctx, f'scroll-up:{target_row}:5')
            self.assertEqual(b._list_scroll, 7)
        finally:
            restore()
            b.stop_workers()

    def test_wheel_up_on_list_clamps_at_zero(self):
        b = self._browser_with_n_items(50)
        restore = self._patch_term(80, 40)
        try:
            b._list_scroll = 1
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            target_row = layout['list_top'] + 2
            dispatch_key(b, ctx, f'scroll-up:{target_row}:5')
            self.assertEqual(b._list_scroll, 0)
        finally:
            restore()
            b.stop_workers()

    # ---- wheel: preview pane -------------------------------------------

    def test_wheel_down_on_preview_advances_preview_scroll(self):
        b = self._browser_with_n_items(3)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            target_row = layout['prev_top'] + 2
            dispatch_key(b, ctx, f'scroll-down:{target_row}:5')
            self.assertEqual(b._preview_scroll, 3)
        finally:
            restore()
            b.stop_workers()

    def test_wheel_up_on_preview_clamps_at_zero(self):
        b = self._browser_with_n_items(3)
        restore = self._patch_term(80, 40)
        try:
            b._preview_scroll = 1
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            target_row = layout['prev_top'] + 2
            dispatch_key(b, ctx, f'scroll-up:{target_row}:5')
            self.assertEqual(b._preview_scroll, 0)
        finally:
            restore()
            b.stop_workers()

    def test_wheel_on_list_does_not_touch_preview_scroll(self):
        b = self._browser_with_n_items(50)
        restore = self._patch_term(80, 40)
        try:
            b._preview_scroll = 5
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            dispatch_key(b, ctx, f'scroll-down:{layout["list_top"]}:5')
            self.assertEqual(b._preview_scroll, 5)
        finally:
            restore()
            b.stop_workers()

    # ---- modal-state interactions --------------------------------------

    def test_search_mode_swallows_mouse_events(self):
        """Mouse click in search mode does not extend the query, does not move cursor."""
        b = self._browser_with_n_items(10)
        restore = self._patch_term(80, 40)
        try:
            b._search_mode = True
            b._search_query = 'foo'
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            cursor_before = b._state.cursor
            dispatch_key(b, ctx, f'mouse-click:{layout["list_top"] + 3}:5')
            self.assertEqual(b._search_query, 'foo')
            self.assertEqual(b._state.cursor, cursor_before)
        finally:
            restore()
            b.stop_workers()

    # ---- headless / unwired fallback -----------------------------------

    def test_headless_no_term_size_is_noop(self):
        """Without term_size patched, mouse events fall through cleanly."""
        b = self._browser_with_n_items(10)
        # Ensure term_size is not on _actions.
        prev_ts = getattr(_actions, 'term_size', None)
        if prev_ts is not None:
            del _actions.term_size
        try:
            ctx = _ctx_for(b)
            cursor_before = b._state.cursor
            scroll_before = b._list_scroll
            dispatch_key(b, ctx, 'mouse-click:5:5')
            dispatch_key(b, ctx, 'scroll-down:5:5')
            self.assertEqual(b._state.cursor, cursor_before)
            self.assertEqual(b._list_scroll, scroll_before)
        finally:
            if prev_ts is not None:
                _actions.term_size = prev_ts
            b.stop_workers()


class TestListScrollDecoupledFromCursor(unittest.TestCase):
    """``_list_scroll`` is independent of ``state.cursor`` after a wheel scroll.

    Pre-fix, ``render_list`` auto-snapped the scroll offset to keep the
    cursor on screen, which fought wheel-scroll. Post-fix, the renderer
    only bounds-clamps; the cursor-on-screen guarantee fires from
    ``Browser._snap_list_scroll_to_row`` driven by the main loop.
    """

    def _browser_with_n_items(self, n):
        b = _make_browser()
        b._state._children[None] = [Item(id=f'I{i}') for i in range(n)]
        return b

    def test_snap_to_row_below_viewport_advances_scroll(self):
        b = self._browser_with_n_items(50)
        # Patch term_size onto _state so the helper can resolve height.
        _state.term_size = lambda: (80, 40)
        _state.layout_panes = _render.layout_panes
        try:
            b._list_scroll = 0
            b._snap_list_scroll_to_row(35)
            self.assertGreater(b._list_scroll, 0)
            self.assertLessEqual(b._list_scroll, 35)
        finally:
            del _state.term_size
            del _state.layout_panes
            b.stop_workers()

    def test_snap_to_row_above_viewport_rewinds_scroll(self):
        b = self._browser_with_n_items(50)
        _state.term_size = lambda: (80, 40)
        _state.layout_panes = _render.layout_panes
        try:
            b._list_scroll = 30
            b._snap_list_scroll_to_row(2)
            self.assertEqual(b._list_scroll, 2)
        finally:
            del _state.term_size
            del _state.layout_panes
            b.stop_workers()

    def test_snap_no_change_when_row_already_visible(self):
        b = self._browser_with_n_items(50)
        _state.term_size = lambda: (80, 40)
        _state.layout_panes = _render.layout_panes
        try:
            b._list_scroll = 5
            scroll_before = b._list_scroll
            b._snap_list_scroll_to_row(7)  # within [5, 5+list_height)
            self.assertEqual(b._list_scroll, scroll_before)
        finally:
            del _state.term_size
            del _state.layout_panes
            b.stop_workers()

    def test_snap_headless_is_noop(self):
        """No term_size wired → snap silently does nothing."""
        b = self._browser_with_n_items(50)
        b._list_scroll = 10
        b._snap_list_scroll_to_row(0)
        # Without term_size, height is 0 → snap returns early.
        self.assertEqual(b._list_scroll, 10)
        b.stop_workers()

    def test_active_list_row_normal_mode_returns_cursor(self):
        b = self._browser_with_n_items(5)
        b._state.cursor = 3
        self.assertEqual(b._active_list_row(), 3)
        b.stop_workers()

    def test_active_list_row_insert_mode_returns_insert_pos(self):
        b = self._browser_with_n_items(5)
        b._insert_mode = True
        b._insert_pos = 2
        b._state.cursor = 4
        self.assertEqual(b._active_list_row(), 2)
        b.stop_workers()


class TestApplyChildrenResultsClampsCursor(unittest.TestCase):
    """``apply_children_results`` clamps ``state.cursor`` to the new list size.

    Regression for ticket #125: a watcher-driven refresh could deliver a
    smaller children list, leaving ``state.cursor`` past the visible
    list end. The renderer then skips the cursor row (no crash) but the
    cursor effectively disappears until the user presses j/k.
    """

    def _browser_with_root(self, ids):
        b = _make_browser()
        b._state._children[None] = [Item(id=i) for i in ids]
        return b

    def test_cursor_clamped_when_list_shrinks(self):
        b = self._browser_with_root(['A', 'B', 'C'])
        b._state.cursor = 2
        # Simulate a worker delivery that shrinks the root.
        b._children_results.append((None, [Item(id='A')]))
        applied = b.apply_children_results()
        self.assertEqual(applied, 1)
        self.assertEqual(b._state.cursor, 0)
        b.stop_workers()

    def test_cursor_clamped_to_zero_when_list_empties(self):
        b = self._browser_with_root(['A', 'B'])
        b._state.cursor = 1
        b._children_results.append((None, []))
        b.apply_children_results()
        self.assertEqual(b._state.cursor, 0)
        b.stop_workers()

    def test_cursor_unchanged_when_still_valid(self):
        b = self._browser_with_root(['A', 'B', 'C', 'D', 'E'])
        b._state.cursor = 1
        b._children_results.append(
            (None, [Item(id=x) for x in ('A', 'B', 'C')])
        )
        b.apply_children_results()
        # Cursor was within the new list — stays put.
        self.assertEqual(b._state.cursor, 1)
        b.stop_workers()

    def test_cursor_unchanged_when_list_grows(self):
        b = self._browser_with_root(['A', 'B'])
        b._state.cursor = 1
        b._children_results.append(
            (None, [Item(id=x) for x in ('A', 'B', 'C', 'D')])
        )
        b.apply_children_results()
        self.assertEqual(b._state.cursor, 1)
        b.stop_workers()


if __name__ == '__main__':
    unittest.main()
