"""fd hygiene at startup (spec §3.1 / §3.5): tty fd 0 / fd 1 detached.

``term_init`` (single-device mode only) detaches the std streams from the
terminal when they are ttys, so the freed ``stdin`` / ``stdout`` become
safe content channels:

* a tty fd 0 -> ``/dev/null``: a mid-session read returns immediate EOF;
* a tty fd 1 -> ``/dev/null`` with the real stdout saved: a stray raw
  ``print()`` vanishes, while the saved fd carries the result at teardown.

A pipe / file fd is left untouched (it is a content channel); ``--tty -``
is excluded entirely (there fd 0/1 *are* the UI device). The state is read
back through :func:`term_result_fd` / :func:`term_stdout_was_tty`.

These mechanics are about real process-level fd wiring (``os.isatty`` on
the inherited fds, ``dup2`` to ``/dev/null``), so each case runs in a
forked child with its fd 0/1/2 set to the device under test -- a pty
slave for the tty cases, a pipe for the piped case. The child reports its
observations back to the parent over a fd the parent reads. The UI device
itself is forced to a *separate* private pty (via a ``_resolve_terminal``
stub) so the redirect logic -- which keys purely on ``isatty(0/1)`` -- is
exercised without the test having to fight raw mode on the same fds.
"""

import os
import pty
import sys
import unittest

from test.unit._loader import load


def _run_in_child(stdio_fds, body):
    """Fork; in the child set fd 0/1/2 to *stdio_fds* and run ``body(report)``.

    ``stdio_fds`` is ``(in_fd, out_fd, err_fd)`` — the fds the child should
    inherit as its std streams (the device under test). ``body`` is called
    with a ``report`` callable that writes bytes to a dedicated pipe the
    parent drains; this keeps the child's observations separate from
    whatever it does to its own (possibly redirected) fd 1. Returns the
    accumulated report bytes.
    """
    rep_r, rep_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        try:
            os.setsid()
        except OSError:
            pass
        in_fd, out_fd, err_fd = stdio_fds
        os.dup2(in_fd, 0)
        os.dup2(out_fd, 1)
        os.dup2(err_fd, 2)
        os.close(rep_r)

        def report(data):
            os.write(rep_w, data)

        try:
            body(report)
        finally:
            os._exit(0)
    os.close(rep_w)
    out = b''
    while True:
        chunk = os.read(rep_r, 4096)
        if not chunk:
            break
        out += chunk
    os.close(rep_r)
    os.waitpid(pid, 0)
    return out


def _term_with_private_device():
    """Load 020-terminal.py with ``_resolve_terminal`` stubbed to a private pty.

    The UI device is a fresh pty slave opened ``O_RDWR`` — independent of
    the child's fd 0/1 — so ``term_init`` resolves a working terminal (raw
    mode succeeds) without us having to special-case ``/dev/tty`` and
    without entering raw mode on the very fds whose hygiene we're testing.
    Returns the loaded module.
    """
    term = load('term_fdhyg', '020-terminal.py')
    _dev_master, dev_slave = pty.openpty()

    def fake_resolve(tty_path):
        term._tty_fd_in = dev_slave
        term._tty_fd_out = dev_slave
        term._tty_writer = os.fdopen(dev_slave, 'w', encoding='utf-8',
                                     newline='')
        term._tty_owns_fd = True

    term._resolve_terminal = fake_resolve
    return term


class TestFdHygiene(unittest.TestCase):

    def test_tty_stdin_reads_eof_after_init(self):
        """A tty fd 0 is redirected to /dev/null -> post-init read = EOF.

        Mid-session, a recipe that reads stdin must get immediate EOF
        rather than blocking on, or stealing keystrokes from, the UI on
        the same terminal. We hand the child a pty slave (a tty) as fd 0,
        run ``term_init``, then read fd 0: it must return ``b''``.
        """
        master, slave = pty.openpty()
        # Keep the master's write end live so a *non*-redirected read on the
        # slave would have data to return — proving the EOF comes from the
        # /dev/null redirect, not an empty/closed pty.
        os.write(master, b'keystrokes-that-must-not-be-read')

        def body(report):
            assert os.isatty(0), 'precondition: fd0 is a tty'
            term = _term_with_private_device()
            term.term_init(None)
            os.set_blocking(0, True)
            data = os.read(0, 64)
            report(b'EOF' if data == b'' else b'GOT:' + data)
            term.term_restore()

        out = _run_in_child((slave, slave, slave), body)
        os.close(master)
        os.close(slave)
        self.assertEqual(out, b'EOF')

    def test_tty_stdout_writes_vanish_and_result_fd_reaches_terminal(self):
        """A tty fd 1 -> /dev/null; the saved fd still reaches the terminal.

        Two guarantees in one child run on a pty:
          * a raw ``os.write(1, ...)`` after ``term_init`` vanishes (fd 1 is
            now /dev/null) — it must NOT appear on the pty master;
          * ``term_result_fd()`` returns the *saved* real stdout (not 1),
            and a write to it lands on the pty master;
          * ``term_stdout_was_tty()`` is True.
        """
        master, slave = pty.openpty()

        def body(report):
            assert os.isatty(1), 'precondition: fd1 is a tty'
            term = _term_with_private_device()
            term.term_init(None)
            result_fd = term.term_result_fd()
            was_tty = term.term_stdout_was_tty()
            # Write the result to the saved fd (must reach the master) and a
            # stray raw write to fd 1 (must vanish to /dev/null).
            os.write(result_fd, b'RESULT_ON_TERMINAL\n')
            os.write(1, b'VANISH_TO_DEVNULL\n')
            term.term_restore()
            report(
                f'result_fd_is_1={result_fd == 1} was_tty={was_tty}'.encode())

        out = _run_in_child((slave, slave, slave), body)
        # Drain the pty master: it must have the saved-fd write but not the
        # /dev/null write.
        os.set_blocking(master, False)
        seen = b''
        try:
            while True:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                seen += chunk
        except (OSError, BlockingIOError):
            pass
        os.close(master)
        os.close(slave)
        self.assertIn(b'RESULT_ON_TERMINAL', seen,
                      'saved-fd write must reach the real terminal')
        self.assertNotIn(b'VANISH_TO_DEVNULL', seen,
                         'a raw fd-1 write must vanish to /dev/null')
        self.assertIn(b'result_fd_is_1=False', out,
                      'term_result_fd() must be the saved fd, not 1')
        self.assertIn(b'was_tty=True', out)

    def test_piped_stdin_stdout_left_untouched(self):
        """Pipe fd 0 / fd 1 are content channels -> left exactly as-is.

        With non-tty std streams, ``term_init`` must not redirect: fd 1
        still carries to the parent's pipe, fd 0 is still readable, and the
        accessors report the no-redirect state (``term_result_fd()`` == 1,
        ``term_stdout_was_tty()`` False).
        """
        in_r, in_w = os.pipe()      # parent -> child stdin
        out_r, out_w = os.pipe()    # child stdout -> parent
        err_r, err_w = os.pipe()
        os.write(in_w, b'piped-stdin-payload')
        os.close(in_w)

        def body(report):
            assert not os.isatty(0) and not os.isatty(1)
            term = _term_with_private_device()
            term.term_init(None)
            result_fd = term.term_result_fd()
            was_tty = term.term_stdout_was_tty()
            # fd 1 must still be the original pipe.
            os.write(1, b'STDOUT_PIPE_INTACT\n')
            data = os.read(0, 64)
            term.term_restore()
            report(f'result_fd_is_1={result_fd == 1} was_tty={was_tty} '
                   f'stdin={data!r}\n'.encode())

        out = _run_in_child((in_r, out_w, err_w), body)
        os.close(in_r)
        os.close(err_w)
        os.close(err_r)
        os.close(out_w)
        child_stdout = b''
        while True:
            chunk = os.read(out_r, 4096)
            if not chunk:
                break
            child_stdout += chunk
        os.close(out_r)
        self.assertIn(b'STDOUT_PIPE_INTACT', child_stdout,
                      'piped fd 1 must be left untouched')
        self.assertIn(b"result_fd_is_1=True", out)
        self.assertIn(b'was_tty=False', out)
        self.assertIn(b"stdin=b'piped-stdin-payload'", out,
                      'piped fd 0 must be left readable')

    def test_tty_dash_does_not_redirect(self):
        """``--tty -`` (tty_path='-') performs NO fd 0/1 hygiene.

        In ``--tty -`` mode the std streams ARE the UI device, so they must
        never be redirected — even though they are ttys here. After
        ``term_init('-')`` fd 0/1 stay ttys, ``term_result_fd()`` == 1, and
        ``term_stdout_was_tty()`` is False (no save happened).
        """
        master, slave = pty.openpty()

        def body(report):
            assert os.isatty(0) and os.isatty(1)
            # Real terminal module — '-' resolves the UI to fd 0/1 and
            # enters raw mode on them; the redirect must be skipped.
            term = load('term_fdhyg_dash', '020-terminal.py')
            term.term_init('-')
            result_fd = term.term_result_fd()
            was_tty = term.term_stdout_was_tty()
            fd0_tty = os.isatty(0)
            fd1_tty = os.isatty(1)
            term.term_restore()
            report(f'result_fd_is_1={result_fd == 1} was_tty={was_tty} '
                   f'fd0_tty={fd0_tty} fd1_tty={fd1_tty}'.encode())

        out = _run_in_child((slave, slave, slave), body)
        os.close(master)
        os.close(slave)
        self.assertIn(b'result_fd_is_1=True', out)
        self.assertIn(b'was_tty=False', out)
        self.assertIn(b'fd0_tty=True', out,
                      '--tty - must leave fd 0 as the tty UI device')
        self.assertIn(b'fd1_tty=True', out,
                      '--tty - must leave fd 1 as the tty UI device')

    def test_release_result_fd_closes_and_resets(self):
        """``term_release_result_fd`` closes the saved fd and clears the state.

        The teardown output dump hands the saved real-stdout fd back to
        the terminal layer once written: the fd must be closed and the
        accessors must report the no-redirect state again. Idempotent —
        a second call (and a call when nothing was saved) is a no-op.
        Runs in-process on a fresh module instance with the saved-fd
        globals planted directly; no terminal needed.
        """
        term = load('term_fdhyg_release', '020-terminal.py')
        saved = os.open(os.devnull, os.O_WRONLY)
        term._saved_stdout_fd = saved
        term._stdout_was_tty = True
        term.term_release_result_fd()
        with self.assertRaises(OSError):
            os.fstat(saved)
        self.assertFalse(term.term_stdout_was_tty())
        self.assertEqual(term.term_result_fd(), 1)
        term.term_release_result_fd()  # idempotent no-op
        self.assertEqual(term.term_result_fd(), 1)

    def test_child_fds_unaffected_by_redirect(self):
        """Shell-out children still get the terminal via ``term_child_fds``.

        The fd 0/1 redirect must not disturb the device the UI rides on:
        ``term_child_fds()`` (handed to run_external / page / action-cmd
        subprocesses) returns the resolved device fds, never the now
        /dev/null fd 0/1. Here both are the private device fd — distinct
        from 0 and 1 — and a write to the out fd reaches the device, while
        fd 1 is /dev/null.
        """
        master, slave = pty.openpty()

        def body(report):
            term = _term_with_private_device()
            term.term_init(None)
            in_fd, out_fd = term.term_child_fds()
            # The child device fds must not be the redirected std fds.
            ok = (in_fd not in (0, 1)) and (out_fd not in (0, 1))
            term.term_restore()
            report(f'child_fds_off_std={ok} in={in_fd} out={out_fd}'.encode())

        out = _run_in_child((slave, slave, slave), body)
        os.close(master)
        os.close(slave)
        self.assertIn(b'child_fds_off_std=True', out,
                      'term_child_fds() must return the device, not fd 0/1')


if __name__ == '__main__':
    unittest.main()
