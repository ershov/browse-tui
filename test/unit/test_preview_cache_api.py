"""Tests for the public preview-cache API.

Covers ``Browser.get_cached_preview`` / ``drop_preview_cache`` /
``preview_item_id`` and their Context passthroughs.
"""

import unittest

from test.unit._loader import load

_term = load('_browse_tui_term', '020-terminal.py')
_data = load('_browse_tui_data', '030-data.py')
_state = load('_browse_tui_state', '040-state.py')
_context = load('_browse_tui_context', '060-context.py')

_state.Item = _data.Item
_state.to_item = _data.to_item
_state.notify_wake = _term.notify_wake
_context.visible_items = _state.visible_items

Item = _data.Item
Browser = _state.Browser
BrowserConfig = _state.BrowserConfig
Context = _context.Context
upsert = _state.upsert
set_preview_op = _state.set_preview_op
append_preview_op = _state.append_preview_op
clear_preview_op = _state.clear_preview_op
invalidate_preview_op = _state.invalidate_preview_op
drop_preview_cache_op = _state.drop_preview_cache_op


def _drain(b):
    b.drain_main_queue()


def _seed(b, id_, text):
    """Register an Item with id and stash ``text`` on its ``preview`` slot."""
    item = Item(id=id_)
    b._state._items_by_id[id_] = item
    item.preview = text
    return item


class TestGetCachedPreview(unittest.TestCase):

    def test_returns_cached_text(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b, 'x', 'hello')
        self.assertEqual(b.get_cached_preview('x'), 'hello')

    def test_returns_none_when_absent(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertIsNone(b.get_cached_preview('nope'))

    def test_does_not_kick_worker(self):
        # Synchronous read — no post-queue side effects.
        b = Browser(BrowserConfig(_headless=True))
        before = b._main_queue.qsize()
        _ = b.get_cached_preview('whatever')
        self.assertEqual(b._main_queue.qsize(), before)


class TestDropPreviewCache(unittest.TestCase):

    def test_drop_single_id(self):
        b = Browser(BrowserConfig(_headless=True))
        a = _seed(b, 'a', 'A')
        bb = _seed(b, 'b', 'B')
        b.drop_preview_cache('a')
        _drain(b)
        self.assertIsNone(a.preview)
        self.assertEqual(bb.preview, 'B')

    def test_drop_all(self):
        b = Browser(BrowserConfig(_headless=True))
        items = [_seed(b, i, i.upper()) for i in ('a', 'b', 'c')]
        b.drop_preview_cache()
        _drain(b)
        for it in items:
            self.assertIsNone(it.preview)

    def test_idempotent_on_missing(self):
        b = Browser(BrowserConfig(_headless=True))
        b.drop_preview_cache('never_existed')
        _drain(b)
        # Nothing should crash; index stays empty.
        self.assertEqual(b._state._items_by_id, {})

    def test_signals_redraw(self):
        b = Browser(BrowserConfig(_headless=True))
        b._needs_redraw.clear()
        b.drop_preview_cache('x')
        _drain(b)
        self.assertIn('preview', b._needs_redraw)

    def test_kicks_worker_when_dropping_cursor_id(self):
        # When the dropped id equals the preview cursor, the
        # framework auto-kicks request_preview so the pane refills.
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'cur'
        _seed(b, 'cur', 'stale')

        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.drop_preview_cache('cur')
        _drain(b)
        self.assertEqual(kicked, ['cur'])

    def test_does_not_kick_for_non_cursor_id(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'cur'
        _seed(b, 'other', 'stale')

        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.drop_preview_cache('other')
        _drain(b)
        self.assertEqual(kicked, [])

    def test_drop_all_kicks_cursor(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'cur'
        _seed(b, 'cur', 'A')
        _seed(b, 'other', 'B')

        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.drop_preview_cache()
        _drain(b)
        self.assertEqual(kicked, ['cur'])


class TestPreviewItemId(unittest.TestCase):

    def test_default_none(self):
        b = Browser(BrowserConfig(_headless=True))
        self.assertIsNone(b.preview_item_id)

    def test_reflects_state(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'abc'
        self.assertEqual(b.preview_item_id, 'abc')


class TestContextPassthroughs(unittest.TestCase):

    def test_get_cached_preview(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b, 'x', 'X')
        ctx = Context(b)
        self.assertEqual(ctx.get_cached_preview('x'), 'X')
        self.assertIsNone(ctx.get_cached_preview('nope'))

    def test_drop_preview_cache(self):
        b = Browser(BrowserConfig(_headless=True))
        a = _seed(b, 'a', 'A')
        _seed(b, 'b', 'B')
        ctx = Context(b)
        ctx.drop_preview_cache('a')
        _drain(b)
        self.assertIsNone(a.preview)

    def test_preview_item_id(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'foo'
        ctx = Context(b)
        self.assertEqual(ctx.preview_item_id, 'foo')


class TestBatchedPreviewOpsKickWorker(unittest.TestCase):
    """``update_data`` batches with preview ops translate to ``request_preview`` calls (#446)."""

    def test_invalidate_preview_op_kicks_via_update_data(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b, 'x', 'old')
        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.update_data([invalidate_preview_op('x')])
        _drain(b)
        self.assertEqual(kicked, ['x'])

    def test_invalidate_preview_op_kicks_unknown_id(self):
        # Matches Browser.invalidate_preview contract — kick fires even
        # when the id is unregistered.
        b = Browser(BrowserConfig(_headless=True))
        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.update_data([invalidate_preview_op('ghost')])
        _drain(b)
        self.assertEqual(kicked, ['ghost'])

    def test_drop_preview_cache_op_kicks_when_cursor_matches(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'cur'
        _seed(b, 'cur', 'stale')
        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.update_data([drop_preview_cache_op('cur')])
        _drain(b)
        self.assertEqual(kicked, ['cur'])

    def test_drop_preview_cache_op_no_kick_when_cursor_differs(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'cur'
        _seed(b, 'other', 'stale')
        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.update_data([drop_preview_cache_op('other')])
        _drain(b)
        self.assertEqual(kicked, [])

    def test_drop_preview_cache_op_all_kicks_cursor(self):
        b = Browser(BrowserConfig(_headless=True))
        b._preview_cursor_id = 'cur'
        _seed(b, 'cur', 'A')
        _seed(b, 'other', 'B')
        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.update_data([drop_preview_cache_op()])
        _drain(b)
        self.assertEqual(kicked, ['cur'])

    def test_multiple_kicks_in_one_batch(self):
        # Two invalidate ops in one batch produce two request_preview
        # calls. request_preview is latest-wins so the observable
        # ``_preview_req`` ends up as the last id, but each call still
        # runs.
        b = Browser(BrowserConfig(_headless=True))
        _seed(b, 'a', 'A')
        _seed(b, 'b', 'B')
        kicked = []
        b.request_preview = lambda id_: kicked.append(id_)

        b.update_data([
            invalidate_preview_op('a'),
            invalidate_preview_op('b'),
        ])
        _drain(b)
        self.assertEqual(kicked, ['a', 'b'])


class TestBatchedPreviewOpsWriteThrough(unittest.TestCase):
    """Mixed batches: tree + preview ops land in one ``update_data`` call (#446)."""

    def test_upsert_and_set_preview_in_one_batch(self):
        # The umbrella composer's hot-path: register an Item and write
        # its preview in the same batch. The within-batch FIFO ordering
        # makes the registration visible to the set_preview op.
        b = Browser(BrowserConfig(_headless=True))
        b.update_data([
            upsert('a', None, title='A'),
            set_preview_op('a', 'hello'),
        ])
        _drain(b)
        item = b._state._items_by_id['a']
        self.assertEqual(item.preview, 'hello')

    def test_one_post_for_mixed_batch(self):
        # Whole batch is delivered through one post-queue closure —
        # not one per op. Asserted via queue size.
        b = Browser(BrowserConfig(_headless=True))
        before = b._main_queue.qsize()
        b.update_data([
            upsert('a', None, title='A'),
            set_preview_op('a', 'x'),
            set_preview_op('a', 'y'),
            set_preview_op('a', 'z'),
        ])
        # Single closure scheduled on the post queue.
        self.assertEqual(b._main_queue.qsize(), before + 1)
        _drain(b)
        # Last write wins (FIFO).
        self.assertEqual(b._state._items_by_id['a'].preview, 'z')

    def test_preview_dirty_signal_marks_redraw(self):
        b = Browser(BrowserConfig(_headless=True))
        _seed(b, 'a', 'cached')
        b._needs_redraw.clear()
        b.update_data([set_preview_op('a', 'fresh')])
        _drain(b)
        self.assertIn('preview', b._needs_redraw)

    def test_batch_without_preview_ops_does_not_flag_preview(self):
        # Pure tree-op batch — preview-dirty flag stays False so
        # _needs_redraw doesn't get 'preview' on its own from this path.
        b = Browser(BrowserConfig(_headless=True))
        b.update_data([upsert('a', None, title='A')])
        # Reset and apply another tree-only batch to check no leak.
        _drain(b)
        b._needs_redraw.clear()
        b.update_data([upsert('a', None, title='A2')])
        _drain(b)
        self.assertNotIn('preview', b._needs_redraw)


if __name__ == '__main__':
    unittest.main()
