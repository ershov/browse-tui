"""UI test: resize the tmux window mid-session and confirm the TUI reflows."""

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


class TestResize(unittest.TestCase):

    def test_resize_reflows_and_does_not_crash(self):
        """Shrinking the window from 120x40 to 60x20 reflows without crash."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.resize(60, 20)
            # SIGWINCH propagation + redraw is asynchronous; wait_stable
            # blocks until two consecutive captures match — i.e. the
            # render has settled at the new dimensions.
            screen = t.wait_stable(timeout=3.0)
            self.assertIn('#a a', screen)
            # Every line must fit the new width (with a small slack for
            # the trailing-pad behaviour on some terminals).
            longest = max(len(line) for line in screen.splitlines())
            self.assertLessEqual(longest, 65,
                                 f'longest line {longest} > 65 after resize')
