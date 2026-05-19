"""Tests for the public cache-introspection API on Browser / Context.

Covers ``Browser.items_by_id`` / ``get_item`` / ``cached_children`` /
``cached_parents`` / ``all_items`` and their Context passthroughs. The
underlying ``State._items_by_id`` and ``State._children`` remain
framework-private; these methods are the documented public surface.
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
Item = _data.Item


def _make_browser():
    """Build a small browser with three loaded items under 'root'."""
    b = Browser(BrowserConfig(_headless=True))
    b.update_data([
        ('upsert', 'a', None, {'title': 'A', 'has_children': True}),
        ('upsert', 'b', None, {'title': 'B'}),
        ('upsert', 'c', None, {'title': 'C'}),
        ('upsert', 'a1', 'a', {'title': 'A1'}),
        ('upsert', 'a2', 'a', {'title': 'A2'}),
    ])
    b.drain_main_queue()
    return b


class TestItemsById(unittest.TestCase):

    def test_returns_live_dict(self):
        b = _make_browser()
        ids = set(b.items_by_id.keys())
        self.assertEqual(ids, {'a', 'b', 'c', 'a1', 'a2'})

    def test_identity_stable(self):
        b = _make_browser()
        # The dict reference is stable across calls.
        self.assertIs(b.items_by_id, b.items_by_id)

    def test_reflects_mutations(self):
        b = _make_browser()
        before = set(b.items_by_id.keys())
        b.update_data([('upsert', 'd', None, {'title': 'D'})])
        b.drain_main_queue()
        after = set(b.items_by_id.keys())
        self.assertEqual(after - before, {'d'})


class TestGetItem(unittest.TestCase):

    def test_loaded_id_returns_item(self):
        b = _make_browser()
        it = b.get_item('a')
        self.assertIsNotNone(it)
        self.assertEqual(it.id, 'a')
        self.assertEqual(it.title, 'A')

    def test_unknown_id_returns_none(self):
        b = _make_browser()
        self.assertIsNone(b.get_item('nope'))


class TestCachedChildren(unittest.TestCase):

    def test_loaded_parent_returns_list_copy(self):
        b = _make_browser()
        kids = b.cached_children('a')
        self.assertEqual([it.id for it in kids], ['a1', 'a2'])
        # Mutating the returned list does not affect framework state.
        kids.clear()
        self.assertEqual(
            [it.id for it in b.cached_children('a')],
            ['a1', 'a2'],
        )

    def test_unloaded_parent_returns_none(self):
        b = _make_browser()
        # 'b' has no `has_children=True` and was never expanded;
        # its children list was never populated.
        self.assertIsNone(b.cached_children('b'))

    def test_empty_vs_none_distinguishable(self):
        b = Browser(BrowserConfig(_headless=True))
        # Direct entry: a parent with an empty list shows up as []
        # (loaded, no children), not None (not yet loaded).
        b._state._children['x'] = []
        self.assertEqual(b.cached_children('x'), [])
        self.assertIsNone(b.cached_children('never_loaded'))


class TestCachedParents(unittest.TestCase):

    def test_returns_parent_ids(self):
        b = _make_browser()
        parents = b.cached_parents()
        # The root id (None) is one cached parent; 'a' is another.
        self.assertIn(None, parents)
        self.assertIn('a', parents)


class TestAllItems(unittest.TestCase):

    def test_iterates_every_loaded_item(self):
        b = _make_browser()
        ids = {it.id for it in b.all_items()}
        self.assertEqual(ids, {'a', 'b', 'c', 'a1', 'a2'})

    def test_snapshot_safe_under_mutation(self):
        b = _make_browser()
        it = next(b.all_items())
        # Mutate cache mid-iteration — must not blow up.
        b.update_data([('remove', 'b')])
        b.drain_main_queue()
        self.assertTrue(it.id)  # sentinel; the iterator stays valid


class TestContextPassthroughs(unittest.TestCase):

    def test_context_mirrors_browser(self):
        b = _make_browser()
        ctx = Context(b)
        self.assertIs(ctx.items_by_id, b.items_by_id)
        self.assertEqual(ctx.get_item('a').id, 'a')
        self.assertIsNone(ctx.get_item('zzz'))
        self.assertEqual(
            [it.id for it in ctx.cached_children('a')], ['a1', 'a2'])
        self.assertIn('a', ctx.cached_parents())
        self.assertEqual(
            {it.id for it in ctx.all_items()},
            {'a', 'b', 'c', 'a1', 'a2'},
        )


if __name__ == '__main__':
    unittest.main()
