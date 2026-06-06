"""Error-notice keypress-clear policy in ``Browser._handle_one_key``.

The top of ``_handle_one_key`` (before dispatch) clears an *error*
notice once a keypress lands at least ``_ERROR_MIN_DISPLAY`` (1s) after
the error appeared. Earlier keypresses leave it in place so an in-flight
key can't instantly wipe an unread error. Either way the key still
performs its normal action (the clear is non-swallowing).

See ``docs/superpowers/specs/2026-06-06-ctx-flash-log-design.md``.
"""

import time
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_actions = load('_browse_tui_actions', '070-actions.py')

# Cross-module wiring (production builds get these via concatenation).
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
# ``_handle_one_key`` dispatches through these action-layer functions by
# bare name; inject them into the state module for the isolated load.
_state.dispatch_key = _actions.dispatch_key
_state._handle_insert_key = _actions._handle_insert_key

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions._resolve_landing = _state._resolve_landing
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Action = _actions.Action
Notice = _state.Notice
_ERROR_MIN_DISPLAY = _state._ERROR_MIN_DISPLAY


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


def _ctx_for(browser):
    _context = load('_browse_tui_context', '060-context.py')
    _context.visible_items = _state.visible_items
    return _context.Context(browser)


class TestErrorKeypressClear(unittest.TestCase):

    def _browser_with_probe(self):
        """Headless browser with a probe action bound to 'z'."""
        b = _make_browser()
        b._state._children[None] = [Item(id='a'), Item(id='b')]
        self._fired = []
        b.add_action(
            Action('z', 'probe', lambda ctx: self._fired.append(True),
                   'none', 'OTHER'))
        return b

    def test_error_clears_after_min_display_and_key_acts(self):
        b = self._browser_with_probe()
        try:
            ctx = _ctx_for(b)
            # Error shown well over the minimum-display window ago.
            b._notice = Notice(
                text='boom', kind='error',
                shown_at=time.monotonic() - (_ERROR_MIN_DISPLAY + 1.0),
                seq=1)
            b._needs_redraw.clear()
            b._handle_one_key(ctx, 'z')
            # Cleared, redraw flagged, and the key still fired.
            self.assertIsNone(b._notice)
            self.assertIn('info', b._needs_redraw)
            self.assertEqual(self._fired, [True])
        finally:
            b.stop_workers()

    def test_error_not_cleared_before_min_display_but_key_acts(self):
        b = self._browser_with_probe()
        try:
            ctx = _ctx_for(b)
            # Error shown just now — under the minimum-display window.
            b._notice = Notice(
                text='boom', kind='error',
                shown_at=time.monotonic(), seq=1)
            b._handle_one_key(ctx, 'z')
            # Still present (too soon to clear), but the key fired anyway.
            self.assertIsNotNone(b._notice)
            self.assertEqual(b._notice.kind, 'error')
            self.assertEqual(self._fired, [True])
        finally:
            b.stop_workers()

    def test_flash_notice_not_cleared_by_keypress(self):
        b = self._browser_with_probe()
        try:
            ctx = _ctx_for(b)
            # A flash (even an old one) is the timer's job, not the
            # keypress path's — it must survive a keypress.
            b._notice = Notice(
                text='ack', kind='flash',
                shown_at=time.monotonic() - (_ERROR_MIN_DISPLAY + 1.0),
                seq=1)
            b._handle_one_key(ctx, 'z')
            self.assertIsNotNone(b._notice)
            self.assertEqual(b._notice.kind, 'flash')
            self.assertEqual(self._fired, [True])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
