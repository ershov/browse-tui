"""UI test: children pane's first-ever appearance stale cells (#961).

In vertical layout, when the cursor starts on a childless node (so the
children column was never shown) and then moves to a node with
children, the column appears for the FIRST time over cells the preview
painted and vacated. The buggy build leaves the preview's content in
the column's trailing cells because the children PaneCache was never
stamped with the disappeared-pane sentinel, so ``end_row`` takes the
``prev_rect is None`` "first paint, no padding" branch.

This is the trailing-whitespace/stale-cell regression the existing
``test_children_pane_redraw`` tests miss: those launch on a node that
already HAS children (so the column is shown from frame 1 over a blank
screen) and assert on substrings that survive tmux's trailing-space
trimming, whereas the stale cells here sit in the column interior.
"""

import os
import re
import shutil
import subprocess
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes',
                       'children_first_appearance.py')

_SGR = re.compile(r'\x1b\[[0-9;]*m')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _capture_colored(t: TmuxFixture) -> str:
    # Capture WITH escape sequences so cells with a non-default state
    # survive (plain ``-p`` trims trailing whitespace, hiding the bug).
    return t.tmux('capture-pane', '-t', 'main', '-p', '-e').stdout


def _separator_columns(plain: str):
    body = plain.splitlines()[:-1]
    counts = {}
    for line in body:
        for col, ch in enumerate(line):
            if ch == '│':
                counts[col] = counts.get(col, 0) + 1
    threshold = max(3, len(body) // 4)
    return sorted(c for c, n in counts.items() if n >= threshold)


def _wait_separator_count(t: TmuxFixture, n: int, timeout=3.0):
    deadline = time.time() + timeout
    cols = []
    while time.time() < deadline:
        cols = _separator_columns(_SGR.sub('', _capture_colored(t)))
        if len(cols) == n:
            return cols
        time.sleep(0.05)
    return cols


class TestChildrenFirstAppearance(unittest.TestCase):
    def test_first_appearance_in_vertical_has_no_stale_preview_cells(self):
        """Childless → has-children: the children column must be clean.

        The column lives between the first two vertical separators. On
        the buggy build its interior holds the preview's ``ZZZZ`` marker
        after the move; the fix clears it.
        """
        with TmuxFixture(cols=240, rows=40) as t:
            t.launch(_BIN, '--run-py', _RECIPE, '--split-type=v')
            t.wait_for('X X')
            t.wait_for('P P')
            t.send('M-1')   # force the true 3-column vertical layout
            # On X (childless) the children column is absent → one
            # vertical separator (list│preview).
            self.assertEqual(
                len(_wait_separator_count(t, 1)), 1,
                'expected a single separator (no children column) on the '
                'childless node')

            # Move to P → children column appears for the first time.
            t.send('Down')
            t.wait_for('p1', timeout=3.0)
            t.wait_for('preview-of-P', timeout=3.0)
            cols = _wait_separator_count(t, 2)
            self.assertEqual(
                len(cols), 2,
                f'expected list│children│preview (two separators) on the '
                f'has-children node; got separators at {cols}')

            # Let the screen settle, then inspect the children-column
            # interior (between the first two separators).
            t.wait_stable(timeout=3.0)
            plain = _SGR.sub('', _capture_colored(t))
            lo, hi = cols[0] + 1, cols[1]
            column = [line[lo:hi] for line in plain.splitlines()]

            self.assertTrue(
                any('p1' in row for row in column),
                f'children content missing from the column:\n'
                + '\n'.join(column))
            stale = [(i, row) for i, row in enumerate(column) if 'ZZZZ' in row]
            self.assertFalse(
                stale,
                'stale preview cells (ZZZZ) left in the children column on '
                'its first appearance — end_row skipped padding because the '
                f'PaneCache was never sentinel-stamped:\n{stale}')


if __name__ == '__main__':
    unittest.main()
