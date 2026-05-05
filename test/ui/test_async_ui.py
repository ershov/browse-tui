"""UI tests: async loading placeholder + background watcher refresh."""

import os
import shutil
import subprocess
import tempfile
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SLOW_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'slow_children.py')
_WATCH_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'fs_watcher.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestAsyncUI(unittest.TestCase):

    def test_loading_placeholder_appears_for_slow_expand(self):
        """Expanding a parent with a slow get_children shows ⧗ loading…
        until results arrive, then content replaces the placeholder."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--python', _SLOW_RECIPE, '--', '1.2')
            # Recipe items have id == title, so the rendered row is
            # just the title (auto-suppressed id segment).
            t.wait_for('parent')
            t.send('Right')
            # The placeholder appears as soon as the main loop renders
            # the post-Right state (cursor moved, tree dirty). The fetch
            # takes ~1.2s so the loading row is visible until then.
            t.wait_for('loading', timeout=1.5)
            # Once children land, the worker-result delivery flips
            # _needs_redraw['list'] and the loop renders them.
            t.wait_for('alpha', timeout=2.5)
            screen = t.wait_stable()
            self.assertNotIn('loading', screen)
            self.assertIn('alpha', screen)
            self.assertIn('beta', screen)

    def test_background_watcher_refreshes_list(self):
        """File-watching recipe picks up an external mutation and the UI
        renders the new contents within a few seconds."""
        with tempfile.TemporaryDirectory() as tmp:
            data_path = os.path.join(tmp, 'data.txt')
            with open(data_path, 'w') as f:
                f.write('initial\n')
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch(_BIN, '--python', _WATCH_RECIPE, '--', data_path)
                t.wait_for('initial', timeout=3.0)
                # Mutate the file from outside the TUI.
                with open(data_path, 'w') as f:
                    f.write('updated\n')
                # Watcher polls every 0.2s, calls browser.refresh() when
                # contents change; the worker round-trip then flips the
                # list-dirty bit so the next loop pass renders the new
                # rows. Two seconds is plenty headroom on a busy host.
                t.wait_for('updated', timeout=2.5)
                screen = t.wait_stable()
                self.assertNotIn('initial', screen)
                self.assertIn('updated', screen)
