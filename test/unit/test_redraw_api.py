"""Tests for the public repaint API.

``redraw`` on ``Browser`` and the ``Context`` pass-through. ``redraw``
is the lightweight counterpart to ``refresh``: it only flags panes dirty
(posted to the main thread) so the next render pass repaints from
already-loaded data — crucially WITHOUT invalidating the children cache
or enqueuing a refetch. Parallels the hint API in test_hint_api.py.
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


class TestRedrawPanes(unittest.TestCase):

    def test_single_pane_name(self):
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.redraw('list')
        b.drain_main_queue()
        self.assertIn('list', b._needs_redraw)

    def test_string_is_one_name_not_chars(self):
        # A bare string must be treated as ONE pane name, not iterated
        # into its characters ('l', 'i', 's', 't', ...).
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.redraw('list')
        b.drain_main_queue()
        self.assertEqual(b._needs_redraw, {'list'})

    def test_iterable_of_names(self):
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.redraw(['list', 'children'])
        b.drain_main_queue()
        self.assertEqual(b._needs_redraw, {'list', 'children'})

    def test_default_is_all(self):
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.redraw()
        b.drain_main_queue()
        self.assertIn('all', b._needs_redraw)

    def test_accumulates_with_existing_flags(self):
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b._needs_redraw.add('info')
        b.redraw('list')
        b.drain_main_queue()
        self.assertEqual(b._needs_redraw, {'info', 'list'})


class TestRedrawIsDeferred(unittest.TestCase):

    def test_posts_to_main_thread(self):
        # Like the other thread-safe ops, the effect lands on the next
        # drain, not synchronously at the call site.
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.redraw('list')
        self.assertNotIn('list', b._needs_redraw)
        b.drain_main_queue()
        self.assertIn('list', b._needs_redraw)


class TestRedrawDoesNotRefetch(unittest.TestCase):

    def test_no_children_fetch_enqueued(self):
        # The defining difference from ``refresh``: ``redraw`` must NOT
        # invalidate caches or enqueue a children fetch. Snapshot the
        # fetch-tracking structures across a redraw+drain and assert they
        # are untouched.
        b = Browser(BrowserConfig(_headless=True))
        b.drain_main_queue()
        queue_before = len(b._children_queue)
        pending_before = set(b._state._children_pending)
        in_flight_before = dict(b._children_in_flight)

        b.redraw(['list', 'children'])
        b.drain_main_queue()

        self.assertEqual(len(b._children_queue), queue_before)
        self.assertEqual(b._state._children_pending, pending_before)
        self.assertEqual(b._children_in_flight, in_flight_before)


class TestContextPassthrough(unittest.TestCase):

    def test_passthrough_single(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        b._needs_redraw.clear()
        ctx.redraw('preview')
        b.drain_main_queue()
        self.assertIn('preview', b._needs_redraw)

    def test_passthrough_list(self):
        b = Browser(BrowserConfig(_headless=True))
        ctx = Context(b)
        b._needs_redraw.clear()
        ctx.redraw(['list', 'children'])
        b.drain_main_queue()
        self.assertEqual(b._needs_redraw, {'list', 'children'})


if __name__ == '__main__':
    unittest.main()
