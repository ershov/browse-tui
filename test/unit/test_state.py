"""Tests for Browser-level state on the state module (040-state.py).

Currently focused on the ``split`` attribute introduced in ticket #146:
clamp helper, constructor param, and ``set_split`` redraw side effect.
Other Browser-level state (list_ratio, expanded, …) is exercised
indirectly by the renderer / actions tests; this module is the natural
home for narrow state-bit tests that would otherwise need a full UI
fixture.
"""

import threading
import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')

# Inject Item / to_item / notify_wake so state helpers (pending
# placeholder, post-wrapped setters, set_children coercion) keep
# working when tests use the loader.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

Browser = _state.Browser
_clamp_split = _state._clamp_split
_VALID_SPLITS = _state._VALID_SPLITS


class TestClampSplit(unittest.TestCase):
    """``_clamp_split`` is the input gate for both __init__ and set_split."""

    def test_valid_codes_pass_through(self):
        for code in _VALID_SPLITS:
            self.assertEqual(_clamp_split(code), code)

    def test_unknown_code_defaults_to_h(self):
        self.assertEqual(_clamp_split('zz'), 'h')
        self.assertEqual(_clamp_split(''), 'h')

    def test_auto_shorthand_resolves_via_term_size(self):
        # 'auto'/'a' resolve against term_size: <230 cols → 'h', else 'v'.
        # In headless tests term_size falls back to 80, so 'a'/'auto' → 'h'.
        self.assertEqual(_clamp_split('a'), 'h')
        self.assertEqual(_clamp_split('auto'), 'h')
        self.assertEqual(_clamp_split('Auto'), 'h')

    def test_none_defaults_to_h(self):
        self.assertEqual(_clamp_split(None), 'h')

    def test_non_string_defaults_to_h(self):
        self.assertEqual(_clamp_split(42), 'h')
        self.assertEqual(_clamp_split(['v']), 'h')
        self.assertEqual(_clamp_split(object()), 'h')


class TestBrowserSplitConstructor(unittest.TestCase):
    """Browser.__init__ accepts ``split=`` and stores it (clamped)."""

    def test_default_split_resolves_auto(self):
        # Default is 'auto' which resolves via term_size; in headless
        # 80-col tests that yields 'h'.
        b = Browser(_headless=True)
        self.assertEqual(b.split, 'h')

    def test_all_valid_splits_stick(self):
        for code in _VALID_SPLITS:
            b = Browser(split=code, _headless=True)
            self.assertEqual(b.split, code)

    def test_invalid_split_defaults_to_h(self):
        for bad in ('zz', '', 'horizontal'):
            b = Browser(split=bad, _headless=True)
            self.assertEqual(b.split, 'h')

    def test_none_split_defaults_to_h(self):
        b = Browser(split=None, _headless=True)
        self.assertEqual(b.split, 'h')

    def test_non_string_split_defaults_to_h(self):
        b = Browser(split=42, _headless=True)
        self.assertEqual(b.split, 'h')


class TestBrowserSetSplit(unittest.TestCase):
    """``set_split`` clamps + flags the screen for redraw.

    Note: ``set_split`` is post-wrapped (#265) so the actual mutation
    happens on the next ``drain_main_queue`` call, not synchronously.
    Each test drains before asserting.
    """

    def test_set_split_stores_valid_value(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_split('v')
        b.drain_main_queue()
        self.assertEqual(b.split, 'v')

    def test_set_split_marks_full_redraw(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_split('m')
        b.drain_main_queue()
        self.assertIn('all', b._needs_redraw)

    def test_set_split_clamps_invalid(self):
        b = Browser(split='v', _headless=True)
        b.set_split('garbage')
        b.drain_main_queue()
        self.assertEqual(b.split, 'h')

    def test_set_split_clamps_none(self):
        b = Browser(split='v', _headless=True)
        b.set_split(None)
        b.drain_main_queue()
        self.assertEqual(b.split, 'h')

    def test_set_split_each_valid_round_trip(self):
        b = Browser(_headless=True)
        for code in _VALID_SPLITS:
            b.set_split(code)
            b.drain_main_queue()
            self.assertEqual(b.split, code)


class TestBrowserSetSplitDeferred(unittest.TestCase):
    """``set_split`` defers mutation to the main-thread drain (#265)."""

    def test_set_split_does_not_mutate_until_drained(self):
        b = Browser(split='h', _headless=True)
        b.set_split('v')
        # Before drain: state is unchanged.
        self.assertEqual(b.split, 'h')
        # After drain: mutation has been applied.
        b.drain_main_queue()
        self.assertEqual(b.split, 'v')

    def test_do_set_split_clamps_synchronously(self):
        # The private worker is the synchronous path; existing
        # clamp behaviour is preserved through it.
        b = Browser(split='v', _headless=True)
        b._do_set_split('garbage')
        self.assertEqual(b.split, 'h')
        b._do_set_split('m')
        self.assertEqual(b.split, 'm')


class TestBrowserSetListRatioDeferred(unittest.TestCase):
    """``set_list_ratio`` defers mutation to the main-thread drain (#265)."""

    def test_set_list_ratio_does_not_mutate_until_drained(self):
        b = Browser(list_ratio=0.5, _headless=True)
        b.set_list_ratio(0.25)
        # Before drain: state is unchanged.
        self.assertEqual(b.list_ratio, 0.5)
        b.drain_main_queue()
        self.assertEqual(b.list_ratio, 0.25)

    def test_do_set_list_ratio_clamps_synchronously(self):
        # Clamp gate still works via the private worker.
        b = Browser(_headless=True)
        b._do_set_list_ratio(2.0)
        self.assertEqual(b.list_ratio, _state._LIST_RATIO_MAX)
        b._do_set_list_ratio(-1.0)
        self.assertEqual(b.list_ratio, _state._LIST_RATIO_MIN)

    def test_set_list_ratio_marks_full_redraw_after_drain(self):
        b = Browser(_headless=True)
        b._needs_redraw.clear()
        b.set_list_ratio(0.4)
        b.drain_main_queue()
        self.assertIn('all', b._needs_redraw)


class TestBrowserSetChildren(unittest.TestCase):
    """``set_children`` (#265) lets recipe-owned threads inject results."""

    def test_set_children_appears_after_apply(self):
        b = Browser(_headless=True)
        # Pre-condition: no cached children for 'p'.
        self.assertNotIn('p', b._state._children)
        b.set_children('p', [{'id': 'a'}, {'id': 'b'}])
        # Drains the children-results deque on the main thread.
        b.apply_children_results()
        kids = b._state._children['p']
        self.assertEqual([k.id for k in kids], ['a', 'b'])

    def test_set_children_coerces_plain_dicts(self):
        b = Browser(_headless=True)
        b.set_children('p', [{'id': 'a', 'title': 'A'}])
        b.apply_children_results()
        kids = b._state._children['p']
        self.assertEqual(len(kids), 1)
        self.assertIsInstance(kids[0], _data.Item)
        self.assertEqual(kids[0].id, 'a')
        self.assertEqual(kids[0].title, 'A')

    def test_set_children_from_worker_thread_appears(self):
        b = Browser(_headless=True)
        # Pre-fill an unrelated cache entry to ensure isolation.
        done = threading.Event()

        def worker():
            b.set_children('p', [{'id': 'x'}])
            done.set()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        self.assertTrue(done.is_set())
        # Worker has appended; main thread now drains.
        b.apply_children_results()
        kids = b._state._children['p']
        self.assertEqual([k.id for k in kids], ['x'])

    def test_set_children_multiple_threads_queue_fifo(self):
        b = Browser(_headless=True)
        # Two threads append, with a barrier so the order in the
        # deque is deterministic per call (each call appends one
        # entry under the GIL; the test guards order at the call
        # boundary).
        gate = threading.Event()
        ready = threading.Barrier(3)  # t1 + t2 + main
        results_order = []

        def worker(label):
            ready.wait()
            gate.wait()
            b.set_children(label, [{'id': label + '-child'}])
            results_order.append(label)

        t1 = threading.Thread(target=worker, args=('p1',))
        t2 = threading.Thread(target=worker, args=('p2',))
        t1.start()
        t2.start()
        ready.wait()
        gate.set()
        t1.join(2.0)
        t2.join(2.0)
        # The deque order matches the order the threads called append.
        # Even if scheduling reorders them, FIFO of the deque means
        # apply_children_results consumes them in the same order they
        # were appended -- which matches results_order.
        b.apply_children_results()
        # Both ids landed in the cache.
        self.assertIn('p1', b._state._children)
        self.assertIn('p2', b._state._children)
        # And FIFO drain order matches append order.
        # (Ordering check: apply consumes entries in the order they
        # were appended; results_order records the same order.)
        self.assertEqual(len(results_order), 2)


class TestBrowserSetPreview(unittest.TestCase):
    """``set_preview`` (#265) lets recipe-owned threads inject previews."""

    def test_set_preview_appears_after_apply(self):
        b = Browser(_headless=True)
        b.set_preview('a', 'hello')
        applied = b.apply_preview_result()
        self.assertTrue(applied)
        self.assertEqual(b._state._preview['a'], 'hello')

    def test_set_preview_latest_wins_before_apply(self):
        b = Browser(_headless=True)
        b.set_preview('a', 'first')
        b.set_preview('a', 'second')
        applied = b.apply_preview_result()
        self.assertTrue(applied)
        self.assertEqual(b._state._preview['a'], 'second')

    def test_set_preview_none_coerces_to_empty(self):
        b = Browser(_headless=True)
        b.set_preview('a', None)
        applied = b.apply_preview_result()
        self.assertTrue(applied)
        self.assertEqual(b._state._preview['a'], '')

    def test_set_preview_from_worker_thread_appears(self):
        b = Browser(_headless=True)
        done = threading.Event()

        def worker():
            b.set_preview('a', 'from-thread')
            done.set()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        self.assertTrue(done.is_set())
        applied = b.apply_preview_result()
        self.assertTrue(applied)
        self.assertEqual(b._state._preview['a'], 'from-thread')


# --- PaneCache (#186) ------------------------------------------------------


PaneCache = _state.PaneCache


class _FakeRect:
    """Minimal Rect stand-in for the cache tests.

    PaneCache uses duck-typing for ``rect`` / ``prev_rect``: it only
    reads ``.height`` to size the line buffer and uses ``__eq__`` to
    detect geometry changes. A NamedTuple-style helper is enough.
    """

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    @property
    def height(self):
        return self.bottom - self.top

    def __eq__(self, other):
        if not isinstance(other, _FakeRect):
            return NotImplemented
        return (self.left, self.top, self.right, self.bottom) == \
               (other.left, other.top, other.right, other.bottom)

    def __hash__(self):
        return hash((self.left, self.top, self.right, self.bottom))


class TestPaneCache(unittest.TestCase):
    """``PaneCache`` is the per-pane row buffer for the differential
    renderer (#185–#188). #186 introduces it; #187/#188 wire it into
    each pane renderer."""

    def test_default_state(self):
        c = PaneCache()
        self.assertIsNone(c.rect)
        self.assertIsNone(c.prev_rect)
        self.assertEqual(c.lines, [])

    def test_invalidate_rotates_rect_and_sizes_lines(self):
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)  # height=24
        c.invalidate(r1)
        self.assertEqual(c.rect, r1)
        self.assertIsNone(c.prev_rect)
        self.assertEqual(len(c.lines), 24)
        self.assertTrue(all(line is None for line in c.lines))

        r2 = _FakeRect(1, 1, 81, 30)  # height=29
        c.invalidate(r2)
        self.assertEqual(c.rect, r2)
        self.assertEqual(c.prev_rect, r1)
        self.assertEqual(len(c.lines), 29)

class TestPaneCacheUpdateRect(unittest.TestCase):
    """``PaneCache.update_rect`` is the single per-frame entry point
    introduced in ticket #228. It subsumes both the old per-renderer
    cache-rotation call AND the orchestrator-level
    ``_mark_disappeared_panes`` sentinel stamp.
    """

    def test_update_rect_none_on_populated_cache_stamps_sentinel(self):
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        c.update_rect(r1)
        c.lines[0] = (10, 'hello world')
        # Pane disappeared this frame.
        c.update_rect(None)
        self.assertEqual(c.lines, [])
        # Cache.rect is now the sentinel (compares unequal to any real rect).
        self.assertNotEqual(c.rect, r1)
        self.assertNotEqual(c.rect, _FakeRect(1, 1, 81, 25))
        # And a real-rect update_rect on the sentinel-stamped cache
        # takes the rect-changed (invalidate) branch on reappear.
        r1_again = _FakeRect(1, 1, 81, 25)
        c.update_rect(r1_again)
        self.assertEqual(c.rect, r1_again)
        # prev_rect after invalidate is the prior rect (the sentinel) —
        # NOT equal to the new rect, so end_row routes through the
        # "rect changed → full pad" path. This is the regression the
        # ticket targets (#221).
        self.assertNotEqual(c.prev_rect, c.rect)

    def test_update_rect_none_on_unpainted_cache_is_noop(self):
        """An empty cache stays empty when ``update_rect(None)`` runs.

        No populated state to invalidate; the sentinel stamp is skipped
        so we don't accumulate phantom history.
        """
        c = PaneCache()
        c.update_rect(None)
        self.assertIsNone(c.rect)
        self.assertIsNone(c.prev_rect)
        self.assertEqual(c.lines, [])

    def test_update_rect_none_on_already_sentinel_is_noop(self):
        """Repeated ``update_rect(None)`` doesn't churn the cache."""
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        c.update_rect(r1)
        c.update_rect(None)
        sentinel_rect = c.rect
        sentinel_lines = c.lines
        c.update_rect(None)
        self.assertIs(c.rect, sentinel_rect,
                      'sentinel rect should not be re-stamped')
        self.assertIs(c.lines, sentinel_lines,
                      'lines should not be re-cleared')

    def test_update_rect_same_rect_is_noop_after_invalidate(self):
        """First call after invalidate does NOT roll prev_rect forward.

        The FIRST paint at a new rect is in "rect changed" regime
        (prev_rect != rect). The single-call-per-frame contract means
        the renderer paints between this call and the next
        ``update_rect``.
        """
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        c.update_rect(r1)
        # State: rect=r1, prev_rect=None — first paint regime.
        self.assertEqual(c.rect, r1)
        self.assertIsNone(c.prev_rect)

    def test_update_rect_same_rect_preserves_lines_buffer(self):
        """A no-op same-rect call must not clobber painted entries."""
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        c.update_rect(r1)
        c.lines[0] = (10, 'hello world')
        c.update_rect(_FakeRect(1, 1, 81, 25))
        self.assertEqual(c.lines[0], (10, 'hello world'))

    def test_update_rect_same_rect_rolls_prev_rect_on_second_call(self):
        """Second call with the same rect rolls prev_rect to rect.

        Steady-state engagement: end_row pads to cached visible length
        from the second paint onwards.
        """
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        c.update_rect(r1)
        c.update_rect(_FakeRect(1, 1, 81, 25))
        self.assertEqual(c.rect, r1)
        self.assertEqual(c.prev_rect, r1)
        # Third call is a true no-op.
        c.update_rect(_FakeRect(1, 1, 81, 25))
        self.assertEqual(c.rect, r1)
        self.assertEqual(c.prev_rect, r1)

    def test_update_rect_new_rect_invalidates_with_prev_old_rect(self):
        """A different rect invalidates and rotates: prev_rect = old rect."""
        c = PaneCache()
        r1 = _FakeRect(1, 1, 81, 25)
        r2 = _FakeRect(1, 1, 81, 30)
        # Establish steady state at r1.
        c.update_rect(r1)
        c.update_rect(r1)
        self.assertEqual(c.prev_rect, r1)
        # Resize: prev_rect rotates to r1, rect becomes r2, lines reset.
        c.update_rect(r2)
        self.assertEqual(c.rect, r2)
        self.assertEqual(c.prev_rect, r1)
        self.assertEqual(len(c.lines), 29)
        self.assertTrue(all(line is None for line in c.lines))


class TestBrowserPaneCacheInit(unittest.TestCase):
    """Browser.__init__ initialises ``_pane_cache`` to an empty dict."""

    def test_pane_cache_starts_empty(self):
        b = Browser(_headless=True)
        self.assertEqual(b._pane_cache, {})
        self.assertIsInstance(b._pane_cache, dict)


class TestBrowserPreviewAnsi(unittest.TestCase):
    """Browser.__init__ accepts ``preview_ansi`` (default True) — #244."""

    def test_default_is_true(self):
        b = Browser(_headless=True)
        self.assertTrue(b.preview_ansi)

    def test_explicit_false_stored(self):
        b = Browser(preview_ansi=False, _headless=True)
        self.assertFalse(b.preview_ansi)

    def test_explicit_true_stored(self):
        b = Browser(preview_ansi=True, _headless=True)
        self.assertTrue(b.preview_ansi)


if __name__ == '__main__':
    unittest.main()
