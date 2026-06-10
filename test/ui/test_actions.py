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

    def test_tty_dash_page_degrade_is_non_corrupting(self):
        """The ``--tty -`` page degrade emits text without corrupting the TUI.

        In ``--tty -`` mode the pane's std streams *are* the terminal, so a
        pager has nowhere to read keys from and ``page`` degrades to writing
        the text directly. That write must go through the same
        ``term_suspend`` / ``term_resume`` bracket the pager path uses:

        1. ``term_suspend`` leaves the alt screen + restores cooked mode, so
           the text lands on the *primary* screen / scrollback — not raw
           onto the alt screen where it would scroll the pane content away.
        2. ``term_resume`` re-enters raw and sets ``g_screen_lost_flag``, so
           the next ``render_full`` drops the stale row cache and fully
           repaints — leaving the live TUI intact.

        The regression this guards: the pre-fix degrade wrote raw to the alt
        screen and never set the screen-lost flag, so the list rows were
        scrolled away / overwritten and stayed corrupted (the cache-hit
        short-circuit in ``end_row`` emitted nothing on the next pass) until
        a resize / Ctrl-L forced a full repaint.

        Driven via the ``~`` log pager (``ctx.page`` on the message log),
        which is the framework path that exercises ``page`` directly rather
        than through ``run_external``. A failing action records a known line
        in the log first so there is non-empty text to page.
        """
        # --root-cmd emits the rows on stdout (it does not consume the
        # binary's stdin), so it composes with --tty - where stdin is the
        # pane's pty. The two known rows are the corruption probe.
        root_cmd = r'printf "ROW_ALPHA\tROW_ALPHA\nROW_BETA\tROW_BETA\n"'
        rows_clean = re.compile(
            r'(?m)^\s*ROW_ALPHA ROW_ALPHA\s*$.*?^\s*ROW_BETA ROW_BETA\s*$',
            re.DOTALL)
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"{_BIN} --tty - --show-ids always "
                     f"--root-cmd '{root_cmd}' "
                     f"--action 'x:Bad:false'")
            # Both known rows render on the alt screen.
            self.assertRegex(t.wait_for('ROW_BETA ROW_BETA'), rows_clean)
            t.wait_stable()
            # Fail the action so a known line lands in the message log.
            t.send('x')
            t.wait_for("action 'x' exited with code 1")
            # Page the log → the --tty - degrade fires (no pager spawn).
            t.send('~')
            # No corruption: the known rows must still render cleanly on the
            # alt screen once control returns. Pre-fix they were scrolled
            # away (blanked) or had the log text jammed onto ROW_BETA, and
            # stayed that way; the screen-lost repaint fixes that. wait_for
            # gives the repaint a moment to land without a fixed sleep.
            after = t.wait_for(rows_clean)
            # Belt-and-suspenders: the paged log line (timestamp-prefixed)
            # must NOT bleed into the rows region of the live alt screen.
            body = '\n'.join(after.splitlines()[:20])
            self.assertNotRegex(
                body, re.compile(r'(?m)^\s*\d\d:\d\d:\d\d\s'),
                'paged log text leaked onto the alt screen (degrade '
                'corrupted the TUI)')
            # Text-visible: quitting leaves the alt screen, restoring the
            # primary screen where the degrade wrote the log line. (Asserting
            # it on the live alt screen is racy — term_resume repaints over
            # the brief suspend window immediately — so we read it from the
            # restored primary screen, which is deterministic.) wait_for
            # raises if the timestamp-prefixed log line never appears there.
            t.send('q')
            t.wait_for(
                re.compile(r'(?m)^\s*\d\d:\d\d:\d\d\s+action .x. exited '
                           r'with code 1'),
                timeout=3.0)
