"""UI tests: search-mode prompt enter / type / escape.

Phase 1's search support is minimal — '/' enters search mode, characters
extend the query and show in the info bar; Esc cancels. Real
fragment-matching is phase 2 (#22).
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestSearch(unittest.TestCase):

    def test_slash_enters_search_mode_and_shows_prompt(self):
        """/ then text shows the query in the info bar; Esc clears it."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'foo\\nbar\\nbaz\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#foo foo')
            t.send('/')
            t.type('bar')
            t.wait_for('/bar', timeout=2.0)
            t.send('Escape')
            # After Escape, the prompt area returns to default hint text.
            t.wait_for('/:search', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('/bar', screen)

    def test_search_query_extends_with_each_keystroke(self):
        """Each printable key extends the query; backspace trims it."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'foo\\nbar\\nbaz\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#foo foo')
            t.send('/')
            t.type('xy')
            t.wait_for('/xy', timeout=2.0)
            t.send('BSpace')
            # Query should be just 'x' now — '/x' present, '/xy' gone.
            t.wait_for('/x', timeout=2.0)
            screen = t.capture()
            # '/x' must be present without 'y' immediately after.
            self.assertIn('/x', screen)
            self.assertNotIn('/xy', screen)
