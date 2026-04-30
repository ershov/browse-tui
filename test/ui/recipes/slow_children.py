"""Recipe: emits a parent immediately whose expansion is slow.

The root level returns one item ('parent') with has_children=True. When
the user presses Right/Enter to expand it, ``get_children('parent')``
sleeps DELAY seconds before returning. This makes the ``⧗ loading…``
placeholder visible in the rendered list while the fetch is in-flight.

Usage:
    browse-tui --python slow_children.py [DELAY]
"""

import sys
import time

from browse_tui import Browser, Item


def main():
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0

    def get_children(parent_id):
        if parent_id in (None, ''):
            return [Item(id='parent', title='parent', has_children=True)]
        # Slow path: simulate a long-running fetch.
        time.sleep(delay)
        return [Item(id='alpha', title='alpha'),
                Item(id='beta', title='beta')]

    b = Browser(get_children=get_children)
    sys.exit(b.run())


if __name__ == '__main__':
    main()
