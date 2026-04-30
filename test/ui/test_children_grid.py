"""UI tests for the children-grid pane (ticket #19)."""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'children_grid.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestChildrenGrid(unittest.TestCase):
    """Verify the grid pane appears/disappears based on cursor + flag."""

    def test_grid_appears_when_cursor_on_branch(self):
        """Cursor on the 'parent' branch reveals its children in the grid."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE)
            t.wait_for('#parent parent')
            # The cursor lands on 'parent' (the first row) at startup.
            # The grid pane should populate with a1 and a2 once the
            # children fetch completes. Force a redraw to flush any
            # late worker delivery into the screen.
            t.redraw()
            screen = t.wait_for('#a1', timeout=3.0)
            self.assertIn('#a1', screen)
            self.assertIn('#a2', screen)
            # The "Children" label sits on the grid's separator.
            self.assertIn('Children', screen)

    def test_grid_hidden_for_leaf(self):
        """Cursor on a leaf hides the grid; the preview takes the space."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE)
            t.wait_for('#parent parent')
            t.redraw()
            t.wait_for('#a1', timeout=3.0)
            # Move cursor to the second row ('leaf').
            t.send('Down')
            t.redraw()
            # The grid should disappear — no Children label visible.
            screen = t.wait_stable()
            self.assertNotIn('#a1', screen)
            self.assertNotIn('#a2', screen)
            # 'Children' separator label gone.
            self.assertNotIn('Children', screen)

    def test_no_children_pane_flag_hides_it(self):
        """``--no-children-pane`` keeps the grid hidden even on a branch."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE, '--', '--no-children-pane')
            t.wait_for('#parent parent')
            t.redraw()
            t.wait_stable()
            screen = t.capture()
            # Grid is suppressed: no Children separator, no a1/a2.
            self.assertNotIn('Children', screen)
            self.assertNotIn('#a1', screen)
            self.assertNotIn('#a2', screen)


if __name__ == '__main__':
    unittest.main()
