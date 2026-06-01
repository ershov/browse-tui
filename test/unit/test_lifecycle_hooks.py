"""Tests for the Browser lifecycle hooks.

All hooks follow the uniform ``(ctx, <subject>)`` convention:
``on_cursor_change(ctx, id)`` fires at most once per main-loop tick
when the cursor row id changed; ``on_selection_change(ctx, ids)``
carries the resulting selected id list; ``on_scope_change(ctx,
scope_id, prev_scope_id, direction)`` fires after every scope
transition with the new + previous scope ids (``None`` at root) and
``'in'``/``'out'``; ``on_quit(ctx, code)`` fires once during shutdown
with the stashed exit code.
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
BrowserConfig = _state.BrowserConfig
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
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(id)))
        _seed(b, [Item(id='a'), Item(id='b'), Item(id='c')])
        b._last_cursor_id = None  # ensure first move is observed
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, ['b'])

    def test_payload_is_id_and_ctx_agrees(self):
        # The id payload matches what the recipe would read off ctx.
        seen = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: seen.append(
                        (id, ctx.cursor.id if ctx.cursor else None))))
        _seed(b, [Item(id='a'), Item(id='b')])
        b._last_cursor_id = None
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(seen, [('b', 'b')])

    def test_id_is_none_on_empty(self):
        # No items → cursor row resolves to None.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(id)))
        b._last_cursor_id = 'sentinel'  # force a delta to None
        b._state.cursor = 0
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        self.assertEqual(fired, [None])

    def test_coalesces_rapid_moves(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(id)))
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
        b = Browser(BrowserConfig(_headless=True,
                    on_cursor_change=lambda ctx, id: fired.append(1)))
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
        def bad(ctx, id):
            raise RuntimeError('boom')
        b = Browser(BrowserConfig(_headless=True, on_cursor_change=bad))
        _seed(b, [Item(id='a'), Item(id='b')])
        b._state.cursor = 1
        mark_cursor_changed(b)
        b._fire_cursor_change_if_pending()
        b.drain_main_queue()
        self.assertIn('boom', b.error_text)
        self.assertIn('on_cursor_change', b.error_text)


class TestOnScopeChange(unittest.TestCase):

    def _seed(self, b):
        b.update_data([
            ('upsert', 'a', None, {'has_children': True}),
            ('upsert', 'b', None, {'has_children': True}),
            ('upsert', 'a1', 'a', {'has_children': True}),
            ('upsert', 'a2', 'a', {}),
        ])
        b.drain_main_queue()

    def test_scope_into_payload_from_root(self):
        # Scoping in from the root: scope_id == target, prev is None,
        # direction == 'in'.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        self.assertEqual(fired, [('a', None, 'in')])

    def test_scope_into_nested_carries_prev(self):
        # Scoping a level deeper: prev is the scope we were in.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_into('a1')
        b.drain_main_queue()
        self.assertEqual(fired[-1], ('a1', 'a', 'in'))

    def test_scope_out_to_root_scope_id_none(self):
        # Scoping out to the root: scope_id is None, prev is the scope
        # we left, direction == 'out'.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(fired[-1], (None, 'a', 'out'))

    def test_scope_out_nested_carries_both(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        self._seed(b)
        b.scope_into('a')
        b.drain_main_queue()
        b.scope_into('a1')
        b.drain_main_queue()
        b.scope_out()
        b.drain_main_queue()
        self.assertEqual(fired[-1], ('a', 'a1', 'out'))

    def test_direct_fire_defaults(self):
        # Calling the private fire directly (no transition) passes the
        # current scope as both ids with no direction.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_scope_change=lambda ctx, sid, prev, d: fired.append(
                        (sid, prev, d))))
        b._fire_scope_change()
        self.assertEqual(fired, [(None, None, None)])

    def test_exception_routed_to_error(self):
        def bad(ctx, sid, prev, d):
            raise ValueError('nope')
        b = Browser(BrowserConfig(_headless=True, on_scope_change=bad))
        b._fire_scope_change()
        b.drain_main_queue()
        self.assertIn('nope', b.error_text)
        self.assertIn('on_scope_change', b.error_text)


class TestOnSelectionChange(unittest.TestCase):

    def test_fires_on_select_all_visible(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(
                        set(ids))))
        b.update_data([
            ('upsert', 'a', None, {}),
            ('upsert', 'b', None, {}),
        ])
        b.drain_main_queue()
        b.select_all_visible()
        b.drain_main_queue()
        self.assertEqual(fired, [{'a', 'b'}])

    def test_payload_is_id_list(self):
        # The payload is a list of ids (the resulting set), not Items.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(ids)))
        b.update_data([
            ('upsert', 'a', None, {}),
            ('upsert', 'b', None, {}),
        ])
        b.drain_main_queue()
        b.select_all_visible()
        b.drain_main_queue()
        self.assertEqual(len(fired), 1)
        payload = fired[0]
        self.assertIsInstance(payload, list)
        self.assertEqual(set(payload), {'a', 'b'})

    def test_fires_on_clear_with_empty_list(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(ids)))
        b._state.selected = {'a'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(fired, [[]])

    def test_no_fire_on_clear_when_already_empty(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(1)))
        b.clear_selection()
        b.drain_main_queue()
        self.assertEqual(fired, [])

    def test_fires_on_select_no_op_is_silent(self):
        # ctx.select() with a set already containing those ids → no change.
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_selection_change=lambda ctx, ids: fired.append(1)))
        b._state.selected = {'a'}
        b.select(['a'], replace=False)
        b.drain_main_queue()
        self.assertEqual(fired, [])

    def test_exception_routed_to_error(self):
        def bad(ctx, ids):
            raise RuntimeError('selection boom')
        b = Browser(BrowserConfig(_headless=True, on_selection_change=bad))
        b._state.selected = {'a'}
        b.clear_selection()
        b.drain_main_queue()
        self.assertIn('selection boom', b.error_text)
        self.assertIn('on_selection_change', b.error_text)


class TestOnQuit(unittest.TestCase):

    def test_fires_once_with_code(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_quit=lambda ctx, code: fired.append(code)))
        b._quit_code = 7
        b._fire_on_quit()
        b._fire_on_quit()  # second call is a no-op
        self.assertEqual(fired, [7])

    def test_default_code_is_zero(self):
        fired = []
        b = Browser(BrowserConfig(_headless=True,
                    on_quit=lambda ctx, code: fired.append(code)))
        b._fire_on_quit()
        self.assertEqual(fired, [0])

    def test_exception_swallowed(self):
        def bad(ctx, code):
            raise RuntimeError('cleanup blew up')
        b = Browser(BrowserConfig(_headless=True, on_quit=bad))
        # Must not raise.
        b._fire_on_quit()


class TestDefaultsAreNoOp(unittest.TestCase):

    def test_no_hooks_no_explosion(self):
        b = Browser(BrowserConfig(_headless=True))
        # All four fire methods should be safe no-ops.
        b._cursor_change_pending = True
        b._fire_cursor_change_if_pending()
        b._fire_scope_change()
        b._fire_selection_change()
        b._fire_on_quit()


if __name__ == '__main__':
    unittest.main()
