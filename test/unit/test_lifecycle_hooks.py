"""Tests for the Browser lifecycle hooks.

``on_cursor_change`` fires at most once per main-loop tick when the
cursor row id changed. ``on_scope_change`` fires after every scope
transition. ``on_quit`` fires once during shutdown. All three receive
a Context.
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
_state.Context = _context.Context        # hooks need Context in scope
_context.visible_items = _state.visible_items

Browser = _state.Browser
Item = _data.Item
mark_cursor_changed = _state.mark_cursor_changed


def _seed(b, items):
    b.update_data([
        ('upsert', it.id, None, {k: v for k, v in vars(it).items()
                                 if not k.startswith('_')})
        for it in items
    ])
    b.drain_main_queue()


class TestOnCursorChange(unittest.TestCase):

    def test_fires_once_per_drain(self):
        fired = []
        b = Browser(_headless=True,
                    on_cursor_change=lambda ctx: fired.append(
                        ctx.cursor.id if ctx.cursor else None))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        b._last_cursor_id = None  # ensure first move is observed
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, ['b'])

    def test_coalesces_rapid_moves(self):
        fired = []
        b = Browser(_headless=True,
                    on_cursor_change=lambda ctx: fired.append(
                        ctx.cursor.id if ctx.cursor else None))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        # Several moves in quick succession (no fire between).
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._state.cursor = 2
        mark_cursor_changed(b)
        b._state.cursor = 0
        mark_cursor_changed(b)
        # Single fire reflecting the latest position.
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, ['a'])

    def test_no_fire_when_id_unchanged(self):
        # Cursor anchor re-positioning often calls mark_cursor_changed
        # without changing the id. The hook must not fire in that case.
        fired = []
        b = Browser(_headless=True,
                    on_cursor_change=lambda ctx: fired.append(1))
        _seed(b, [Item(id='a'), Item(id='b')])
        b._state.cursor = 0
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(len(fired), 1)
        # Re-mark with the cursor unchanged.
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(len(fired), 1)  # no new fire

    def test_exception_routed_to_error(self):
        def bad(ctx):
            raise RuntimeError('boom')
        b = Browser(_headless=True, on_cursor_change=bad)
        _seed(b, [Item(id='a'), Item(id='b')])
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        b.drain_main_queue()
        self.assertIn('boom', b.error_text)
        self.assertIn('on_cursor_change', b.error_text)


class TestOnScopeChange(unittest.TestCase):

    def test_fires_on_scope_change(self):
        fired = []
        b = Browser(_headless=True,
                    on_scope_change=lambda ctx: fired.append(
                        tuple(ctx.state.scope_stack)))
        b._fire_scope_change()
        self.assertEqual(fired, [()])  # current state of the stack

    def test_exception_routed_to_error(self):
        def bad(ctx):
            raise ValueError('nope')
        b = Browser(_headless=True, on_scope_change=bad)
        b._fire_scope_change()
        b.drain_main_queue()
        self.assertIn('nope', b.error_text)


class TestOnSelectionChange(unittest.TestCase):

    def test_fires_on_select_all_visible(self):
        fired = []
        b = Browser(_headless=True,
                    on_selection_change=lambda ctx: fired.append(
                        set(ctx.state.selected)))
        b.update_data([
            ('upsert', 'a', None, {}),
            ('upsert', 'b', None, {}),
        ])
        b.drain_main_queue()
        b.select_all_visible()
        b.drain_main_queue()
        self.assertEqual(fired, [{'a', 'b'}])

    def test_fires_on_clear(self):
        fired = []
        b = Browser(_headless=True,
                    on_selection_change=lambda ctx: fired.append(1))
        b._state.selected = {'a'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(fired, [1])

    def test_no_fire_on_clear_when_already_empty(self):
        fired = []
        b = Browser(_headless=True,
                    on_selection_change=lambda ctx: fired.append(1))
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(fired, [])

    def test_fires_on_select_no_op_is_silent(self):
        # ctx.select() with a set already containing those ids → no change.
        fired = []
        b = Browser(_headless=True,
                    on_selection_change=lambda ctx: fired.append(1))
        b._state.selected = {'a'}
        b.select(['a'], replace=False)
        b.drain_main_queue()
        self.assertEqual(fired, [])

    def test_exception_routed_to_error(self):
        def bad(ctx):
            raise RuntimeError('selection boom')
        b = Browser(_headless=True, on_selection_change=bad)
        b._state.selected = {'a'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertIn('selection boom', b.error_text)


class TestOnQuit(unittest.TestCase):

    def test_fires_once(self):
        fired = []
        b = Browser(_headless=True,
                    on_quit=lambda ctx: fired.append(ctx))
        b._fire_on_quit()
        b._fire_on_quit()  # second call is a no-op
        self.assertEqual(len(fired), 1)

    def test_exception_swallowed(self):
        def bad(ctx):
            raise RuntimeError('cleanup blew up')
        b = Browser(_headless=True, on_quit=bad)
        # Must not raise.
        b._fire_on_quit()


class TestDefaultsAreNoOp(unittest.TestCase):

    def test_no_hooks_no_explosion(self):
        b = Browser(_headless=True)
        # All three fire methods should be safe no-ops.
        b._cursor_change_pending = True
        b._fire_cursor_change_if_pending()
        b._fire_scope_change()
        b._fire_on_quit()


if __name__ == '__main__':
    unittest.main()
