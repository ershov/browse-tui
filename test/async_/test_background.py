"""Background-thread tests: watchers, signal handlers, daemon-thread lifecycle.

Covers the production pattern where a watcher thread (filesystem watcher,
poller, MQ subscriber) calls ``browser.refresh()`` or ``browser.flash()``
from outside the main thread. The post queue ferries the call back to
the main thread; the worker fetches; the cache populates.
"""

import threading
import time
import unittest

from test.async_._helpers import make_browser


class TestBackgroundUpdates(unittest.TestCase):

    def test_watcher_thread_can_refresh(self):
        b = make_browser(get_children=lambda _id, *, reload=False: [f'{_id}/c'])
        try:
            def watcher():
                b.refresh('A')
            t = threading.Thread(target=watcher)
            t.start()
            t.join()
            b.run_until_idle()
            self.assertIn('A', b._state._children)
            ids = [it.id for it in b._state._children['A']]
            self.assertEqual(ids, ['A/c'])
        finally:
            b.stop_workers()

    def test_watcher_thread_can_post_flash(self):
        b = make_browser()
        try:
            def watcher():
                b.flash('hi')
            t = threading.Thread(target=watcher)
            t.start()
            t.join()
            b.drain_main_queue()
            self.assertEqual(b._notice.text, 'hi')
            self.assertEqual(b._notice.kind, 'flash')
        finally:
            b.stop_workers()

    def test_multiple_background_updates_progressively(self):
        # Each watcher tick adds an item to the recipe's view of the
        # world; after each refresh, the cache reflects the latest view.
        store = {'children': []}
        def get_children(_id, *, reload=False):
            return list(store['children'])
        b = make_browser(get_children=get_children)
        try:
            def watcher_tick(payload):
                store['children'].append(payload)   # str → id
                b.refresh('A')
            t1 = threading.Thread(target=watcher_tick, args=('one',))
            t1.start(); t1.join()
            b.run_until_idle()
            self.assertEqual([it.id for it in b._state._children['A']],
                             ['one'])

            t2 = threading.Thread(target=watcher_tick, args=('two',))
            t2.start(); t2.join()
            b.run_until_idle()
            self.assertEqual([it.id for it in b._state._children['A']],
                             ['one', 'two'])

            t3 = threading.Thread(target=watcher_tick, args=('three',))
            t3.start(); t3.join()
            b.run_until_idle()
            self.assertEqual([it.id for it in b._state._children['A']],
                             ['one', 'two', 'three'])
        finally:
            b.stop_workers()

    def test_watcher_daemon_exits_when_browser_stops(self):
        # A long-running daemon watcher must not prevent process exit.
        # The Browser's workers are daemon threads; this test confirms a
        # user-supplied watcher can also be a daemon and that
        # stop_workers shuts down the Browser threads regardless.
        b = make_browser()
        stop = threading.Event()
        def watcher():
            while not stop.is_set():
                b.flash('tick')
                time.sleep(0.005)
        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        try:
            time.sleep(0.02)  # let it tick a few times
            b.drain_main_queue()
            self.assertEqual(b._notice.text, 'tick')
        finally:
            stop.set()
            t.join(timeout=1.0)
            self.assertFalse(t.is_alive())
            b.stop_workers()
            self.assertFalse(b._children_thread.is_alive())
            self.assertFalse(b._preview_thread.is_alive())


if __name__ == '__main__':
    unittest.main()
