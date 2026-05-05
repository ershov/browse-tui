"""UI test: resize the tmux window mid-session and confirm the TUI reflows."""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GRID_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'children_grid.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestResize(unittest.TestCase):

    def test_resize_reflows_and_does_not_crash(self):
        """Shrinking the window from 120x40 to 60x20 reflows without crash."""
        with TmuxFixture(cols=120, rows=40) as t:
            # --show-ids always pins the row layout so 'a a' is a stable
            # substring even though the input has id == title.
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('a a')
            t.resize(60, 20)
            # SIGWINCH propagation + redraw is asynchronous; wait_stable
            # blocks until two consecutive captures match — i.e. the
            # render has settled at the new dimensions.
            screen = t.wait_stable(timeout=3.0)
            self.assertIn('a a', screen)
            # Every line must fit the new width (with a small slack for
            # the trailing-pad behaviour on some terminals).
            longest = max(len(line) for line in screen.splitlines())
            self.assertLessEqual(longest, 65,
                                 f'longest line {longest} > 65 after resize')

    def test_resize_with_expanded_tree_and_grid(self):
        """Resize works while the tree is hierarchical and a branch is expanded.

        Drives the children_grid recipe (parent → a1, a2; leaf):
          * Launch at 120x40 with the grid pane visible.
          * Cursor lands on 'parent' — the grid populates with a1 / a2.
          * Shrink to 60x20 — both list and grid panes must keep
            rendering legibly without crashing the renderer.
          * Restore to 120x40 — full expanded layout reappears.
        """
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _GRID_RECIPE)
            # The recipe pre-renders 'parent' on first row; flush async
            # children-fetch into the screen with a redraw. The recipe
            # pins show_ids='always' so each row has its id segment.
            t.wait_for('parent parent')
            t.redraw()
            t.wait_for('a1', timeout=3.0)
            wide = t.wait_stable()
            # Sanity: the grid is up — Children separator + both kids.
            self.assertIn('Children', wide)
            self.assertIn('a1', wide)
            self.assertIn('a2', wide)

            # Shrink. SIGWINCH triggers reflow; renderer must handle the
            # narrower geometry without raising or producing oversized
            # lines. With only 60 cols the renderer may collapse the
            # grid pane (insufficient width) — that is acceptable; we
            # assert only that nothing crashes and lines fit.
            t.resize(60, 20)
            narrow = t.wait_stable(timeout=3.0)
            self.assertIn('parent', narrow,
                          f'cursor item disappeared after shrink:\n{narrow}')
            longest = max(len(line) for line in narrow.splitlines())
            self.assertLessEqual(longest, 65,
                                 f'longest line {longest} > 65 after shrink')

            # Restore. The grid should come back; cursor must stay on
            # 'parent' (and a1/a2 visible again).
            t.resize(120, 40)
            t.redraw()
            restored = t.wait_stable(timeout=3.0)
            self.assertIn('parent parent', restored)
            self.assertIn('a1', restored)
            self.assertIn('a2', restored)
            self.assertIn('Children', restored)
            t.send('q')
