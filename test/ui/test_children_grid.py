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
            t.wait_for('parent parent')
            # The cursor lands on 'parent' (the first row) at startup.
            # The grid pane should populate with a1 and a2 once the
            # children fetch completes. Force a redraw to flush any
            # late worker delivery into the screen.
            t.redraw()
            screen = t.wait_for('a1 [running] alpha', timeout=3.0)
            self.assertIn('a1 [running] alpha', screen)
            self.assertIn('a2 bravo', screen)
            # The "Children" label sits on the grid's separator.
            self.assertIn('Children', screen)

    def test_grid_hidden_for_leaf(self):
        """Cursor on a leaf hides the grid; the preview takes the space."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE)
            t.wait_for('parent parent')
            t.redraw()
            t.wait_for('a1 [running] alpha', timeout=3.0)
            # Move cursor to the second row ('leaf').
            t.send('Down')
            t.redraw()
            # The grid should disappear — no Children label visible.
            screen = t.wait_stable()
            self.assertNotIn('a1 [running] alpha', screen)
            self.assertNotIn('a2 bravo', screen)
            # 'Children' separator label gone.
            self.assertNotIn('Children', screen)

    def test_grid_renders_tag_colors(self):
        """Grid uses styled tags — a1 has tag='running' tag_style='green'.

        Capture with ANSI escape passthrough (``-e``) and confirm the
        renderer emitted a 256-colour fg-2 (green) SGR sequence somewhere
        on screen and that the tag text 'running' is present.
        """
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE)
            t.wait_for('parent parent')
            t.redraw()
            t.wait_for('a1 [running] alpha', timeout=3.0)
            t.wait_stable()
            screen = t.capture(colors=True)
            # The 'running' tag must be visible in the grid.
            self.assertIn('running', screen,
                          f'tag text not present:\n{screen}')
            # set_style emits SGR 38;5;<n> for 256-colour fg. green = 2.
            self.assertIn('\x1b[', screen,
                          'no ANSI escape sequences in colour capture')
            self.assertRegex(
                screen, r'\x1b\[[0-9;]*38;5;2[m;]',
                f'green (256-colour fg=2) not emitted:\n{screen!r}')

    def test_no_children_pane_flag_hides_it(self):
        """``--no-children-pane`` keeps the grid hidden even on a branch."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE, '--', '--no-children-pane')
            t.wait_for('parent parent')
            t.redraw()
            t.wait_stable()
            screen = t.capture()
            # Grid is suppressed: no Children separator, no a1/a2.
            self.assertNotIn('Children', screen)
            self.assertNotIn('a1 [running] alpha', screen)
            self.assertNotIn('a2 bravo', screen)


if __name__ == '__main__':
    unittest.main()
