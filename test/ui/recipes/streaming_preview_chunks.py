"""Recipe: ``get_preview`` generator that yields chunks with delays (#273).

The root has a single item; ``get_preview`` is a generator that yields
three short lines with a small delay between yields. Each yield is
appended to the preview buffer so the renderer paints chunks
incrementally — the UI test verifies each line appears in the preview
pane before the next is added.

Distinct from ``preview_resume.py`` (#274) which exercises the
scroll-driven demand-resume path; this recipe stays under any buffer cap
and never pauses, so the test focuses purely on the eager-streaming
behaviour.

Usage:  browse-tui --run-py streaming_preview_chunks.py [DELAY]
"""

import sys
import time

from browse_tui import Browser, Item


def main():
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2

    def get_children(parent_id):
        if parent_id in (None, ''):
            return [Item(id='item', title='item')]
        return []

    def get_preview(_item_id):
        yield 'first line\n'
        time.sleep(delay)
        yield 'second line\n'
        time.sleep(delay)
        yield 'third line\n'

    b = Browser(
        get_children=get_children,
        get_preview=get_preview,
        show_ids='always',
    )
    sys.exit(b.run())


if __name__ == '__main__':
    main()
