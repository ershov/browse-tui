"""Recipe: children pane's FIRST-EVER appearance (ticket #961).

Tree shape:

    X    (no children; a wide, distinctive preview ZZZZ…)
    P    (has_children: p1, p2)

The cursor lands on X (a childless node) at launch, so the children
pane is NEVER shown on the first frames — in vertical layout the
preview occupies the full right area and paints the wide ZZZZ marker
across the columns the children pane will later occupy.

Moving down to P makes the children pane appear for the FIRST time. On
the buggy build the children column's first paint takes ``end_row``'s
``prev_rect is None`` "no padding" branch (its PaneCache was never
stamped with the disappeared-pane sentinel), so the preview's ZZZZ
cells in the column's trailing region are left uncleared.

Usage:
    browse-tui --run-py children_first_appearance.py --split-type=v
"""

import sys

from browse_tui import Browser, BrowserConfig, Item


def main():
    children = {
        '': [
            Item(id='X', title='X'),
            Item(id='P', title='P', has_children=True),
        ],
        'P': [
            Item(id='p1', title='p1'),
            Item(id='p2', title='p2'),
        ],
    }

    def get_children(parent_id, *, reload=False):
        return list(children.get(parent_id or '', []))

    def get_preview(item_id):
        # X's preview is wide and distinctive so any cell it paints in
        # the future children-column region is easy to detect as stale.
        if item_id == 'X':
            return '\n'.join(['ZZZZ ' * 30] * 18)
        if item_id == 'P':
            return 'preview-of-P\n' * 5
        return f'pv:{item_id}'

    b = Browser(BrowserConfig(
        get_children=get_children,
        get_preview=get_preview,
        show_ids='always',
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
