"""Recipe driving the full-duplex content-channel UI tests (spec §3.2 + §3.4).

Exercises BOTH std content channels in one session: the ``on_stdin``
hook reads records flowing IN while ``ctx.print`` writes results OUT —
so the select loop services fd 0 (read-set) and fd 1 (write-set) in the
same run.

One static root row (``ready``) plus a newline-record ``on_stdin`` hook:

  * each record ``r`` -> ``ctx.print(f'out:{r}')`` on the stdout channel
    AND ``ctx.upsert('rec:<r>')`` so the row also shows on the pty screen
    (the tests watch the UI device to confirm the stream is being
    serviced while output drains);
  * the final EOF call -> ``ctx.print('out:eof:<trailing>')`` AND an
    ``eof:<trailing>:<errno>`` row, so the output stream carries an
    unambiguous end marker after the last record (proving prints and
    stdin stay in lockstep to EOF) and the tests can also wait on the
    EOF delivery via the pty screen.

``enter`` keeps the default print-exit (quit 0, cursor row id as the
quit output), so the stream reads back the echoed records, then the
EOF marker, then the quit output — strict FIFO across the full session.

Usage:
    browse-tui --run-py stdin_duplex.py [--tty PATH]
"""

import sys

from browse_tui import Browser, BrowserConfig, Item


def main():
    def get_children(parent_id, *, reload=False):
        return [Item(id='ready', title='ready')] if parent_id is None else []

    def on_stdin(ctx, data, *, delimiter, is_eof, errno):
        if is_eof:
            ctx.print(f'out:eof:{data}')
            ctx.upsert(f'eof:{data}:{errno}', None, title=f'eof:{data}:{errno}')
        else:
            ctx.print(f'out:{data}')
            ctx.upsert(f'rec:{data}', None, title=f'rec:{data}')

    b = Browser(BrowserConfig(
        get_children=get_children,
        show_preview=False,
        on_stdin=on_stdin,
        stdin_delimiter='\n',
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
