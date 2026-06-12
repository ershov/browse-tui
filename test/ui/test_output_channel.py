"""End-to-end output channel (spec §3.2): the shipped binary's stdout delivery.

The pipe cases drive the binary on a private pty (``--tty /dev/pts/N``)
with stdout as a pipe the test owns — full control of both the UI device
(keys in, frames out via the pty master) and the content channel:

  * **FIFO** — prints reach the pipe in call order, the print-exit quit
    output last, across live drains and the final teardown write.
  * **Backpressure** — a consumer that stops reading strands >64 KiB
    (more than a Linux pipe holds) in the channel while the UI keeps
    answering keys; the remainder arrives once the consumer resumes.
  * **EPIPE** — the consumer closing its end kills the channel for good:
    the UI stays alive, later prints no-op, and the run still exits 0
    with no traceback.

The tty case rides tmux (stdout must be a terminal the user actually
sees): buffered prints stay off the live screen and land in normal
scrollback after the alt-screen teardown, directly before the selection
— the fzf model. The driving recipe is ``recipes/output_channel.py``
(``p`` short print / ``b`` big print / ``m`` on-screen marker).
"""

import fcntl
import os
import pty
import select
import shutil
import stat
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'output_channel.py')

_BIG_LINE = b'big-1:' + b'x' * (128 * 1024) + b':end-1\n'

# See test/ui/test_fd_hygiene.py for the sentinel convention.
_SENTINEL = 'SENTINEL-RUN-DONE'


def setUpModule():
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class _PtyApp:
    """The shipped binary on a private pty, stdout piped to the test.

    The test process plays both roles the spec separates: terminal
    (writes keys to / reads frames from the pty master) and stdout
    consumer (reads — or pointedly does not read — ``proc.stdout``).
    """

    def __init__(self, rows=30, cols=100):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
        self.proc = subprocess.Popen(
            [_BIN, '--run-py', _RECIPE, '--tty', os.ttyname(self.slave)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self._screen = b''

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for f in (self.proc.stdout, self.proc.stderr):
            if f is not None and not f.closed:
                f.close()
        os.close(self.master)
        os.close(self.slave)

    # ---- terminal side ----------------------------------------------

    def keys(self, s):
        """Write key bytes to the pty (the app's terminal input)."""
        os.write(self.master, s.encode())

    def wait_screen(self, needle, timeout=5.0):
        """Accumulate UI output from the pty master until *needle* shows."""
        needle_b = needle.encode() if isinstance(needle, str) else needle
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle_b in self._screen:
                return
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    self._screen += os.read(self.master, 65536)
                except OSError:
                    break  # slave side gone (app exited)
        raise AssertionError(
            f'{needle!r} never appeared on the pty screen; last '
            f'{min(len(self._screen), 2000)} screen bytes:\n'
            f'{self._screen[-2000:]!r}')

    # ---- consumer side ----------------------------------------------

    def read_stdout_to_eof(self, timeout=15.0):
        """Drain the stdout pipe until EOF, bounded by *timeout*."""
        fd = self.proc.stdout.fileno()
        os.set_blocking(fd, False)
        out = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return out
                out += chunk
        raise AssertionError('stdout did not reach EOF within timeout')

    def stderr_text(self):
        return self.proc.stderr.read().decode('utf-8', 'replace')


class TestPipeStdout(unittest.TestCase):

    def test_fifo_prints_then_quit_output(self):
        """Everything written lands on the pipe in strict FIFO order.

        Short prints, a >pipe-capacity print, another short print, then
        the default print-exit (Enter on 'alpha') — the stream must read
        back exactly in that order with the quit output last, regardless
        of how the bytes split between live drains and the teardown
        write.
        """
        app = _PtyApp()
        try:
            app.wait_screen('alpha')
            app.keys('ppbp')
            app.keys('\r')  # enter → print-exit: quit(0, 'alpha\n')
            out = app.read_stdout_to_eof()
            rc = app.proc.wait(timeout=10)
            err = app.stderr_text()
        finally:
            app.close()
        self.assertEqual(
            out,
            b'print-1\nprint-2\n' + _BIG_LINE + b'print-3\nalpha\n')
        self.assertEqual(rc, 0)
        self.assertNotIn('Traceback', err)

    def test_stalled_consumer_does_not_block_ui(self):
        """A consumer that reads nothing must never stall the UI.

        'b' buffers far more than the pipe holds; with the test reading
        zero bytes the channel is backpressured. Two key→flash round
        trips on the terminal prove the loop is still serving input.
        Quitting then delivers the remainder through the final blocking
        drain once the consumer (the test) starts reading.
        """
        app = _PtyApp()
        try:
            app.wait_screen('alpha')
            app.keys('b')  # 128 KiB into a ≤64 KiB pipe; nobody reading
            app.keys('m')
            app.wait_screen('PONG-1')
            app.keys('m')
            app.wait_screen('PONG-2')
            app.keys('\r')
            out = app.read_stdout_to_eof()
            rc = app.proc.wait(timeout=10)
        finally:
            app.close()
        self.assertEqual(out, _BIG_LINE + b'alpha\n')
        self.assertEqual(rc, 0)

    def test_epipe_kills_channel_but_not_ui(self):
        """Consumer gone (EPIPE) → channel dead, UI alive, clean exit.

        After the test closes its read end, the first drain hits EPIPE:
        the buffer is dropped and fd 1 leaves the loop forever. The UI
        must keep responding (flash round-trips), later prints must
        no-op, and the print-exit quit must still return 0 — with the
        undeliverable quit output skipped rather than raising.
        """
        app = _PtyApp()
        try:
            app.wait_screen('alpha')
            app.proc.stdout.close()  # consumer departs
            app.keys('p')   # first print → drain → EPIPE → channel dead
            app.keys('m')
            app.wait_screen('PONG-1')
            app.keys('p')   # dead channel: no-op
            app.keys('m')
            app.wait_screen('PONG-2')
            app.keys('\r')  # print-exit; quit output undeliverable
            rc = app.proc.wait(timeout=10)
            err = app.stderr_text()
        finally:
            app.close()
        self.assertEqual(rc, 0, f'clean exit expected; stderr:\n{err}')
        self.assertNotIn('Traceback', err)


class TestTtyDashCompat(unittest.TestCase):
    """``--tty -``: fd 0/1 ARE the UI device — the channel must never
    live-drain fd 1 (that would paint over the alt screen); prints +
    quit output flush via ``sys.stdout`` only after the restore."""

    def test_prints_flush_after_alt_screen_exit(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', 30, 100, 0, 0))
        proc = subprocess.Popen(
            [_BIN, '--run-py', _RECIPE, '--tty', '-'],
            stdin=slave, stdout=slave, stderr=subprocess.PIPE)
        stream = b''

        def read_some(timeout):
            """Pull pty bytes into ``stream``; True iff data arrived."""
            nonlocal stream
            r, _, _ = select.select([master], [], [], timeout)
            if not r:
                return False
            try:
                data = os.read(master, 65536)
            except OSError:
                return False  # pty gone
            stream += data
            return bool(data)

        def wait_for(needle, timeout=5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if needle in stream:
                    return
                read_some(0.05)
            raise AssertionError(f'{needle!r} never appeared; tail:\n'
                                 f'{stream[-2000:]!r}')

        try:
            wait_for(b'alpha')
            os.write(master, b'ppm')
            wait_for(b'PONG-1')  # both prints dispatched by now
            self.assertNotIn(b'print-1', stream,
                             'a held --tty - print must not surface '
                             'while the UI is up')
            os.write(master, b'\r')
            rc = proc.wait(timeout=10)
            # Drain whatever the teardown flushed onto the pty.
            while read_some(0.2):
                pass
            err = proc.stderr.read().decode('utf-8', 'replace')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
            os.close(master)
            os.close(slave)
        self.assertEqual(rc, 0, f'stderr:\n{err}')
        # FIFO, delivered in the normal screen: the flush (prints then
        # the print-exit selection, ONLCR-translated) must follow the
        # final leave-alt-screen sequence.
        leave_alt = stream.rfind(b'\x1b[?1049l')
        self.assertNotEqual(leave_alt, -1)
        self.assertIn(b'print-1\r\nprint-2\r\nalpha', stream[leave_alt:])


class TestTtyStdoutHeld(unittest.TestCase):
    """tty stdout: prints held all session, dumped to scrollback at exit."""

    def test_prints_land_in_scrollback_before_selection(self):
        if not shutil.which('tmux'):
            self.skipTest('tmux not available')
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, 'run.sh')
            with open(script, 'w') as f:
                f.write(
                    '#!/bin/bash\n'
                    f'{_BIN} --run-py {_RECIPE}\n'
                    f'echo {_SENTINEL}\n')
            os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
            with TmuxFixture(cols=120, rows=40) as t:
                t.launch('bash', script)
                t.wait_for('alpha')
                t.send('p')
                t.send('p')
                # Held channel: nothing printed may surface while the
                # alt-screen UI is up (fd 1 is /dev/null; the buffer is
                # dumped only after restore).
                self.assertNotIn('print-1', t.wait_stable())
                t.send('Down')
                t.send('Enter')
                cap = t.wait_for(_SENTINEL)
        lines = [line.rstrip() for line in cap.splitlines()]
        sentinel_idx = next(i for i, line in enumerate(lines)
                            if _SENTINEL in line)
        self.assertEqual(
            lines[sentinel_idx - 3:sentinel_idx],
            ['print-1', 'print-2', 'beta'],
            f'prints then selection must precede the sentinel in '
            f'scrollback; capture was:\n{cap}')


if __name__ == '__main__':
    unittest.main()
