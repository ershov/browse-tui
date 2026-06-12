"""End-to-end streaming input (spec §3.4): the shipped binary's on_stdin hook.

Drives the binary on a private pty (``--tty /dev/pts/N``) with stdin as
a pipe the test owns — full control of both the UI device (keys in,
frames out via the pty master) and the content channel:

  * **Live streaming** — records fed to the pipe WHILE the UI runs
    appear as new tree rows; closing the pipe delivers the EOF call
    (carrying the trailing unterminated record), after which the
    process idles at ~zero CPU — proof fd 0 left the select set rather
    than spinning on the permanently-readable EOF condition.
  * **Saturation fairness** — a ``yes``-style producer keeps fd 0
    readable at every select; the terminal-first priority in
    ``read_key`` must keep the session quittable from the keyboard
    regardless.
  * **Compose hand-off** — every byte is in the pipe before the app
    starts; the recipe's bounded pre-run ``sys.stdin.buffer`` read
    pulls the whole payload into BufferedReader read-ahead, so the
    kernel buffer is empty when streaming arms and select alone would
    never surface the remainder. The arming residue drain must deliver
    exactly the unread bytes, and the stream must then keep flowing for
    data fed live afterwards.

The driving recipe is ``recipes/stdin_stream.py`` (newline records →
``rec:<r>`` rows; final call → ``eof:<trailing>:<errno>`` row). The
read-error end is unit-tested (``test/unit/test_stdin_channel.py``) —
a pipe cannot be made to fail mid-read from outside.
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
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'stdin_stream.py')


def setUpModule():
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _cpu_seconds(pid):
    """Total CPU seconds (utime + stime) consumed by *pid* so far."""
    with open(f'/proc/{pid}/stat') as f:
        # Split after the parenthesised comm field; the remainder starts
        # at field 3 (state), so utime/stime (fields 14/15) are at
        # offsets 11/12.
        parts = f.read().rsplit(')', 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / os.sysconf('SC_CLK_TCK')


class _PtyStdinApp:
    """The shipped binary on a private pty, stdin piped from the test.

    The test process plays both roles the spec separates: terminal
    (writes keys to / reads frames from the pty master) and stdin
    producer (feeds — and eventually closes — the content pipe).
    """

    def __init__(self, rows=30, cols=100, env=None, stdin_preload=b'',
                 stdin_file=None):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
        if stdin_file is not None:
            # Caller-supplied stdin (e.g. a flooding producer's stdout);
            # the caller owns and closes it. feed()/end_input() unused.
            stdin_src = stdin_file
            self.stdin_w = -1
        else:
            stdin_src, self.stdin_w = os.pipe()
            if stdin_preload:
                os.write(self.stdin_w, stdin_preload)
        run_env = dict(os.environ)
        run_env.update(env or {})
        self.proc = subprocess.Popen(
            [_BIN, '--run-py', _RECIPE, '--tty', os.ttyname(self.slave)],
            stdin=stdin_src,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=run_env)
        if stdin_file is None:
            os.close(stdin_src)  # the child holds its own copy
        self._screen = b''

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if not self.proc.stderr.closed:
            self.proc.stderr.close()
        if self.stdin_w >= 0:
            os.close(self.stdin_w)
        os.close(self.master)
        os.close(self.slave)

    # ---- producer side ----------------------------------------------

    def feed(self, data):
        """Write content bytes to the app's stdin pipe."""
        os.write(self.stdin_w, data)

    def end_input(self):
        """Close the write end — the app's next read sees EOF."""
        os.close(self.stdin_w)
        self.stdin_w = -1

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

    def stderr_text(self):
        return self.proc.stderr.read().decode('utf-8', 'replace')


class TestLiveStreaming(unittest.TestCase):

    def test_records_grow_tree_then_eof_then_idle(self):
        """Feed the pipe while the UI runs: rows appear as records land,
        the EOF call flushes the trailing partial, and after the end
        delivery the process burns ~no CPU (fd 0 left the select set —
        an EOF'd pipe is permanently readable, so a leak here would
        spin the loop at 100%)."""
        app = _PtyStdinApp()
        try:
            app.wait_screen('ready')
            app.feed(b'alpha\nbeta\n')
            app.wait_screen('rec:alpha')
            app.wait_screen('rec:beta')
            app.feed(b'tail-no-newline')  # unterminated: held until EOF
            app.end_input()
            app.wait_screen('eof:tail-no-newline:0')
            cpu0 = _cpu_seconds(app.proc.pid)
            time.sleep(1.0)
            cpu_delta = _cpu_seconds(app.proc.pid) - cpu0
            self.assertLess(
                cpu_delta, 0.30,
                f'idle CPU after EOF should be ~0, burned {cpu_delta:.2f}s '
                f'in 1s wall — fd 0 likely never left the select set')
            app.keys('q')
            rc = app.proc.wait(timeout=10)
            err = app.stderr_text()
        finally:
            app.close()
        self.assertEqual(rc, 1)  # 'q' quits with the cancel code
        self.assertNotIn('Traceback', err)


class TestSaturatedProducer(unittest.TestCase):

    def test_keyboard_quit_wins_under_stdin_flood(self):
        """A producer that sustainably outpaces the hook+render cycle
        keeps fd 0 readable at EVERY select. ``read_key``'s
        terminal-first priority must still let 'q' through promptly —
        without it the keyboard is starved indefinitely and the session
        is unquittable (raw mode: ctrl-c is just another starved key)."""
        producer = subprocess.Popen(['yes', 'x' * 64],
                                    stdout=subprocess.PIPE)
        app = _PtyStdinApp(stdin_file=producer.stdout)
        try:
            app.wait_screen('ready')
            app.wait_screen('rec:' + 'x' * 16)  # streaming is live
            time.sleep(0.5)                     # sustained saturation
            app.keys('q')
            try:
                rc = app.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("'q' never dispatched — keyboard starved by "
                          'the saturated stdin stream')
            err = app.stderr_text()
        finally:
            producer.kill()
            producer.wait()
            producer.stdout.close()
            app.close()
        self.assertEqual(rc, 1)  # 'q' quits with the cancel code
        self.assertNotIn('Traceback', err)


class TestComposeHandOff(unittest.TestCase):

    def test_pre_run_buffer_read_streams_exact_remainder(self):
        """Slurp-then-stream: the pre-run ``sys.stdin.buffer.read(4)``
        consumes the header but drags the whole preloaded payload into
        BufferedReader read-ahead — the kernel side of fd 0 is already
        empty when streaming arms. The remainder must still reach
        ``on_stdin`` (via the arming residue drain), exactly once and
        in order, and the stream must keep working for live data."""
        app = _PtyStdinApp(env={'STDIN_STREAM_PREREAD': '4'},
                           stdin_preload=b'HDR\nrest1\nrest2\n')
        try:
            app.wait_screen('pre:HDR')
            app.wait_screen('rec:rest1')
            app.wait_screen('rec:rest2')
            app.feed(b'live\n')
            app.wait_screen('rec:live')
            app.end_input()
            app.wait_screen('eof::0')  # no trailing partial
            app.keys('q')
            rc = app.proc.wait(timeout=10)
            err = app.stderr_text()
        finally:
            app.close()
        self.assertEqual(rc, 1)
        self.assertNotIn('Traceback', err)


if __name__ == '__main__':
    unittest.main()
