"""Unit tests for the preview stale-hold (preview-flicker design §B).

``render_preview`` keeps painting the last successfully painted
per-item preview (``Browser._preview_snapshot``) while the cursor row
pends — the synthetic placeholder row, or a normal item with
``preview is None`` OR no delivery for the current visit yet (#954:
cached rows hold until the cursor settles) — and blanks only when no
snapshot exists yet, the visible list is empty, or the item has a
delivered value for a settled visit (including ``''``).

Covers:

  * Move from a painted item to an uncached one — the pane keeps the
    old content until the delivery lands, then swaps.
  * Move to a cached row — the pane keeps the old content until the
    worker's settle nudge, then swaps without any delivery (#954).
  * Delivered ``''`` blanks once the visit settles (delivered-empty
    is a real result).
  * Streaming: the first ``append_preview`` chunk swaps the held view.
  * #456 guard: the snapshot survives the abandoned-partial cache
    clear, and a revisit still refetches fresh.
  * Resize while holding re-wraps from the snapshot's raw text; the
    snapshot itself is never updated from a held paint.
  * The held view is frozen at the snapshot's (clamped) scroll.
  * The cursored pending item's ``preview_render`` is not polluted.
  * Meta rows never pend: a delivered meta preview paints, a missing
    one blanks rather than holding (no delivery ever comes for meta).
  * Empty visible list / no snapshot / help mode paint as before.

See docs/superpowers/specs/2026-06-12-preview-flicker-design.md.
"""

import io
import unittest

from test.unit._loader import load


# --- Module loading + cross-wiring ----------------------------------------

_term = load('_browse_tui_term_sh', '020-terminal.py')
_data = load('_browse_tui_data_sh', '030-data.py')
_state = load('_browse_tui_state_sh', '040-state.py')
_render = load('_browse_tui_render_sh', '050-render.py')
_context = load('_browse_tui_context_sh', '060-context.py')
_actions = load('_browse_tui_actions_sh', '070-actions.py')

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
# ``render_list`` coerces a str row-content result via ``_normalize_content``
# (040-state); inject it for the isolated load (#738).
_render._normalize_content = _state._normalize_content
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

# ``compose_help_text`` (the help-mode paint) reads the built-in action
# table from the action layer.
_render.default_actions = _actions.default_actions


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig


def _make_browser(specs, **kw):
    """Headless Browser with root items per ``specs``: (id, preview) pairs.

    ``preview=None`` leaves the cache unset — the row renders as
    pending. Extra Item kwargs ride in a third tuple slot.

    Deliveries are injected synchronously (``_deliver_preview`` /
    ``append_preview``); no test here wants a live worker fetch, which
    would flip a pending row to delivered-``''`` mid-assertion. The 5s
    debounce parks any requested fetch far past the assertion window
    instead of leaning on the 0.2s default (#939).
    """
    kw.setdefault('_headless', True)
    kw.setdefault('get_preview', lambda _id: '')
    kw.setdefault('preview_debounce', 5.0)
    b = Browser(BrowserConfig(**kw))
    children = []
    for spec in specs:
        id_, preview = spec[0], spec[1]
        extra = spec[2] if len(spec) > 2 else {}
        item = _data.to_item(Item(id=id_, **extra))
        if preview is not None:
            item.preview = preview
        b._state._items_by_id[id_] = item
        children.append(item)
    b._state._children[None] = children
    _state.mark_visible_dirty(b._state)
    return b


def _render_full(browser):
    """Run render_full, swallowing the emitted terminal output."""
    orig = _term._tty_writer
    _term._tty_writer = io.StringIO()
    try:
        _render.render_full(browser)
    finally:
        _term._tty_writer = orig


def _preview_rows(browser):
    """Content rows of the preview pane as last painted.

    Reads the differential renderer's row cache rather than the emitted
    bytes — a held repaint of identical rows emits nothing, but the
    cache always holds what is on screen. Row 0 of the 'h'-layout
    preview rect is the info-bar header; everything below is content.
    """
    lines = browser._pane_cache['preview'].lines
    return [entry[1] if entry is not None else '' for entry in lines[1:]]


def _pane_text(browser):
    return '\n'.join(_preview_rows(browser))


def _move_cursor(browser, index):
    """Move the cursor and run the main loop's per-tick preview update."""
    browser._state.cursor = index
    browser._update_preview_for_cursor()


class _StaleHoldBase(unittest.TestCase):
    """Pin the terminal geometry so wrap widths are deterministic."""

    def setUp(self):
        self._saved_term_size = _render.term_size
        self._set_term_size((80, 24))

    def tearDown(self):
        _render.term_size = self._saved_term_size

    def _set_term_size(self, size):
        _render.term_size = lambda: size


# --- Hold until delivery, then swap ----------------------------------------


class TestHoldUntilDelivery(_StaleHoldBase):

    def test_move_to_uncached_holds_then_delivery_swaps(self):
        b = _make_browser([('a', 'alpha-one\nalpha-two'), ('b', None)])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            self.assertIn('alpha-one', _pane_text(b))
            snap = b._preview_snapshot
            self.assertIsNotNone(snap)
            self.assertEqual(snap.text, 'alpha-one\nalpha-two')

            # Cursor onto the uncached row: the pane keeps a's content.
            _move_cursor(b, 1)
            _render_full(b)
            self.assertIn('alpha-one', _pane_text(b))
            self.assertIn('alpha-two', _pane_text(b))
            # Self-contained hold: the pending item's wrap cache stays
            # empty and the snapshot is untouched (same object).
            self.assertIsNone(b._state._items_by_id['b'].preview_render)
            self.assertIs(b._preview_snapshot, snap)

            # Delivery lands → the pane swaps to b's content in one step
            # and the snapshot now tracks the new paint.
            b._deliver_preview('b', 'bravo-content')
            _render_full(b)
            self.assertIn('bravo-content', _pane_text(b))
            self.assertNotIn('alpha-one', _pane_text(b))
            self.assertEqual(b._preview_snapshot.text, 'bravo-content')
        finally:
            b.stop_workers()

    def test_move_to_cached_holds_until_settle_nudge(self):
        # #954: a row with a CACHED preview holds the old content too —
        # the swap signal is the worker's settle nudge, not the cursor
        # move. No delivery ever happens for the cached row.
        b = _make_browser([('a', 'alpha-content'), ('c', 'charlie-cached')])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))

            # Cursor onto the cached row: visit not settled → hold.
            _move_cursor(b, 1)
            self.assertFalse(b._preview_visit_delivered)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))
            self.assertNotIn('charlie-cached', _pane_text(b))

            # The settle nudge ends the hold; the cached content swaps
            # in with the cache untouched.
            b._settle_cached_preview('c')
            self.assertTrue(b._preview_visit_delivered)
            _render_full(b)
            self.assertIn('charlie-cached', _pane_text(b))
            self.assertNotIn('alpha-content', _pane_text(b))
            self.assertEqual(
                b._state._items_by_id['c'].preview, 'charlie-cached')
        finally:
            b.stop_workers()

    def test_no_snapshot_yet_blanks(self):
        # Startup shape: the first cursor row is still pending and
        # nothing has been painted — current (blank) behavior.
        b = _make_browser([('a', None)])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            self.assertEqual(_pane_text(b).strip(), '')
            self.assertIsNone(b._preview_snapshot)
        finally:
            b.stop_workers()

    def test_pending_placeholder_row_holds(self):
        # An expanded branch with uncached children emits a synthetic
        # ``⧗ loading…`` row (kind='pending'); the cursor landing there
        # holds the snapshot just like an undelivered item row.
        b = _make_browser([
            ('a', 'alpha-content'),
            ('p', 'parent-content', {'has_children': True}),
        ])
        try:
            b._state.expanded.add('p')
            _state.mark_visible_dirty(b._state)
            _move_cursor(b, 0)
            _render_full(b)
            # visible: a, p, placeholder — land on the placeholder.
            _move_cursor(b, 2)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))
        finally:
            b.stop_workers()


# --- Delivered values blank / swap ------------------------------------------


class TestDeliveredValues(_StaleHoldBase):

    def test_delivered_empty_blanks(self):
        b = _make_browser([('a', 'alpha-content'), ('c', '')])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))

            # '' is a delivered result: the pane must blank, not hold —
            # but only once the visit settles (#954); inject the
            # worker's cache-hit nudge to end the settle window.
            _move_cursor(b, 1)
            b._settle_cached_preview('c')
            _render_full(b)
            self.assertEqual(_pane_text(b).strip(), '')
        finally:
            b.stop_workers()

    def test_streaming_first_chunk_swaps(self):
        b = _make_browser([('a', 'alpha-content'), ('b', None)])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            _move_cursor(b, 1)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))

            # First streamed chunk makes the preview a delivered value;
            # the held content gives way immediately.
            b.append_preview('b', 'chunk-one ')
            b.drain_main_queue()
            _render_full(b)
            self.assertIn('chunk-one', _pane_text(b))
            self.assertNotIn('alpha-content', _pane_text(b))
        finally:
            b.stop_workers()


# --- #456 abandoned partial ------------------------------------------------


class TestAbandonedPartialGuard(_StaleHoldBase):

    def test_snapshot_survives_456_clear_and_revisit_refetches(self):
        b = _make_browser([('a', 'partial-chunk '), ('b', None)])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            _move_cursor(b, 1)
            # Cursor-move abandon path (#456): the partial buffer is
            # cleared — exactly what the snapshot must survive.
            b._post_clear_abandoned_preview('a')
            b.drain_main_queue()
            self.assertIsNone(b._state._items_by_id['a'].preview)
            _render_full(b)
            self.assertIn('partial-chunk', _pane_text(b))

            # Revisit: a's cache is gone, so the move re-fires a fetch
            # (refetch-fresh contract) while the pane keeps holding.
            _move_cursor(b, 0)
            self.assertEqual(b._preview_req, 'a')
            _render_full(b)
            self.assertIn('partial-chunk', _pane_text(b))

            # The fresh delivery replaces the held view.
            b._deliver_preview('a', 'fresh-full')
            _render_full(b)
            self.assertIn('fresh-full', _pane_text(b))
            self.assertNotIn('partial-chunk', _pane_text(b))
        finally:
            b.stop_workers()

    def test_invalidate_cursored_item_holds_own_content(self):
        # invalidate_preview on the cursored item (e.g. the
        # resize-refetch path) holds the item's own previous content
        # during the refetch instead of blanking.
        b = _make_browser([('a', 'alpha-content')])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            b.invalidate_preview('a')
            b.drain_main_queue()
            self.assertIsNone(b._state._items_by_id['a'].preview)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))
        finally:
            b.stop_workers()


# --- Geometry: re-wrap on resize, frozen scroll ------------------------------


class TestHeldGeometry(_StaleHoldBase):

    def test_resize_while_holding_rewraps_snapshot(self):
        b = _make_browser([('a', 'X' * 150), ('b', None)])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            snap = b._preview_snapshot
            self.assertEqual(snap.width, 80)
            self.assertEqual(snap.wrapped, ['X' * 80, 'X' * 70])

            _move_cursor(b, 1)
            self._set_term_size((40, 24))
            # Mimic the main loop's resize handling for the wrap caches.
            b._invalidate_all_preview_renders()
            _render_full(b)
            rows = [r for r in _preview_rows(b) if 'X' in r]
            self.assertEqual(rows, ['X' * 40, 'X' * 40, 'X' * 40, 'X' * 30])
            # The stale paint never updates the snapshot: still the
            # 80-col capture, same object.
            self.assertIs(b._preview_snapshot, snap)
            self.assertEqual(snap.width, 80)
        finally:
            b.stop_workers()

    def test_held_view_frozen_at_snapshot_scroll(self):
        text = '\n'.join(f'l{i}' for i in range(200))
        b = _make_browser([('a', text), ('b', None)])
        try:
            _move_cursor(b, 0)
            b._preview_scroll = 50
            _render_full(b)
            self.assertEqual(b._preview_snapshot.scroll, 50)
            self.assertEqual(_preview_rows(b)[0], 'l50')

            # The cursor-move scroll reset and scroll keys during the
            # hold do not move the held view.
            _move_cursor(b, 1)
            self.assertEqual(b._preview_scroll, 0)
            _render_full(b)
            self.assertEqual(_preview_rows(b)[0], 'l50')
            b._preview_scroll = 120
            _render_full(b)
            self.assertEqual(_preview_rows(b)[0], 'l50')

            # Delivery: the real content paints with the live scroll
            # (clamped back into its own range).
            b._deliver_preview('b', 'bravo')
            _render_full(b)
            self.assertEqual(_preview_rows(b)[0], 'bravo')
            self.assertEqual(b._preview_scroll, 0)
        finally:
            b.stop_workers()

    def test_held_scroll_clamps_to_pane(self):
        # A taller pane shrinks max_scroll below the snapshot's offset;
        # the held view clamps so the tail stays at the bottom.
        text = '\n'.join(f'l{i}' for i in range(200))
        b = _make_browser([('a', text), ('b', None)])
        try:
            _move_cursor(b, 0)
            b._preview_scroll = 10 ** 6
            _render_full(b)
            captured = b._preview_snapshot.scroll
            self.assertGreater(captured, 0)

            _move_cursor(b, 1)
            self._set_term_size((80, 60))
            b._invalidate_all_preview_renders()
            _render_full(b)
            rows = _preview_rows(b)
            self.assertEqual(rows[-1], 'l199')
            self.assertEqual(rows[0], f'l{200 - len(rows)}')
        finally:
            b.stop_workers()


# --- Meta rows ---------------------------------------------------------------


class TestMetaRows(_StaleHoldBase):
    """Meta rows are not placeholder-pending: ``_update_preview_for_cursor``
    never requests a delivery for them, so a hold would never end."""

    def test_meta_row_with_delivered_preview_paints_it(self):
        b = _make_browser([
            ('a', 'alpha-content'),
            ('m', 'meta-body', {'meta': True}),
        ])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            _move_cursor(b, 1)
            _render_full(b)
            self.assertIn('meta-body', _pane_text(b))
            self.assertNotIn('alpha-content', _pane_text(b))
        finally:
            b.stop_workers()

    def test_meta_row_without_preview_blanks_not_holds(self):
        b = _make_browser([
            ('a', 'alpha-content'),
            ('m', None, {'meta': True}),
        ])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))
            _move_cursor(b, 1)
            _render_full(b)
            self.assertEqual(_pane_text(b).strip(), '')
            # The snapshot stays around for a later pending row.
            self.assertIsNotNone(b._preview_snapshot)
        finally:
            b.stop_workers()


# --- Blank / unaffected paths ------------------------------------------------


class TestBlankAndUnaffectedPaths(_StaleHoldBase):

    def test_empty_visible_list_blanks(self):
        b = _make_browser([('a', 'alpha-content')])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            self.assertIn('alpha-content', _pane_text(b))

            b._state._children[None] = []
            _state.mark_visible_dirty(b._state)
            _move_cursor(b, 0)
            _render_full(b)
            self.assertEqual(_pane_text(b).strip(), '')
            # The snapshot stays around for a later pending row.
            self.assertIsNotNone(b._preview_snapshot)
        finally:
            b.stop_workers()

    def test_help_mode_paints_help_not_snapshot(self):
        b = _make_browser([('a', 'alpha-content'), ('b', None)])
        try:
            _move_cursor(b, 0)
            _render_full(b)
            b._state.cursor = 1  # pending row, but help owns the pane
            b._help_mode = True
            _render_full(b)
            self.assertIn('NAVIGATION', _pane_text(b))
            self.assertNotIn('alpha-content', _pane_text(b))
            # Help paints never touch the snapshot.
            self.assertEqual(b._preview_snapshot.text, 'alpha-content')
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
