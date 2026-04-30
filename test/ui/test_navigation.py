"""UI tests: initial render, j/k navigation, expand/collapse, quit."""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_NAV_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'slow_children.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestNavigation(unittest.TestCase):

    def test_initial_render_three_items(self):
        """A flat 3-item tree renders as #a/#b/#c."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            screen = t.wait_for('#a a')
            screen = t.wait_stable()
            self.assertIn('#a a', screen)
            self.assertIn('#b b', screen)
            self.assertIn('#c c', screen)

    def test_down_arrow_moves_cursor(self):
        """Pressing Down moves the cursor (verified via reverse-video region)."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.wait_stable()
            # In a plain capture the cursor is invisible; capture with
            # ANSI codes shows the [7m reverse-video sequence on the
            # cursor row. We diff before/after to confirm movement.
            before_color = t.capture(colors=True)
            t.send('Down')
            t.wait_stable()
            after_color = t.capture(colors=True)
            self.assertNotEqual(before_color, after_color,
                                'cursor did not move on Down arrow')
            # b should now be on the reverse-video row. The pattern is
            # ESC [ 7 m followed by the row text; we check the cursor
            # marker is on a line that contains "#b".
            self.assertIn('\x1b[7m', after_color)
            # Find the [7m chunk and confirm "b" follows shortly.
            idx = after_color.index('\x1b[7m')
            window = after_color[idx:idx + 80]
            self.assertIn('#b', window,
                          f'cursor row does not contain #b: {window!r}')

    def test_q_quits_with_cancel_code(self):
        """q exits the TUI with the cancel exit code (1)."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\n' | {_BIN} --root-cmd cat ; "
                     f"echo EXIT=$?")
            t.wait_for('#a a')
            t.send('q')
            t.wait_for('EXIT=1', timeout=3.0)

    def test_expand_and_collapse_with_children_cmd(self):
        """Right expands a parent; Left collapses it."""
        with TmuxFixture(cols=80, rows=24) as t:
            # The slow_children recipe returns one parent immediately,
            # then a small fixed list when expanded. A small delay keeps
            # the fetch fast for tests but is still long enough that any
            # latent loading state is observable on slower hosts.
            t.launch(_BIN, '--python', _NAV_RECIPE, '--', '0.05')
            t.wait_for('#parent parent')
            t.send('Right')
            t.wait_for('#alpha', timeout=3.0)
            t.send('Left')
            # Left collapse happens synchronously on the main thread —
            # no worker round-trip. wait_stable settles the cell-diff.
            screen = t.wait_stable()
            self.assertNotIn('#alpha', screen)
            self.assertIn('#parent', screen)
