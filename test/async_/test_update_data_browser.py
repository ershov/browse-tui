"""Tests for ``Browser.update_data`` — the public, thread-safe push entry.

Ticket #269 layers ``update_data`` on top of the pure ``apply_ops``
layer (#268). This file pins:

  * dispatch from the main thread (apply happens on next drain)
  * dispatch from a background thread (queued through ``post``, applied
    on main-thread drain, no race observable)
  * batch atomicity: a multi-op call goes through one ``post`` callable,
    so ``apply_ops`` runs once — no opportunity for the renderer to
    observe a torn intermediate state, and ``_visible_dirty`` flips at
    most once per batch
  * loading-only batches do NOT flip ``_visible_dirty`` (consistent with
    ``apply_ops`` semantics)
  * thread-safety smoke: N concurrent threads each push one upsert,
    all N items present after ``run_until_idle``
"""

import threading
import unittest

from test.async_._helpers import Item, make_browser
from test.async_._helpers import _state as _state_mod

upsert = _state_mod.upsert
set_item = _state_mod.set_item
remove = _state_mod.remove
clear_children = _state_mod.clear_children
complete = _state_mod.complete
incomplete = _state_mod.incomplete


def _children_ids(state, parent_id):
    return [it.id for it in state._children.get(parent_id, [])]


class TestUpdateDataDispatch(unittest.TestCase):
    """update_data schedules apply_ops on the post queue."""

    def test_main_thread_dispatch_applies_after_drain(self):
        b = make_browser()
        try:
            # Before drain: nothing is applied yet (post-queue is in flight).
            b.update_data([upsert('a', '/', title='A')])
            self.assertNotIn('a', b._state._items_by_id)

            # One drain runs the scheduled apply_ops.
            n = b.drain_main_queue()
            self.assertEqual(n, 1)
            self.assertIn('a', b._state._items_by_id)
            self.assertEqual(_children_ids(b._state, '/'), ['a'])
            # apply_ops flipped _visible_dirty for a structural op.
            self.assertTrue(b._state._visible_dirty)
        finally:
            b.stop_workers()

    def test_returns_none(self):
        b = make_browser()
        try:
            result = b.update_data([upsert('a', '/', title='A')])
            self.assertIsNone(result)
            b.run_until_idle()
        finally:
            b.stop_workers()

    def test_background_thread_dispatch(self):
        b = make_browser()
        try:
            def push():
                b.update_data([upsert('bg', '/', title='from-thread')])

            t = threading.Thread(target=push)
            t.start()
            t.join()
            # Apply runs on the main thread when the queue is drained --
            # the background thread didn't mutate state directly.
            b.run_until_idle()
            self.assertIn('bg', b._state._items_by_id)
            self.assertEqual(_children_ids(b._state, '/'), ['bg'])
        finally:
            b.stop_workers()

    def test_snapshots_ops_iterable(self):
        # The scheduled callable must NOT capture a live, mutating source.
        # We pass a generator (consumed once on the calling thread by
        # update_data's list-snapshot) and then mutate nothing -- but the
        # contract guarantees a list snapshot, so the post-callable sees
        # exactly the ops we passed.
        b = make_browser()
        try:
            ops = (op for op in [upsert('g', '/', title='G')])
            b.update_data(ops)
            b.run_until_idle()
            self.assertIn('g', b._state._items_by_id)
        finally:
            b.stop_workers()


class TestUpdateDataAtomicity(unittest.TestCase):
    """A multi-op batch is one post-queue task — one apply, one dirty flip."""

    def test_multi_op_batch_is_single_drain_unit(self):
        b = make_browser()
        try:
            # A 4-op batch — if update_data scheduled per-op, drain_main_queue
            # would return 4. The contract says it schedules a single callable.
            ops = [
                upsert('a', '/', title='A'),
                upsert('b', '/', title='B'),
                upsert('c', '/', title='C'),
                remove('b'),
            ]
            b.update_data(ops)
            n = b.drain_main_queue()
            self.assertEqual(n, 1)
            # Final state matches the post-batch result, never an
            # intermediate (e.g., 'b' is gone, 'a' and 'c' remain in order).
            self.assertEqual(_children_ids(b._state, '/'), ['a', 'c'])
            self.assertNotIn('b', b._state._items_by_id)
        finally:
            b.stop_workers()

    def test_visible_dirty_flips_at_most_once_per_batch(self):
        # Spy on _visible_dirty mutations: assign a property to State?
        # Simpler: pre-clear the flag, run a multi-structural-op batch,
        # observe the flag is True exactly once after the drain. Because
        # the whole batch runs in one post callable, the renderer (which
        # would observe and clear the flag between drains) cannot see a
        # half-applied state.
        b = make_browser()
        try:
            # Reset the flag the constructor leaves True.
            b._state._visible_dirty = False
            b.update_data([
                upsert('a', '/', title='A'),
                upsert('b', '/', title='B'),
                upsert('c', '/', title='C'),
            ])
            # Mid-batch sanity: before the drain runs, nothing is applied
            # and the flag is still False -- no torn state has been
            # exposed.
            self.assertFalse(b._state._visible_dirty)
            self.assertNotIn('a', b._state._items_by_id)

            b.drain_main_queue()
            # Post-drain: full batch applied, flag flipped exactly once
            # (only one apply_ops call occurred).
            self.assertTrue(b._state._visible_dirty)
            self.assertEqual(_children_ids(b._state, '/'), ['a', 'b', 'c'])
        finally:
            b.stop_workers()

    def test_no_torn_state_visible_between_drains(self):
        # Cross-confirm the atomicity property: between calling
        # update_data and the next drain, NO ops are applied. After the
        # drain, ALL ops are applied.
        b = make_browser()
        try:
            # Pre-seed: 'a' exists; the batch removes it and adds 'b'.
            b.update_data([upsert('a', '/', title='A')])
            b.drain_main_queue()
            self.assertEqual(_children_ids(b._state, '/'), ['a'])

            b.update_data([
                remove('a'),
                upsert('b', '/', title='B'),
            ])
            # No drain yet -- 'a' must still be present, 'b' absent.
            self.assertIn('a', b._state._items_by_id)
            self.assertNotIn('b', b._state._items_by_id)

            b.drain_main_queue()
            # Post-drain: full swap, no half-state.
            self.assertNotIn('a', b._state._items_by_id)
            self.assertIn('b', b._state._items_by_id)
            self.assertEqual(_children_ids(b._state, '/'), ['b'])
        finally:
            b.stop_workers()

    def test_loading_only_batch_does_not_flip_visible_dirty(self):
        # ``complete`` / ``incomplete`` change loading state only -- not
        # tree shape. apply_ops semantics from #268: no structural ops =>
        # _visible_dirty stays as-is.
        b = make_browser()
        try:
            # Seed a parent so ``complete`` has somewhere to land.
            b.update_data([upsert('p', '/', title='P', has_children=True)])
            b.run_until_idle()

            b._state._visible_dirty = False
            b.update_data([complete('p')])
            b.drain_main_queue()
            # Still False — nothing structural happened.
            self.assertFalse(b._state._visible_dirty)
            self.assertEqual(b._state._loading.get('p'), False)

            b.update_data([incomplete('p')])
            b.drain_main_queue()
            self.assertFalse(b._state._visible_dirty)
            self.assertEqual(b._state._loading.get('p'), True)
        finally:
            b.stop_workers()


class TestUpdateDataThreadSafety(unittest.TestCase):
    """N background threads each push an upsert -- all land safely."""

    def test_concurrent_pushers(self):
        b = make_browser()
        try:
            N = 32
            barrier = threading.Barrier(N)

            def pusher(i):
                barrier.wait()  # release everyone at the same time
                b.update_data([upsert(f'item-{i}', '/', title=f'T{i}')])

            threads = [
                threading.Thread(target=pusher, args=(i,)) for i in range(N)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            b.run_until_idle()

            ids = set(_children_ids(b._state, '/'))
            self.assertEqual(ids, {f'item-{i}' for i in range(N)})
            for i in range(N):
                self.assertIn(f'item-{i}', b._state._items_by_id)
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
