"""Recipe: a small hierarchy used to exercise the children-grid pane.

Tree shape:

    parent  (has_children, with two leaf children)
      a1
      a2
    leaf

When the cursor lands on ``parent`` the children-grid pane should
populate with ``a1`` and ``a2`` (and a tag in green for ``a1``).
When the cursor lands on ``leaf`` the grid should disappear.

Usage:
    browse-tui --python children_grid.py
"""

import sys

from browse_tui import Browser, Item


def main():
    children = {
        '': [
            Item(id='parent', title='parent', has_children=True),
            Item(id='leaf', title='leaf'),
        ],
        'parent': [
            Item(id='a1', title='alpha', tag='running', tag_style='green'),
            Item(id='a2', title='bravo'),
        ],
    }

    def get_children(parent_id):
        return list(children.get(parent_id or '', []))

    show_children_pane = '--no-children-pane' not in sys.argv[1:]
    b = Browser(
        get_children=get_children,
        show_children_pane=show_children_pane,
    )
    sys.exit(b.run())


if __name__ == '__main__':
    main()
