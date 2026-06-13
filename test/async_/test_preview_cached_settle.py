"""Cached previews settle before swap (preview-flicker design §A/§B, #954).

Every cursor move routes through the preview worker — cached or not.
For an id whose ``Item.preview`` is already cached, the worker waits
out the debounce and then posts a ``_settle_cached_preview`` nudge
instead of re-running ``get_preview`` (the #442 cached-revisit
promise). The nudge drains the request slot, sets the per-visit
delivery bit, and flags the preview redraw — the single swap signal
the renderer's hold rule waits for. Render-level hold/swap pins live
in test/unit/test_preview_stale_hold.py; these tests drive the real
worker against an explicit ``preview_debounce``.

Coverage (see docs/superpowers/specs/2026-06-12-preview-flicker-design.md):

  * A move onto a cached row keeps the visit undelivered through the
    settle, then the nudge ends it — with zero ``get_preview`` calls
    and the cache untouched.
  * A burst over cached rows nudges only the row the cursor settles
    on; nothing is refetched.
  * ``run_until_idle`` treats the settling slot as busy and returns
    once the nudge drains it.
  * The ``⧗ Preview`` label shows during a cached settle (the slot
    holds the cursored id) and clears after the nudge.
"""

import time
import unittest

from test.async_._helpers import Item, make_browser
from test.unit._loader import load

# ``_preview_label`` is a pure function of browser state — the isolated
# render load needs no terminal wiring for it (as in
# test_preview_loading.py).
_render = load('_browse_tui_render_cs', '050-render.py')
_preview_label = _render._preview_label


DEBOUNCE = 0.15


def _seed_cached_items(b, ids):
    """Register ``ids`` under root, every preview pre-cached."""
    children = []
    for id_ in ids:
        item = Item(id=id_)
        item.preview = f'cached-{id_}'
        b._state._items_by_id[id_] = item
        children.append(item)
    b._state._children[None] = children
    b._state._visible_dirty = True
    return {id_: b._state._items_by_id[id_] for id_ in ids}


def _tick(b):
    """One main-loop maintenance pass, in ``Browser.run`` order."""
    b.drain_main_queue()
    b._update_preview_for_cursor()
    b._flag_preview_loading_if_changed()


class TestCachedSettleNoRefetch(unittest.TestCase):
    """Cache hits resolve as a settle nudge — never a get_preview re-run."""

    def test_cached_move_settles_without_refetch(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'fetched-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=DEBOUNCE)
        try:
            items = _seed_cached_items(b, ['A', 'B'])
            b._state.cursor = 0
            b._update_preview_for_cursor()
            # Settling: the slot holds the cursored id and the visit is
            # undelivered — exactly the state the renderer holds over.
            self.assertEqual(b._preview_req, 'A')
            self.assertFalse(b._preview_visit_delivered)

            b._needs_redraw.clear()
            b.run_until_idle()
            # The nudge — not a delivery — ended the visit: no fetch,
            # cache untouched, slot drained, swap repaint flagged.
            self.assertEqual(calls, [],
                             'cache hit must not re-run get_preview')
            self.assertTrue(b._preview_visit_delivered)
            self.assertIsNone(b._preview_req)
            self.assertEqual(items['A'].preview, 'cached-A')
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()

    def test_burst_over_cached_rows_nudges_only_settled_row(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'fetched-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=DEBOUNCE)
        try:
            ids = [f'I{n}' for n in range(10)]
            items = _seed_cached_items(b, ids)
            nudges = []

            def _counting_nudge(id_, _orig=b._settle_cached_preview):
                nudges.append(id_)
                _orig(id_)

            b._settle_cached_preview = _counting_nudge
            # Held-key shape: the cursor runs over every (visited) row
            # well inside one debounce window.
            for i in range(10):
                b._state.cursor = i
                b._update_preview_for_cursor()
            b.run_until_idle()
            self.assertEqual(nudges, ['I9'],
                             f'only the settled row may nudge; {nudges!r}')
            self.assertEqual(calls, [])
            self.assertTrue(b._preview_visit_delivered)
            for id_ in ids:
                self.assertEqual(items[id_].preview, f'cached-{id_}')
        finally:
            b.stop_workers()


class TestCachedSettleIdleAndLabel(unittest.TestCase):
    """run_until_idle interaction + the §C glyph on cached settles."""

    def test_run_until_idle_returns_after_nudge_drains_slot(self):
        b = make_browser(get_preview=lambda id_: f'fetched-{id_}',
                         preview_debounce=DEBOUNCE)
        try:
            _seed_cached_items(b, ['A'])
            b._state.cursor = 0
            b._update_preview_for_cursor()
            t0 = time.monotonic()
            # The slot stays set through the settle, so the worker
            # counts as busy; the nudge drains it and idle is reached
            # — no TimeoutError, no hang.
            b.run_until_idle()
            self.assertGreaterEqual(time.monotonic() - t0, 0.14)
            self.assertIsNone(b._preview_req)
        finally:
            b.stop_workers()

    def test_glyph_during_cached_settle_clears_after(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'fetched-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0.25)
        try:
            _seed_cached_items(b, ['A', 'B'])
            b._state.cursor = 0
            _tick(b)
            # Cached moves blink the indicator too: the slot holds the
            # cursored id for the length of the settle.
            self.assertTrue(b._preview_loading())
            self.assertEqual(_preview_label(b), '⧗ Preview')

            b.run_until_idle()
            _tick(b)
            self.assertFalse(b._preview_loading())
            self.assertEqual(_preview_label(b), 'Preview')
            self.assertEqual(calls, [])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
