"""Recipe: slow ``get_children`` for an expandable parent.

Used by the loading-spinner UI test (#276). The root level returns one
item ``parent`` with ``has_children=True``. Expanding it (e.g. pressing
Right) triggers ``get_children('parent')`` which sleeps DELAY seconds
(default 1.0s) before returning two items. While the fetch is in flight
the renderer should show the ``⧗ loading…`` placeholder under the
parent; once items arrive the placeholder is cleared and the children
appear.

This is the streaming-spec companion to ``slow_children.py`` and is kept
separate so the streaming tests stay self-contained.

Usage:  browse-tui --run-py streaming_slow_root.py [DELAY]
"""

import sys
import time

from browse_tui import Browser, BrowserConfig, Item


def main():
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0

    def get_children(parent_id, *, reload=False):
        if parent_id in (None, ''):
            return [Item(id='parent', title='parent', has_children=True)]
        time.sleep(delay)
        return [Item(id='alpha', title='alpha'),
                Item(id='beta', title='beta')]

    b = Browser(BrowserConfig(get_children=get_children, show_ids='always'))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
