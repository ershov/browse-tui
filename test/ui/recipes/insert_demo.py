"""Recipe used by the insert-mode UI tests (ticket #21).

Registers a single 'c' action that enters insert mode via ``ctx.insert``;
on confirm, writes the resolved ``(relation, dest_id)`` to a log file
and quits. On cancel (esc), writes ``<cancelled>`` and quits. The
exit-on-result keeps the test cycle fast.

The log path comes from the first positional ``--`` argument; falls
back to ``$INSERT_LOG`` and finally to ``/tmp/insert.log`` so the
recipe is also runnable by hand.

Usage:
    ./browse-tui --python insert_demo.py -- /tmp/x.log
"""

import os
import sys

from browse_tui import Action, Browser, Item


def _logfile():
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    return os.environ.get('INSERT_LOG', '/tmp/insert.log')


def _get_children(parent):
    if parent is None:
        return [
            Item(id='a', has_children=True),
            Item(id='b'),
            Item(id='c'),
        ]
    if parent == 'a':
        return [Item(id='a1'), Item(id='a2')]
    return []


def _do_insert(ctx):
    def _on_confirm(relation, dest_id):
        with open(_logfile(), 'w') as f:
            f.write('{}:{}'.format(relation, dest_id))
        ctx.quit()
    ctx.insert('create', _on_confirm)


def _confirm_or_cancel(_ctx):
    """Spare 'x' action: writes <cancelled> to the log + quits.

    Lets the cancel-flow test detect that the user actually returned to
    nav mode (i.e. insert mode exited cleanly) — pressing 'x' in nav
    mode logs and quits; pressing 'x' inside insert mode would be
    swallowed (insert mode ignores unmapped keys).
    """
    with open(_logfile(), 'w') as f:
        f.write('<cancelled>')
    _ctx.quit()


def main():
    b = Browser(
        get_children=_get_children,
        actions=[
            Action('c', 'Create', _do_insert, 'cursor'),
            Action('x', 'Cancel-marker', _confirm_or_cancel, 'none'),
        ],
    )
    sys.exit(b.run())


if __name__ == '__main__':
    main()
