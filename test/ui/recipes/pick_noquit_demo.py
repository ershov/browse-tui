"""Repro recipe: pick() then RETURN to the UI (no quit).

Press 's' -> picker; on select/cancel the action returns without
quitting so we can capture the post-pick screen and confirm the
overlay was repainted away.
"""

import sys

from browse_tui import Action, Browser, BrowserConfig, Item


def _get_children(_, *, reload=False):
    return [
        Item(id='alpha', title='ALPHA-ROW'),
        Item(id='beta', title='BETA-ROW'),
        Item(id='gamma', title='GAMMA-ROW'),
    ]


def _get_preview(_item):
    return 'PREVIEW-LINE-ONE\nPREVIEW-LINE-TWO\nPREVIEW-LINE-THREE'


def _pick_status(ctx):
    ctx.pick('Status', ['open', 'in-progress', 'done', 'wontfix'])
    # No quit — return to the UI.


def main():
    b = Browser(BrowserConfig(
        get_children=_get_children,
        get_preview=_get_preview,
        actions=[Action('s', 'Status', _pick_status, 'cursor')],
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
