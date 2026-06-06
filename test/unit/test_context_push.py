"""Tests for the streaming-push surface on :class:`Context` (ticket #270).

Covers the new pass-throughs and convenience wrappers wired in
060-context.py for ticket #270:

  * ``ctx.update_data(ops)``     — direct pass-through to Browser.
  * ``ctx.upsert/set_item/remove`` — single-op convenience wrappers.
  * ``ctx.set_preview/append_preview/clear_preview`` — preview pass-throughs.
  * ``ctx.run_in_worker(fn)``    — one-shot daemon-thread runner with
                                   exception surfacing via ``browser.error``.

The Browser-side ``set_preview`` already existed (#265) so its happy
path is exercised in ``test_state.py``; here we just verify the
Context wrapper forwards unchanged. ``append_preview`` / ``clear_preview``
are new on Browser too — those Browser-direct cases live alongside.
"""

import threading
import time
import unittest

from test.unit._loader import load


_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_context = load('_browse_tui_context', '060-context.py')

# Production builds resolve these via concatenation; under tests we
# wire them in by hand.
_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake

_context.visible_items = _state.visible_items
# The new convenience wrappers reach for the module-level op helpers
# from 040-state.py. In the concatenated build these are in the same
# namespace; under tests we inject them onto the context module.
_context.upsert = _state.upsert
_context.set_item = _state.set_item
_context.remove = _state.remove
# Terminal helpers — same shape as test_context.py.
_context.term_suspend = _term.term_suspend
_context.term_resume = _term.term_resume
_context.term_size = _term.term_size
_context.move = _term.move
_context.clear_line = _term.clear_line
_context.clear_columns = _term.clear_columns
_context.set_style = _term.set_style
_context.reset_style = _term.reset_style
_context.write = _term.write
_context.flush = _term.flush
_context.read_key = _term.read_key

_render = load('_browse_tui_render', '050-render.py')
_render.Item = _data.Item
_render.PreviewRender = _data.PreviewRender
_render.Mode = _state.Mode
_context.layout_panes = _render.layout_panes


Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context


def _make_browser(**kw):
    kw.setdefault('_headless', True)
    return Browser(BrowserConfig(**kw))


def _seed_items(b, *ids):
    """Register Items for ``ids`` so preview writes have a target."""
    items = {}
    for id_ in ids:
        item = Item(id=id_)
        b._state._items_by_id[id_] = item
        items[id_] = item
    return items


# --- update_data pass-through --------------------------------------------


class TestUpdateDataPassThrough(unittest.TestCase):
    """``ctx.update_data(ops)`` forwards to ``browser.update_data``."""

    def test_returns_none(self):
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            result = ctx.update_data([_state.upsert('a', '/', title='A')])
            self.assertIsNone(result)
        finally:
            b.stop_workers()

    def test_ops_applied_after_drain(self):
        # End-to-end: build an op list via the helpers, push through ctx,
        # drain, and verify the resulting state.
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            ctx.update_data([
                _state.upsert('A', '/', title='Alpha'),
                _state.upsert('B', '/', title='Beta'),
            ])
            b.drain_main_queue()
            ids = [it.id for it in b._state._children.get('/', [])]
            self.assertEqual(ids, ['A', 'B'])
        finally:
            b.stop_workers()

    def test_forwards_via_browser(self):
        # Confirm we go through Browser.update_data (not directly into
        # apply_ops) — monkey-patch to record the call.
        b = _make_browser()
        try:
            captured = []
            original = b.update_data
            b.update_data = lambda ops: captured.append(list(ops))
            ctx = Context(b)
            ops = [_state.upsert('a', '/', title='A')]
            ctx.update_data(ops)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0], ops)
            b.update_data = original
        finally:
            b.stop_workers()


# --- single-op convenience wrappers --------------------------------------


class TestUpsertConvenience(unittest.TestCase):
    """``ctx.upsert(id, parent, **fields)`` → one upsert op via update_data."""

    def test_returns_none(self):
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.upsert('a', '/', title='A'))
        finally:
            b.stop_workers()

    def test_inserts_new_item(self):
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            ctx.upsert('a', '/', title='A')
            b.drain_main_queue()
            kids = b._state._children.get('/', [])
            self.assertEqual(len(kids), 1)
            self.assertEqual(kids[0].id, 'a')
            self.assertEqual(kids[0].title, 'A')
        finally:
            b.stop_workers()

    def test_forwards_a_single_upsert_op(self):
        # Recipe-level contract: ctx.upsert('a','/',title='A') results in
        # one update_data call carrying a single upsert tuple.
        b = _make_browser()
        try:
            captured = []
            b.update_data = lambda ops: captured.append(list(ops))
            ctx = Context(b)
            ctx.upsert('a', '/', title='A')
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0], [('upsert', 'a', '/', {'title': 'A'})])
        finally:
            b.stop_workers()

    def test_forwards_where_kwarg(self):
        # ``where`` passes through to the helper, producing a 5-tuple.
        b = _make_browser()
        try:
            captured = []
            b.update_data = lambda ops: captured.append(list(ops))
            ctx = Context(b)
            ctx.upsert('a', '/', where=('first', None), title='A')
            self.assertEqual(captured, [[
                ('upsert', 'a', '/', {'title': 'A'}, ('first', None)),
            ]])
        finally:
            b.stop_workers()


class TestSetItemConvenience(unittest.TestCase):
    """``ctx.set_item(id, parent, **fields)`` → one ``set`` op."""

    def test_returns_none(self):
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.set_item('a', '/', title='A'))
        finally:
            b.stop_workers()

    def test_forwards_a_single_set_op(self):
        b = _make_browser()
        try:
            captured = []
            b.update_data = lambda ops: captured.append(list(ops))
            ctx = Context(b)
            ctx.set_item('a', '/', title='A')
            self.assertEqual(captured, [[('set', 'a', '/', {'title': 'A'})]])
        finally:
            b.stop_workers()

    def test_forwards_where_kwarg(self):
        b = _make_browser()
        try:
            captured = []
            b.update_data = lambda ops: captured.append(list(ops))
            ctx = Context(b)
            ctx.set_item('a', '/', where=('last', None), title='A')
            self.assertEqual(captured, [[
                ('set', 'a', '/', {'title': 'A'}, ('last', None)),
            ]])
        finally:
            b.stop_workers()

    def test_replaces_item(self):
        # ``set`` semantics: insert-or-replace; unspecified fields
        # revert to defaults.
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            ctx.upsert('a', '/', title='Original', has_children=True)
            b.drain_main_queue()
            ctx.set_item('a', '/', title='Replaced')
            b.drain_main_queue()
            kids = b._state._children.get('/', [])
            self.assertEqual(len(kids), 1)
            self.assertEqual(kids[0].title, 'Replaced')
            # has_children was not specified in the set, so it reverts
            # to the dataclass default (False).
            self.assertFalse(kids[0].has_children)
        finally:
            b.stop_workers()


class TestRemoveConvenience(unittest.TestCase):
    """``ctx.remove(id)`` → one ``remove`` op."""

    def test_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.remove('a'))
        finally:
            b.stop_workers()

    def test_forwards_a_single_remove_op(self):
        b = _make_browser()
        try:
            captured = []
            b.update_data = lambda ops: captured.append(list(ops))
            ctx = Context(b)
            ctx.remove('a')
            self.assertEqual(captured, [[('remove', 'a')]])
        finally:
            b.stop_workers()

    def test_drops_item(self):
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            ctx.upsert('a', '/', title='A')
            b.drain_main_queue()
            ctx.remove('a')
            b.drain_main_queue()
            self.assertEqual(b._state._children.get('/', []), [])
        finally:
            b.stop_workers()


# --- preview pass-throughs ------------------------------------------------


class TestSetPreviewPassThrough(unittest.TestCase):
    """``ctx.set_preview(id, text)`` forwards to ``browser.set_preview``."""

    def test_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.set_preview('a', 'hello'))
        finally:
            b.stop_workers()

    def test_text_lands_in_cache_after_apply(self):
        # #431: set_preview routes through the FIFO post queue, drained
        # by drain_main_queue. No more single-slot _preview_result write.
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            ctx.set_preview('a', 'hello')
            b.drain_main_queue()
            self.assertEqual(items['a'].preview, 'hello')
        finally:
            b.stop_workers()


class TestAppendPreviewPassThrough(unittest.TestCase):
    """``ctx.append_preview(id, chunk)`` forwards to Browser.

    Browser.append_preview routes via post() so the read-modify-write
    of ``item.preview`` happens on the main thread.
    """

    def test_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.append_preview('a', 'x'))
        finally:
            b.stop_workers()

    def test_appends_to_empty_cache(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            ctx.append_preview('a', 'hello')
            b.drain_main_queue()
            self.assertEqual(items['a'].preview, 'hello')
        finally:
            b.stop_workers()

    def test_appends_to_existing_cache(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            items['a'].preview = 'foo'
            ctx.append_preview('a', 'bar')
            b.drain_main_queue()
            self.assertEqual(items['a'].preview, 'foobar')
        finally:
            b.stop_workers()

    def test_multiple_appends_concatenate(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            ctx.append_preview('a', 'a')
            ctx.append_preview('a', 'b')
            ctx.append_preview('a', 'c')
            b.drain_main_queue()
            self.assertEqual(items['a'].preview, 'abc')
        finally:
            b.stop_workers()

    def test_per_id_isolation(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a', 'b')
            ctx = Context(b)
            ctx.append_preview('a', 'A')
            ctx.append_preview('b', 'B')
            b.drain_main_queue()
            self.assertEqual(items['a'].preview, 'A')
            self.assertEqual(items['b'].preview, 'B')
        finally:
            b.stop_workers()

    def test_none_chunk_coerces_to_empty(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            items['a'].preview = 'foo'
            ctx.append_preview('a', None)
            b.drain_main_queue()
            self.assertEqual(items['a'].preview, 'foo')
        finally:
            b.stop_workers()

    def test_marks_preview_dirty(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            b._needs_redraw.clear()
            ctx.append_preview('a', 'hello')
            b.drain_main_queue()
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()


class TestClearPreviewPassThrough(unittest.TestCase):
    """``ctx.clear_preview(id)`` forwards to Browser.clear_preview."""

    def test_returns_none(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            self.assertIsNone(ctx.clear_preview('a'))
        finally:
            b.stop_workers()

    def test_drops_cached_entry(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            items['a'].preview = 'old'
            ctx.clear_preview('a')
            b.drain_main_queue()
            self.assertIsNone(items['a'].preview)
        finally:
            b.stop_workers()

    def test_unknown_id_silent_noop(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            ctx.clear_preview('nonexistent')
            b.drain_main_queue()  # should not raise
            self.assertNotIn('nonexistent', b._state._items_by_id)
        finally:
            b.stop_workers()

    def test_marks_preview_dirty(self):
        b = _make_browser()
        try:
            items = _seed_items(b, 'a')
            ctx = Context(b)
            items['a'].preview = 'old'
            b._needs_redraw.clear()
            ctx.clear_preview('a')
            b.drain_main_queue()
            self.assertIn('preview', b._needs_redraw)
        finally:
            b.stop_workers()


# --- run_in_worker --------------------------------------------------------


class TestRunInWorker(unittest.TestCase):
    """``ctx.run_in_worker(fn)`` runs ``fn`` on a fresh daemon thread.

    Exceptions are surfaced via ``browser.error`` rather than crashing
    the process, mirroring ``Browser.watch``.
    """

    def test_runs_on_non_main_thread(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            main_tid = threading.get_ident()
            captured = {}
            done = threading.Event()

            def fn():
                captured['tid'] = threading.get_ident()
                done.set()

            t = ctx.run_in_worker(fn)
            self.assertTrue(done.wait(timeout=2.0))
            t.join(timeout=1.0)
            self.assertNotEqual(captured['tid'], main_tid)
        finally:
            b.stop_workers()

    def test_returns_thread_handle(self):
        b = _make_browser()
        try:
            ctx = Context(b)
            done = threading.Event()
            t = ctx.run_in_worker(lambda: done.set())
            self.assertIsInstance(t, threading.Thread)
            self.assertTrue(t.daemon)
            done.wait(timeout=1.0)
            t.join(timeout=1.0)
        finally:
            b.stop_workers()

    def test_exception_surfaces_via_browser_error(self):
        # An unhandled exception inside the callable should land in the
        # message log (via post + drain), not crash the process.
        b = _make_browser()
        try:
            ctx = Context(b)
            done = threading.Event()

            def boom():
                try:
                    raise ValueError('explode')
                finally:
                    # Signal completion so the test doesn't race.
                    done.set()

            t = ctx.run_in_worker(boom)
            self.assertTrue(done.wait(timeout=2.0))
            t.join(timeout=1.0)
            # The error() call posts; drain to surface it.
            # Allow a brief moment for the post to land before draining.
            for _ in range(20):
                b.drain_main_queue()
                if b._log:
                    break
                time.sleep(0.01)
            log = '\n'.join(b._log)
            self.assertIn('run_in_worker', log)
            self.assertIn('ValueError', log)
            self.assertIn('explode', log)
        finally:
            b.stop_workers()

    def test_callable_can_drive_browser_push_api(self):
        # The most common use: run a slow fetch off the main thread,
        # then push results via update_data.
        b = _make_browser(root_id='/')
        try:
            ctx = Context(b)
            done = threading.Event()

            def fetch_and_push():
                ctx.upsert('a', '/', title='Pushed')
                done.set()

            t = ctx.run_in_worker(fetch_and_push)
            self.assertTrue(done.wait(timeout=2.0))
            t.join(timeout=1.0)
            b.drain_main_queue()
            kids = b._state._children.get('/', [])
            self.assertEqual([k.id for k in kids], ['a'])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
