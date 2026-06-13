"""Recipe: a streaming-input recipe with a key-bound modal dialog.

Drives the "stdin keeps ingesting while a dialog is open" UI test
(ticket #976). The modal loop services ``_stdin`` events and calls
``browser._pump_stdin()``, so the ``on_stdin`` hook must keep firing
while a dialog blocks the panes.

Two observable channels prove ingestion, independent of screen repaint
(which is suppressed while the dialog owns the screen):

  * each record ``r`` APPENDS a ``r\n`` line to ``$MODAL_STDIN_LOG`` —
    written the instant the hook runs, so the test can confirm a record
    fed DURING the dialog was ingested WHILE the box is still up;
  * each record also upserts a ``rec:<r>`` row, so after the dialog
    closes the repainted tree shows everything that streamed in.

Key ``a`` opens ``ctx.alert`` (a single-button dialog that simply waits
for dismissal — the test feeds stdin while it sits open). The alert
returns nothing; the action does not quit, so the post-close UI is
observable.

Usage:
    browse-tui --run-py modal_stdin_demo.py [--tty PATH]
"""

import os
import sys

from browse_tui import Action, Browser, BrowserConfig, Item


def _logfile():
    return os.environ.get('MODAL_STDIN_LOG', '/tmp/modal_stdin.log')


def main():
    def get_children(parent_id, *, reload=False):
        return [Item(id='ready', title='ready')] if parent_id is None else []

    def on_stdin(ctx, data, *, delimiter, is_eof, errno):
        if is_eof:
            with open(_logfile(), 'a') as f:
                f.write(f'eof:{data}\n')
            ctx.upsert(f'eof:{data}', None, title=f'eof:{data}')
        else:
            with open(_logfile(), 'a') as f:
                f.write(f'{data}\n')
            ctx.upsert(f'rec:{data}', None, title=f'rec:{data}')

    def open_alert(ctx):
        ctx.alert('Heads up', title='Note')
        # No quit — return to the UI so the post-close repaint is observable.

    b = Browser(BrowserConfig(
        get_children=get_children,
        show_preview=False,
        on_stdin=on_stdin,
        stdin_delimiter='\n',
        actions=[Action('a', 'Alert', open_alert, 'cursor')],
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
