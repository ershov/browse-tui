"""Tests for the public selection-helper API.

``select_all_visible`` / ``clear_selection`` / ``invert_selection``
all mutate ``state.selected`` on the main thread via the post queue.
WYSIWYG semantics: anything outside the visible list (hidden,
collapsed, out-of-scope) is dropped or untouched as documented.
"""

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


def _seed(b):
    b.update_data([
        ('upsert', 'a', None, {'has_children': True}),
        ('upsert', 'b', None, {}),
        ('upsert', 'c', None, {}),
        ('upsert', 'a1', 'a', {}),
    ])
    # 'a' is not expanded so a1 is invisible.
    b.drain_main_queue()


class TestSelectAllVisible(unittest.TestCase):

    def test_selects_visible(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.select_all_visible()
        b.drain_main_queue()
        # 'a', 'b', 'c' visible; 'a1' is under collapsed 'a' so hidden.
        self.assertEqual(b._state.selected, {'a', 'b', 'c'})

    def test_drops_invisible_previously_selected(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        # Pre-select an item under a collapsed parent.
        b._state.selected = {'a1'}
        b.select_all_visible()
        b.drain_main_queue()
        # 'a1' dropped because it's not visible; 'a','b','c' added.
        self.assertNotIn('a1', b._state.selected)
        self.assertEqual(b._state.selected, {'a', 'b', 'c'})

    def test_includes_scope_row_when_scoped(self):
        # After scope-root unification the scope row at depth 0 is a
        # normal row and participates in Ctrl-A. (Previously it was a
        # separate 'scope_root' kind and was excluded.)
        b = Browser(BrowserConfig(_headless=True))
        b.update_data([
            ('upsert', 'a', None, {'has_children': True}),
            ('upsert', 'a1', 'a', {}),
        ])
        b.drain_main_queue()
        b.scope_into('a')
        b.drain_main_queue()
        b.select_all_visible()
        b.drain_main_queue()
        # Both the scope row ('a') and its child ('a1') are selected.
        self.assertEqual(b._state.selected, {'a', 'a1'})


class TestClearSelection(unittest.TestCase):

    def test_clears_all(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b._state.selected = {'a', 'b', 'c'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(b._state.selected, set())

    def test_idempotent(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(b._state.selected, set())


class TestInvertSelection(unittest.TestCase):

    def test_flips_visible(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b._state.selected = {'a'}
        b.invert_selection()
        b.drain_main_queue()
        # 'a' was selected, now deselected; 'b' and 'c' newly selected.
        self.assertEqual(b._state.selected, {'b', 'c'})

    def test_preserves_invisible(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        # 'a1' is invisible (under collapsed 'a'); pre-select it.
        b._state.selected = {'a1'}
        b.invert_selection()
        b.drain_main_queue()
        # 'a1' stays selected (invisible rows aren't touched);
        # visible 'a','b','c' all flipped to selected.
        self.assertIn('a1', b._state.selected)
        self.assertIn('a', b._state.selected)


class TestContextPassthroughs(unittest.TestCase):

    def test_all_three(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        ctx = Context(b)
        ctx.select_all_visible()
        b.drain_main_queue()
        self.assertEqual(b._state.selected, {'a', 'b', 'c'})
        ctx.invert_selection()
        b.drain_main_queue()
        self.assertEqual(b._state.selected, set())
        b._state.selected = {'a'}
        ctx.clear_selection()
        b.drain_main_queue()
        self.assertEqual(b._state.selected, set())


if __name__ == '__main__':
    unittest.main()
