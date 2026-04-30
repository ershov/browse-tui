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
_actions = load('_browse_tui_actions', '070-actions.py')

# Wire up cross-module names — production builds get them via
# concatenation; the test loader needs them by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty


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


if __name__ == '__main__':
    unittest.main()
