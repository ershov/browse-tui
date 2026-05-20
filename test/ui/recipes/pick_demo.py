"""Recipe used by the pick UI tests (ticket #20).

Registers a single 's' action that pops the fzf-style picker, writes
the chosen string (or '<cancelled>') to a log file, then exits
cleanly so the test can read the log.

The exit-on-pick keeps the test cycle fast — UI tests just have to wait
for the program to terminate (the bash shell prompt reappears) and
then read the log file.

The log path comes from the first positional ``--`` argument; falls
back to ``$PICK_LOG`` and finally to ``/tmp/pick.log`` so the recipe
is also runnable by hand.

Usage:
    ./browse-tui --run-py pick_demo.py -- /tmp/x.log
"""

import os
import sys

from browse_tui import Action, Browser, BrowserConfig, Item


def _logfile():
    # Args after `--` arrive as sys.argv[1:] (cli.py forwards them).
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    return os.environ.get('PICK_LOG', '/tmp/pick.log')


def _get_children(_, *, reload=False):
    return [Item(id='item', title='one')]


def _pick_status(ctx):
    chosen = ctx.pick('Status', ['open', 'in-progress', 'done', 'wontfix'])
    with open(_logfile(), 'w') as f:
        f.write('<cancelled>' if chosen is None else chosen)
    ctx.quit()


def main():
    b = Browser(BrowserConfig(
        get_children=_get_children,
        actions=[Action('s', 'Status', _pick_status, 'cursor')],
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
