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
# Cross-wires for the #423 in-place ``append_preview`` extension path:
# ``_extend_or_drop_preview_render`` (in 040-state.py) calls the wrap
# and sanitisation helpers defined in 050-render.py. In the artifact
# they share a module namespace; under the test loader they don't, so
# inject explicitly.
_state.PreviewRender = _data.PreviewRender
_state._wrap_preview_line = _render._wrap_preview_line
_state._sanitize_preview = _render._sanitize_preview

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


class TestAppendPreviewExtendsRender(unittest.TestCase):
    """``append_preview`` extends the wrap in place when possible (#423).

    Sentinel-cache cases (no real wrap behind the cache) keep the
    extension contract observable by checking the splice happens: a
    non-empty append re-wraps the affected tail and updates offsets;
    an empty chunk is a cheap no-op; a missing cache leaves
    ``preview_render`` None for the next paint to lazy-fill.
    """

    def test_append_preview_extends_render_cache(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'foo')
        # Cached state for raw "foo": wrap is one row, offset 0, tail
        # offset 0 (the whole preview is still the open partial line).
        item.preview_render = PreviewRender(
            wrapped=['foo'],
            raw_tail_offset=0,
            wrapped_tail_offset=0,
            width=80,
            ansi_on=True,
        )
        b.append_preview('a', 'bar')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'foobar')
        # Cache extended in place — not dropped.
        self.assertIsNotNone(item.preview_render)
        self.assertEqual(item.preview_render.wrapped, ['foobar'])
        self.assertEqual(item.preview_render.raw_tail_offset, 0)
        self.assertEqual(item.preview_render.wrapped_tail_offset, 0)

    def test_append_preview_empty_chunk_keeps_cache(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'foo')
        cached = PreviewRender(
            wrapped=['foo'],
            raw_tail_offset=0,
            wrapped_tail_offset=0,
            width=80,
            ansi_on=True,
        )
        item.preview_render = cached
        b.append_preview('a', '')
        b.drain_main_queue()
        # No change to preview text or the cache.
        self.assertEqual(item.preview, 'foo')
        self.assertIs(item.preview_render, cached)

    def test_append_preview_with_no_cache_stays_none(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'foo')
        # No preview_render attached → still None; next paint will
        # lazy-fill the wrap.
        self.assertIsNone(item.preview_render)
        b.append_preview('a', 'bar')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'foobar')
        self.assertIsNone(item.preview_render)

    def test_append_preview_ansi_mismatch_drops_cache(self):
        # Defensive fallback: if the cached ansi_on doesn't match the
        # Browser's current policy (eager invalidation should normally
        # prevent this), drop the cache so the next render rebuilds.
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'foo')
        item.preview_render = PreviewRender(
            wrapped=['foo'],
            raw_tail_offset=0,
            wrapped_tail_offset=0,
            width=80,
            ansi_on=not b.preview_ansi,  # mismatch
        )
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


# --- #423 in-place extension via render round-trip -----------------------
#
# These tests build a baseline by setting the full text + rendering, then
# build the same content incrementally (initial set + append) and re-render.
# The two ``wrapped`` lists must be byte-identical: extending in place must
# never diverge from a fresh full re-wrap.


def _render_and_get_wrapped(b):
    """Render once and return the cached ``wrapped`` list."""
    _render_preview(b)
    cached = b._state._items_by_id['a'].preview_render
    assert cached is not None, 'expected preview_render to be populated'
    return list(cached.wrapped)


def _build_baseline(text):
    """Browser with the full preview pre-set and rendered once."""
    b = _make_browser_with_preview(text)
    return b


def _build_incremental(prefix, chunk):
    """Browser with ``prefix`` set + rendered, then ``chunk`` appended."""
    b = _make_browser_with_preview(prefix)
    _render_preview(b)  # populate the wrap cache
    b.append_preview('a', chunk)
    b.drain_main_queue()
    return b


class TestAppendExtendMatchesFreshWrap(unittest.TestCase):
    """The #423 invariant: incremental wrap == fresh full wrap, byte-for-byte."""

    def _assert_match(self, prefix, chunk):
        full = prefix + chunk
        baseline = _build_baseline(full)
        incremental = _build_incremental(prefix, chunk)
        try:
            base_wrapped = _render_and_get_wrapped(baseline)
            inc_wrapped = _render_and_get_wrapped(incremental)
            self.assertEqual(
                inc_wrapped, base_wrapped,
                'in-place extension diverged from fresh wrap for '
                'prefix={!r}, chunk={!r}'.format(prefix, chunk),
            )
            # The cached preview text must match too.
            self.assertEqual(
                incremental._state._items_by_id['a'].preview, full,
            )
        finally:
            baseline.stop_workers()
            incremental.stop_workers()

    def test_no_newline_chunk(self):
        # Append text without any '\n' — the open last line extends.
        self._assert_match('hello', ' world')

    def test_one_newline_chunk(self):
        # Append text with one '\n' — closes the partial, opens a new one.
        self._assert_match('hello', '\nworld')

    def test_multi_newline_chunk(self):
        # Append text with several newlines — multiple new wrapped lines.
        self._assert_match('foo', '\nbar\nbaz\nqux')

    def test_chunk_starts_with_newline(self):
        # Edge: chunk begins with '\n' — previous open line closes
        # immediately, new lines follow.
        self._assert_match('alpha', '\nbeta\ngamma')

    def test_chunk_ends_with_newline(self):
        # Edge: chunk ends with '\n' — new tail line is empty.
        self._assert_match('one', '\ntwo\n')

    def test_chunk_into_empty_preview(self):
        # Append to a preview that was originally empty but rendered.
        self._assert_match('', 'fresh content')

    def test_long_chunk_wraps_multiple_times(self):
        # A chunk long enough to wrap many times under default width.
        # The narrow-width path is exercised in a dedicated test below.
        self._assert_match('start ', 'x' * 500)

    def test_append_after_existing_newlines(self):
        # Prefix has multiple newlines already.
        self._assert_match('a\nb\nc', '\nd\ne')

    def test_append_preserves_partial_line_wrap(self):
        # Prefix ends mid-line (no trailing '\n'); chunk extends it
        # then breaks. Tail offsets must point past the new break.
        self._assert_match('partial line text', ' more\nnext')

    def test_multiple_appends_chained(self):
        # Two sequential appends — the second extension reads offsets
        # written by the first. Catches any off-by-one in update logic.
        full = 'a\nbb\nccc\ndddd'
        baseline = _build_baseline(full)
        b = _make_browser_with_preview('a')
        try:
            _render_preview(b)
            b.append_preview('a', '\nbb')
            b.drain_main_queue()
            b.append_preview('a', '\nccc\ndddd')
            b.drain_main_queue()
            base_wrapped = _render_and_get_wrapped(baseline)
            inc_wrapped = _render_and_get_wrapped(b)
            self.assertEqual(inc_wrapped, base_wrapped)
        finally:
            baseline.stop_workers()
            b.stop_workers()

    def test_property_various_split_points(self):
        # For a fixed full text, split at several positions and verify
        # set(prefix) + append(suffix) == set(full) for each split.
        full = 'lorem\nipsum dolor sit amet\nconsectetur\nadipiscing'
        for k in (0, 1, 5, 6, 7, 15, 23, 27, len(full) - 1, len(full)):
            with self.subTest(split=k):
                self._assert_match(full[:k], full[k:])


class TestAppendExtendOffsetsBookkeeping(unittest.TestCase):
    """Verify the recorded offsets after an extension are usable."""

    def test_offsets_match_fresh_wrap(self):
        # After an in-place extension the offsets should be equal to
        # the offsets a fresh full render would have produced.
        prefix = 'first line\nsecond'
        chunk = ' line tail\nthird line'
        full = prefix + chunk
        baseline = _make_browser_with_preview(full)
        incremental = _make_browser_with_preview(prefix)
        try:
            _render_preview(baseline)
            _render_preview(incremental)
            incremental.append_preview('a', chunk)
            incremental.drain_main_queue()
            # Render the baseline already happened; just inspect.
            base_cached = baseline._state._items_by_id['a'].preview_render
            inc_cached = incremental._state._items_by_id['a'].preview_render
            self.assertIsNotNone(inc_cached)
            self.assertEqual(inc_cached.raw_tail_offset,
                             base_cached.raw_tail_offset)
            self.assertEqual(inc_cached.wrapped_tail_offset,
                             base_cached.wrapped_tail_offset)
            self.assertEqual(inc_cached.width, base_cached.width)
            self.assertEqual(inc_cached.ansi_on, base_cached.ansi_on)
        finally:
            baseline.stop_workers()
            incremental.stop_workers()


class TestAppendAfterWidthChange(unittest.TestCase):
    """Width change drops the cache; subsequent append extends from fresh."""

    def test_width_change_then_append_extends_correctly(self):
        # Simulate: render at one width, simulate a width invalidation
        # (drop_preview_cache helper), append → next paint regens
        # fresh, then a second append extends correctly.
        prefix = 'aaa\nbbb'
        chunk1 = '\nccc'
        chunk2 = '\nddd'
        full = prefix + chunk1 + chunk2

        # Build incremental: prefix → render → invalidate (simulating
        # an eager-invalidation hook firing) → append chunk1 → render
        # (fresh) → append chunk2 → render.
        incremental = _make_browser_with_preview(prefix)
        try:
            _render_preview(incremental)
            # Width change ⇒ wrap cache dropped on every item. Mimic
            # what ``_invalidate_all_preview_renders`` does.
            incremental._invalidate_all_preview_renders()
            self.assertIsNone(
                incremental._state._items_by_id['a'].preview_render,
            )
            # First append after invalidation: extension helper sees
            # ``preview_render is None`` and leaves it None.
            incremental.append_preview('a', chunk1)
            incremental.drain_main_queue()
            self.assertIsNone(
                incremental._state._items_by_id['a'].preview_render,
            )
            # Next render lazy-fills.
            _render_preview(incremental)
            cached_after_render = (
                incremental._state._items_by_id['a'].preview_render
            )
            self.assertIsNotNone(cached_after_render)
            # Second append: extension helper now has a cache to grow.
            incremental.append_preview('a', chunk2)
            incremental.drain_main_queue()
            cached_after_chunk2 = (
                incremental._state._items_by_id['a'].preview_render
            )
            self.assertIsNotNone(cached_after_chunk2)
            # Match against a fresh baseline.
            baseline = _make_browser_with_preview(full)
            try:
                base_wrapped = _render_and_get_wrapped(baseline)
                self.assertEqual(cached_after_chunk2.wrapped, base_wrapped)
            finally:
                baseline.stop_workers()
        finally:
            incremental.stop_workers()


class TestAppendNarrowWidthWrap(unittest.TestCase):
    """Verify long-chunk multi-wrap correctness using a tiny pane."""

    def test_long_chunk_wraps_to_correct_row_count(self):
        # Build a Browser with a tiny preview pane so wrapping is
        # certain to kick in. We can't easily set the pane width
        # directly from a unit test (it comes from term geometry), so
        # we forge the cache state with a small width and call the
        # extension helper directly to exercise the wrap math.
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'ab')
        width = 4
        item.preview_render = PreviewRender(
            wrapped=['ab'],
            raw_tail_offset=0,
            wrapped_tail_offset=0,
            width=width,
            ansi_on=True,
        )
        # Append a chunk that — combined with the open 'ab' — needs to
        # wrap. With width=4, "ab" + "cdefghij" = "abcdefghij" → 10
        # cols → ceil(10/4) = 3 wrapped rows.
        b.append_preview('a', 'cdefghij')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'abcdefghij')
        cached = item.preview_render
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached.wrapped), 3)
        # Sanity: every row is at most ``width`` columns wide (no SGR
        # in this content, so byte-length = visible cols).
        for row in cached.wrapped:
            self.assertLessEqual(len(row), width)
        # The concatenation of the wrapped rows reproduces the raw text.
        self.assertEqual(''.join(cached.wrapped), 'abcdefghij')


if __name__ == '__main__':
    unittest.main()
