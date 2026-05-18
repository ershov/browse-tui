"""Tests for the worker-supersede API.

``Browser.run_in_slot(name, fn)`` returns a ``CancellationToken``;
calling it again with the same ``name`` cancels the prior token
before launching the new worker. Cancellation is cooperative —
``fn`` must poll ``token.is_cancelled()``.
"""

import threading
import time
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

Browser = _state.Browser
Context = _context.Context
CancellationToken = _state.CancellationToken


class TestCancellationToken(unittest.TestCase):

    def test_default_not_cancelled(self):
        t = CancellationToken()
        self.assertFalse(t.is_cancelled())

    def test_cancel_flips_flag(self):
        t = CancellationToken()
        t.cancel()
        self.assertTrue(t.is_cancelled())

    def test_cancel_idempotent(self):
        t = CancellationToken()
        t.cancel()
        t.cancel()
        self.assertTrue(t.is_cancelled())


class TestRunInSlot(unittest.TestCase):

    def test_returns_token(self):
        b = Browser(_headless=True)
        done = threading.Event()
        token = b.run_in_slot('s', lambda t: done.set())
        done.wait(timeout=2.0)
        self.assertIsInstance(token, CancellationToken)

    def test_resubmission_cancels_prior(self):
        # First worker spins on its token; second submission cancels it.
        b = Browser(_headless=True)
        first_started = threading.Event()
        first_exited = threading.Event()

        def first(token):
            first_started.set()
            while not token.is_cancelled():
                time.sleep(0.01)
            first_exited.set()

        t1 = b.run_in_slot('s', first)
        first_started.wait(timeout=2.0)
        self.assertFalse(t1.is_cancelled())
        b.run_in_slot('s', lambda token: None)
        # First token must now be cancelled and its worker exits.
        first_exited.wait(timeout=2.0)
        self.assertTrue(t1.is_cancelled())
        self.assertTrue(first_exited.is_set())

    def test_different_slots_are_independent(self):
        b = Browser(_headless=True)
        t1 = b.run_in_slot('a', lambda token: time.sleep(0.05))
        t2 = b.run_in_slot('b', lambda token: time.sleep(0.05))
        # Neither is cancelled — different slots.
        self.assertFalse(t1.is_cancelled())
        self.assertFalse(t2.is_cancelled())

    def test_exception_routed_to_error(self):
        b = Browser(_headless=True)
        done = threading.Event()
        def bad(token):
            done.set()
            raise RuntimeError('worker boom')
        b.run_in_slot('s', bad)
        done.wait(timeout=2.0)
        # ``Browser.error`` posts; drain so error_text reflects it.
        for _ in range(50):
            b.drain_main_queue()
            if 'worker boom' in b.error_text:
                break
            time.sleep(0.01)
        self.assertIn('worker boom', b.error_text)
        self.assertIn('run_in_slot', b.error_text)

    def test_slot_entry_removed_on_exit(self):
        b = Browser(_headless=True)
        done = threading.Event()
        b.run_in_slot('s', lambda token: done.set())
        done.wait(timeout=2.0)
        # Give the runner's finally: clause time to run.
        for _ in range(50):
            if 's' not in b._slots:
                break
            time.sleep(0.01)
        self.assertNotIn('s', b._slots)


class TestContextPassthrough(unittest.TestCase):

    def test_ctx_run_in_slot(self):
        b = Browser(_headless=True)
        ctx = Context(b)
        done = threading.Event()
        token = ctx.run_in_slot('s', lambda t: done.set())
        done.wait(timeout=2.0)
        self.assertIsInstance(token, CancellationToken)


if __name__ == '__main__':
    unittest.main()
