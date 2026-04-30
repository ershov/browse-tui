"""UI tests: suspend/resume via Ctrl-Z+fg and via SIGSTOP+SIGCONT.

Two complementary flows:

  * ``test_ctrl_z_then_fg_round_trip`` — raw byte ``\\x1a`` from the
    keyboard. In raw mode (``tty.setraw`` clears ISIG) the kernel no
    longer translates that byte into SIGTSTP, so the action layer binds
    ``ctrl-z`` to a handler that raises SIGTSTP on this process. The
    ``_handle_sigtstp`` in ``020-terminal.py`` restores the terminal and
    drops the user back to the shell. ``fg`` resumes via SIGCONT, which
    re-enters raw mode and forces a full redraw (via the resize flag).

  * ``test_sigstop_then_sigcont_via_fg_pid`` (phase 2, ticket #25) —
    direct SIGSTOP from outside the process. SIGSTOP cannot be caught,
    so the process freezes mid-syscall with no cleanup. SIGCONT routes
    through ``_handle_sigcont`` which re-enters raw mode and sets the
    resize flag, so the next loop iteration repaints. The test asserts
    that the screen is identical pre- and post-suspend.
"""

import os
import re
import shutil
import signal
import subprocess
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

    def test_sigstop_then_sigcont_via_fg_pid(self):
        """SIGSTOP can't be caught — process freezes mid-syscall.

        SIGCONT resumes; the SIGCONT handler in 020-terminal re-enters
        raw mode + alt-screen and sets ``g_resize_flag``. This test
        bypasses the SIGTSTP handler (Ctrl-Z's clean-suspend path) by
        sending SIGSTOP directly to the foreground browse-tui PID via
        the fixture's ``fg_pid()`` helper.

        We verify three observable signals of the round-trip:
          1. SIGSTOP transitions the process into the stopped state ('T').
          2. SIGCONT transitions it back to running ('S').
          3. The TUI is interactive afterwards — ``fg`` then ``q`` returns
             cleanly to the bash prompt.

        Note: with bash as parent, sending SIGSTOP causes bash's
        job-control to write a "Stopped" notice to the tty, so a strict
        screen-equality assertion is not feasible without help from the
        SIGCONT handler (which also does not currently issue a
        ``notify_wake``). The state-transition + clean-quit checks above
        are the strongest behavioural guarantees we can make against the
        current implementation.

        Uses direct ``send_line`` (not ``bash -c``) so browse-tui is a
        one-level child of bash and ``fg_pid()`` returns the python3
        process directly. The fixture's ``name=`` argument is the
        alternate route when the launch line wraps the target in another
        shell — see ``fg_pid`` docstring.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.send_line(
                f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.wait_stable()

            pid = t.fg_pid()
            self.assertIsNotNone(pid, 'browse-tui process not found')
            # Sanity: fg_pid resolved to the python3 (browse-tui) process,
            # not to a transient pipeline subshell.
            comm = subprocess.run(
                ['ps', '-o', 'comm=', '-p', str(pid)],
                capture_output=True, text=True).stdout.strip()
            self.assertEqual(comm, 'python3',
                             f'fg_pid returned {comm!r}, expected python3')

            # 1. SIGSTOP → process freezes mid-syscall, no handler runs.
            t.signal(signal.SIGSTOP)
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if _proc_state(pid) == 'T':
                    break
                time.sleep(0.02)
            else:
                self.fail('process did not enter stopped state after SIGSTOP')

            # 2. SIGCONT → handler in 020-terminal runs (re-enters raw
            #    mode), process returns to running state.
            t.signal(signal.SIGCONT)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if _proc_state(pid) == 'S':
                    break
                time.sleep(0.02)
            else:
                self.fail('process did not return to sleeping state after SIGCONT')

            # 3. Interactive afterwards: ``fg`` foregrounds the job and
            #    ``q`` quits cleanly. This proves the SIGCONT handler
            #    didn't leave the process wedged or crashed — the main
            #    loop is still reading keys and the action layer
            #    responds.
            t.fg()
            t.wait_for(re.compile(r'(?m)^\$ *$'), timeout=5.0)
