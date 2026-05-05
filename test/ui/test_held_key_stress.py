"""Stress tests: holding arrow keys at high repeat rate must not leave
blank rows in the rendered list.

Background -- ticket #77. plan-tui exhibited a similar bug: when the
key stream outpaces the renderer, an intermediate ``render_partial``
might clear a row and then fail to re-emit it before the next key
arrives, leaving a visible gap in the middle of the visible list.

The renderer in ``050-render.py`` repaints every row in the list pane
on each ``render_list`` call (clear_line + write per row), so blanks
shouldn't occur. These tests are the safety net: a regression that
re-introduced partial-row redraws would surface here.
"""

import os
import shutil
import subprocess
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run(['./build-tui.sh'], check=True)


def _list_rows_until_separator(cap):
    """Return the lines of the list pane (above the first separator).

    Returns ``[]`` when no separator is found (defensive — capture
    timing edge cases).
    """
    lines = cap.splitlines()
    for i, ln in enumerate(lines):
        if '─' in ln:  # separator row uses U+2500
            return lines[:i]
    return []


def _assert_no_mid_list_blanks(test, cap):
    """Fail when a non-data row sits between two data rows in the list pane.

    A "data row" is any row with non-whitespace content. The held-key
    bug's signature is a fully blank row sandwiched between two
    populated rows — so detecting it via ``bool(line.strip())`` is
    independent of how items render (no longer keyed on a leading '#'
    sigil since show_ids='auto' suppresses ids when id == title).
    """
    list_rows = _list_rows_until_separator(cap)
    data_idx = [i for i, ln in enumerate(list_rows) if ln.strip()]
    if not data_idx:
        return  # no data on screen — nothing to assert
    first, last = data_idx[0], data_idx[-1]
    blanks = [
        i for i in range(first, last + 1)
        if not list_rows[i].strip()
    ]
    test.assertEqual(
        blanks, [],
        f'blank rows in middle of list pane: rows {blanks}\n'
        f'full capture:\n{cap}',
    )


class TestHeldKeyStress(unittest.TestCase):
    """Hammering arrow keys at high repeat rate must leave a clean list pane."""

    def test_200_down_presses_no_blank_rows(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f'seq 1 100 | {_BIN} --show-ids always --root-cmd cat --no-children-pane')
            t.wait_for('1 1')
            t.wait_stable()
            # Send all 200 in a single tmux send-keys call so they queue
            # in stdin and exercise the read_key/render race.
            t.send(*(['Down'] * 200))
            time.sleep(0.5)  # let renderer settle past the burst
            cap = t.wait_stable()
            _assert_no_mid_list_blanks(self, cap)

    def test_200_alternating_keys_no_blank_rows(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f'seq 1 200 | {_BIN} --show-ids always --root-cmd cat --no-children-pane')
            t.wait_for('1 1')
            t.wait_stable()
            keys = (
                ['Down'] * 30
                + ['PageDown'] * 5
                + ['Up'] * 20
                + ['PageUp'] * 3
                + ['Down'] * 50
                + ['Up'] * 50
            )
            t.send(*keys)
            time.sleep(0.5)
            cap = t.wait_stable()
            _assert_no_mid_list_blanks(self, cap)

    def test_300_down_with_preview_pane_no_blank_rows(self):
        """Same hammering with the preview pane active.

        The preview worker delivers async results between keystrokes
        (each cursor move triggers ``request_preview``). The result
        delivery wakes the main loop via the self-pipe and triggers a
        partial redraw — the failure mode this guards against is that
        an interleaved ``render_partial`` from the worker wake-up paints
        over the list incompletely.
        """
        with TmuxFixture(cols=80, rows=40) as t:
            t.launch('bash', '-c',
                     f'seq 1 300 | {_BIN} --show-ids always --root-cmd cat --no-children-pane '
                     f'--preview-cmd "echo preview \\$TUI_ID"')
            t.wait_for('1 1')
            t.wait_stable()
            t.send(*(['Down'] * 300))
            time.sleep(0.5)
            cap = t.wait_stable()
            _assert_no_mid_list_blanks(self, cap)


if __name__ == '__main__':
    unittest.main()
