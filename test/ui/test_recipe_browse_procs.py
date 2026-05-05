"""UI tests for the ``recipes/browse-procs`` process-tree recipe.

The recipe shells out to ``ps -eo …`` and reads ``/proc/<pid>/status``;
both are universally available on Linux and don't need any special
fixture setup. We just launch under tmux and assert that the root
process (PID 1) renders, plus that the preview pane shows the expected
``/proc/1/status`` snippet.

The shebang ``#!/usr/bin/env -S browse-tui --run-py`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --run-py recipes/browse-procs`` directly so the tests
are independent of the user's PATH.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-procs')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not shutil.which('ps'):
        raise unittest.SkipTest('ps not available; browse-procs tests skipped')
    if not os.path.isdir('/proc/1'):
        raise unittest.SkipTest('/proc not available; browse-procs tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestBrowseProcs(unittest.TestCase):

    def test_lists_root_process(self):
        """Top-level renders PID 1 and quits cleanly."""
        with TmuxFixture(cols=120, rows=30) as t:
            t.launch(_BIN, '--run-py', _RECIPE)
            # PID 1 always exists on Linux. ``pid=1`` is in the tag we
            # render for every row, so it's a uniquely cheap match.
            t.wait_for('pid=1', timeout=5.0)
            t.send('q')

    def test_preview_shows_proc_status(self):
        """The preview pane renders /proc/<pid>/status content."""
        with TmuxFixture(cols=120, rows=30) as t:
            t.launch(_BIN, '--run-py', _RECIPE)
            t.wait_for('pid=1', timeout=5.0)
            # /proc/<pid>/status always begins with a ``Name:`` line —
            # the cheapest signal that the preview worker fired.
            t.wait_for('Name:', timeout=5.0)
            t.send('q')


if __name__ == '__main__':
    unittest.main()
