"""UI tests: custom actions invoked by key, TUI_* env vars, error display."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestActions(unittest.TestCase):

    def test_custom_action_runs_with_tui_env_vars(self):
        """A keybound action runs bash CMD with TUI_ID/TUI_TITLE set."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'log.txt')
            # The action writes a sentinel line we can poll for, so the
            # test waits on the file rather than a fixed sleep.
            action_cmd = (
                f'echo "id=$TUI_ID title=$TUI_TITLE" >> {log} ; '
                f'echo DONE >> {log}')
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch('bash', '-c',
                         f"printf 'a\\nb\\n' | "
                         f"{_BIN} --show-ids always --root-cmd cat "
                         f"--action 'e:Edit:{action_cmd}'")
                t.wait_for('a a')
                t.send('e')
                # Poll for the action's sentinel — far more reliable than
                # a fixed sleep, especially under CI load.
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    if os.path.exists(log):
                        with open(log) as f:
                            content = f.read()
                        if 'DONE' in content:
                            break
                    time.sleep(0.03)
                else:
                    self.fail(f'action did not complete within 3s: {log}')
                t.send('q')
            self.assertIn('id=a', content)
            self.assertIn('title=a', content)

    def test_action_failure_surfaces_error(self):
        """An action that exits non-zero displays the error in the preview."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\n' | {_BIN} --show-ids always "
                     f"--root-cmd cat --action 'x:Bad:false'")
            t.wait_for('a a')
            t.send('x')
            # The action layer posts the error back to the main thread
            # which adds 'preview' to _needs_redraw, so wait_for finds
            # the diagnostic message without a manual redraw kick.
            t.wait_for('exited with code', timeout=3.0)
