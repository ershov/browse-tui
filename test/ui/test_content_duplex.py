"""Full-duplex content channels (spec §3.2 + §3.4): both std streams at once.

The per-direction behaviour is covered by ``test/ui/test_output_channel``
(stdout) and ``test/ui/test_stdin_channel`` (stdin). This module pins the
GAP between them: ``ctx.print`` draining OUT while ``on_stdin`` streams
records IN, in the SAME shipped-binary session — fd 0 in the select
read-set and fd 1 in the write-set at the same time.

Both content fds are pipes the test owns; the UI rides a private pty
(``--tty /dev/pts/N``). The test plays all three roles the spec
separates: terminal (keys in / frames out via the pty master), stdin
producer (feeds + closes the content pipe), and stdout consumer (reads
the echoed results). The driving recipe is ``recipes/stdin_duplex.py``
(each record -> ``out:<r>`` on stdout + a ``rec:<r>`` row; EOF ->
``out:eof:<trailing>``).

  * **Full-duplex** — records fed live appear both as ``rec:`` rows on
    the pty screen AND as ``out:`` lines on the stdout pipe; closing
    stdin delivers the EOF marker; the print-exit quit output lands last
    (strict FIFO across the whole session).
  * **EPIPE while streaming** — the stdout consumer departs (its read
    end closed) mid-session: the first print hits EPIPE and the output
    channel dies for good, yet ``on_stdin`` keeps delivering records (the
    UI is on the pty, independent of stdout) right through to EOF, and
    the run still exits cleanly with no traceback.
"""

import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import time
import unittest


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'stdin_duplex.py')


def setUpModule():
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class _DuplexApp:
    """The shipped binary on a private pty, BOTH std streams piped.

    fd 0 (the test feeds it) and fd 1 (the test reads it) are pipes the
    test owns; the UI device is the pty (master held by the test). The
    plumbing reuses the same pty+pipe idioms as ``_PtyApp`` /
    ``_PtyStdinApp`` — this class just owns both content fds at once.
    """

    def __init__(self, rows=30, cols=100):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
        self.stdin_r, self.stdin_w = os.pipe()
        self.proc = subprocess.Popen(
            [_BIN, '--run-py', _RECIPE, '--tty', os.ttyname(self.slave)],
            stdin=self.stdin_r,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        os.close(self.stdin_r)  # the child holds its own copy
        self.stdin_r = -1
        self._screen = b''

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for f in (self.proc.stdout, self.proc.stderr):
            if f is not None and not f.closed:
                f.close()
        if self.stdin_w >= 0:
            os.close(self.stdin_w)
        os.close(self.master)
        os.close(self.slave)

    # ---- stdin producer side -----------------------------------------

    def feed(self, data):
        os.write(self.stdin_w, data)

    def end_input(self):
        os.close(self.stdin_w)
        self.stdin_w = -1

    # ---- terminal side -----------------------------------------------

    def keys(self, s):
        os.write(self.master, s.encode())

    def wait_screen(self, needle, timeout=5.0):
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

    # ---- stdout consumer side ----------------------------------------

    def read_stdout_to_eof(self, timeout=15.0):
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


class TestFullDuplex(unittest.TestCase):

    def test_prints_out_while_stdin_streams_in(self):
        """Records IN echo to ``rec:`` rows AND ``out:`` lines, FIFO.

        Each fed record must surface both on the UI device (a ``rec:``
        row — proof fd 0 is being serviced) and on the stdout pipe (an
        ``out:`` line — proof fd 1 drains in the same loop). Closing
        stdin delivers the EOF marker; the print-exit quit output lands
        last. The stream must read back in exact send order.
        """
        app = _DuplexApp()
        try:
            app.wait_screen('ready')
            app.feed(b'alpha\nbeta\n')
            # Both directions for each record: row on the pty, line on stdout.
            app.wait_screen('rec:alpha')
            app.wait_screen('rec:beta')
            app.feed(b'gamma\n')
            app.wait_screen('rec:gamma')
            app.end_input()
            # Wait for the EOF delivery (its row) before quitting, so the
            # 'out:eof:' print is buffered ahead of the quit output —
            # deterministic FIFO regardless of select-wake interleaving.
            app.wait_screen('eof::0')
            app.keys('\r')  # print-exit on 'ready' -> quit(0, 'ready\n')
            out = app.read_stdout_to_eof()
            rc = app.proc.wait(timeout=10)
            err = app.stderr_text()
        finally:
            app.close()
        # FIFO across the whole session: the three echoed records, the
        # EOF marker (trailing record empty -> 'out:eof:'), then the
        # print-exit quit output.
        self.assertEqual(
            out, b'out:alpha\nout:beta\nout:gamma\nout:eof:\nready\n')
        self.assertEqual(rc, 0)
        self.assertNotIn('Traceback', err)

    def test_epipe_on_stdout_does_not_stop_stdin_stream(self):
        """Consumer departs (EPIPE) yet ``on_stdin`` keeps flowing.

        After the test closes its stdout read end, the first record's
        ``ctx.print`` hits EPIPE and the output channel dies for good.
        The stdin stream is independent: subsequent records must still
        upsert ``rec:`` rows on the pty, the EOF call must still fire,
        and the run must exit cleanly (the undeliverable prints no-op
        rather than raising).
        """
        app = _DuplexApp()
        try:
            app.wait_screen('ready')
            app.proc.stdout.close()  # consumer departs before any print
            app.feed(b'one\n')       # print -> drain -> EPIPE -> channel dead
            app.wait_screen('rec:one')   # stdin stream unaffected
            app.feed(b'two\nthree\n')
            app.wait_screen('rec:two')
            app.wait_screen('rec:three')
            app.end_input()          # EOF call still fires (dead-stdout print no-op)
            app.wait_screen('eof::0')
            app.keys('q')            # quit from the keyboard (cancel code)
            rc = app.proc.wait(timeout=10)
            err = app.stderr_text()
        finally:
            app.close()
        self.assertEqual(rc, 1, f'clean exit expected; stderr:\n{err}')
        self.assertNotIn('Traceback', err)


if __name__ == '__main__':
    unittest.main()
