"""Tests for the documented Context escape hatches.

``ctx.browser`` returns the underlying Browser. ``ctx.state`` returns
the underlying State dataclass. Both are read-only by convention and
exist for advanced use cases not covered by Context's typed methods.
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


class TestEscapeHatches(unittest.TestCase):

    def test_ctx_browser_returns_browser(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        self.assertIs(ctx.browser, b)

    def test_ctx_state_returns_state(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        self.assertIs(ctx.state, b._state)

    def test_state_fields_readable(self):
        # The documented use case is reading state fields like
        # ``expanded``, ``scope_stack``, ``cursor``, ``selected``,
        # ``root_id`` directly.
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        self.assertIsInstance(ctx.state.expanded, set)
        self.assertIsInstance(ctx.state.scope_stack, list)
        self.assertIsInstance(ctx.state.selected, set)
        self.assertEqual(ctx.state.cursor, 0)

    def test_state_reflects_mutations(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        b._state.expanded.add('foo')
        self.assertIn('foo', ctx.state.expanded)


if __name__ == '__main__':
    unittest.main()
