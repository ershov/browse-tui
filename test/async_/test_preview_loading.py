"""Loading indicator in the preview label (preview-flicker design §C).

``Browser._preview_loading()`` is the display predicate — True while
the request slot holds the cursored id with no paused stream matching
it — and ``_preview_label`` (050-render) prefixes the pending glyph
(``⧗ Preview``) while it holds. The main loop runs
``_flag_preview_loading_if_changed()`` once per tick to flag
``'preview'`` + ``'info'`` redraws when the value flips.

These tests drive the loop's maintenance steps manually (drain →
``_update_preview_for_cursor`` → memo check — the same order
``Browser.run`` uses) against real workers. Coverage:

  * Glyph during a (gated) fetch; plain once the delivery lands.
  * Glyph through the debounce window (slot set before the fetch).
  * Glyph while a streaming generator actively pulls; plain after
    clean exhaustion.
  * Plain while paused at the buffer cap; glyph again after a
    demand-signal resume; plain on re-pause.
  * Plain when the outstanding request is for a non-cursored id.
  * Help mode wins over the loading variant.
  * The memo flip flags exactly {'preview', 'info'}, once per flip.
"""

import threading
import time
import unittest

from test.async_._helpers import Item, make_browser, get_preview_text
from test.unit._loader import load

# ``_preview_label`` is a pure function of browser state (``_help_mode``
# + the ``_preview_loading()`` predicate) — the isolated render load
# needs no terminal wiring for it.
_render = load('_browse_tui_render_lbl', '050-render.py')
_preview_label = _render._preview_label


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


def _tick(b):
    """One main-loop maintenance pass, in ``Browser.run`` order."""
    b.drain_main_queue()
    b._update_preview_for_cursor()
    b._flag_preview_loading_if_changed()


def _drain_until(b, predicate, timeout=2.0, interval=0.005):
    """Drain main queue + poll until predicate True. Returns final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        b.drain_main_queue()
        v = predicate()
        if v:
            return v
        time.sleep(interval)
    b.drain_main_queue()
    return predicate()


class TestLoadingLabelFetch(unittest.TestCase):
    """Non-streaming fetch: glyph while outstanding, plain on delivery."""

    def test_glyph_during_fetch_plain_after_delivery(self):
        gate = threading.Event()

        def get_preview(id_):
            gate.wait(timeout=5)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0)
        try:
            _seed_items(b, ['a'])
            b._state.cursor = 0
            _tick(b)  # cursor lands on 'a' → request slot set
            self.assertTrue(b._preview_loading())
            self.assertEqual(_preview_label(b), '⧗ Preview')

            gate.set()
            b.run_until_idle()
            _tick(b)  # delivery drained the slot
            self.assertFalse(b._preview_loading())
            self.assertEqual(_preview_label(b), 'Preview')
            self.assertEqual(get_preview_text(b, 'a'), 'p-a')
        finally:
            gate.set()
            b.stop_workers()

    def test_glyph_through_debounce_window(self):
        b = make_browser(get_preview=lambda id_: f'p-{id_}',
                         preview_debounce=0.25)
        try:
            _seed_items(b, ['a'])
            b._state.cursor = 0
            _tick(b)
            # The fetch hasn't even started — the slot alone holds the
            # indicator ON through the settle wait.
            self.assertEqual(_preview_label(b), '⧗ Preview')
            b.run_until_idle()
            _tick(b)
            self.assertEqual(_preview_label(b), 'Preview')
        finally:
            b.stop_workers()

    def test_plain_when_request_is_for_non_cursored_id(self):
        gate = threading.Event()

        def get_preview(id_):
            if id_ == 'b':
                gate.wait(timeout=5)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0)
        try:
            _seed_items(b, ['a', 'b'])
            b._state.cursor = 0
            _tick(b)
            b.run_until_idle()  # 'a' delivered; slot empty
            # A background re-fetch for a non-cursored id (the
            # ``_kick_after_invalidate`` shape) must not light the
            # cursored item's label.
            b.request_preview('b')
            _tick(b)
            self.assertFalse(b._preview_loading())
            self.assertEqual(_preview_label(b), 'Preview')
        finally:
            gate.set()
            b.stop_workers()

    def test_help_mode_wins_over_loading(self):
        gate = threading.Event()

        def get_preview(id_):
            gate.wait(timeout=5)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0)
        try:
            _seed_items(b, ['a'])
            b._state.cursor = 0
            _tick(b)
            self.assertTrue(b._preview_loading())
            b._help_mode = True
            self.assertEqual(_preview_label(b), 'Help')
        finally:
            gate.set()
            b.stop_workers()


class TestLoadingLabelStreaming(unittest.TestCase):
    """Streaming: ON while pulling, OFF at exhaustion / while paused."""

    def test_glyph_while_pulling_plain_after_exhaustion(self):
        gate = threading.Event()

        def get_preview(_id):
            yield 'first '
            gate.wait(timeout=5)
            yield 'second'

        b = make_browser(get_preview=get_preview, preview_debounce=0)
        try:
            _seed_items(b, ['a'])
            b._state.cursor = 0
            _tick(b)
            # First chunk landed, generator blocked mid-pull: the slot
            # stays set until exhaustion, so the stream counts as
            # loading.
            self.assertTrue(_drain_until(
                b, lambda: get_preview_text(b, 'a') == 'first '))
            _tick(b)
            self.assertIsNone(b._preview_paused)
            self.assertEqual(_preview_label(b), '⧗ Preview')

            gate.set()
            self.assertTrue(_drain_until(b, lambda: b._preview_req is None),
                            'stream did not exhaust')
            _tick(b)
            self.assertEqual(_preview_label(b), 'Preview')
            self.assertEqual(get_preview_text(b, 'a'), 'first second')
        finally:
            gate.set()
            b.stop_workers()

    def test_plain_while_paused_glyph_after_resume_plain_on_repause(self):
        resume_gate = threading.Event()

        def get_preview(_id):
            # Two chunks reach the first cap window (2×50 ≥ 100);
            # resumed pulls block on the gate so the actively-pulling
            # window is observable, then run to the next cap.
            yield 'x' * 50
            yield 'x' * 50
            while True:
                resume_gate.wait(timeout=5)
                yield 'x' * 50

        b = make_browser(
            get_preview=get_preview,
            preview_debounce=0,
            preview_buffer_cap_chars=100,
            preview_buffer_cap_lines=1_000_000,
        )
        try:
            _seed_items(b, ['a'])
            b._state.cursor = 0
            _tick(b)
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not pause at the cap',
            )
            _tick(b)
            # Paused at the cap: slot still holds the id, but the
            # worker has voluntarily stopped — no glyph.
            self.assertEqual(b._preview_req, 'a')
            self.assertFalse(b._preview_loading())
            self.assertEqual(_preview_label(b), 'Preview')

            # Demand-signal resume: paused state clears, the worker
            # blocks inside ``next(gen)`` — actively pulling again.
            b.signal_preview_demand('a')
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is None),
                'worker did not resume on demand signal',
            )
            _tick(b)
            self.assertEqual(_preview_label(b), '⧗ Preview')

            # Let chunks flow: one more cap window (+100 chars), then
            # the worker re-pauses and the glyph drops again.
            resume_gate.set()
            self.assertTrue(
                _drain_until(b, lambda: b._preview_paused is not None),
                'worker did not re-pause at the next cap',
            )
            _tick(b)
            self.assertEqual(_preview_label(b), 'Preview')
        finally:
            resume_gate.set()
            b.stop_workers()


class TestLoadingMemoFlags(unittest.TestCase):
    """The memo flip flags {'preview', 'info'} exactly once per flip."""

    def test_flip_flags_both_panes_and_repeat_check_is_quiet(self):
        gate = threading.Event()

        def get_preview(id_):
            gate.wait(timeout=5)
            return f'p-{id_}'

        b = make_browser(get_preview=get_preview, preview_debounce=0)
        try:
            _seed_items(b, ['a'])
            b._state.cursor = 0
            b._update_preview_for_cursor()  # slot set → predicate ON

            b._needs_redraw.clear()
            b._flag_preview_loading_if_changed()
            self.assertEqual(b._needs_redraw, {'preview', 'info'})

            # No flip → no flags.
            b._needs_redraw.clear()
            b._flag_preview_loading_if_changed()
            self.assertEqual(b._needs_redraw, set())

            gate.set()
            b.run_until_idle()
            b.drain_main_queue()
            b._needs_redraw.clear()
            b._flag_preview_loading_if_changed()  # delivery → OFF flip
            self.assertEqual(b._needs_redraw, {'preview', 'info'})
        finally:
            gate.set()
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
