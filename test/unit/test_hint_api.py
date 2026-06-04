"""Tests for the public info-bar hint API.

``hint`` / ``set_hint`` on ``Browser`` and the ``Context``
pass-throughs, plus the ``BrowserConfig.hint`` construction field.
Parallels the search-query API in test_search_api.py.
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

DEFAULT_HINT = _state.DEFAULT_HINT


class TestHintReader(unittest.TestCase):

    def test_default_value(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertEqual(b.hint, ' /:search  ?:help  q:quit ')

    def test_default_matches_shared_constant(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertEqual(b.hint, DEFAULT_HINT)

    def test_reflects_state(self):
        b = Browser(BrowserConfig(_headless=True))
        b._hint = ' a:add  d:del '
        self.assertEqual(b.hint, ' a:add  d:del ')


class TestSetHint(unittest.TestCase):

    def test_replaces_hint(self):
        b = Browser(BrowserConfig(_headless=True))
        b.set_hint(' a:add  d:del ')
        b.drain_main_queue()
        self.assertEqual(b.hint, ' a:add  d:del ')

    def test_none_coerced_to_empty(self):
        b = Browser(BrowserConfig(_headless=True))
        b._hint = 'stale'
        b.set_hint(None)
        b.drain_main_queue()
        self.assertEqual(b.hint, '')

    def test_signals_info_redraw(self):
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.set_hint(' x ')
        b.drain_main_queue()
        self.assertIn('info', b._needs_redraw)


class TestConfigField(unittest.TestCase):

    def test_config_default(self):
        self.assertEqual(BrowserConfig().hint, ' /:search  ?:help  q:quit ')

    def test_config_overrides_initial_hint(self):
        b = Browser(BrowserConfig(hint=' custom hint ', _headless=True))
        self.assertEqual(b.hint, ' custom hint ')


class TestContextPassthroughs(unittest.TestCase):

    def test_reader(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        self.assertEqual(ctx.hint, ' /:search  ?:help  q:quit ')

    def test_writer(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        ctx.set_hint(' a:add ')
        b.drain_main_queue()
        self.assertEqual(ctx.hint, ' a:add ')


if __name__ == '__main__':
    unittest.main()
