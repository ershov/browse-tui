"""Post-queue tests: thread-safe scheduling onto the main thread.

``Browser.post(fn)`` is the canonical way for any thread (workers,
watchers, signal handlers) to mutate Browser state. The fn runs on the
main thread the next time ``drain_main_queue`` is called -- in tests via
``run_until_idle``, in production via the main loop's wake handler.
"""

import threading
import unittest

from test.async_._helpers import make_browser


class TestPostQueue(unittest.TestCase):

    def test_post_from_main_thread(self):
        b = make_browser()
        try:
            calls = []
            b.post(lambda: calls.append('hello'))
            self.assertEqual(b.drain_main_queue(), 1)
            self.assertEqual(calls, ['hello'])
        finally:
            b.stop_workers()

    def test_post_from_background_thread(self):
        b = make_browser()
        try:
            calls = []
            def submit():
                b.post(lambda: calls.append('bg'))
            t = threading.Thread(target=submit)
            t.start()
            t.join()
            b.drain_main_queue()
            self.assertEqual(calls, ['bg'])
        finally:
            b.stop_workers()

    def test_posts_run_in_fifo_order(self):
        b = make_browser()
        try:
            calls = []
            for i in range(5):
                b.post(lambda i=i: calls.append(i))
            n = b.drain_main_queue()
            self.assertEqual(n, 5)
            self.assertEqual(calls, [0, 1, 2, 3, 4])
        finally:
            b.stop_workers()

    def test_posted_fn_can_mutate_browser_state(self):
        b = make_browser()
        try:
            b.post(lambda: b._state.expanded.add('x'))
            b.drain_main_queue()
            self.assertIn('x', b._state.expanded)
        finally:
            b.stop_workers()

    def test_drain_returns_count_of_fns_run(self):
        b = make_browser()
        try:
            self.assertEqual(b.drain_main_queue(), 0)  # empty -> 0
            b.post(lambda: None)
            b.post(lambda: None)
            b.post(lambda: None)
            self.assertEqual(b.drain_main_queue(), 3)
            self.assertEqual(b.drain_main_queue(), 0)  # drained
        finally:
            b.stop_workers()


if __name__ == '__main__':
    unittest.main()
