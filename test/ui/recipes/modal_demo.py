"""Recipe driving the modal-dialog UI tests (ticket #976).

One key per remaining dialog kind (the picker has its own recipe). Each
action opens its dialog, records the OUTCOME to ``$MODAL_LOG`` (so the
test asserts the resolved result rather than the reverse-video
highlight, which tmux text capture can't see), then RETURNS without
quitting — the recipe stays running so the test can capture the
post-close screen and confirm the regular UI repainted over the dialog
(the cache-poison restore the whole design hinges on).

  * ``c`` — ``ctx.confirm('Delete 3 items?', title='Confirm')``; records
    the resolved label (``Yes`` / ``No``) or ``<cancelled>``.
  * ``m`` — ``ctx.menu([...])`` (anchored at the list cursor by default);
    records the chosen item or ``<cancelled>``.
  * ``i`` — ``ctx.input('Name?')``; records ``val:<text>`` or
    ``<cancelled>``.
  * ``A`` — ``ctx.alert('Heads up', title='Note')``; records ``alert:done``
    once dismissed (alert always returns ``None``).

Each outcome is written as the exact string with NO trailing newline
(the tests assert exact log equality) and the action stays in the UI, so
a test reads the log to learn the outcome and then asserts restore on the
live screen.

Usage:
    browse-tui --run-py modal_demo.py
"""

import os
import sys

from browse_tui import Action, Browser, BrowserConfig, Item


def _logfile():
    return os.environ.get('MODAL_LOG', '/tmp/modal.log')


def _record(text):
    with open(_logfile(), 'w') as f:
        f.write(text)


def _get_children(_, *, reload=False):
    return [
        Item(id='alpha', title='ALPHA-ROW'),
        Item(id='beta', title='BETA-ROW'),
        Item(id='gamma', title='GAMMA-ROW'),
    ]


def _get_preview(_item):
    return 'PREVIEW-LINE-ONE\nPREVIEW-LINE-TWO\nPREVIEW-LINE-THREE'


def _do_confirm(ctx):
    chosen = ctx.confirm('Delete 3 items?', title='Confirm')
    _record('<cancelled>' if chosen is None else chosen)


def _do_menu(ctx):
    chosen = ctx.menu(['Open', 'Rename', 'Delete'])
    _record('<cancelled>' if chosen is None else chosen)


def _do_input(ctx):
    val = ctx.input('Name?')
    _record('<cancelled>' if val is None else f'val:{val}')


def _do_alert(ctx):
    ctx.alert('Heads up', title='Note')
    _record('alert:done')


def main():
    b = Browser(BrowserConfig(
        get_children=_get_children,
        get_preview=_get_preview,
        actions=[
            Action('c', 'Confirm', _do_confirm, 'cursor'),
            Action('m', 'Menu', _do_menu, 'cursor'),
            Action('i', 'Input', _do_input, 'cursor'),
            Action('A', 'Alert', _do_alert, 'cursor'),
        ],
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
