"""Recipe: slow get_children for the ROOT, fixed get_preview.

Triggers ticket #124's bug repro: when the root children fetch resolves
*after* Browser.run()'s 500ms startup-wait window, the cursor lands on
row 0 once children arrive — but the preview pane stayed blank until a
key was pressed. The fix runs ``_update_preview_for_cursor`` at the top
of every main-loop iteration (after worker results land), not just
after key dispatch.

Usage:  browse-tui --run-py slow_root_with_preview.py [DELAY]
"""

import sys
import time

from browse_tui import Browser, Item


def main():
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0

    def get_children(parent_id):
        if parent_id in (None, ''):
            time.sleep(delay)
            return [Item(id='alpha', title='alpha'),
                    Item(id='beta', title='beta'),
                    Item(id='gamma', title='gamma')]
        return []

    def get_preview(item_id):
        return f'PREVIEW:{item_id}\nline2\nline3'

    b = Browser(
        get_children=get_children,
        get_preview=get_preview,
        show_ids='always',
    )
    sys.exit(b.run())


if __name__ == '__main__':
    main()
