"""Tests for the public mode + search-query API.

``ctx.mode`` (``Mode.NORMAL`` / ``SEARCH_EDIT`` / ``FILTER_EDIT``)
plus ``search_query`` / ``set_search_query`` / ``clear_search``.
Parallels the existing filter API.
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
Context = _context.Context
Mode = _state.Mode


def _seed(b):
    b.update_data([
        ('upsert', 'apple', None, {'title': 'apple'}),
        ('upsert', 'banana', None, {'title': 'banana'}),
        ('upsert', 'cherry', None, {'title': 'cherry'}),
    ])
    b.drain_main_queue()


class TestMode(unittest.TestCase):

    def test_default_normal(self):
        b = Browser(_headless=True)
        self.assertIs(b.mode, Mode.NORMAL)

    def test_reflects_search_edit(self):
        b = Browser(_headless=True)
        b._mode = Mode.SEARCH_EDIT
        self.assertIs(b.mode, Mode.SEARCH_EDIT)

    def test_reflects_filter_edit(self):
        b = Browser(_headless=True)
        b._mode = Mode.FILTER_EDIT
        self.assertIs(b.mode, Mode.FILTER_EDIT)


class TestSearchQueryReader(unittest.TestCase):

    def test_default_empty(self):
        b = Browser(_headless=True)
        self.assertEqual(b.search_query, '')

    def test_reflects_state(self):
        b = Browser(_headless=True)
        b._search_query = 'foo'
        self.assertEqual(b.search_query, 'foo')


class TestSetSearchQuery(unittest.TestCase):

    def test_replaces_query(self):
        b = Browser(_headless=True)
        _seed(b)
        b.set_search_query('ban')
        b.drain_main_queue()
        self.assertEqual(b.search_query, 'ban')

    def test_empty_clears(self):
        b = Browser(_headless=True)
        _seed(b)
        b._search_query = 'old'
        b.set_search_query('')
        b.drain_main_queue()
        self.assertEqual(b.search_query, '')

    def test_forces_normal_mode(self):
        b = Browser(_headless=True)
        _seed(b)
        b._mode = Mode.SEARCH_EDIT
        b.set_search_query('foo')
        b.drain_main_queue()
        self.assertIs(b.mode, Mode.NORMAL)

    def test_none_coerced_to_empty(self):
        b = Browser(_headless=True)
        _seed(b)
        b._search_query = 'stale'
        b.set_search_query(None)
        b.drain_main_queue()
        self.assertEqual(b.search_query, '')

    def test_jumps_to_match(self):
        # After setting a query, the cursor lands on the first
        # match (forward from row 0).
        b = Browser(_headless=True)
        _seed(b)
        b.set_search_query('ban')
        b.drain_main_queue()
        vis = _state.visible_items(b._state)
        self.assertEqual(vis[b._state.cursor].item.id, 'banana')

    def test_signals_redraw(self):
        b = Browser(_headless=True)
        _seed(b)
        b._needs_redraw.clear()
        b.set_search_query('x')
        b.drain_main_queue()
        self.assertIn('list', b._needs_redraw)
        self.assertIn('info', b._needs_redraw)


class TestClearSearch(unittest.TestCase):

    def test_clear_empties_query(self):
        b = Browser(_headless=True)
        _seed(b)
        b._search_query = 'something'
        b.clear_search()
        b.drain_main_queue()
        self.assertEqual(b.search_query, '')


class TestContextPassthroughs(unittest.TestCase):

    def test_readers(self):
        b = Browser(_headless=True)
        ctx = Context(b)
        self.assertIs(ctx.mode, Mode.NORMAL)
        self.assertEqual(ctx.search_query, '')

    def test_writers(self):
        b = Browser(_headless=True)
        _seed(b)
        ctx = Context(b)
        ctx.set_search_query('apple')
        b.drain_main_queue()
        self.assertEqual(ctx.search_query, 'apple')
        ctx.clear_search()
        b.drain_main_queue()
        self.assertEqual(ctx.search_query, '')


if __name__ == '__main__':
    unittest.main()
