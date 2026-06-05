"""Recipe: a flat list with meta rows at the edges and a short middle run.

Tree shape (visible-list order, depth 0):

    ── top ──     (meta)
    a             (normal)
    ── m1 ──      (meta)   ┐ short meta run
    ── m2 ──      (meta)   ┘
    b             (normal)
    ── bot ──     (meta)

Exercises the unified cursor-skip resolver end-to-end through a real
binary: Down from ``a`` skips the two-row middle run onto ``b``; Home
lands on ``a`` (not the top meta divider); End lands on ``b`` (not the
bottom meta divider).

Usage:
    browse-tui --run-py meta_rows.py
"""

import sys

from browse_tui import Browser, BrowserConfig, Item


def main():
    children = {
        '': [
            Item(id='top', title='-- top --', meta=True),
            Item(id='a', title='a'),
            Item(id='m1', title='-- m1 --', meta=True),
            Item(id='m2', title='-- m2 --', meta=True),
            Item(id='b', title='b'),
            Item(id='bot', title='-- bot --', meta=True),
        ],
    }

    def get_children(parent_id, *, reload=False):
        return list(children.get(parent_id or '', []))

    b = Browser(BrowserConfig(
        get_children=get_children,
        show_preview=True,
        # Force the id segment on every row so UI tests can assert on a
        # stable 'id title' shape per row.
        show_ids='always',
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
