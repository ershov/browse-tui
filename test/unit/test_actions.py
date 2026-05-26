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

import io
import os
import sys
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
_render.PreviewRender = _data.PreviewRender
_render.VisibleEntry = _state.VisibleEntry

_actions.write = _term.write
_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.current_scope = _state.current_scope
_actions._search_find = _state._search_find
_actions._search_jump_nearest = _state._search_jump_nearest
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode
_actions.point_in_rect = _render.point_in_rect
_actions._sub_needed_rows = _render._sub_needed_rows
_actions._fmt_child = _render._fmt_child
# term_size / layout_panes are deliberately NOT wired at module scope:
# the existing TestPreviewScrollActions cases verify the headless
# fallback path. The TestPageSizesTrackTerminalHeight cases below
# patch the symbols on _actions inside try/finally to exercise the
# wired path.


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Mode = _state.Mode
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
    return Browser(BrowserConfig(**kw))


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
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertTrue(dispatch_key(b, ctx, '/'))
            self.assertIs(b._mode, Mode.SEARCH_EDIT)
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()


class TestDispatchInSearchMode(unittest.TestCase):

    def test_typing_extends_query(self):
        b = _make_browser()
        b._mode = Mode.SEARCH_EDIT
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'a'))
            self.assertTrue(dispatch_key(b, ctx, 'b'))
            self.assertEqual(b._search_query, 'ab')
        finally:
            b.stop_workers()

    def test_esc_exits_search_mode(self):
        b = _make_browser()
        b._mode = Mode.SEARCH_EDIT
        b._search_query = 'foo'
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'esc'))
            self.assertIs(b._mode, Mode.NORMAL)
            self.assertEqual(b._search_query, '')
        finally:
            b.stop_workers()

    def test_backspace_removes_last_char(self):
        b = _make_browser()
        b._mode = Mode.SEARCH_EDIT
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

    def test_shift_pgdn_is_alias_for_alt_pgdn(self):
        # Shift-PgDn mirrors Alt-PgDn — many terminals intercept the
        # Shift variant for scrollback, but the binding is registered
        # for emulators that pass it through.
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'shift-pgdn')
            self.assertEqual(b._preview_scroll, 20)
        finally:
            b.stop_workers()

    def test_shift_pgup_is_alias_for_alt_pgup(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._preview_scroll = 7
            dispatch_key(b, ctx, 'shift-pgup')
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()

    def test_shift_home_jumps_preview_to_top(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._preview_scroll = 42
            dispatch_key(b, ctx, 'shift-home')
            self.assertEqual(b._preview_scroll, 0)
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_alt_home_jumps_preview_to_top(self):
        # Alt-Home is the universal-fallback binding (terminals that
        # swallow Shift-Home still send Alt-Home).
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._preview_scroll = 42
            dispatch_key(b, ctx, 'alt-home')
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()

    def test_shift_end_engages_tail_pin(self):
        # ``_preview_end`` sets ``_preview_at_tail`` so the renderer
        # forces ``_preview_scroll = max_scroll`` on every paint. The
        # ``_preview_scroll`` value itself is untouched until render runs.
        # See ``docs/superpowers/specs/2026-05-17-preview-tail-design.md``.
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'shift-end')
            self.assertTrue(b._preview_at_tail)
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_alt_end_engages_tail_pin(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            dispatch_key(b, ctx, 'alt-end')
            self.assertTrue(b._preview_at_tail)
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

    def test_scope_root_cursor_triggers_preview_fetch(self):
        """Cursor on the scope_root row requests a preview for its id.

        Recipes that scope into a rich item (browse-claude scoping into
        a .jsonl or a #prompt: umbrella, browse-plan scoping into a
        ticket) want the scope_root's preview visible from the moment
        the user launches — the top row is the first thing the eye
        lands on.
        """
        b = _make_browser(initial_scope='SCOPE_ID')
        try:
            b._update_preview_for_cursor()
            self.assertEqual(b._preview_cursor_id, 'SCOPE_ID')
            self.assertEqual(b._preview_req, 'SCOPE_ID')
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
            )['list'].height
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
            )['list'].height
            self.assertEqual(_actions._list_pane_height(b), expected)
            # And it should genuinely be bigger than the 24-row case.
            small = _render.layout_panes(
                80, 24,
                show_preview=b.show_preview,
                show_children_pane=b.show_children_pane,
            )['list'].height
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
            )['list'].height
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
            )['list'].height
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
            )['list'].height
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
            )['list'].height
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
            expected = layout['preview'].height - 1  # excludes separator
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
            target_row = layout['list'].top + 2
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
            target_row = layout['list'].top  # first visible row
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
            dispatch_key(b, ctx, f'mouse-click:{layout["list"].top + 8}:5')
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
            target_row = layout['preview'].top + 2  # inside preview content
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
            dispatch_key(b, ctx, f'mouse-click:{layout["preview"].top}:5')
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
            target_row = layout['list'].top + 2
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
            target_row = layout['list'].top + 2
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
            target_row = layout['list'].top + 2
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
            target_row = layout['preview'].top + 2
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
            target_row = layout['preview'].top + 2
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
            dispatch_key(b, ctx, f'scroll-down:{layout["list"].top}:5')
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
            b._mode = Mode.SEARCH_EDIT
            b._search_query = 'foo'
            ctx = _ctx_for(b)
            layout = _render.layout_panes(80, 40, show_preview=True,
                                          show_children_pane=True)
            cursor_before = b._state.cursor
            dispatch_key(b, ctx, f'mouse-click:{layout["list"].top + 3}:5')
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


class TestDispatchMouseLayouts(unittest.TestCase):
    """Mouse dispatch in 2D layouts (v / m / pc).

    Pre-#152, ``_pane_at`` did row-only hit-testing — fine for layout
    'h' (full-width panes stacked vertically) but wrong for any 2D
    layout where panes occupy partial-width columns. These tests drive
    each layout family and assert that clicks in each pane region
    route to the correct handler.
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

    def _browser(self, n=10, *, split='h', show_children=True):
        b = _make_browser()
        b._state._children[None] = [Item(id=f'I{i}') for i in range(n)]
        b.split = split
        b.show_children_pane = show_children
        return b

    # ---- layout 'h' (sanity, regression coverage) ----------------------

    def test_dispatch_mouse_horizontal(self):
        b = self._browser(20, split='h', show_children=False)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                80, 40, split='h', show_preview=True, show_children_pane=False,
            )
            # Click in list pane.
            r = layout['list'].top + 3
            dispatch_key(b, ctx, f'mouse-click:{r}:5')
            self.assertEqual(b._state.cursor, 3)
            # Click in preview content (below the separator row).
            b._help_mode = True
            r = layout['preview'].top + 2
            dispatch_key(b, ctx, f'mouse-click:{r}:40')
            self.assertFalse(b._help_mode)
        finally:
            restore()
            b.stop_workers()

    # ---- layout 'v' (side-by-side: list | preview) ---------------------

    def test_dispatch_mouse_vertical(self):
        b = self._browser(20, split='v', show_children=False)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                80, 40, split='v', show_preview=True, show_children_pane=False,
            )
            list_rect = layout['list']
            preview_rect = layout['preview']
            # Click in list (left column).
            r = list_rect.top + 4
            c = list_rect.left + 2
            dispatch_key(b, ctx, f'mouse-click:{r}:{c}')
            self.assertEqual(b._state.cursor, 4)
            # Click in preview (right column) → dismisses help.
            b._help_mode = True
            r = preview_rect.top + 2
            c = preview_rect.left + 2
            dispatch_key(b, ctx, f'mouse-click:{r}:{c}')
            self.assertFalse(b._help_mode)
            # Wheel-scroll in list (left column) bumps list_scroll only.
            preview_before = b._preview_scroll
            r = list_rect.top + 4
            c = list_rect.left + 2
            dispatch_key(b, ctx, f'scroll-down:{r}:{c}')
            self.assertEqual(b._list_scroll, 3)
            self.assertEqual(b._preview_scroll, preview_before)
            # Wheel-scroll in preview (right column) bumps preview_scroll.
            r = preview_rect.top + 2
            c = preview_rect.left + 2
            dispatch_key(b, ctx, f'scroll-down:{r}:{c}')
            self.assertEqual(b._preview_scroll, 3)
        finally:
            restore()
            b.stop_workers()

    def test_dispatch_mouse_vertical_with_children(self):
        # Per #166 layout 'v' is structurally identical to 'pc':
        # ``list | (children-above-preview)`` — children is a ROW at the
        # top of the right column, preview below.
        b = self._browser(5, split='v', show_children=True)
        b._state._children['I0'] = [
            Item(id=f'C{i}') for i in range(3)
        ]
        b._state._children[None] = [
            Item(id='I0', has_children=True),
            *[Item(id=f'I{i}') for i in range(1, 5)],
        ]
        b._state.cursor = 0
        restore = self._patch_term(120, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                120, 40, split='v', show_preview=True, show_children_pane=True,
                children_rows_needed=3,
            )
            children_rect = layout.get('children')
            if children_rect is None:
                # Geometry may degrade to no-children at this size; skip
                # the children-row assertion. Still verify list and
                # preview hit-testing.
                preview_rect = layout['preview']
                list_rect = layout['list']
                dispatch_key(b, ctx, f'mouse-click:{list_rect.top + 1}:'
                             f'{list_rect.left + 1}')
                self.assertEqual(b._state.cursor, 1)
                b._help_mode = True
                dispatch_key(b, ctx,
                             f'mouse-click:{preview_rect.top + 2}:'
                             f'{preview_rect.left + 1}')
                self.assertFalse(b._help_mode)
                return
            preview_rect = layout['preview']
            list_rect = layout['list']
            # Click in children (top of right column) → no list-cursor
            # change, no help dismiss (children clicks are no-ops).
            cursor_before = b._state.cursor
            dispatch_key(b, ctx,
                         f'mouse-click:{children_rect.top}:'
                         f'{children_rect.left + 1}')
            self.assertEqual(b._state.cursor, cursor_before)
            # Click in left (list) column moves cursor.
            dispatch_key(b, ctx,
                         f'mouse-click:{list_rect.top + 2}:'
                         f'{list_rect.left + 1}')
            self.assertEqual(b._state.cursor, 2)
            # Click in preview (bottom of right column) dismisses help.
            b._help_mode = True
            dispatch_key(b, ctx,
                         f'mouse-click:{preview_rect.top + 2}:'
                         f'{preview_rect.left + 1}')
            self.assertFalse(b._help_mode)
        finally:
            restore()
            b.stop_workers()

    # ---- layout 'm' (list+children left, preview right) ----------------

    def test_dispatch_mouse_mixed(self):
        b = self._browser(5, split='m', show_children=True)
        b._state._children['I0'] = [Item(id=f'C{i}') for i in range(3)]
        b._state._children[None] = [
            Item(id='I0', has_children=True),
            *[Item(id=f'I{i}') for i in range(1, 5)],
        ]
        b._state.cursor = 0
        restore = self._patch_term(120, 60)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                120, 60, split='m', show_preview=True, show_children_pane=True,
                children_rows_needed=3,
            )
            list_rect = layout['list']
            preview_rect = layout['preview']
            children_rect = layout.get('children')
            # Click in top-left (list).
            dispatch_key(b, ctx,
                         f'mouse-click:{list_rect.top + 1}:'
                         f'{list_rect.left + 1}')
            self.assertEqual(b._state.cursor, 1)
            # Click in right (preview) column dismisses help.
            b._help_mode = True
            dispatch_key(b, ctx,
                         f'mouse-click:{preview_rect.top + 2}:'
                         f'{preview_rect.left + 1}')
            self.assertFalse(b._help_mode)
            # Click in bottom-left (children) → no-op for cursor.
            if children_rect is not None:
                cursor_before = b._state.cursor
                dispatch_key(b, ctx,
                             f'mouse-click:{children_rect.top + 0}:'
                             f'{children_rect.left + 1}')
                self.assertEqual(b._state.cursor, cursor_before)
        finally:
            restore()
            b.stop_workers()

    # ---- layout 'pc' (list left, children+preview stacked right) ------

    def test_dispatch_mouse_preview_children(self):
        b = self._browser(5, split='pc', show_children=True)
        b._state._children['I0'] = [Item(id=f'C{i}') for i in range(3)]
        b._state._children[None] = [
            Item(id='I0', has_children=True),
            *[Item(id=f'I{i}') for i in range(1, 5)],
        ]
        b._state.cursor = 0
        restore = self._patch_term(120, 60)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                120, 60, split='pc', show_preview=True,
                show_children_pane=True, children_rows_needed=3,
            )
            list_rect = layout['list']
            preview_rect = layout['preview']
            children_rect = layout.get('children')
            # Click left → list. Land on row 0 (item I0) so the cursor
            # stays on the branch with children — the dispatcher
            # re-derives the layout per click using the cursor's children
            # to size the grid, so a click that moved the cursor onto a
            # leaf would collapse the children pane.
            dispatch_key(b, ctx,
                         f'mouse-click:{list_rect.top + 0}:'
                         f'{list_rect.left + 1}')
            self.assertEqual(b._state.cursor, 0)
            # Click bottom-right → preview (dismiss help).
            b._help_mode = True
            dispatch_key(b, ctx,
                         f'mouse-click:{preview_rect.top + 1}:'
                         f'{preview_rect.left + 1}')
            self.assertFalse(b._help_mode)
            # Click top-right → children (no-op for help / cursor).
            if children_rect is not None:
                b._help_mode = True
                cursor_before = b._state.cursor
                dispatch_key(b, ctx,
                             f'mouse-click:{children_rect.top + 0}:'
                             f'{children_rect.left + 1}')
                # Click in children pane should NOT dismiss help (only
                # clicks in preview do).
                self.assertTrue(b._help_mode)
                self.assertEqual(b._state.cursor, cursor_before)
        finally:
            restore()
            b.stop_workers()

    # ---- click outside any pane rect (e.g. info bar) -------------------

    def test_dispatch_mouse_outside_rects(self):
        b = self._browser(10, split='v', show_children=False)
        restore = self._patch_term(80, 40)
        try:
            ctx = _ctx_for(b)
            layout = _render.layout_panes(
                80, 40, split='v', show_preview=True, show_children_pane=False,
            )
            info_bar = layout['info_bar']
            cursor_before = b._state.cursor
            scroll_before = b._list_scroll
            preview_before = b._preview_scroll
            # Click in the bottom info-bar row → recognised as info_bar
            # but no-op for cursor / scroll.
            dispatch_key(b, ctx,
                         f'mouse-click:{info_bar.top}:'
                         f'{info_bar.left + 5}')
            self.assertEqual(b._state.cursor, cursor_before)
            self.assertEqual(b._list_scroll, scroll_before)
            self.assertEqual(b._preview_scroll, preview_before)
        finally:
            restore()
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

    def test_snap_uses_current_list_ratio_after_resize(self):
        """Regression: after the user resizes the split, snap math must
        use the new list_ratio. Pre-fix ``_list_pane_height_safe`` called
        ``layout_panes`` without ``list_ratio=`` so it always produced the
        default 30% pane height — pgup/pgdn and cursor-snap then used the
        wrong height after a -/= resize.
        """
        b = self._browser_with_n_items(200)
        _state.term_size = lambda: (80, 40)
        _state.layout_panes = _render.layout_panes
        try:
            # Default 30% of 40 rows = 12 list rows. Snap to row 50 from
            # scroll=0: scroll should land at 50 - 12 + 1 = 39.
            b._list_scroll = 0
            b._snap_list_scroll_to_row(50)
            self.assertEqual(b._list_scroll, 39)
            # Now grow the list to 80% of 40 = 32 rows. Snap to row 50
            # from scroll=0 should now land at 50 - 32 + 1 = 19.
            b.list_ratio = 0.80
            b._list_scroll = 0
            b._snap_list_scroll_to_row(50)
            self.assertEqual(b._list_scroll, 19,
                             '_list_pane_height_safe must reflect '
                             'the current list_ratio')
        finally:
            del _state.term_size
            del _state.layout_panes
            b.stop_workers()

    def test_snap_uses_current_layout_after_split_switch(self):
        """Regression (mirrors ``test_snap_uses_current_list_ratio_after_resize``):
        after the user switches the split layout (alt-1..alt-4), snap math
        must use the new layout's list-pane height. In ``'h'`` the list
        takes a fraction (30% default) of the body; in ``'v'`` / ``'m'`` /
        ``'pc'`` it spans the full body height. If
        ``_list_pane_height_safe`` cached or hard-coded the 'h' geometry,
        cursor-snap (and pgup/pgdn) would land at the wrong scroll
        position immediately after a layout switch.
        """
        b = self._browser_with_n_items(200)
        _state.term_size = lambda: (80, 40)
        _state.layout_panes = _render.layout_panes
        try:
            # 'h' default: list is 30% of 40 = 12 rows. Snap row 50 from
            # scroll=0 → 50 - 12 + 1 = 39.
            b.set_split('h')
            b.drain_main_queue()
            b._list_scroll = 0
            b._snap_list_scroll_to_row(50)
            self.assertEqual(b._list_scroll, 39,
                             "snap in 'h' must use 12-row list pane")

            # Switch to 'v': list spans the full 39-row body. Snap row
            # 50 from scroll=0 → 50 - 39 + 1 = 12.
            b.set_split('v')
            b.drain_main_queue()
            b._list_scroll = 0
            b._snap_list_scroll_to_row(50)
            self.assertEqual(b._list_scroll, 12,
                             "snap after switch to 'v' must use the new "
                             "(taller) list pane height")

            # Switch back to 'h': scroll math reverts to 12-row list.
            b.set_split('h')
            b.drain_main_queue()
            b._list_scroll = 0
            b._snap_list_scroll_to_row(50)
            self.assertEqual(b._list_scroll, 39,
                             "snap after switching back to 'h' must use "
                             "the 12-row list pane again")

            # 'm' and 'pc' also span full body; sanity-check both.
            for code in ('m', 'pc'):
                b.set_split(code)
                b.drain_main_queue()
                b._list_scroll = 0
                b._snap_list_scroll_to_row(50)
                self.assertEqual(
                    b._list_scroll, 12,
                    f"snap in {code!r} must use the full-body list pane",
                )
        finally:
            del _state.term_size
            del _state.layout_panes
            b.stop_workers()

    def test_pgdn_page_size_tracks_layout_switch(self):
        """Regression: ``pgdn`` jump distance must reflect the current
        layout's list-pane height, not a stale value from the previous
        split. Companion to ``test_snap_uses_current_layout_after_split_switch``;
        both flow through ``_list_pane_height`` (actions) /
        ``_list_pane_height_safe`` (state) which read ``browser.split``
        live.
        """
        b = self._browser_with_n_items(200)
        # Wire term_size + layout_panes onto _actions so _list_pane_height
        # uses the real geometry (matches TestPageSizesTrackTerminalHeight).
        _actions.term_size = lambda: (80, 40)
        _actions.layout_panes = _render.layout_panes
        try:
            b.set_split('h')
            b.drain_main_queue()
            self.assertEqual(_actions._list_pane_height(b), 12,
                             "h: 30%% of 40 rows = 12")

            b.set_split('v')
            b.drain_main_queue()
            self.assertEqual(_actions._list_pane_height(b), 39,
                             "v: list pane spans the full body height")

            b.set_split('m')
            b.drain_main_queue()
            self.assertEqual(_actions._list_pane_height(b), 39,
                             "m: list pane spans the full body height")

            b.set_split('pc')
            b.drain_main_queue()
            self.assertEqual(_actions._list_pane_height(b), 39,
                             "pc: list pane spans the full body height")

            b.set_split('h')
            b.drain_main_queue()
            self.assertEqual(_actions._list_pane_height(b), 12,
                             "back to h: 12-row list pane again")
        finally:
            del _actions.term_size
            del _actions.layout_panes
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


# --- v / e default actions ------------------------------------------------


class TestViewEditDefaults(unittest.TestCase):
    """``v`` views and ``e`` edits the cursor item's preview text.

    The handlers write the cached preview to a tempfile (UTF-8 +
    surrogateescape so non-printable bytes round-trip) and shell out to
    ``$PAGER`` / ``$EDITOR``. Tests stub ``ctx.run_external`` to capture
    the constructed command and read the tempfile while it still exists
    (the handler unlinks in a ``finally``).
    """

    _DEFAULT_GET_PREVIEW = object()  # sentinel for "use a default fetcher"

    def _setup(self, preview_text=None, get_preview=_DEFAULT_GET_PREVIEW,
               show_preview=True):
        # Default to a stub get_preview so the cache-hit path is exercised
        # without each test needing to wire one. Tests that need to
        # exercise the no-fetcher path pass ``get_preview=None`` explicitly.
        if get_preview is self._DEFAULT_GET_PREVIEW:
            get_preview = lambda item_id: f'GENERATED:{item_id}'
        b = _make_browser(show_preview=show_preview, get_preview=get_preview)
        item = Item(id='x', title='X')
        b._state._children[None] = [item]
        b._state._items_by_id['x'] = item
        b._state.cursor = 0
        if preview_text is not None:
            item.preview = preview_text
        ctx = _ctx_for(b)

        captured = {'cmd': None, 'bytes': None, 'existed': None,
                    'messages': [], 'errors': []}

        def stub_run_external(cmd, env=None):
            captured['cmd'] = cmd
            import shlex
            parts = shlex.split(cmd)
            path = parts[-1]
            captured['existed'] = os.path.exists(path)
            if captured['existed']:
                with open(path, 'rb') as f:
                    captured['bytes'] = f.read()
            return 0

        def stub_message(text):
            captured['messages'].append(text)

        def stub_error(text):
            captured['errors'].append(text)

        ctx.run_external = stub_run_external
        b.message = stub_message
        b.error = stub_error
        return b, ctx, captured

    def test_v_pages_preview_with_default_pager_when_unset(self):
        b, ctx, cap = self._setup(preview_text='hello world\n')
        try:
            old = os.environ.pop('PAGER', None)
            try:
                _actions._view_in_pager(ctx)
            finally:
                if old is not None:
                    os.environ['PAGER'] = old
            self.assertIsNotNone(cap['cmd'])
            self.assertTrue(cap['cmd'].startswith('less -R '))
            self.assertTrue(cap['existed'])
            self.assertEqual(cap['bytes'], b'hello world\n')
        finally:
            b.stop_workers()

    def test_v_uses_pager_env_var(self):
        b, ctx, cap = self._setup(preview_text='hi')
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertTrue(cap['cmd'].startswith('cat '))
        finally:
            b.stop_workers()

    def test_e_uses_editor_env_var(self):
        b, ctx, cap = self._setup(preview_text='draft')
        try:
            os.environ['EDITOR'] = 'nano'
            try:
                _actions._edit_in_editor(ctx)
            finally:
                del os.environ['EDITOR']
            self.assertTrue(cap['cmd'].startswith('nano '))
            self.assertEqual(cap['bytes'], b'draft')
        finally:
            b.stop_workers()

    def test_e_falls_back_to_vi_when_unset(self):
        b, ctx, cap = self._setup(preview_text='x')
        try:
            old = os.environ.pop('EDITOR', None)
            try:
                _actions._edit_in_editor(ctx)
            finally:
                if old is not None:
                    os.environ['EDITOR'] = old
            self.assertTrue(cap['cmd'].startswith('vi '))
        finally:
            b.stop_workers()

    def test_v_noop_when_cursor_is_none(self):
        # Empty visible list → ctx.cursor is None → handler returns silently.
        b = _make_browser()
        ctx = _ctx_for(b)
        called = []
        ctx.run_external = lambda cmd, env=None: called.append(cmd) or 0
        try:
            _actions._view_in_pager(ctx)
            self.assertEqual(called, [])
        finally:
            b.stop_workers()

    def test_v_works_on_scope_root_row(self):
        # Scope_root row has ``ctx.cursor is None`` (the property
        # filters synthetic kinds), but its preview is shown in the
        # pane and ``v``/``e`` should operate on it just like a
        # normal row.
        b = _make_browser(get_preview=lambda id_: f'SCOPED_PREVIEW:{id_}')
        scope_id = 'project/scope'
        # Set up a scope so the visible list emits a scope_root entry.
        b._state._children[scope_id] = []
        b._state.scope_stack[:] = [scope_id]
        b._state.cursor = 0  # the scope_root row
        ctx = _ctx_for(b)
        captured = {'cmd': None, 'bytes': None}

        def stub_run_external(cmd, env=None):
            captured['cmd'] = cmd
            import shlex
            path = shlex.split(cmd)[-1]
            if os.path.exists(path):
                with open(path, 'rb') as f:
                    captured['bytes'] = f.read()
            return 0

        ctx.run_external = stub_run_external
        try:
            _actions._view_in_pager(ctx)
            self.assertIsNotNone(
                captured['cmd'],
                '_view_in_pager must operate on scope_root rows '
                '(their preview is shown in the pane)',
            )
            self.assertEqual(
                captured['bytes'], b'SCOPED_PREVIEW:project/scope',
                'scope_root preview text should reach the pager',
            )
        finally:
            b.stop_workers()

    def test_v_messages_when_no_get_preview_and_no_cache(self):
        # No cache + no get_preview fetcher → 'No preview available'.
        b, ctx, cap = self._setup(preview_text=None, get_preview=None)
        try:
            _actions._view_in_pager(ctx)
            self.assertIsNone(cap['cmd'])
            self.assertEqual(cap['messages'], ['No preview available'])
        finally:
            b.stop_workers()

    def test_e_messages_when_no_get_preview_and_no_cache(self):
        b, ctx, cap = self._setup(preview_text=None, get_preview=None)
        try:
            _actions._edit_in_editor(ctx)
            self.assertIsNone(cap['cmd'])
            self.assertEqual(cap['messages'], ['No preview available'])
        finally:
            b.stop_workers()

    def test_v_falls_back_to_get_preview_on_cache_miss(self):
        # Cache miss + recipe-supplied get_preview → synchronous fetch,
        # tempfile gets the generated text, and the result is cached.
        called = []

        def get_preview(item_id):
            called.append(item_id)
            return f'GENERATED:{item_id}'

        b, ctx, cap = self._setup(preview_text=None, get_preview=get_preview)
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertEqual(called, ['x'])
            self.assertEqual(cap['bytes'], b'GENERATED:x')
            # Cached for next time.
            self.assertEqual(b._state._items_by_id['x'].preview, 'GENERATED:x')
        finally:
            b.stop_workers()

    def test_e_falls_back_to_get_preview_on_cache_miss(self):
        def get_preview(item_id):
            return f'EDIT:{item_id}'

        b, ctx, cap = self._setup(preview_text=None, get_preview=get_preview)
        try:
            os.environ['EDITOR'] = 'cat'
            try:
                _actions._edit_in_editor(ctx)
            finally:
                del os.environ['EDITOR']
            self.assertEqual(cap['bytes'], b'EDIT:x')
            self.assertEqual(b._state._items_by_id['x'].preview, 'EDIT:x')
        finally:
            b.stop_workers()

    def test_works_when_preview_pane_hidden(self):
        # User has the preview pane disabled. Cache miss falls through
        # to get_preview just like when the pane is visible.
        b, ctx, cap = self._setup(
            preview_text=None,
            get_preview=lambda i: f'HIDDEN:{i}',
            show_preview=False,
        )
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertFalse(b.show_preview)
            self.assertEqual(cap['bytes'], b'HIDDEN:x')
        finally:
            b.stop_workers()

    def test_cache_hit_skips_get_preview(self):
        # Cached entry takes precedence over the fetcher.
        called = []

        def get_preview(item_id):
            called.append(item_id)
            return 'SHOULD-NOT-BE-USED'

        b, ctx, cap = self._setup(
            preview_text='from-cache',
            get_preview=get_preview,
        )
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertEqual(called, [])
            self.assertEqual(cap['bytes'], b'from-cache')
        finally:
            b.stop_workers()

    def test_get_preview_returns_none_messages(self):
        # get_preview returned None → no content to show.
        b, ctx, cap = self._setup(
            preview_text=None,
            get_preview=lambda i: None,
        )
        try:
            _actions._view_in_pager(ctx)
            self.assertIsNone(cap['cmd'])
            self.assertEqual(cap['messages'], ['No preview available'])
        finally:
            b.stop_workers()

    def test_get_preview_raises_surfaces_error(self):
        def boom(item_id):
            raise RuntimeError('connection refused')

        b, ctx, cap = self._setup(preview_text=None, get_preview=boom)
        try:
            _actions._view_in_pager(ctx)
            self.assertIsNone(cap['cmd'])
            self.assertEqual(len(cap['errors']), 1)
            self.assertIn('RuntimeError', cap['errors'][0])
            self.assertIn('connection refused', cap['errors'][0])
        finally:
            b.stop_workers()

    def test_empty_string_cache_is_used_not_treated_as_miss(self):
        # An empty preview ('') is a legitimate cached result and must
        # not trigger a refetch.
        called = []

        def get_preview(item_id):
            called.append(item_id)
            return 'SHOULD-NOT-BE-USED'

        b, ctx, cap = self._setup(preview_text='', get_preview=get_preview)
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertEqual(called, [])
            self.assertEqual(cap['bytes'], b'')
        finally:
            b.stop_workers()

    def test_no_get_preview_takes_precedence_over_empty_cache(self):
        # The preview worker fills the cache with '' when get_preview is
        # None (see ``_preview_worker``). That placeholder must NOT be
        # treated as a legitimate empty preview — bail with a message
        # rather than open an empty pager / editor.
        b, ctx, cap = self._setup(preview_text='', get_preview=None)
        try:
            _actions._view_in_pager(ctx)
            self.assertIsNone(cap['cmd'])
            self.assertEqual(cap['messages'], ['No preview available'])
        finally:
            b.stop_workers()

    def test_preview_with_control_chars_roundtrips(self):
        # Non-printable bytes (NUL, BEL, ESC, DEL) must reach the
        # tempfile verbatim — no replacement '?'s.
        text = 'A\x00B\x07C\x1bD\x7fE'
        b, ctx, cap = self._setup(preview_text=text)
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertEqual(
                cap['bytes'],
                b'A\x00B\x07C\x1bD\x7fE',
                'control chars should be preserved verbatim',
            )
        finally:
            b.stop_workers()

    def test_preview_with_surrogate_escape_roundtrips(self):
        # Non-decodable bytes (e.g. 0xff in a "UTF-8" filesystem read)
        # become lone surrogates U+DCFF when decoded with surrogateescape;
        # encoding back with surrogateescape must restore the original
        # byte rather than replace it with '?'.
        raw = b'before \xff\xfe after'
        decoded = raw.decode('utf-8', errors='surrogateescape')
        b, ctx, cap = self._setup(preview_text=decoded)
        try:
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertEqual(cap['bytes'], raw)
        finally:
            b.stop_workers()

    def test_tempfile_unlinked_after_run(self):
        b, ctx, cap = self._setup(preview_text='hi')
        try:
            seen_path = []

            def stub(cmd, env=None):
                import shlex
                seen_path.append(shlex.split(cmd)[-1])
                return 0

            ctx.run_external = stub
            os.environ['PAGER'] = 'cat'
            try:
                _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            # File existed during the call; no longer exists after.
            self.assertEqual(len(seen_path), 1)
            self.assertFalse(os.path.exists(seen_path[0]))
        finally:
            b.stop_workers()

    def test_tempfile_unlinked_even_if_run_external_raises(self):
        # The handler wraps the run in try/finally; an exception from
        # run_external must not leak the tempfile.
        b, ctx, cap = self._setup(preview_text='hi')
        try:
            seen_path = []

            def boom(cmd, env=None):
                import shlex
                seen_path.append(shlex.split(cmd)[-1])
                raise RuntimeError('nope')

            ctx.run_external = boom
            os.environ['PAGER'] = 'cat'
            try:
                with self.assertRaises(RuntimeError):
                    _actions._view_in_pager(ctx)
            finally:
                del os.environ['PAGER']
            self.assertEqual(len(seen_path), 1)
            self.assertFalse(os.path.exists(seen_path[0]))
        finally:
            b.stop_workers()

    def test_v_and_e_in_default_actions(self):
        # Sanity: both keys are wired into default_actions() with the
        # expected handlers, in the OTHER section, gated on 'none' so
        # they also fire on scope_root rows (handler resolves the
        # visible entry itself and skips pending placeholders).
        keys = {a.key: a for a in default_actions()}
        self.assertIn('v', keys)
        self.assertIn('e', keys)
        self.assertIs(keys['v'].handler, _actions._view_in_pager)
        self.assertIs(keys['e'].handler, _actions._edit_in_editor)
        self.assertEqual(keys['v'].requires, 'none')
        self.assertEqual(keys['e'].requires, 'none')
        self.assertEqual(keys['v'].section, 'OTHER')
        self.assertEqual(keys['e'].section, 'OTHER')


# --- list/preview split resize -------------------------------------------


class TestResizeStepFormula(unittest.TestCase):
    """``_resize_step(list_h, prev_content) = (min(list, prev) // 5) + 1``."""

    def test_user_examples(self):
        # 40-line list, 50-line preview content: (40 // 5) + 1 = 9.
        # (User's spec moved from 10% to 20% with the +1 floor.)
        self.assertEqual(_actions._resize_step(40, 50), 9)
        self.assertEqual(_actions._resize_step(30, 60), 7)

    def test_floor_at_one(self):
        # Tiny panes still nudge by at least 1 row.
        self.assertEqual(_actions._resize_step(0, 100), 1)
        self.assertEqual(_actions._resize_step(1, 1), 1)
        self.assertEqual(_actions._resize_step(4, 100), 1)

    def test_uses_smaller_pane(self):
        # min(list, prev) drives the step regardless of which is smaller.
        self.assertEqual(_actions._resize_step(10, 80), 3)  # 10//5 + 1
        self.assertEqual(_actions._resize_step(80, 10), 3)


class TestShrinkGrowList(unittest.TestCase):
    """``_shrink_list`` / ``_grow_list`` mutate ``browser.list_ratio``.

    The handlers read the current layout to compute the step. Tests
    inject ``term_size`` and ``layout_panes`` on the actions module so
    the headless Browser produces a deterministic geometry.
    """

    def setUp(self):
        # Inject layout dependencies on the actions module.
        self._saved_term_size = getattr(_actions, 'term_size', None)
        self._saved_layout_panes = getattr(_actions, 'layout_panes', None)
        _actions.term_size = lambda: (80, 100)
        _actions.layout_panes = _render.layout_panes
        # The clamp helper is normally injected via concatenation; tests
        # need it for the post-resize sanity clamp inside _resize_list.
        _actions._clamp_list_ratio = _state._clamp_list_ratio

    def tearDown(self):
        if self._saved_term_size is not None:
            _actions.term_size = self._saved_term_size
        else:
            _actions.term_size = None
        if self._saved_layout_panes is not None:
            _actions.layout_panes = self._saved_layout_panes
        else:
            _actions.layout_panes = None

    def test_shrink_decreases_list_ratio(self):
        b = _make_browser(list_ratio=0.50)
        try:
            ctx = _ctx_for(b)
            before = b.list_ratio
            _actions._shrink_list(ctx)
            self.assertLess(b.list_ratio, before)
        finally:
            b.stop_workers()

    def test_grow_increases_list_ratio(self):
        b = _make_browser(list_ratio=0.30)
        try:
            ctx = _ctx_for(b)
            before = b.list_ratio
            _actions._grow_list(ctx)
            self.assertGreater(b.list_ratio, before)
        finally:
            b.stop_workers()

    def test_step_matches_user_formula(self):
        # 80×100 terminal, ratio 0.50 → list=50, prev=50 (sep+49 content).
        # Step = (min(50, 49) // 5) + 1 = 10. Shrink → list_h goes 50→40.
        b = _make_browser(list_ratio=0.50)
        try:
            ctx = _ctx_for(b)
            _actions._shrink_list(ctx)
            # New ratio reflects 40/100 = 0.40.
            self.assertAlmostEqual(b.list_ratio, 0.40, places=4)
        finally:
            b.stop_workers()

    def test_shrink_floors_at_one_list_row(self):
        # Repeated shrinking should never push list_h below 1.
        b = _make_browser(list_ratio=0.50)
        try:
            ctx = _ctx_for(b)
            for _ in range(50):
                _actions._shrink_list(ctx)
            cols, rows = (80, 100)
            layout = _render.layout_panes(
                cols, rows, show_preview=True, list_ratio=b.list_ratio,
            )
            self.assertGreaterEqual(layout['list'].height, 1)
        finally:
            b.stop_workers()

    def test_grow_caps_to_leave_preview_content(self):
        # Repeated growing should leave at least 1 preview content row.
        b = _make_browser(list_ratio=0.50)
        try:
            ctx = _ctx_for(b)
            for _ in range(50):
                _actions._grow_list(ctx)
            cols, rows = (80, 100)
            layout = _render.layout_panes(
                cols, rows, show_preview=True, list_ratio=b.list_ratio,
            )
            self.assertGreaterEqual(layout['preview'].height, 2)
        finally:
            b.stop_workers()

    def test_noop_when_preview_hidden(self):
        # show_preview=False → -/= are no-ops; ratio untouched.
        b = _make_browser(list_ratio=0.50, show_preview=False)
        try:
            ctx = _ctx_for(b)
            before = b.list_ratio
            _actions._shrink_list(ctx)
            _actions._grow_list(ctx)
            self.assertEqual(b.list_ratio, before)
        finally:
            b.stop_workers()

    def test_minus_underscore_equals_plus_all_bound(self):
        keys = {a.key: a for a in default_actions()}
        for k in ('-', '_'):
            self.assertIn(k, keys)
            self.assertIs(keys[k].handler, _actions._shrink_list)
        for k in ('=', '+'):
            self.assertIn(k, keys)
            self.assertIs(keys[k].handler, _actions._grow_list)

    def test_dispatch_minus_resizes(self):
        # End-to-end via dispatch_key: pressing '-' shrinks the list.
        b = _make_browser(list_ratio=0.50)
        try:
            ctx = _ctx_for(b)
            before = b.list_ratio
            self.assertTrue(dispatch_key(b, ctx, '-'))
            self.assertLess(b.list_ratio, before)
        finally:
            b.stop_workers()

    def test_dispatch_equals_resizes(self):
        b = _make_browser(list_ratio=0.30)
        try:
            ctx = _ctx_for(b)
            before = b.list_ratio
            self.assertTrue(dispatch_key(b, ctx, '='))
            self.assertGreater(b.list_ratio, before)
        finally:
            b.stop_workers()

    def test_resize_persists_across_simulated_terminal_resize(self):
        # The ratio (not the line count) is what's stored. After a
        # simulated terminal resize, the same ratio yields a
        # proportionally-different list height.
        b = _make_browser(list_ratio=0.50)
        try:
            ctx = _ctx_for(b)
            _actions._shrink_list(ctx)
            ratio = b.list_ratio
            # Now ask layout for the size at a smaller terminal.
            small = _render.layout_panes(80, 50, show_preview=True,
                                         list_ratio=ratio)
            big = _render.layout_panes(80, 200, show_preview=True,
                                       list_ratio=ratio)
            # 0.40 ratio → 50 rows = 20 list, 200 rows = 80 list.
            self.assertEqual(small['list'].height, int(50 * ratio))
            self.assertEqual(big['list'].height, int(200 * ratio))
        finally:
            b.stop_workers()

    # ---- axis-aware resize (#166) ------------------------------------

    def test_resize_list_uses_rows_in_horizontal(self):
        # Regression: in layout 'h' the resize step is computed from
        # rows (list height vs preview content height) and the new
        # ratio is new_list_h / rows.
        b = _make_browser(list_ratio=0.50, split='h')
        try:
            ctx = _ctx_for(b)
            # 80×100, ratio 0.50 → list_h=50, prev=50 (sep+49 content).
            # Step = (min(50,49)//5)+1 = 10. Shrink → 50→40 → 0.40.
            _actions._shrink_list(ctx)
            self.assertAlmostEqual(b.list_ratio, 0.40, places=4)
        finally:
            b.stop_workers()

    def test_resize_list_uses_cols_in_vertical(self):
        # In layout 'v' the primary axis is cols. With cols=80, ratio
        # 0.50 → list_w=40, sep_main=1, preview_w=39. Step =
        # (min(40,39)//5)+1 = 8. Shrink → 40→32 → ratio 32/80 = 0.40.
        b = _make_browser(list_ratio=0.50, split='v')
        try:
            ctx = _ctx_for(b)
            _actions._shrink_list(ctx)
            self.assertAlmostEqual(b.list_ratio, 0.40, places=4)
        finally:
            b.stop_workers()

    def test_resize_list_uses_cols_in_mixed(self):
        # Same col-based math for layout 'm': list_w=40, preview_w=39,
        # step=8. Shrink → 40→32 → ratio 0.40.
        b = _make_browser(list_ratio=0.50, split='m')
        try:
            ctx = _ctx_for(b)
            _actions._shrink_list(ctx)
            self.assertAlmostEqual(b.list_ratio, 0.40, places=4)
        finally:
            b.stop_workers()

    def test_resize_list_uses_cols_in_preview_children(self):
        # Same col-based math for layout 'pc'.
        b = _make_browser(list_ratio=0.50, split='pc')
        try:
            ctx = _ctx_for(b)
            _actions._shrink_list(ctx)
            self.assertAlmostEqual(b.list_ratio, 0.40, places=4)
        finally:
            b.stop_workers()

    def test_grow_list_uses_cols_in_vertical(self):
        # Symmetric grow in 'v': start at 0.30 → list_w=24, preview_w=55.
        # step = (min(24,55)//5)+1 = 5. Grow → 24→29 → 29/80 = 0.3625.
        b = _make_browser(list_ratio=0.30, split='v')
        try:
            ctx = _ctx_for(b)
            _actions._grow_list(ctx)
            self.assertAlmostEqual(b.list_ratio, 29 / 80.0, places=4)
        finally:
            b.stop_workers()


class TestSetListRatio(unittest.TestCase):
    """``Browser.set_list_ratio`` clamps to the safe range."""

    def test_in_range_value_set_verbatim(self):
        b = _make_browser()
        try:
            b.set_list_ratio(0.42)
            b.drain_main_queue()
            self.assertEqual(b.list_ratio, 0.42)
        finally:
            b.stop_workers()

    def test_above_max_clamped(self):
        b = _make_browser()
        try:
            b.set_list_ratio(5.0)
            b.drain_main_queue()
            self.assertLess(b.list_ratio, 1.0)
            self.assertGreaterEqual(b.list_ratio, _state._LIST_RATIO_MAX)
        finally:
            b.stop_workers()

    def test_below_min_clamped(self):
        b = _make_browser()
        try:
            b.set_list_ratio(-0.5)
            b.drain_main_queue()
            self.assertGreater(b.list_ratio, 0.0)
            self.assertLessEqual(b.list_ratio, _state._LIST_RATIO_MIN)
        finally:
            b.stop_workers()

    def test_invalid_input_falls_back_to_default(self):
        # Non-numeric input shouldn't crash; use sentinel default.
        b = _make_browser()
        try:
            b.set_list_ratio('not a number')  # type: ignore[arg-type]
            b.drain_main_queue()
            self.assertAlmostEqual(b.list_ratio, 0.30)
        finally:
            b.stop_workers()


class TestLayoutSplitActions(unittest.TestCase):
    """Alt-1..4 set ``browser.split`` directly; ``\\`` cycles v→h→m→pc→v.

    All five handlers route through ``Browser.set_split``, which clamps
    invalid values and adds ``'all'`` to ``_needs_redraw`` — so the tests
    exercise both the state mutation and the redraw flag.
    """

    def test_set_layout_v_updates_browser_split(self):
        b = _make_browser(split='h')
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            _actions._set_layout_v(ctx)
            b.drain_main_queue()
            self.assertEqual(b.split, 'v')
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_set_layout_h_updates_browser_split(self):
        b = _make_browser(split='v')
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            _actions._set_layout_h(ctx)
            b.drain_main_queue()
            self.assertEqual(b.split, 'h')
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_set_layout_m_updates_browser_split(self):
        b = _make_browser(split='h')
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            _actions._set_layout_m(ctx)
            b.drain_main_queue()
            self.assertEqual(b.split, 'm')
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_set_layout_pc_updates_browser_split(self):
        b = _make_browser(split='h')
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            _actions._set_layout_pc(ctx)
            b.drain_main_queue()
            self.assertEqual(b.split, 'pc')
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_cycle_layout_cycles_in_order(self):
        # Starting at 'v', four cycles should return: h, m, pc, v.
        b = _make_browser(split='v')
        try:
            ctx = _ctx_for(b)
            seq = []
            for _ in range(4):
                _actions._cycle_layout(ctx)
                b.drain_main_queue()
                seq.append(b.split)
            self.assertEqual(seq, ['h', 'm', 'pc', 'v'])
        finally:
            b.stop_workers()

    def test_cycle_layout_from_each_starting_point(self):
        # Symmetry check: every layout, when cycled once, lands on the
        # documented next one.
        next_of = {'v': 'h', 'h': 'm', 'm': 'pc', 'pc': 'v'}
        for start, expected in next_of.items():
            b = _make_browser(split=start)
            try:
                ctx = _ctx_for(b)
                _actions._cycle_layout(ctx)
                b.drain_main_queue()
                self.assertEqual(
                    b.split, expected,
                    f'{start!r} should cycle to {expected!r}, got {b.split!r}',
                )
            finally:
                b.stop_workers()

    def test_cycle_layout_from_unknown_state_falls_back(self):
        # Defensive: if browser.split somehow holds a value outside the
        # cycle (set_split clamps inputs, but tests can poke directly),
        # cycle should land on the first entry rather than raise.
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b.split = 'bogus'  # bypass set_split's clamp on purpose
            _actions._cycle_layout(ctx)
            b.drain_main_queue()
            self.assertEqual(b.split, _actions._LAYOUT_CYCLE[0])
        finally:
            b.stop_workers()

    def test_layout_keys_all_bound(self):
        keys = {a.key: a for a in default_actions()}
        self.assertIs(keys['\\'].handler, _actions._cycle_layout)
        self.assertIs(keys['alt-1'].handler, _actions._set_layout_v)
        self.assertIs(keys['alt-2'].handler, _actions._set_layout_h)
        self.assertIs(keys['alt-3'].handler, _actions._set_layout_m)
        self.assertIs(keys['alt-4'].handler, _actions._set_layout_pc)

    def test_dispatch_alt_1_sets_layout_v(self):
        b = _make_browser(split='h')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-1'))
            b.drain_main_queue()
            self.assertEqual(b.split, 'v')
        finally:
            b.stop_workers()

    def test_dispatch_alt_2_sets_layout_h(self):
        b = _make_browser(split='v')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-2'))
            b.drain_main_queue()
            self.assertEqual(b.split, 'h')
        finally:
            b.stop_workers()

    def test_dispatch_alt_3_sets_layout_m(self):
        b = _make_browser(split='h')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-3'))
            b.drain_main_queue()
            self.assertEqual(b.split, 'm')
        finally:
            b.stop_workers()

    def test_dispatch_alt_4_sets_layout_pc(self):
        b = _make_browser(split='h')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-4'))
            b.drain_main_queue()
            self.assertEqual(b.split, 'pc')
        finally:
            b.stop_workers()

    def test_dispatch_backslash_cycles_layout(self):
        b = _make_browser(split='v')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, '\\'))
            b.drain_main_queue()
            self.assertEqual(b.split, 'h')
        finally:
            b.stop_workers()


class TestRedrawAction(unittest.TestCase):
    """Ctrl-L (``_redraw``): clear screen, drop pane cache, flag 'all' dirty.

    Ticket #189. The action is the user-initiated explicit redraw bound
    to Ctrl-L. It must replicate the empty-screen first-paint path on
    the next render — so it emits ``\\e[2J``, clears ``_pane_cache``
    (so each pane's ``prev_rect`` resets to ``None``), and adds
    ``'all'`` to ``_needs_redraw`` so the next loop tick runs
    ``render_full``.
    """

    def test_redraw_emits_clear_screen(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            saved = sys.stdout
            buf = io.StringIO()
            sys.stdout = buf
            try:
                _actions._redraw(ctx)
            finally:
                sys.stdout = saved
            self.assertIn('\033[2J', buf.getvalue())
        finally:
            b.stop_workers()

    def test_redraw_clears_pane_cache(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            # Seed the cache with a sentinel so we can confirm it's wiped.
            b._pane_cache['list'] = object()
            b._pane_cache['preview'] = object()
            saved = sys.stdout
            sys.stdout = io.StringIO()
            try:
                _actions._redraw(ctx)
            finally:
                sys.stdout = saved
            self.assertEqual(b._pane_cache, {})
        finally:
            b.stop_workers()

    def test_redraw_marks_all_dirty(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            saved = sys.stdout
            sys.stdout = io.StringIO()
            try:
                _actions._redraw(ctx)
            finally:
                sys.stdout = saved
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_redraw_dispatched_via_ctrl_l(self):
        # Pin the keybinding so the action stays wired to Ctrl-L.
        keys = {a.key: a for a in default_actions()}
        self.assertIn('ctrl-l', keys)
        self.assertIs(keys['ctrl-l'].handler, _actions._redraw)


class TestTogglePreviewAnsi(unittest.TestCase):
    """Capital-R (``_toggle_preview_ansi``) flips ``preview_ansi`` and
    flags the preview pane dirty (#244).

    The action does NOT clear ``_pane_cache`` — invalidation is driven by
    the per-row byte-stream comparison in ``end_row``: colour-bearing
    rows produce different bytes and redraw, plain rows stay cache-hit.
    """

    def test_toggle_flips_flag_true_to_false(self):
        b = _make_browser()
        try:
            self.assertTrue(b.preview_ansi)
            ctx = _ctx_for(b)
            _actions._toggle_preview_ansi(ctx)
            self.assertFalse(b.preview_ansi)
        finally:
            b.stop_workers()

    def test_toggle_flips_flag_false_to_true(self):
        b = _make_browser(preview_ansi=False)
        try:
            self.assertFalse(b.preview_ansi)
            ctx = _ctx_for(b)
            _actions._toggle_preview_ansi(ctx)
            self.assertTrue(b.preview_ansi)
        finally:
            b.stop_workers()

    def test_toggle_marks_preview_dirty(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            _actions._toggle_preview_ansi(ctx)
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_capital_r_dispatches_toggle(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            self.assertTrue(b.preview_ansi)
            self.assertTrue(dispatch_key(b, ctx, 'R'))
            self.assertFalse(b.preview_ansi)
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_capital_r_keybinding_registered(self):
        # Pin the keybinding so the action stays wired to capital-R.
        keys = {a.key: a for a in default_actions()}
        self.assertIn('R', keys)
        self.assertIs(keys['R'].handler, _actions._toggle_preview_ansi)


class TestToggleChildrenPane(unittest.TestCase):
    """Alt-P (``_toggle_children_pane``) flips ``show_children_pane`` and
    forces a full redraw (the children pane appearing/disappearing
    reshapes the layout, so every pane may shift).
    """

    def test_toggle_flips_flag_true_to_false(self):
        b = _make_browser()
        try:
            self.assertTrue(b.show_children_pane)
            ctx = _ctx_for(b)
            _actions._toggle_children_pane(ctx)
            self.assertFalse(b.show_children_pane)
        finally:
            b.stop_workers()

    def test_toggle_flips_flag_false_to_true(self):
        b = _make_browser(show_children_pane=False)
        try:
            self.assertFalse(b.show_children_pane)
            ctx = _ctx_for(b)
            _actions._toggle_children_pane(ctx)
            self.assertTrue(b.show_children_pane)
        finally:
            b.stop_workers()

    def test_toggle_marks_all_dirty(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            b._needs_redraw.clear()
            _actions._toggle_children_pane(ctx)
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_alt_p_dispatches_toggle(self):
        b = _make_browser()
        try:
            ctx = _ctx_for(b)
            self.assertTrue(b.show_children_pane)
            self.assertTrue(dispatch_key(b, ctx, 'alt-p'))
            self.assertFalse(b.show_children_pane)
            self.assertIn('all', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_alt_p_keybinding_registered(self):
        # Pin the keybinding so the action stays wired to Alt-P.
        keys = {a.key: a for a in default_actions()}
        self.assertIn('alt-p', keys)
        self.assertIs(keys['alt-p'].handler, _actions._toggle_children_pane)


class TestAltDigitParsing(unittest.TestCase):
    """``read_key`` (020-terminal) maps ``ESC + 'N'`` → ``'alt-N'``.

    The Alt-1..4 layout keybindings rely on this Meta-prefix encoding —
    if it ever regressed, the layout-switch keys would silently stop
    routing. Pin the contract here so the link is explicit.
    """

    def _read_key_from_bytes(self, payload: bytes) -> str:
        # ``read_key`` reads from ``sys.stdin.fileno()`` directly; a pipe
        # dup'd over fd 0 is the simplest way to feed canned bytes
        # through the production parser without monkey-patching the
        # module's internals.
        import sys
        r, w = os.pipe()
        os.write(w, payload)
        os.close(w)
        saved_stdin_fd = os.dup(0)
        try:
            os.dup2(r, 0)
            # ``sys.stdin`` caches the underlying fd object; clear any
            # buffered state by ensuring we read via fileno() (read_key
            # uses os.read on the bare fd, so the cache is irrelevant).
            return _term.read_key()
        finally:
            os.dup2(saved_stdin_fd, 0)
            os.close(saved_stdin_fd)
            try:
                os.close(r)
            except OSError:
                pass

    def test_alt_digit_parsing(self):
        for digit in ('1', '2', '3', '4'):
            with self.subTest(digit=digit):
                key = self._read_key_from_bytes(b'\x1b' + digit.encode())
                self.assertEqual(key, 'alt-' + digit)


if __name__ == '__main__':
    unittest.main()
