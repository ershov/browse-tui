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
    so the process freezes mid-syscall with no cleanup. The job is then
    resumed through bash job control (``fg``), which routes a SIGCONT
    through ``_handle_sigcont``. The test asserts the two observable
    state transitions: SIGSTOP -> stopped ('T'), resume -> running.
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


def _wait_state(pid, accept, timeout=2.0, interval=0.02):
    """Poll until ``_proc_state(pid)`` is one of ``accept``; return bool.

    Synchronises on the kernel's reported process state rather than a
    fixed sleep, so SIGSTOP/SIGCONT round-trips are deterministic.
    ``accept`` is a string of acceptable single-char states, e.g. ``'T'``
    for stopped or ``'SRD'`` for any running state (sleeping / on-CPU /
    uninterruptible disk wait).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _proc_state(pid)
        if st and st in accept:
            return True
        time.sleep(interval)
    return False


class TestSuspend(unittest.TestCase):

    def test_ctrl_z_then_fg_round_trip(self):
        """Ctrl-Z byte suspends; bash regains control; fg resumes; screen identical."""
        with TmuxFixture(cols=80, rows=24) as t:
            # Direct send_line (not 'bash -c') so the running browse-tui
            # is a one-level child of bash and the shell job-control
            # framework can suspend / resume it cleanly.
            t.send_line(
                f"printf 'a\\nb\\nc\\n' | {_BIN} --show-ids always --root-cmd cat")
            t.wait_for('a a')
            before = t.wait_stable()
            self.assertIn('a a', before)
            self.assertIn('b b', before)
            self.assertIn('c c', before)

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
        """External SIGSTOP stops the job; ``fg`` resumes it via SIGCONT.

        This exercises the SIGCONT path *without* going through the
        SIGTSTP handler (Ctrl-Z's clean-suspend path — covered by
        ``test_ctrl_z_then_fg_round_trip``): SIGSTOP is sent directly to
        the foreground browse-tui PID via the fixture's ``fg_pid()``
        helper, so the process freezes mid-syscall with no handler run.

        We verify the two observable state transitions:
          1. SIGSTOP transitions the process into the stopped state ('T').
          2. ``fg`` resumes it and the process returns to a running state.

        Resume MUST go through bash job control (``fg``), not a bare
        ``os.kill(pid, SIGCONT)``. The launch line is a pipeline
        (``printf … | browse-tui``), so browse-tui's process group is
        NOT the terminal's foreground group while bash sits at its
        prompt. A bare SIGCONT to that *background* group lets
        ``_handle_sigcont``'s ``tcsetattr``/terminal writes draw
        SIGTTOU (default disposition: stop), which intermittently
        RE-STOPS the process — the historical flake (it would stay 'T'
        and the poll for 'S' would time out). ``fg`` makes the group the
        terminal's foreground group *before* delivering SIGCONT, so the
        handler's terminal I/O is legal and no SIGTTOU fires. We also
        accept any running state (S/R/D), not just 'S', so a process
        that happens to be on-CPU at poll time isn't misread as stuck.

        Uses direct ``send_line`` (not ``bash -c``) so browse-tui is a
        one-level child of bash and ``fg_pid()`` returns the python3
        process directly. The fixture's ``name=`` argument is the
        alternate route when the launch line wraps the target in another
        shell — see ``fg_pid`` docstring.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.send_line(
                f"printf 'a\\nb\\nc\\n' | {_BIN} --show-ids always --root-cmd cat")
            t.wait_for('a a')
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
            #    Poll until the kernel reports it stopped.
            t.signal(signal.SIGSTOP)
            self.assertTrue(
                _wait_state(pid, 'T', timeout=2.0),
                'process did not enter stopped state after SIGSTOP')

            # Wait for bash's job-control "Stopped" notice so the job is
            # registered as stopped before we resume it. This also makes
            # ``fg`` deterministic: it has a stopped job to foreground.
            t.wait_for('Stopped', timeout=3.0)

            # 2. Resume via bash job control. ``fg`` makes the job's
            #    process group the terminal's foreground group, THEN
            #    sends SIGCONT — so ``_handle_sigcont`` re-enters raw
            #    mode without tripping SIGTTOU. The process must return
            #    to a running state (any of sleeping/running/disk-wait).
            t.fg()
            self.assertTrue(
                _wait_state(pid, 'SRD', timeout=3.0),
                'process did not return to a running state after fg/SIGCONT')
