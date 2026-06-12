"""Recipe driving the output-channel UI tests (ctx.print / quit output).

Two static rows (``alpha`` / ``beta``) plus three keyed actions the
tests poke from outside:

  * ``p`` — ``ctx.print`` one short numbered line (``print-<n>``).
  * ``b`` — ``ctx.print`` one big numbered payload (``big-<n>:`` +
    128 KiB of filler + ``:end-<n>``) — larger than a Linux pipe's
    default capacity, so a consumer that stops reading forces
    backpressure on the channel.
  * ``m`` — ``ctx.flash`` a numbered on-screen marker (``PONG-<n>``);
    the tests use it as a key→repaint round-trip probe that the UI is
    still responding while stdout is stalled or gone.

``enter`` keeps the default print-exit behaviour (quit 0, cursor row id
as the quit output) so the tests can also assert prints-then-quit-output
FIFO order on the stream.

Usage:
    browse-tui --run-py output_channel.py [--tty PATH]
"""

import sys

from browse_tui import Action, Browser, BrowserConfig, Item

_BIG_FILLER = 'x' * (128 * 1024)


def _get_children(_parent_id, *, reload=False):
    return [Item(id='alpha', title='alpha'), Item(id='beta', title='beta')]


def main():
    counters = {'p': 0, 'b': 0, 'm': 0}

    def _print_small(ctx):
        counters['p'] += 1
        ctx.print(f"print-{counters['p']}")

    def _print_big(ctx):
        counters['b'] += 1
        ctx.print(f"big-{counters['b']}:{_BIG_FILLER}:end-{counters['b']}")

    def _mark(ctx):
        counters['m'] += 1
        ctx.flash(f"PONG-{counters['m']}")

    b = Browser(BrowserConfig(
        get_children=_get_children,
        show_preview=False,
        actions=[
            Action('p', 'print a short line', _print_small),
            Action('b', 'print a big payload', _print_big),
            Action('m', 'flash an on-screen marker', _mark),
        ],
    ))
    sys.exit(b.run())


if __name__ == '__main__':
    main()
