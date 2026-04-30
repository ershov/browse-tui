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
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('#item')
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
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('#item')
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
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('#item')
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
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('#item')
                t.send('s')
                t.wait_for('Status>')
                # Down twice from 'open' lands on 'done' (3rd item).
                t.send('Down')
                t.send('Down')
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'done')


if __name__ == '__main__':
    unittest.main()
