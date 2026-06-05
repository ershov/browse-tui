"""Unit tests for the preview tail-follow pin (``_preview_at_tail``).

Covers the flag's lifecycle:

  * Defaults to False.
  * ``_preview_end`` engages it; renderer override forces
    ``_preview_scroll = max_scroll`` while engaged.
  * Tail-follow: appending more preview content keeps the view at the
    new bottom on the next render.
  * Upward motions clear it (Shift-Up, Alt-PgUp, Shift-Home, wheel-up).
  * Downward motions leave it engaged (Shift-Down, Alt-PgDn, wheel-down).
  * Cursor-item change and help-toggle clear it.
  * ``Browser.preview_to_tail`` posts a thread-safe engagement.
  * ``Context.preview_to_tail`` passes through.

See ``docs/superpowers/specs/2026-05-17-preview-tail-design.md``.
"""

import io
import sys
import unittest

from test.unit._loader import load


# --- Module loading + cross-wiring ----------------------------------------

_term = load('_browse_tui_term_pt', '020-terminal.py')
_data = load('_browse_tui_data_pt', '030-data.py')
_state = load('_browse_tui_state_pt', '040-state.py')
_render = load('_browse_tui_render_pt', '050-render.py')
_context = load('_browse_tui_context_pt', '060-context.py')
_actions = load('_browse_tui_actions_pt', '070-actions.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_render.DEFAULT_HINT = _state.DEFAULT_HINT
_render.VisibleEntry = _state.VisibleEntry
_render.PaneCache = _state.PaneCache
_render.visible_items = _state.visible_items
_render._search_matches = _state._search_matches
_render._search_text = _state._search_text
_render._ANSI_CSI_RE = _term._ANSI_CSI_RE
_render.SgrState = _term.SgrState
_render._char_width = _term._char_width
_render._visible_len = _term._visible_len
for _name in ('write', 'move', 'set_style', 'reset_style', 'clear_line',
              'clear_columns', 'begin_row', 'end_row', 'begin_sync',
              'end_sync', 'flush', 'term_size'):
    setattr(_render, _name, getattr(_term, _name))
# Default row-format handlers live in 040-state but reference render-layer
# constants/helpers at call time; inject them so a render through a
# state-loaded Browser resolves them (the concatenated build does so by name).
for _name in ('_TAG_STYLE', '_id_visible', '_ID_COLOR', '_MARKER_COLOR',
              'cell_width'):
    setattr(_state, _name, getattr(_render, _name))

_context.visible_items = _state.visible_items

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
Context = _context.Context
dispatch_key = _actions.dispatch_key


def _make_browser(preview_text='', **kw):
    """Build a headless Browser with one item carrying ``preview_text``."""
    def gp(_id):
        return preview_text

    kw.setdefault('_headless', True)
    kw.setdefault('get_preview', gp)
    b = Browser(BrowserConfig(**kw))
    item = _data.to_item(Item(id='a'))
    b._state._children[None] = [item]
    b._state._items_by_id['a'] = item
    _state.mark_visible_dirty(b._state)
    item.preview = preview_text
    return b


def _ctx_for(browser):
    return Context(browser)


def _render_preview(browser):
    """Run render_full while capturing stdout. Returns nothing."""
    orig = sys.stdout
    sys.stdout = io.StringIO()
    try:
        _render.render_full(browser)
    finally:
        sys.stdout = orig


# --- Defaults --------------------------------------------------------------


class TestFlagDefault(unittest.TestCase):

    def test_defaults_false(self):
        b = _make_browser()
        try:
            self.assertFalse(b._preview_at_tail)
        finally:
            b.stop_workers()


# --- Engagement: _preview_end + renderer override --------------------------


class TestEngagement(unittest.TestCase):

    def test_preview_end_engages_flag(self):
        b = _make_browser('line1\nline2\nline3\n')
        try:
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_renderer_forces_scroll_to_max_when_engaged(self):
        # Many lines so max_scroll > 0.
        text = '\n'.join(f'l{i}' for i in range(200))
        b = _make_browser(text)
        try:
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            _render_preview(b)
            # After render with flag engaged, _preview_scroll holds the
            # current max_scroll. Re-rendering twice should leave it stable.
            scroll_after = b._preview_scroll
            self.assertGreater(scroll_after, 0,
                               'expected max_scroll > 0 with 200-line preview')
            _render_preview(b)
            self.assertEqual(b._preview_scroll, scroll_after)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_alt_end_key_engages_flag(self):
        b = _make_browser('a\nb\nc\n')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'alt-end'))
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_shift_end_key_engages_flag(self):
        b = _make_browser('a\nb\nc\n')
        try:
            ctx = _ctx_for(b)
            self.assertTrue(dispatch_key(b, ctx, 'shift-end'))
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()


# --- Tail-follow: growing content keeps view at bottom ---------------------


class TestTailFollow(unittest.TestCase):

    def test_appending_content_keeps_view_at_new_bottom(self):
        b = _make_browser('\n'.join(f'l{i}' for i in range(100)))
        try:
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            _render_preview(b)
            initial_max = b._preview_scroll
            # Now append more content; renderer sees longer wrapped list.
            # Direct mutation bypasses ``append_preview``'s auto-invalidate
            # of the wrap cache (#422), so drop it by hand.
            item_a = b._state._items_by_id['a']
            item_a.preview += (
                '\n'.join(f'x{i}' for i in range(50)) + '\n'
            )
            item_a.preview_render = None
            b._needs_redraw.add('preview')
            _render_preview(b)
            self.assertGreater(
                b._preview_scroll, initial_max,
                'tail-follow should advance max_scroll as content grows',
            )
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()


# --- Upward motions clear --------------------------------------------------


class TestUpwardMotionsClear(unittest.TestCase):

    def _engaged_browser(self):
        b = _make_browser('\n'.join(f'l{i}' for i in range(200)))
        ctx = _ctx_for(b)
        _actions._preview_end(ctx)
        _render_preview(b)  # writes back _preview_scroll = max_scroll
        return b, ctx

    def test_shift_up_clears_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._preview_scroll_up(ctx)
            self.assertFalse(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_shift_up_decrements_from_rendered_max(self):
        b, ctx = self._engaged_browser()
        try:
            max_after_render = b._preview_scroll
            _actions._preview_scroll_up(ctx)
            self.assertEqual(b._preview_scroll, max_after_render - 1)
        finally:
            b.stop_workers()

    def test_alt_pgup_clears_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._preview_page_up(ctx)
            self.assertFalse(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_shift_home_clears_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._preview_home(ctx)
            self.assertFalse(b._preview_at_tail)
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()

    def test_wheel_up_clears_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._scroll_preview(b, -3)
            self.assertFalse(b._preview_at_tail)
        finally:
            b.stop_workers()


# --- Downward motions leave engaged ----------------------------------------


class TestDownwardMotionsKeepFlag(unittest.TestCase):

    def _engaged_browser(self):
        b = _make_browser('\n'.join(f'l{i}' for i in range(200)))
        ctx = _ctx_for(b)
        _actions._preview_end(ctx)
        _render_preview(b)
        return b, ctx

    def test_shift_down_keeps_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._preview_scroll_down(ctx)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_alt_pgdn_keeps_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._preview_page_down(ctx)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_wheel_down_keeps_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._scroll_preview(b, 3)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_repeat_preview_end_keeps_flag(self):
        b, ctx = self._engaged_browser()
        try:
            _actions._preview_end(ctx)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()


# --- Cursor / help reset paths ---------------------------------------------


class TestStickiness(unittest.TestCase):
    """Tail pin is a sticky intent that survives cursor moves and help.

    Once engaged, the pin is only cleared by explicit upward scroll
    motion. Cursor changes, help-mode toggle, and recipe-driven cache
    invalidation all preserve the pin so the new content also opens
    at the tail.
    """

    def test_help_toggle_preserves_flag(self):
        b = _make_browser('a\nb\n')
        try:
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            _actions._toggle_help(ctx)
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_cursor_item_change_preserves_flag(self):
        """Sticky pin: a cursor move to another item keeps tail-follow
        engaged so the new item also opens at the bottom."""
        b = _make_browser('a\nb\n')
        try:
            # Two items, cursor on first; engage tail.
            ix = _data.to_item(Item(id='x'))
            iy = _data.to_item(Item(id='y'))
            b._state._children[None] = [ix, iy]
            b._state._items_by_id['x'] = ix
            b._state._items_by_id['y'] = iy
            _state.mark_visible_dirty(b._state)
            ix.preview = 'xx\n' * 10
            iy.preview = 'yy\n' * 10
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            self.assertTrue(b._preview_at_tail)
            # Move cursor → next _update_preview_for_cursor sees a new id.
            b._state.cursor = 1
            b._update_preview_for_cursor()
            self.assertTrue(b._preview_at_tail,
                            'tail pin should survive cursor-item change')
            self.assertEqual(b._preview_scroll, 0,
                             'scroll resets to 0 on cursor change; the '
                             'renderer\'s pin override snaps it back to '
                             'max_scroll on next paint')
        finally:
            b.stop_workers()

    def test_render_after_cursor_change_snaps_to_new_tail(self):
        """The scroll=0 reset on cursor change is overridden by the
        pin on the next render — new item opens at its tail."""
        b = _make_browser('a\nb\n')
        try:
            ix = _data.to_item(Item(id='x'))
            iy = _data.to_item(Item(id='y'))
            b._state._children[None] = [ix, iy]
            b._state._items_by_id['x'] = ix
            b._state._items_by_id['y'] = iy
            _state.mark_visible_dirty(b._state)
            ix.preview = 'xx\n' * 10
            iy.preview = '\n'.join(f'l{i}' for i in range(200))
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            b._state.cursor = 1
            b._update_preview_for_cursor()
            _render_preview(b)
            self.assertTrue(b._preview_at_tail)
            self.assertGreater(b._preview_scroll, 0,
                               'pin should advance scroll to the new '
                               'item\'s max_scroll on render')
        finally:
            b.stop_workers()


# --- Public API: Browser.preview_to_tail / Context pass-through -----------


class TestPublicAPI(unittest.TestCase):

    def test_browser_preview_to_tail_posts_and_engages(self):
        b = _make_browser('a\nb\n')
        try:
            self.assertFalse(b._preview_at_tail)
            b.preview_to_tail()
            # The mutation lives in the post queue.
            self.assertFalse(b._preview_at_tail)
            b.drain_main_queue()
            self.assertTrue(b._preview_at_tail)
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_context_preview_to_tail_forwards(self):
        b = _make_browser('a\nb\n')
        try:
            ctx = _ctx_for(b)
            ctx.preview_to_tail()
            b.drain_main_queue()
            self.assertTrue(b._preview_at_tail)
        finally:
            b.stop_workers()


# --- invalidate_preview preserves view state -------------------------------


class TestInvalidatePreviewPreservesViewState(unittest.TestCase):
    """``invalidate_preview`` re-fetches without resetting view state.

    Contract: a recipe whose umbrella preview text changes underneath
    (children streamed in, file content changed) must not clobber the
    user's pinned-to-tail view state. This is the regression we hit
    when umbrella row tail-follow stopped working — the recipe was
    nulling ``_preview_cursor_id`` which routed through the cursor-
    move reset path, killing the tail pin.
    """

    def test_invalidate_preserves_tail_pin(self):
        b = _make_browser('\n'.join(f'l{i}' for i in range(50)))
        try:
            ctx = _ctx_for(b)
            _actions._preview_end(ctx)
            _render_preview(b)
            self.assertTrue(b._preview_at_tail)
            scroll_before = b._preview_scroll
            # Recipe simulates "preview text underneath changed":
            b.invalidate_preview('a')
            b.drain_main_queue()
            # View state survives.
            self.assertTrue(b._preview_at_tail)
            # Scroll position untouched by the invalidate itself.
            self.assertEqual(b._preview_scroll, scroll_before)
            # And a re-request landed in the worker queue (for headless
            # the worker may or may not have fetched yet; the
            # ``_preview_req`` slot is what we assert).
            self.assertEqual(b._preview_req, 'a')
        finally:
            b.stop_workers()

    def test_invalidate_preserves_scroll_when_not_tailing(self):
        b = _make_browser('\n'.join(f'l{i}' for i in range(50)))
        try:
            b._preview_scroll = 7
            self.assertFalse(b._preview_at_tail)
            b.invalidate_preview('a')
            b.drain_main_queue()
            self.assertEqual(b._preview_scroll, 7)
            self.assertFalse(b._preview_at_tail)
        finally:
            b.stop_workers()

    def test_invalidate_preserves_help_mode(self):
        b = _make_browser('a\n')
        try:
            b._help_mode = True
            b.invalidate_preview('a')
            b.drain_main_queue()
            self.assertTrue(b._help_mode)
        finally:
            b.stop_workers()

    def test_invalidate_drops_cache_entry(self):
        b = _make_browser('cached body\n')
        try:
            b._state._items_by_id['a'].preview = 'stale text'
            b.invalidate_preview('a')
            b.drain_main_queue()
            # Cache cleared; renderer will see empty until worker fetches.
            self.assertIsNone(b._state._items_by_id['a'].preview)
        finally:
            b.stop_workers()

    def test_context_invalidate_preview_forwards(self):
        b = _make_browser('a\n')
        try:
            ctx = _ctx_for(b)
            b._state._items_by_id['a'].preview = 'stale'
            ctx.invalidate_preview('a')
            b.drain_main_queue()
            self.assertIsNone(b._state._items_by_id['a'].preview)
            self.assertEqual(b._preview_req, 'a')
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
