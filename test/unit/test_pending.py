"""Tests for the Pending class in browse-tui state layer.

Pending is the handle for an async operation that may chain follow-up
callbacks. ``.then(cb)`` registers a callback; ``._resolve()`` fires all
registered callbacks in registration order. ``.then(cb)`` on an already-
resolved Pending invokes the callback synchronously.
"""

import unittest

from test.unit._loader import load

_state = load('_browse_tui_state', '040-state.py')
Pending = _state.Pending


class TestPendingState(unittest.TestCase):
    """The done flag and basic resolve semantics."""

    def test_newly_constructed_not_done(self):
        p = Pending()
        self.assertFalse(p.done)

    def test_done_after_resolve(self):
        p = Pending()
        p._resolve()
        self.assertTrue(p.done)

    def test_resolve_is_idempotent(self):
        # A second _resolve() must not re-fire registered callbacks.
        p = Pending()
        calls = []
        p.then(lambda: calls.append('a'))
        p._resolve()
        self.assertEqual(calls, ['a'])
        p._resolve()
        self.assertEqual(calls, ['a'])


class TestPendingThenBeforeResolve(unittest.TestCase):
    """.then() before _resolve() registers the callback for later firing."""

    def test_then_before_resolve_does_not_fire(self):
        p = Pending()
        calls = []
        p.then(lambda: calls.append('a'))
        self.assertEqual(calls, [])

    def test_callback_fires_exactly_once_after_resolve(self):
        p = Pending()
        calls = []
        p.then(lambda: calls.append('a'))
        p._resolve()
        self.assertEqual(calls, ['a'])

    def test_multiple_callbacks_fire_in_registration_order(self):
        p = Pending()
        calls = []
        p.then(lambda: calls.append('a'))
        p.then(lambda: calls.append('b'))
        p.then(lambda: calls.append('c'))
        self.assertEqual(calls, [])
        p._resolve()
        self.assertEqual(calls, ['a', 'b', 'c'])

    def test_then_returns_self(self):
        p = Pending()
        self.assertIs(p.then(lambda: None), p)

    def test_then_chain_returns_same_instance(self):
        # .then(a).then(b).then(c) — each link returns the same Pending.
        p = Pending()
        result = p.then(lambda: None).then(lambda: None).then(lambda: None)
        self.assertIs(result, p)


class TestPendingThenAfterResolve(unittest.TestCase):
    """.then() after _resolve() fires the callback synchronously."""

    def test_then_after_resolve_fires_immediately(self):
        p = Pending()
        p._resolve()
        calls = []
        p.then(lambda: calls.append('a'))
        # Already populated by the time .then returned.
        self.assertEqual(calls, ['a'])

    def test_then_after_resolve_is_synchronous_before_return(self):
        # Use a sentinel marker around .then() to prove synchronous call.
        p = Pending()
        p._resolve()
        events = []

        def cb():
            events.append('cb')

        events.append('before-then')
        p.then(cb)
        events.append('after-then')
        self.assertEqual(events, ['before-then', 'cb', 'after-then'])

    def test_late_then_after_earlier_callbacks_already_fired(self):
        # Earlier callbacks fire on _resolve(); a later .then() fires alone.
        p = Pending()
        calls = []
        p.then(lambda: calls.append('a'))
        p.then(lambda: calls.append('b'))
        p._resolve()
        self.assertEqual(calls, ['a', 'b'])
        p.then(lambda: calls.append('d'))
        self.assertEqual(calls, ['a', 'b', 'd'])


class TestPendingChainExtension(unittest.TestCase):
    """Callbacks may register further callbacks on Pendings."""

    def test_callback_registers_inner_on_same_resolved_pending(self):
        # cb registered before resolve. cb registers cb_inner on the same
        # Pending — by the time cb runs, the Pending is already resolved
        # (snapshot-and-clear semantics in _resolve), so cb_inner fires
        # immediately during cb's execution.
        p = Pending()
        calls = []

        def cb():
            calls.append('cb')
            p.then(lambda: calls.append('inner'))

        p.then(cb)
        p._resolve()
        self.assertEqual(calls, ['cb', 'inner'])

    def test_callback_registers_on_different_pending(self):
        # A callback may freely call .then on an unrelated Pending.
        p1 = Pending()
        p2 = Pending()
        calls = []

        def cb():
            calls.append('p1-cb')
            p2.then(lambda: calls.append('p2-cb'))

        p1.then(cb)
        p1._resolve()
        # p2 has not resolved yet, so its callback is queued, not fired.
        self.assertEqual(calls, ['p1-cb'])
        p2._resolve()
        self.assertEqual(calls, ['p1-cb', 'p2-cb'])


class TestPendingChainCallbackOrderUnderResolve(unittest.TestCase):
    """Subtle ordering: .then() called from within a callback during _resolve.

    The implementation snapshots ``_chain`` and clears it before iterating,
    so any callback registered during iteration sees ``_done=True`` and
    fires synchronously inside its own ``.then()`` call. Concretely:
    register cb_a, cb_b. Call _resolve(). During cb_a, register cb_c.
    Order: cb_a runs, cb_c fires synchronously (because Pending is already
    done), then iteration continues to cb_b. So: a, c, b.
    """

    def test_then_during_callback_fires_immediately_then_iteration_resumes(self):
        p = Pending()
        calls = []

        def cb_a():
            calls.append('a')
            p.then(lambda: calls.append('c'))

        def cb_b():
            calls.append('b')

        p.then(cb_a)
        p.then(cb_b)
        p._resolve()
        self.assertEqual(calls, ['a', 'c', 'b'])


if __name__ == '__main__':
    unittest.main()
