"""Recipe: watcher pushing live tag updates via ``update_data`` (#271).

The root has two rows with the placeholder tag ``-``. A watcher thread
sleeps briefly, pushes an ``upsert`` patching ``row-a``'s tag to
``UPD``, sleeps again, then patches ``row-b``'s tag to ``!!``. The UI
test observes the tag column flipping on each row in turn — no key
presses, no refresh, just background pushes hitting the renderer.

Usage:  browse-tui --run-py streaming_watcher_update.py [DELAY]
"""

import sys
import time

from browse_tui import Browser, BrowserConfig, Item, upsert


def main():
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5

    def get_children(parent_id, *, reload=False):
        if parent_id in (None, ''):
            return [Item(id='row-a', title='A', tag='-'),
                    Item(id='row-b', title='B', tag='-')]
        return []

    b = Browser(BrowserConfig(get_children=get_children, show_ids='always'))

    def watcher(browser):
        time.sleep(delay)
        browser.update_data([upsert('row-a', None, tag='UPD')])
        time.sleep(delay)
        browser.update_data([upsert('row-b', None, tag='!!')])

    # ``interval=None`` runs the watcher exactly once (it does its own
    # sleeps); the daemon thread exits after the second push.
    b.watch(watcher)
    sys.exit(b.run())


if __name__ == '__main__':
    main()
