"""Preview-fetch debounce in ``_preview_worker`` (preview-flicker design §A).

With ``BrowserConfig.preview_debounce`` > 0 the worker sleeps after
adopting a request and re-reads the latest-wins slot before fetching: a
newer id restarts the wait, so rapid cursor movement coalesces into a
single ``get_preview`` call for the row the cursor settles on. ``0``
disables (immediate fetch — the pre-debounce behavior).

Coverage (see docs/superpowers/specs/2026-06-12-preview-flicker-design.md):

  * A tight burst of ``request_preview`` calls / cursor moves within
    one window yields exactly one fetch, for the final id.
  * ``preview_debounce=0`` fetches immediately (no sleep).
  * Move-away-and-back within one sleep window still fetches that id
    (the slot equals the adopted id again — no per-request stamp).
  * ``run_until_idle`` treats a debouncing worker as busy and returns
    once the debounced fetch delivers.
  * ``stop_workers`` during the sleep joins promptly without fetching.
"""

import time
import unittest

from test.async_._helpers import Item, make_browser, get_preview_text


DEBOUNCE = 0.15


def _seed_items(b, ids):
    """Register ``ids`` under root so they are visible (cursor-movable)."""
    children = []
    for id_ in ids:
        item = Item(id=id_)
        b._state._items_by_id[id_] = item
        children.append(item)
    b._state._children[None] = children
    b._state._visible_dirty = True
    return {id_: b._state._items_by_id[id_] for id_ in ids}


class TestPreviewDebounceCoalescing(unittest.TestCase):
    """A burst within the window collapses to one fetch for the final id."""

    def test_request_burst_yields_one_fetch_for_final_id(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=DEBOUNCE)
        try:
            ids = [f'id-{n}' for n in range(10)]
            for id_ in ids:
                b._state._items_by_id[id_] = Item(id=id_)
            # Tight burst — all requests land well inside one sleep
            # window, so every id but the last is superseded before
            # the worker's post-sleep re-read.
            for id_ in ids:
                b.request_preview(id_)
            b.run_until_idle()
            self.assertEqual(
                calls, [ids[-1]],
                f'burst must coalesce to one fetch; got {calls!r}',
            )
            self.assertEqual(get_preview_text(b, ids[-1]), f'p-{ids[-1]}')
        finally:
            b.stop_workers()

    def test_cursor_move_burst_yields_one_fetch_for_settled_row(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=DEBOUNCE)
        try:
            ids = [f'I{n}' for n in range(10)]
            items = _seed_items(b, ids)
            # Held-key shape: cursor runs over every row, each move
            # firing request_preview via the cursor helper.
            for i in range(10):
                b._state.cursor = i
                b._update_preview_for_cursor()
            b.run_until_idle()
            self.assertEqual(
                calls, ['I9'],
                f'cursor burst must fetch only the settled row; got {calls!r}',
            )
            self.assertEqual(items['I9'].preview, 'p-I9')
        finally:
            b.stop_workers()

    def test_move_away_and_back_within_window_still_fetches(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        # Generous window so the away/back pair lands inside one sleep.
        b = make_browser(get_preview=get_preview, preview_debounce=0.2)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            b._state._items_by_id['B'] = Item(id='B')
            b.request_preview('A')
            time.sleep(0.05)  # worker adopts A and enters the sleep
            b.request_preview('B')
            b.request_preview('A')
            b.run_until_idle()
            # The slot ended back on the adopted id — the worker must
            # proceed with that fetch, and B is never fetched.
            self.assertEqual(calls, ['A'])
            self.assertEqual(get_preview_text(b, 'A'), 'p-A')
            self.assertIsNone(get_preview_text(b, 'B'))
        finally:
            b.stop_workers()


class TestPreviewImmediateBypass(unittest.TestCase):
    """``immediate=True`` requests skip the debounce sleep.

    The debounce coalesces cursor movement; an invalidation-driven
    refetch (same view, changed content/geometry — e.g. a preview-width
    resize) has no cursor settling to wait for, so it must fetch at once
    instead of lingering on the stale-width preview for a window.
    """

    def test_immediate_request_skips_debounce(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        # Generous window — a debounced fetch would take >= 0.3s.
        b = make_browser(get_preview=get_preview, preview_debounce=0.3)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            t0 = time.monotonic()
            b.request_preview('A', immediate=True)
            b.run_until_idle()
            self.assertLess(time.monotonic() - t0, 0.1)
            self.assertEqual(calls, ['A'])
            self.assertEqual(get_preview_text(b, 'A'), 'p-A')
        finally:
            b.stop_workers()

    def test_kick_after_invalidate_is_immediate(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        # The invalidate/drop_preview_cache path resolves through
        # ``_kick_after_invalidate`` — it must not pay the debounce.
        b = make_browser(get_preview=get_preview, preview_debounce=0.3)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            t0 = time.monotonic()
            b._kick_after_invalidate('A')
            b.run_until_idle()
            self.assertLess(time.monotonic() - t0, 0.1)
            self.assertEqual(calls, ['A'])
            self.assertEqual(get_preview_text(b, 'A'), 'p-A')
        finally:
            b.stop_workers()

    def test_same_cursor_recovery_after_delivery_is_immediate(self):
        """Recovery of an already-shown preview re-fires ``immediate``.

        The run loop re-derives the cursor preview every iteration via
        ``_update_preview_for_cursor``. When an invalidation dropped the
        current row's cache (``item.preview is None``) and the cursor
        has not moved AFTER its preview was already delivered
        (``_preview_visit_delivered``), that re-fire is an invalidation
        recovery — it must be immediate so the resize refetch doesn't
        debounce (flicker). Mirrors the run-loop order: drain (kick)
        then _update_preview_for_cursor.
        """
        b = make_browser(get_preview=lambda id_: f'p-{id_}',
                         preview_debounce=0.3)
        try:
            items = _seed_items(b, ['A'])
            b._state.cursor = 0
            b._preview_cursor_id = 'A'
            b._preview_visit_delivered = True   # A's preview was shown
            items['A'].preview = None           # cache just dropped (resize)
            b.request_preview('A', immediate=True)   # _kick_after_invalidate
            b._update_preview_for_cursor()           # same-tick re-derive
            self.assertTrue(
                b._preview_immediate,
                'recovery of a shown preview must keep the immediate flag',
            )
        finally:
            b.stop_workers()

    def test_recovery_during_inflight_first_fetch_stays_debounced(self):
        """In-flight first-fetch re-derive must NOT flip to immediate.

        Cursor just moved to an uncached row: the bottom path requested
        a debounced fetch and nothing has been delivered yet
        (``_preview_visit_delivered`` False). An async wake (common in
        browse-git: git subprocess results post + wake) re-enters the
        loop and runs ``_update_preview_for_cursor`` while the cache is
        still None. That re-derive must stay debounced — otherwise every
        async wake during navigation defeats cursor-move coalescing.
        """
        b = make_browser(get_preview=lambda id_: f'p-{id_}',
                         preview_debounce=0.3)
        try:
            items = _seed_items(b, ['A'])
            b._state.cursor = 0
            b._preview_cursor_id = 'A'
            b._preview_visit_delivered = False  # first fetch still in flight
            items['A'].preview = None
            b.request_preview('A')              # cursor-move (debounced)
            b._update_preview_for_cursor()      # async-wake re-derive
            self.assertFalse(
                b._preview_immediate,
                'in-flight recovery must stay debounced',
            )
        finally:
            b.stop_workers()

    def test_default_request_still_debounces(self):
        """A plain (cursor-move) request keeps the debounce wait."""
        b = make_browser(get_preview=lambda id_: f'p-{id_}',
                         preview_debounce=DEBOUNCE)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            t0 = time.monotonic()
            b.request_preview('A')
            b.run_until_idle()
            self.assertGreaterEqual(time.monotonic() - t0, 0.14)
            self.assertEqual(get_preview_text(b, 'A'), 'p-A')
        finally:
            b.stop_workers()


class TestPreviewDebounceZeroAndIdle(unittest.TestCase):
    """Disable knob, run_until_idle interaction, shutdown mid-sleep."""

    def test_zero_debounce_fetches_immediately(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            t0 = time.monotonic()
            b.request_preview('A')
            b.run_until_idle()
            # No sleep on the fetch path — delivery lands well under
            # one default debounce window.
            self.assertLess(time.monotonic() - t0, 0.1)
            self.assertEqual(calls, ['A'])
            self.assertEqual(get_preview_text(b, 'A'), 'p-A')
        finally:
            b.stop_workers()

    def test_run_until_idle_waits_out_debounced_delivery(self):
        b = make_browser(get_preview=lambda id_: f'p-{id_}',
                         preview_debounce=DEBOUNCE)
        try:
            b._state._items_by_id['A'] = Item(id='A')
            t0 = time.monotonic()
            b.request_preview('A')
            # The slot stays set through the sleep, so the worker
            # counts as busy — run_until_idle must not return early.
            b.run_until_idle()
            self.assertGreaterEqual(time.monotonic() - t0, 0.14)
            self.assertEqual(get_preview_text(b, 'A'), 'p-A')
        finally:
            b.stop_workers()

    def test_stop_during_debounce_joins_promptly_without_fetch(self):
        calls = []

        def get_preview(id_):
            calls.append(id_)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0.3)
        b._state._items_by_id['A'] = Item(id='A')
        b.request_preview('A')
        time.sleep(0.05)  # worker adopts A and enters the sleep
        b.stop_workers(timeout=1.0)
        # The post-sleep _stop check bails before get_preview; the
        # 0.3s sleep is well inside stop_workers' join timeout.
        self.assertFalse(b._preview_thread.is_alive())
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
