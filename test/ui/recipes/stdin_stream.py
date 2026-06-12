"""Recipe driving the streaming-input UI tests (on_stdin; spec §3.4).

One static root row (``ready``) plus an ``on_stdin`` hook in
newline-record mode: every record upserts a ``rec:<record>`` row, and
the final call upserts ``eof:<trailing>:<errno>`` — so the tests can
watch the tree grow on the pty screen while they feed the stdin pipe,
and see the EOF delivery (trailing unterminated record included)
arrive.

With ``STDIN_STREAM_PREREAD=N`` set, the recipe first reads N bytes
from ``sys.stdin.buffer`` BEFORE ``run()`` (the composed
slurp-then-stream shape) and surfaces them as a ``pre:<text>`` row —
the streaming phase must then deliver exactly the remainder, including
the BufferedReader read-ahead the bounded pre-read left behind.

Usage:
    browse-tui --run-py stdin_stream.py [--tty PATH]
"""

import os
import sys

from browse_tui import Browser, BrowserConfig, Item


def main():
    rows = [Item(id='ready', title='ready')]
    pre_n = int(os.environ.get('STDIN_STREAM_PREREAD', '0'))
    if pre_n:
        pre = sys.stdin.buffer.read(pre_n).decode('utf-8', 'replace')
        rows.append(Item(id='pre', title=f'pre:{pre.strip()}'))

    def get_children(parent_id, *, reload=False):
        return list(rows) if parent_id is None else []

    def on_stdin(ctx, data, *, delimiter, is_eof, errno):
        if is_eof:
            ctx.upsert(f'eof:{data}:{errno}', None,
                       title=f'eof:{data}:{errno}')
        else:
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
