"""Tests for the per-Item wrap cache (ticket #422 commit 2).

Covers ``Item.preview_render`` and its eager-invalidation hooks:

  * ``set_preview`` clears ``preview_render``.
  * ``append_preview`` clears ``preview_render``.
  * ``clear_preview`` clears both ``preview`` and ``preview_render``.
  * ``invalidate_preview`` clears both fields.
  * Terminal resize walks loaded Items and clears every
    ``preview_render`` (preview text untouched).
  * ``_toggle_preview_ansi`` action clears every ``preview_render``.
  * ``update_data`` mod / upsert on an existing Item drops both the
    cached ``preview`` and ``preview_render``.
  * Lazy fill: first ``render_preview`` populates ``preview_render``;
    a second render under the same width / ansi_on / non-search
    conditions reuses the cache (observable via call-count on
    ``_wrap_preview_line``).
"""

import io
import sys
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term_prc', '020-terminal.py')
_data = load('_browse_tui_data_prc', '030-data.py')
_state = load('_browse_tui_state_prc', '040-state.py')
_render = load('_browse_tui_render_prc', '050-render.py')
_context = load('_browse_tui_context_prc', '060-context.py')
_actions = load('_browse_tui_actions_prc', '070-actions.py')


# Cross-wiring (mirrors test_preview_tail.py).
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
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

_context.visible_items = _state.visible_items

_actions.visible_items = _state.visible_items
_actions.mark_visible_dirty = _state.mark_visible_dirty
_actions.mark_cursor_changed = _state.mark_cursor_changed
_actions.PIN_FIRST = _state.PIN_FIRST
_actions.PIN_LAST = _state.PIN_LAST
_actions.Mode = _state.Mode


Item = _data.Item
PreviewRender = _data.PreviewRender
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context


def _seed(b, id_, text=None):
    """Register an Item under ``id_`` with optional preview text."""
    item = Item(id=id_)
    b._state._items_by_id[id_] = item
    if text is not None:
        item.preview = text
    return item


def _attach_render_cache(item, *, width=80, ansi_on=True,
                        wrapped=None):
    """Populate ``preview_render`` with a sentinel cache entry."""
    item.preview_render = PreviewRender(
        wrapped=wrapped if wrapped is not None else ['cached'],
        raw_tail_offset=len(item.preview) if item.preview else 0,
        wrapped_tail_offset=1,
        width=width,
        ansi_on=ansi_on,
    )


# --- Eager invalidation hooks ---------------------------------------------


class TestSetPreviewClearsRender(unittest.TestCase):

    def test_set_preview_drops_render_cache(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'old')
        _attach_render_cache(item)
        b.set_preview('a', 'new')
        # set_preview routes through _preview_result + apply_preview_result.
        self.assertTrue(b.apply_preview_result())
        self.assertEqual(item.preview, 'new')
        self.assertIsNone(item.preview_render)


class TestAppendPreviewClearsRender(unittest.TestCase):

    def test_append_preview_drops_render_cache(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'foo')
        _attach_render_cache(item)
        b.append_preview('a', 'bar')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'foobar')
        self.assertIsNone(item.preview_render)


class TestClearPreviewDropsBoth(unittest.TestCase):

    def test_clear_preview_drops_both_fields(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'old')
        _attach_render_cache(item)
        b.clear_preview('a')
        b.drain_main_queue()
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)


class TestInvalidatePreviewDropsBoth(unittest.TestCase):

    def test_invalidate_preview_drops_both_fields(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'old')
        _attach_render_cache(item)
        b.invalidate_preview('a')
        b.drain_main_queue()
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)


class TestDropPreviewCacheDropsRender(unittest.TestCase):

    def test_drop_single_id_drops_render(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'text')
        _attach_render_cache(item)
        b.drop_preview_cache('a')
        b.drain_main_queue()
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)

    def test_drop_all_drops_every_render(self):
        b = Browser(BrowserConfig(_headless=True))
        items = [_seed(b, i, i) for i in ('a', 'b', 'c')]
        for it in items:
            _attach_render_cache(it)
        b.drop_preview_cache()
        b.drain_main_queue()
        for it in items:
            self.assertIsNone(it.preview)
            self.assertIsNone(it.preview_render)


class TestResizeInvalidatesAllRenders(unittest.TestCase):

    def test_invalidate_helper_walks_all_loaded_items(self):
        # The resize hook in the main loop calls
        # ``_invalidate_all_preview_renders``; exercise the helper
        # directly so the test doesn't depend on the loop.
        b = Browser(BrowserConfig(_headless=True))
        items = [_seed(b, i, f'text-{i}') for i in ('a', 'b')]
        for it in items:
            _attach_render_cache(it)
        # Preview text must survive — only the wrap cache goes.
        b._invalidate_all_preview_renders()
        for it in items:
            self.assertIsNotNone(it.preview)
            self.assertIsNone(it.preview_render)


class TestToggleAnsiInvalidatesAllRenders(unittest.TestCase):

    def test_toggle_action_clears_render_on_every_item(self):
        b = Browser(BrowserConfig(_headless=True))
        items = [_seed(b, i, f'text-{i}') for i in ('a', 'b')]
        for it in items:
            _attach_render_cache(it)
        ctx = Context(b)
        before = b.preview_ansi
        _actions._toggle_preview_ansi(ctx)
        self.assertNotEqual(b.preview_ansi, before)
        for it in items:
            self.assertIsNotNone(it.preview)
            self.assertIsNone(it.preview_render)


class TestUpdateDataMutationDropsBoth(unittest.TestCase):

    def test_mod_on_existing_item_drops_preview_and_render(self):
        b = Browser(BrowserConfig(_headless=True))
        # Seed an item via update_data so _items_by_id and _children
        # are properly populated.
        b._state._children[None] = []
        b.update_data([_state.upsert('a', None, title='A')])
        b.drain_main_queue()
        item = b._state._items_by_id['a']
        item.preview = 'cached'
        _attach_render_cache(item)
        b.update_data([_state.mod('a', title='A renamed')])
        b.drain_main_queue()
        self.assertEqual(item.title, 'A renamed')
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)

    def test_upsert_existing_id_drops_preview_and_render(self):
        b = Browser(BrowserConfig(_headless=True))
        b._state._children[None] = []
        b.update_data([_state.upsert('a', None, title='A')])
        b.drain_main_queue()
        item = b._state._items_by_id['a']
        item.preview = 'cached'
        _attach_render_cache(item)
        b.update_data([_state.upsert('a', None, title='A v2')])
        b.drain_main_queue()
        self.assertEqual(item.title, 'A v2')
        self.assertIsNone(item.preview)
        self.assertIsNone(item.preview_render)


# --- Lazy fill / cache reuse ----------------------------------------------


def _render_preview(browser):
    """Run render_full while capturing stdout."""
    orig = sys.stdout
    sys.stdout = io.StringIO()
    try:
        _render.render_full(browser)
    finally:
        sys.stdout = orig


def _make_browser_with_preview(text):
    b = Browser(BrowserConfig(_headless=True, get_preview=lambda _: text))
    item = _data.to_item(Item(id='a'))
    b._state._children[None] = [item]
    b._state._items_by_id['a'] = item
    _state.mark_visible_dirty(b._state)
    item.preview = text
    return b


class TestLazyFillAndReuse(unittest.TestCase):

    def test_first_render_populates_cache(self):
        b = _make_browser_with_preview('line\n' * 5)
        try:
            self.assertIsNone(b._state._items_by_id['a'].preview_render)
            _render_preview(b)
            cached = b._state._items_by_id['a'].preview_render
            self.assertIsNotNone(cached)
            self.assertGreater(len(cached.wrapped), 0)
        finally:
            b.stop_workers()

    def test_second_render_reuses_cache(self):
        # Spy on _wrap_preview_line: count invocations across two
        # consecutive renders. The second render should reuse the cache
        # and skip the per-line wrap pass entirely.
        b = _make_browser_with_preview('hello\nworld\n')
        try:
            calls = {'n': 0}
            real_wrap = _render._wrap_preview_line

            def counting_wrap(*args, **kw):
                calls['n'] += 1
                return real_wrap(*args, **kw)

            _render._wrap_preview_line = counting_wrap
            try:
                _render_preview(b)
                first = calls['n']
                self.assertGreater(first, 0)
                # Second render — same geometry, same ansi, no search.
                b._needs_redraw.add('preview')
                _render_preview(b)
                self.assertEqual(
                    calls['n'], first,
                    '_wrap_preview_line should not be called on a '
                    'cache hit',
                )
            finally:
                _render._wrap_preview_line = real_wrap
        finally:
            b.stop_workers()

    def test_width_mismatch_regenerates(self):
        # Defensive path: if eager invalidation somehow misses, a
        # width mismatch on the cache forces regeneration.
        b = _make_browser_with_preview('aaa\n')
        try:
            _render_preview(b)
            stored = b._state._items_by_id['a'].preview_render
            self.assertIsNotNone(stored)
            # Forge a stale cache with a wrong width — the next render
            # must overwrite it.
            forged = stored._replace(width=stored.width + 999,
                                     wrapped=['STALE'])
            b._state._items_by_id['a'].preview_render = forged
            b._needs_redraw.add('preview')
            _render_preview(b)
            new = b._state._items_by_id['a'].preview_render
            self.assertNotEqual(new.wrapped, ['STALE'])
            self.assertEqual(new.width, stored.width)
        finally:
            b.stop_workers()


# --- Item.preview round-trip ---------------------------------------------


class TestPreviewRoundTrip(unittest.TestCase):

    def test_set_then_cached_then_clear(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a')
        b.set_preview('a', 'hello')
        b.apply_preview_result()
        self.assertEqual(item.preview, 'hello')
        self.assertEqual(b.get_cached_preview('a'), 'hello')

        b.clear_preview('a')
        b.drain_main_queue()
        self.assertIsNone(item.preview)
        self.assertIsNone(b.get_cached_preview('a'))


if __name__ == '__main__':
    unittest.main()
