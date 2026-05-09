"""Recipe: generator-streaming ``get_children`` (#272).

The root ``get_children`` is a generator that yields three chunks with a
small inter-chunk delay. Each yield is delivered as its own
``update_data`` batch (per the streaming-push spec) so the UI sees rows
appear in waves rather than all at once. The trailing ``complete`` op
fires only when the generator exhausts.

The chunks intentionally mix shapes:

* yield #1: a list of two ``Item``s
* yield #2: a list of two ``Item``s
* yield #3: a single ``Item`` (auto-promoted to a one-item batch by
  ``_stream_children_from_generator``)

Usage:  browse-tui --run-py streaming_generator_children.py [DELAY]
"""

import sys
import time

from browse_tui import Browser, Item


def main():
    # Default delay is large enough that the test reliably catches an
    # intermediate state between each chunk: ``Browser.run`` waits up
    # to 500 ms for an initial idle before the first paint, so the
    # inter-chunk gap must comfortably exceed that for "b-1 not yet
    # visible" assertions to be deterministic on a busy host.
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0

    def get_children(parent_id):
        if parent_id not in (None, ''):
            return []
        yield [Item(id='a-1', title='a-1'),
               Item(id='a-2', title='a-2')]
        time.sleep(delay)
        yield [Item(id='b-1', title='b-1'),
               Item(id='b-2', title='b-2')]
        time.sleep(delay)
        yield Item(id='c-1', title='c-1')

    b = Browser(get_children=get_children, show_ids='always')
    sys.exit(b.run())


if __name__ == '__main__':
    main()
