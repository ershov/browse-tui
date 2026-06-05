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
        # #431: set_preview now routes through the FIFO post queue.
        b.drain_main_queue()
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
        b.drain_main_queue()
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


# --- Preview-API registration prerequisite (ticket #430) ------------------
#
# The preview cache lives on ``Item.preview`` (per #422). The four mutating
# preview APIs (``set_preview`` / ``append_preview`` / ``clear_preview`` /
# ``invalidate_preview``) all look up ``_items_by_id[id]`` on the main
# thread and silently no-op when the id is not present — there's no Item
# to write to. These tests pin that contract so a future refactor can't
# accidentally start auto-synthesising Items from preview writes.
#
# Plus the documented escape hatch: ``update_data([upsert(id, parent)])``
# is the cheap idempotent-ensure pattern. For a missing id it creates a
# minimal Item; for an existing id with no extra fields it's a no-op
# (patch-merge with no fields).


class TestPreviewAPIRegistrationPrerequisite(unittest.TestCase):
    """Mutating preview APIs no-op silently for unregistered ids."""

    def test_set_preview_on_unregistered_id_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        # No Item registered for 'ghost'.
        self.assertNotIn('ghost', b._state._items_by_id)
        b.set_preview('ghost', 'should-be-dropped')
        # #431: set_preview's closure looks up _items_by_id and returns
        # early if the id is unknown — no Item is synthesised and no
        # text is cached.
        b.drain_main_queue()
        self.assertNotIn('ghost', b._state._items_by_id)
        self.assertIsNone(b.get_cached_preview('ghost'))

    def test_set_preview_on_registered_id_writes(self):
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a')  # registered, preview=None initially
        b.set_preview('a', 'hello')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'hello')
        self.assertEqual(b.get_cached_preview('a'), 'hello')

    def test_append_preview_on_unregistered_id_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertNotIn('ghost', b._state._items_by_id)
        b.append_preview('ghost', 'chunk')
        b.drain_main_queue()
        # No Item created; no preview cached.
        self.assertNotIn('ghost', b._state._items_by_id)
        self.assertIsNone(b.get_cached_preview('ghost'))

    def test_clear_preview_on_unregistered_id_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        # Should not raise; should not create an Item.
        b.clear_preview('ghost')
        b.drain_main_queue()
        self.assertNotIn('ghost', b._state._items_by_id)

    def test_invalidate_preview_on_unregistered_id_is_noop(self):
        b = Browser(BrowserConfig(_headless=True))
        # Capture any worker kicks — invalidate_preview calls
        # request_preview, which should still run (it's the recipe's
        # responsibility to either tolerate that no-op or pre-register
        # the Item via upsert).
        b.request_preview = lambda id_: None
        # Should not raise; should not create an Item.
        b.invalidate_preview('ghost')
        b.drain_main_queue()
        self.assertNotIn('ghost', b._state._items_by_id)


class TestIdempotentEnsureUpsertPattern(unittest.TestCase):
    """``update_data([upsert(id, parent)])`` is the documented ensure path.

    For a missing id, this creates a minimal Item with default fields
    so subsequent ``set_preview`` lands. For an existing id with no
    extra fields, it is a patch-merge-with-no-fields no-op.
    """

    def test_upsert_then_set_preview_caches_text(self):
        b = Browser(BrowserConfig(_headless=True))
        # Seed an empty children list for the parent so the upsert can
        # land — without it ``_children[parent]`` remains None and
        # tree-side bookkeeping skips the insertion.
        b._state._children[None] = []
        # 'unknown' has no Item yet.
        self.assertNotIn('unknown', b._state._items_by_id)
        b.update_data([_state.upsert('unknown', None)])
        b.drain_main_queue()
        # Item now exists with default field values. Note: Item.__post_init__
        # backfills an empty ``title`` from ``str(id)``, so a bare upsert
        # leaves the row visible as the id rather than as a blank line.
        item = b._state._items_by_id.get('unknown')
        self.assertIsNotNone(item)
        self.assertEqual(item.id, 'unknown')
        self.assertEqual(item.title, 'unknown')
        self.assertEqual(item.tag, '')
        self.assertFalse(item.has_children)
        # And it is recorded as a child of the root parent.
        self.assertIn(item, b._state._children[None])

        # set_preview now lands.
        b.set_preview('unknown', 'text-for-unknown')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'text-for-unknown')
        self.assertEqual(b.get_cached_preview('unknown'),
                         'text-for-unknown')

    def test_upsert_existing_id_no_fields_does_not_overwrite(self):
        b = Browser(BrowserConfig(_headless=True))
        b._state._children[None] = []
        # Seed via upsert with real fields.
        b.update_data([_state.upsert('a', None, title='Original',
                                     tag='v1')])
        b.drain_main_queue()
        item = b._state._items_by_id['a']
        self.assertEqual(item.title, 'Original')
        self.assertEqual(item.tag, 'v1')
        # Idempotent-ensure: upsert with no fields must not zero out
        # existing fields.
        b.update_data([_state.upsert('a', None)])
        b.drain_main_queue()
        self.assertEqual(item.title, 'Original')
        self.assertEqual(item.tag, 'v1')


# --- #431/#442: set_preview routes through the FIFO post queue -----------
#
# Pre-#431: ``set_preview`` wrote to the single-slot ``_preview_result``,
# so two recipe writes in quick succession lost the first (latest-wins).
# Post-#431: ``set_preview`` posts a main-thread closure per call, so
# every write lands. Post-#442: the framework worker also delivers via
# the post queue (the ``_preview_result`` lane was removed), so all
# writes — recipe and worker — share a single FIFO ordering.


class TestSetPreviewPostQueueSemantics(unittest.TestCase):
    """``set_preview`` lands every write via the FIFO post queue (#431)."""

    def test_multiple_set_preview_calls_all_land(self):
        # The motivating case for #431: three writes to three different
        # ids, all of which must land. Pre-#431 only the last would survive.
        b = Browser(BrowserConfig(_headless=True))
        items = {id_: _seed(b, id_) for id_ in ('A', 'B', 'C')}
        b.set_preview('A', 'a-text')
        b.set_preview('B', 'b-text')
        b.set_preview('C', 'c-text')
        b.run_until_idle()
        self.assertEqual(items['A'].preview, 'a-text')
        self.assertEqual(items['B'].preview, 'b-text')
        self.assertEqual(items['C'].preview, 'c-text')

    def test_no_single_slot_preview_result_lane(self):
        # #442: the legacy ``_preview_result`` slot is gone. The worker
        # delivers via the post queue too, so there is no shared "worker
        # lane" to race against.
        b = Browser(BrowserConfig(_headless=True))
        _seed(b, 'A')
        b.set_preview('A', 'a-text')
        self.assertFalse(hasattr(b, '_preview_result'))

    def test_update_data_then_set_preview_fifo_orders_writes(self):
        # ``update_data`` posts the apply step on the main thread; a
        # subsequent ``set_preview`` lands in the queue after it. FIFO
        # drain guarantees the upsert lands before the preview write,
        # so the new Item is in ``_items_by_id`` when ``set_preview``'s
        # closure runs.
        b = Browser(BrowserConfig(_headless=True))
        b._state._children[None] = []
        self.assertNotIn('new', b._state._items_by_id)
        b.update_data([_state.upsert('new', None)])
        b.set_preview('new', 'preview-for-new')
        b.run_until_idle()
        item = b._state._items_by_id.get('new')
        self.assertIsNotNone(item)
        self.assertEqual(item.preview, 'preview-for-new')

    def test_set_preview_then_clear_preview_fifo_orders_writes(self):
        # FIFO across the mutating preview APIs: a clear posted after a
        # set must observe the set on the way through.
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a')
        b.set_preview('a', 'hello')
        b.clear_preview('a')
        b.drain_main_queue()
        # The clear wins because it was posted last.
        self.assertIsNone(item.preview)

    def test_clear_preview_then_set_preview_fifo_orders_writes(self):
        # Reverse order: clear first, then set — the set wins.
        b = Browser(BrowserConfig(_headless=True))
        item = _seed(b, 'a', 'old')
        b.clear_preview('a')
        b.set_preview('a', 'new')
        b.drain_main_queue()
        self.assertEqual(item.preview, 'new')

    def test_set_preview_id_late_binding_in_loop_is_safe(self):
        # Construct ``set_preview`` calls in a comprehension/loop with
        # different ids. The closure inside ``set_preview`` captures
        # ``id_`` as a parameter (function arg), not a free variable,
        # so this is safe — but pin the contract here so a future
        # refactor can't accidentally regress.
        b = Browser(BrowserConfig(_headless=True))
        items = {id_: _seed(b, id_) for id_ in ('x', 'y', 'z')}
        ids_and_text = [('x', 'X-text'), ('y', 'Y-text'), ('z', 'Z-text')]
        for id_, text in ids_and_text:
            b.set_preview(id_, text)
        b.drain_main_queue()
        for id_, text in ids_and_text:
            self.assertEqual(items[id_].preview, text)


if __name__ == '__main__':
    unittest.main()
