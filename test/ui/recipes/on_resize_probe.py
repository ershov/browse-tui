"""Recipe: probe the broadened ``on_resize`` hook end-to-end (#834).

Registers ``on_resize`` and makes every fire OBSERVABLE in the preview,
so a tmux test can assert the hook fires (and the recipe's reaction
self-completes) on a layout change with NO extra user input — the exact
gap that #828 shipped: the fire lagged into an un-woken loop iteration,
so the preview stayed stale until the next keypress.

Each ``on_resize`` fire bumps a counter and drops the preview cache (the
production recipe pattern). ``get_preview`` reports the live fire count
and the current ``preview_width``, so a fresh capture after a resize /
split toggle shows an INCREMENTED count + the NEW width once — and only
once — the wake chain (fire -> drop_preview_cache -> refetch -> repaint)
has run to completion on its own.

Usage:
    browse-tui --run-py on_resize_probe.py
"""

import sys

from browse_tui import Browser, BrowserConfig, Item

# Number of times ``on_resize`` has fired. A list cell so the closures
# below mutate it without a ``global`` dance.
_FIRES = [0]
_BROWSER = None


def get_preview(node_id):
    """Report the live fire count + preview width.

    Reads ``_BROWSER.preview_width`` live (the production ``get_preview``
    pattern), so the rendered text changes whenever a layout change has
    actually re-fetched at the new width — observable as the ``W=`` value.
    """
    width = (_BROWSER.preview_width if _BROWSER else 0) or 0
    return f'FIRES={_FIRES[0]} W={width}'


def _on_resize(ctx, cols, rows):
    """Bump the fire counter, then drop the preview cache.

    ``drop_preview_cache`` refetches the cursor preview, so the new
    ``FIRES=`` / ``W=`` lands on screen via the framework's own wake
    chain — no user keystroke required if ``on_resize`` self-completes.
    """
    _FIRES[0] += 1
    ctx.drop_preview_cache()


def main():
    global _BROWSER
    items = [
        Item(id='only', title='only-row'),
    ]

    b = Browser(BrowserConfig(
        get_children=lambda parent_id, *, reload=False: (
            list(items) if not parent_id else []),
        get_preview=get_preview,
        on_resize=_on_resize,
        show_preview=True,
        show_ids='always',
    ))
    _BROWSER = b
    sys.exit(b.run())


if __name__ == '__main__':
    main()
