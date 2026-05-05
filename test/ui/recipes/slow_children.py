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
    # First positional argv is the delay; remaining argv are passed
    # through as flags (e.g. ``--no-children-pane``). Only one flag is
    # interpreted today; everything else is silently ignored.
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    show_children_pane = '--no-children-pane' not in sys.argv[2:]

    def get_children(parent_id):
        if parent_id in (None, ''):
            return [Item(id='parent', title='parent', has_children=True)]
        # Slow path: simulate a long-running fetch.
        time.sleep(delay)
        return [Item(id='alpha', title='alpha'),
                Item(id='beta', title='beta')]

    b = Browser(
        get_children=get_children,
        show_children_pane=show_children_pane,
        # Force the id segment on every row so UI tests can assert on a
        # stable shape regardless of whether id == title.
        show_ids='always',
    )
    sys.exit(b.run())


if __name__ == '__main__':
    main()
