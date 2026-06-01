"""Tests for the public programmatic-scope API.

``Browser.scope`` / ``scope_stack`` (read-only properties) and
``scope_into(id)`` / ``scope_out()`` (thread-safe ops). Each scope
transition fires the ``on_scope_change`` hook (when installed).
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
_state.Context = _context.Context
_context.visible_items = _state.visible_items

Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context


def _seed(b):
    b.update_data([
        ('upsert', 'a', None, {'has_children': True}),
        ('upsert', 'b', None, {'has_children': True}),
        ('upsert', 'a1', 'a', {}),
        ('upsert', 'a2', 'a', {}),
        ('upsert', 'b1', 'b', {}),
    ])
    b.drain_main_queue()


class TestScopeReaders(unittest.TestCase):

    def test_root_scope_is_none(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertIsNone(b.scope)
        self.assertEqual(b.scope_stack, ())

    def test_after_scope_into(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(b.scope, 'a')
        self.assertEqual(b.scope_stack, ('a',))

    def test_nested_scope_stack(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_into('a1')
        b.drain_main_queue()
        self.assertEqual(b.scope, 'a1')
        self.assertEqual(b.scope_stack, ('a', 'a1'))


class TestScopeInto(unittest.TestCase):

    def test_changes_state(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, ['a'])

    def test_repeat_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, ['a'])

    def test_fires_hook(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, *a: fired.append(
                        tuple(ctx.scope_stack))))
        _seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(fired, [('a',)])

    def test_signals_redraw(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b._needs_redraw.clear()
        b.scope_into('a')
        b.drain_main_queue()
        self.assertIn('all', b._needs_redraw)


class TestScopeOut(unittest.TestCase):

    def test_pops_top(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b._state.scope_stack = ['a', 'a1']
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, ['a'])

    def test_noop_at_root(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, [])

    def test_fires_hook(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, *a: fired.append(
                        tuple(ctx.scope_stack))))
        _seed(b)
        b._state.scope_stack = ['a']
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(fired, [()])

    def test_noop_does_not_fire(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, *a: fired.append(1)))
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(fired, [])


class TestContextPassthroughs(unittest.TestCase):

    def test_readers(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        ctx = Context(b)
        self.assertIsNone(ctx.scope)
        self.assertEqual(ctx.scope_stack, ())
        b._state.scope_stack = ['a']
        self.assertEqual(ctx.scope, 'a')
        self.assertEqual(ctx.scope_stack, ('a',))

    def test_writers(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b)
        ctx = Context(b)
        ctx.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, ['a'])
        ctx.scope_out()
        b.drain_main_queue()
        self.assertEqual(b._state.scope_stack, [])


if __name__ == '__main__':
    unittest.main()
