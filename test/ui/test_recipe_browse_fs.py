"""UI tests for the ``recipes/browse-fs`` filesystem-browser recipe.

Drives the recipe under tmux against a temp directory, verifying that
entries render and that the background mtime watcher picks up an
external file creation.

The shebang ``#!/usr/bin/env -S browse-tui --run-py`` requires the
binary to be on PATH, which is fragile in tests; instead we invoke
``./browse-tui --run-py recipes/browse-fs -- <tmp>`` directly so the
test is independent of the user's PATH.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BIN = os.path.join(_REPO, 'browse-tui')
_RECIPE = os.path.join(_REPO, 'recipes', 'browse-fs')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestBrowseFs(unittest.TestCase):

    def test_lists_directory_entries(self):
        """The recipe enumerates dirs and files with expected styling."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'sub'))
            with open(os.path.join(tmp, 'a.txt'), 'w') as f:
                f.write('hello')
            with open(os.path.join(tmp, 'b.txt'), 'w') as f:
                f.write('world')
            with TmuxFixture(cols=120, rows=30) as t:
                t.launch(_BIN, '--run-py', _RECIPE, tmp)
                t.wait_for('a.txt')
                t.wait_for('b.txt')
                t.wait_for('sub/')
                t.send('q')

    def test_watcher_picks_up_new_file(self):
        """An externally-created file appears within the watcher's poll cadence."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'one.txt'), 'w') as f:
                f.write('1')
            with TmuxFixture(cols=120, rows=30) as t:
                t.launch(_BIN, '--run-py', _RECIPE, tmp)
                t.wait_for('one.txt')
                # The watcher captures its mtime baseline on its first
                # tick (~1s after start); any dir mutation before then is
                # absorbed silently. Wait past the first tick before
                # mutating so the next tick observes a real change.
                time.sleep(1.5)
                with open(os.path.join(tmp, 'two.txt'), 'w') as f:
                    f.write('2')
                # Watcher polls every ~1s; the main loop sometimes only
                # repaints on the next keystroke after async work lands,
                # so fall back to a forced redraw if we don't see the
                # update on the first try.
                try:
                    t.wait_for('two.txt', timeout=3.0)
                except AssertionError:
                    t.redraw()
                    t.wait_for('two.txt', timeout=2.0)
                t.send('q')


if __name__ == '__main__':
    unittest.main()
