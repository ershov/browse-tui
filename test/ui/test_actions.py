"""UI tests: custom actions invoked by key, TUI_* env vars, error display."""

import os
import re
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
                     f"--root-cmd cat --preview "
                     f"--action 'x:Bad:false'")
            t.wait_for('a a')
            t.send('x')
            # The action layer posts the error back to the main thread
            # which adds 'preview' to _needs_redraw, so wait_for finds
            # the diagnostic message without a manual redraw kick.
            t.wait_for('exited with code', timeout=3.0)

    def test_action_output_lands_on_terminal_not_captured_stdout(self):
        """A shell-out action paints to the terminal even when stdout is piped.

        The terminal-separation contract (spec 3.6): an interactive child
        gets the terminal on its fd 0/1/2 via ``term_child_fds``, leaving
        the parent's ``stdout`` untouched. Here the binary's own stdout is
        redirected to a file; the action echoes a marker to *its* stdout.
        The marker must appear on the terminal (the tmux pane) and NOT in
        the captured stdout file — which carries only the clean print-exit
        result.

        The action echoes the marker then briefly sleeps so the suspend
        window (alt screen left, child on the primary screen) is still up
        when we capture the pane; otherwise ``term_resume``'s redraw would
        paint the alt-screen TUI back over the transient echo.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cap = os.path.join(tmp, 'stdout.txt')
            action_cmd = 'echo HELLO_FROM_CHILD ; sleep 1.0'
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch('bash', '-c',
                         f"printf 'a\\n' | "
                         f"{_BIN} --show-ids always --root-cmd cat "
                         f"--action 'x:Echo:{action_cmd}' > {cap}")
                t.wait_for('a a')
                t.wait_stable()
                t.send('x')
                # Child has echoed and is now sleeping, holding the
                # terminal — capture the primary screen mid-suspend.
                during = t.wait_for('HELLO_FROM_CHILD', timeout=3.0)
                self.assertIn('HELLO_FROM_CHILD', during,
                              'action output did not paint to the terminal')
                # Let the child exit + the TUI redraw, then select-and-quit
                # so the print-exit result is the only thing on stdout.
                t.wait_for('a a', timeout=3.0)
                t.send('Enter')
                # Poll the captured stdout until the result lands.
                deadline = time.time() + 3.0
                captured = ''
                while time.time() < deadline:
                    if os.path.exists(cap):
                        with open(cap) as f:
                            captured = f.read()
                        if captured.strip():
                            break
                    time.sleep(0.03)
            # The marker went to the terminal, never to the parent's
            # stdout; the captured file holds only the clean print-exit
            # selection (no child output, no escape bytes).
            self.assertNotIn('HELLO_FROM_CHILD', captured,
                             'child output leaked into the captured stdout')
            self.assertNotIn('\x1b', captured,
                             'escape bytes contaminated the captured stdout')
            self.assertEqual(captured, 'a\n')

    def test_navigated_selection_is_the_only_thing_on_stdout(self):
        """Result-capture separation: the *chosen* row lands cleanly on stdout.

        The terminal-separation contract (spec §5): the UI paints to the
        terminal device while stdout carries only the print-exit result,
        so a command substitution captures exactly the selection. The
        sibling #832 test proves this for the *default* row (``a``); this
        one navigates first (``j`` → cursor on ``b``) and presses Enter,
        proving the captured stdout is the *navigated* value (``b\\n``) —
        not a hardcoded first-row capture — with zero escape bytes from
        the alt-screen UI.

        Driven over a pty (tmux) with the binary's stdout redirected to a
        file: the live TUI renders to the pane while the file accumulates
        only the result. Regression shapes this would catch: the UI
        bytes bleeding into stdout (would add ``\\x1b``), or the wrong row
        being emitted (would fail the ``b\\n`` equality).
        """
        with tempfile.TemporaryDirectory() as tmp:
            cap = os.path.join(tmp, 'stdout.txt')
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch('bash', '-c',
                         f"printf 'a\\nb\\n' | "
                         f"{_BIN} --show-ids always --root-cmd cat > {cap}")
                # Both rows must be present before we navigate, else the
                # ``j`` could land before ``b`` exists in the list.
                t.wait_for('a a')
                t.wait_for('b b')
                t.wait_stable()
                t.send('j')          # cursor: a → b
                t.wait_stable()
                t.send('Enter')      # print-exit emits the cursor's id
                # Poll the captured stdout until the result lands.
                deadline = time.time() + 3.0
                captured = ''
                while time.time() < deadline:
                    if os.path.exists(cap):
                        with open(cap) as f:
                            captured = f.read()
                        if captured.strip():
                            break
                    time.sleep(0.03)
            # The navigated selection — and only that — reached stdout.
            self.assertEqual(captured, 'b\n')
            self.assertNotIn('\x1b', captured,
                             'escape bytes contaminated the captured stdout')
