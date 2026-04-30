"""Chained-completion tests: .then() composition across worker fetches.

Verifies that the Pending returned by ``refresh()`` resolves on the main
thread after the worker delivers, and that ``.then`` callbacks fire in
registration order. Also covers the cross-refresh chain pattern used by
production code: ``refresh('A').then(lambda: refresh('B').then(...))``.
"""

import unittest

from test.async_._helpers import Pending, make_browser


class TestThen(unittest.TestCase):

    def test_refresh_then_callback_fires_after_fetch(self):
        events = []
        b = make_browser(get_children=lambda _: [])
        try:
            p = b.refresh('A').then(lambda: events.append('done'))
            self.assertIsInstance(p, Pending)
            # Before run_until_idle, the worker hasn't delivered yet.
            self.assertEqual(events, [])
            b.run_until_idle()
            self.assertEqual(events, ['done'])
        finally:
            b.stop_workers()

    def test_on_complete_kwarg_equivalent_to_then(self):
        events = []
        b = make_browser(get_children=lambda _: [])
        try:
            b.refresh('A', on_complete=lambda: events.append('done'))
            b.run_until_idle()
            self.assertEqual(events, ['done'])
        finally:
            b.stop_workers()

    def test_two_thens_fire_in_order(self):
        events = []
        b = make_browser(get_children=lambda _: [])
        try:
            (b.refresh('A')
                .then(lambda: events.append(1))
                .then(lambda: events.append(2)))
            b.run_until_idle()
            self.assertEqual(events, [1, 2])
        finally:
            b.stop_workers()

    def test_refresh_then_kicks_second_refresh(self):
        seen = []
        def get_children(id_):
            seen.append(id_)
            return []
        b = make_browser(get_children=get_children)
        try:
            (b.refresh('A')
                .then(lambda: b.refresh('B')))
            b.run_until_idle()
            self.assertEqual(seen, ['A', 'B'])
            self.assertIn('A', b._state._children)
            self.assertIn('B', b._state._children)
        finally:
            b.stop_workers()

    def test_two_level_chain_across_refreshes(self):
        events = []
        def get_children(id_):
            return []
        b = make_browser(get_children=get_children)
        try:
            (b.refresh('A')
                .then(lambda: b.refresh('B')
                    .then(lambda: events.append('done'))))
            b.run_until_idle()
            self.assertEqual(events, ['done'])
        finally:
            b.stop_workers()

    def test_then_on_already_resolved_pending_fires_immediately(self):
        # The Pending returned by refresh resolves on the main thread once
        # apply_children_results runs. After run_until_idle, attaching a
        # .then must fire synchronously.
        events = []
        b = make_browser(get_children=lambda _: [])
        try:
            p = b.refresh('A')
            b.run_until_idle()
            self.assertTrue(p.done)
            p.then(lambda: events.append('immediate'))
            self.assertEqual(events, ['immediate'])
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
