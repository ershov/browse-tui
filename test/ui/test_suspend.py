"""UI test: Ctrl-Z (raw byte ``\\x1a``) + fg round-trip.

In raw mode (``tty.setraw`` clears ISIG) the kernel no longer translates
the keyboard ``\\x1a`` byte into SIGTSTP. The action layer therefore
binds ``ctrl-z`` to a handler that raises SIGTSTP on this process so the
existing ``_handle_sigtstp`` in ``020-terminal.py`` runs, restoring the
terminal and dropping the user back to the shell. ``fg`` resumes the
process via SIGCONT, which re-enters raw mode and forces a full redraw
(via the resize flag).

Acceptance criterion 6: this test demonstrates the Ctrl-Z+fg flow works
end-to-end. The companion direct-signal test (``SIGSTOP``+``SIGCONT``)
is phase 2 (#25) and intentionally not covered here.
"""

import os
import re
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


def _proc_state(pid):
    """Return the single-char ps STAT field for ``pid`` (or '' if gone)."""
    out = subprocess.run(
        ['ps', '-o', 'stat=', '-p', str(pid)],
        capture_output=True, text=True).stdout.strip()
    return out[:1] if out else ''


class TestSuspend(unittest.TestCase):

    def test_ctrl_z_then_fg_round_trip(self):
        """Ctrl-Z byte suspends; bash regains control; fg resumes; screen identical."""
        with TmuxFixture(cols=80, rows=24) as t:
            # Direct send_line (not 'bash -c') so the running browse-tui
            # is a one-level child of bash and the shell job-control
            # framework can suspend / resume it cleanly.
            t.send_line(
                f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            before = t.wait_stable()
            self.assertIn('#a a', before)
            self.assertIn('#b b', before)
            self.assertIn('#c c', before)

            pid = t.fg_pid()
            self.assertIsNotNone(pid, 'browse-tui process not found')

            # Send the literal Ctrl-Z byte. The action layer routes
            # ``ctrl-z`` to ``os.kill(getpid(), SIGTSTP)`` which fires
            # the terminal layer's clean-suspend handler.
            t.ctrl_z()
            # Bash prints "[N]+ Stopped ..." once it inherits the tty.
            t.wait_for('Stopped', timeout=3.0)
            # Process should be in stopped state.
            self.assertEqual(_proc_state(pid), 'T',
                             'process did not enter stopped state')

            # Resume via the bash builtin. Bash sends SIGCONT to the
            # process group, which routes through ``_handle_sigcont``:
            # re-enter raw mode + alt-screen, set resize flag so the
            # main loop schedules a full redraw.
            t.fg()
            after = t.wait_stable(timeout=5.0)
            self.assertEqual(after, before,
                             'screen contents differ after fg resume')
            # Process should be running again.
            self.assertEqual(_proc_state(pid), 'S',
                             'process did not return to sleeping state')

            # Sanity: the TUI is still interactive — q should quit.
            t.send('q')
            t.wait_for(re.compile(r'(?m)^\$ *$'), timeout=3.0)
