"""UI tests: drill into a scope (alt-down), pop back (alt-up), crumb display.

The recipe used here is a hierarchical ``--children-cmd`` script that
serves three levels:

  ROOT   -> A (has_children), B (has_children)
  A      -> a1, a2
  B      -> b1

Drill-down via M-Down (Alt+Down) should hide siblings and show only the
scope item plus its children. Drill-up via M-Up should restore the root
view and place the cursor on the item we left from.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# A self-contained children-cmd that branches on $TUI_ID. Embedded into
# the launch line via shlex-quoted argv. The TSV format is:
#   id\ttitle\thas_children
# The CLI's --fields plumbs has_children through.
_CHILDREN_CMD = (
    'case "$TUI_ID" in '
    '"") printf "A\\tA\\t1\\nB\\tB\\t1\\n" ;; '
    'A) printf "a1\\ta1\\t0\\na2\\ta2\\t0\\n" ;; '
    'B) printf "b1\\tb1\\t0\\n" ;; '
    'esac'
)


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestScope(unittest.TestCase):

    def _launch(self, t):
        """Launch browse-tui with the hierarchical children-cmd.

        The grid pane is disabled here so the assertions can talk about
        the list pane in isolation — otherwise the cursor item's
        children show up in the grid even when the list pane has them
        collapsed, breaking the ``not in screen`` assertions.
        """
        t.launch(
            _BIN,
            '--children-cmd', _CHILDREN_CMD,
            '--fields', 'id,title,has_children',
            '--no-children-pane',
            '--show-ids', 'always',
            '--scope-crumb',
        )

    def test_alt_down_drills_into_cursor(self):
        """M-Down drills into the cursor item; siblings become hidden."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._launch(t)
            t.wait_for('A A')
            t.wait_stable()
            # Cursor starts on A. Drill in.
            t.send('M-Down')
            # After drilling, A's children should appear; B should not.
            t.wait_for('a1 a1', timeout=3.0)
            screen = t.wait_stable()
            self.assertIn('A A', screen)
            self.assertIn('a1 a1', screen)
            self.assertIn('a2 a2', screen)
            self.assertNotIn('B B', screen)
            self.assertNotIn('b1 b1', screen)
            t.send('q')

    def test_alt_up_returns_to_root(self):
        """M-Up after drilling into A returns to the root view."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._launch(t)
            t.wait_for('A A')
            t.wait_stable()
            t.send('M-Down')
            t.wait_for('a1 a1', timeout=3.0)
            t.wait_stable()
            t.send('M-Up')
            # Back at root: B is visible again, a1 is not (collapsed).
            t.wait_for('B B', timeout=3.0)
            screen = t.wait_stable()
            self.assertIn('A A', screen)
            self.assertIn('B B', screen)
            self.assertNotIn('a1 a1', screen)
            t.send('q')

    def test_nested_scope_drill_in_and_out_twice(self):
        """Two M-Down drills then two M-Up climbs restore the root view.

        Uses a three-level hierarchy: ROOT → A (branch) → a1 (branch) →
        a1x (leaf). After drilling into A then into a1, alt-up twice
        should return us all the way back to the root with B visible
        again.
        """
        nested_cmd = (
            'case "$TUI_ID" in '
            '"") printf "A\\tA\\t1\\nB\\tB\\t1\\n" ;; '
            'A) printf "a1\\ta1\\t1\\na2\\ta2\\t0\\n" ;; '
            'a1) printf "a1x\\ta1x\\t0\\na1y\\ta1y\\t0\\n" ;; '
            'B) printf "b1\\tb1\\t0\\n" ;; '
            'esac'
        )
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(
                _BIN,
                '--children-cmd', nested_cmd,
                '--fields', 'id,title,has_children',
                '--no-children-pane',
                '--show-ids', 'always',
                '--scope-crumb',
            )
            t.wait_for('A A')
            t.wait_stable()
            # Drill into A.
            t.send('M-Down')
            t.wait_for('a1 a1', timeout=3.0)
            t.wait_stable()
            # Position cursor on a1 (first child) — drill-in uses cursor.
            t.send('Down')
            t.wait_stable()
            # Drill into a1: scope=a1, children a1x / a1y appear.
            t.send('M-Down')
            t.wait_for('a1x a1x', timeout=3.0)
            screen = t.wait_stable()
            # Crumb shows the full chain. Renderer formats nested
            # crumbs as '▸ A ▸ a1' (or similar) in the info bar.
            self.assertIn('▸ A', screen)
            self.assertIn('a1', screen)
            self.assertNotIn('B B', screen)
            self.assertNotIn('a2 a2', screen)
            # Pop one level: scope=A, a1's children hidden again,
            # siblings (a2) visible.
            t.send('M-Up')
            t.wait_for('a2 a2', timeout=3.0)
            screen = t.wait_stable()
            self.assertIn('A A', screen)
            self.assertIn('a1 a1', screen)
            self.assertIn('a2 a2', screen)
            self.assertNotIn('a1x a1x', screen)
            self.assertNotIn('B B', screen)
            # Pop the second level: back at root, B visible.
            t.send('M-Up')
            t.wait_for('B B', timeout=3.0)
            screen = t.wait_stable()
            self.assertIn('A A', screen)
            self.assertIn('B B', screen)
            self.assertNotIn('a1 a1', screen)
            t.send('q')

    def test_scope_crumb_appears_in_info_bar(self):
        """The info bar shows a '▸ A' crumb after drilling into A."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._launch(t)
            t.wait_for('A A')
            t.wait_stable()
            t.send('M-Down')
            t.wait_for('a1 a1', timeout=3.0)
            screen = t.wait_stable()
            # Crumb glyph + id should appear somewhere on the screen
            # (the renderer puts it on the info-bar row between the
            # selection count and the spacer).
            self.assertIn('▸ A', screen)
            t.send('q')


if __name__ == '__main__':
    unittest.main()
