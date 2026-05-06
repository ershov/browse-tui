"""Recipe: a tree exercising the children-pane redraw bug (ticket #201).

Tree shape:

    A    (has_children: a1, a2)
    B    (no children, distinctive preview)
    C    (has_children: c1, c2)

In vertical layout, when the cursor is on A or C the children pane is
present (showing a1/a2 or c1/c2). When the cursor is on B the children
pane disappears and the preview pane expands to fill the freed space.

Item B's preview contains a distinctive marker string (``BBBBPREVIEW``)
that should NEVER appear in the children-pane columns when the cursor
is on A or C — but the regression after the row-cache + sync-output
landing leaves stale preview cells in those columns.

Usage:
    browse-tui --run-py children_pane_redraw.py
"""

import sys

from browse_tui import Browser, Item


def main():
    children = {
        '': [
            Item(id='A', title='A', has_children=True),
            Item(id='B', title='B'),
            Item(id='C', title='C', has_children=True),
        ],
        'A': [
            Item(id='a1', title='a1'),
            Item(id='a2', title='a2'),
        ],
        'C': [
            Item(id='c1', title='c1'),
            Item(id='c2', title='c2'),
        ],
    }

    def get_children(parent_id):
        return list(children.get(parent_id or '', []))

    def get_preview(item_id):
        # B's preview contains a distinctive marker that fills enough
        # rows to be visible everywhere the children pane used to sit.
        # The marker is intentionally long so it spans the full width
        # of the expanded preview pane on a 240-col terminal.
        if item_id == 'B':
            marker = 'BBBBPREVIEW ' * 20
            lines = [marker.rstrip() for _ in range(20)]
            return '\n'.join(lines)
        if item_id == 'A':
            return 'preview-of-A\n' * 5
        if item_id == 'C':
            return 'preview-of-C\n' * 5
        return f'preview:{item_id}'

    b = Browser(
        get_children=get_children,
        get_preview=get_preview,
        show_ids='always',
    )
    sys.exit(b.run())


if __name__ == '__main__':
    main()
