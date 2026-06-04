"""UI tests for ctx.pick — fzf-style sub-picker (ticket #20).

Drives the picker end-to-end: launches the pick_demo recipe in a real
terminal under tmux, presses the bound key 's' to invoke the picker,
then exercises filter / cursor / select / cancel paths. The recipe
writes the chosen string (or '<cancelled>') to a tempfile and quits;
each test polls that file to confirm the picker returned the expected
value.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'pick_demo.py')
_NOQUIT_RECIPE = os.path.join(
    _REPO, 'test', 'ui', 'recipes', 'pick_noquit_demo.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _read_log_when_ready(path, timeout=3.0):
    """Poll ``path`` until it exists with non-empty content. Returns the text."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if content:
                return content
        time.sleep(0.03)
    raise AssertionError(
        f'log file {path!r} not populated within {timeout}s')


class TestPick(unittest.TestCase):

    def test_pick_select_with_enter(self):
        """Type to filter, press enter, recipe records the matching option."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                # Wait for the picker prompt to appear on the info bar.
                t.wait_for('Status>')
                # 'in' filters to 'in-progress'; enter selects.
                t.send('i')
                t.send('n')
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'in-progress')

    def test_pick_cancel_with_esc(self):
        """Esc cancels the picker; recipe records '<cancelled>'."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('Status>')
                t.send('Escape')
                content = _read_log_when_ready(log)
            self.assertEqual(content, '<cancelled>')

    def test_pick_filter_narrows_to_unique_match(self):
        """Type 'do' — only 'done' matches; Enter selects it.

        The pick recipe offers ['open', 'in-progress', 'done', 'wontfix'].
        'do' is a substring of 'done' only (not 'open' / 'in-progress' /
        'wontfix'), so the picker filters to one row and Enter picks it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('Status>')
                t.type('do')
                # Wait for query echo so subsequent Enter applies to the
                # filtered list (not the unfiltered one).
                t.wait_for('Status> do', timeout=2.0)
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'done')

    def test_pick_navigate_with_arrows(self):
        """Arrow keys reposition the picker cursor before enter."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('Status>')
                # Down twice from 'open' lands on 'done' (3rd item).
                t.send('Down')
                t.send('Down')
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'done')


class TestPickRedrawOnExit(unittest.TestCase):
    """Regression: after pick() returns to the UI, the overlay repaints away.

    Ticket #719. The picker draws its prompt + options directly to the
    screen, bypassing the per-pane row cache. When the picking action
    returns to the UI (instead of quitting), the next render must fully
    repaint the regular UI over the overlay. Before the fix the renderer
    cache-hit every unchanged row and emitted nothing, so the ``Status>``
    prompt and the option list (notably the last option, on an otherwise
    blank preview row) stayed on screen.

    The no-quit recipe stays running so we can capture the post-pick
    screen — the quit-after-pick path can't show the bug because teardown
    leaves the alternate screen entirely.
    """

    def _assert_ui_restored(self, t):
        cap = t.wait_for('PREVIEW-LINE-ONE', timeout=3.0)
        # Picker chrome must be gone.
        self.assertNotIn('Status>', cap,
                         f'picker prompt left on screen:\n{cap}')
        for opt in ('open', 'in-progress', 'done', 'wontfix'):
            self.assertNotIn(opt, cap,
                             f'picker option {opt!r} left on screen:\n{cap}')
        # Regular UI must be back: rows, preview content, info-bar hints.
        self.assertIn('ALPHA-ROW', cap)
        self.assertIn('PREVIEW-LINE-THREE', cap)
        self.assertIn('q:quit', cap)

    def test_overlay_gone_after_cancel(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _NOQUIT_RECIPE)
            t.wait_for('ALPHA-ROW')
            t.send('s')
            t.wait_for('Status>')
            t.send('Escape')
            self._assert_ui_restored(t)

    def test_overlay_gone_after_select(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _NOQUIT_RECIPE)
            t.wait_for('ALPHA-ROW')
            t.send('s')
            t.wait_for('Status>')
            t.send('Enter')
            self._assert_ui_restored(t)


if __name__ == '__main__':
    unittest.main()
