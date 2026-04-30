"""Tmux-driven fixture for browse-tui UI tests.

Spins up a private tmux server (unique socket per fixture) so parallel
test runs don't collide and we never touch the user's tmux sessions.
"""

import os
import re
import secrets
import shlex
import subprocess
import time


class TmuxFixture:
    """Manages a private tmux server + session for one test.

    Use as a context manager:

        with TmuxFixture(cols=120, rows=40) as t:
            t.launch('./browse-tui', '--root-cmd', 'cat')
            t.wait_for('a')
            t.send('Down')
            ...

    Each fixture spins up a unique tmux server (private socket) so
    parallel test runs don't collide.
    """

    def __init__(self, cols=120, rows=40, env=None):
        self.socket = f'browse-tui-test-{os.getpid()}-{secrets.token_hex(4)}'
        self.cols = cols
        self.rows = rows
        self.env = env or {}

    def __enter__(self):
        # Start a detached session running an interactive bash without
        # rcfiles — keeps the prompt simple and predictable.
        self.tmux('new-session', '-d', '-s', 'main',
                  '-x', str(self.cols), '-y', str(self.rows),
                  'bash', '--norc', '--noprofile', '-i')
        # Quiet the prompt so captures are predictable.
        self.send_line(r"PS1='$ ' ; unset HISTFILE")
        # Block until the prompt-echo round-trip lands — every test
        # subsequently launches a program from this prompt, so racing
        # ahead of bash leaves keystrokes queued in tmux's input buffer
        # and the launch line silently disappears.
        self.wait_for(re.compile(r'(?m)^\$ *$'), timeout=2.0)
        return self

    def __exit__(self, *_):
        self.tmux('kill-server', check=False)

    # ---- low-level tmux invocation ----------------------------------

    def tmux(self, *args, check=True, capture=True):
        cmd = ['tmux', '-L', self.socket, *args]
        env = {**os.environ, **self.env}
        if capture:
            return subprocess.run(cmd, check=check, capture_output=True,
                                  text=True, env=env)
        return subprocess.run(cmd, check=check, env=env)

    # ---- input ------------------------------------------------------

    def send_line(self, line):
        """Type a shell command + Enter into the pane."""
        self.tmux('send-keys', '-t', 'main', line, 'Enter')

    def launch(self, *argv):
        """Launch a program inside bash (shell-quotes argv)."""
        self.send_line(' '.join(shlex.quote(a) for a in argv))

    def send(self, *keys):
        """Send tmux key names: 'Down', 'Enter', 'q', 'PageDown', etc."""
        self.tmux('send-keys', '-t', 'main', *keys)

    def send_bytes(self, raw):
        """Send raw bytes (for Ctrl-codes)."""
        self.tmux('send-keys', '-t', 'main', raw)

    def type(self, text):
        """Send literal text without translating special key names."""
        self.tmux('send-keys', '-l', '-t', 'main', text)

    def ctrl_c(self):
        self.send_bytes('\x03')

    def ctrl_z(self):
        self.send_bytes('\x1a')

    def fg(self):
        self.send_line('fg')

    def redraw(self):
        """Send Ctrl-L to force a full repaint.

        Works around a phase-1 quirk: when async workers complete (children
        fetch, watcher refresh) the main loop doesn't always mark the
        screen dirty until the next user keystroke. Tests that depend on
        seeing the post-async state can call this to kick a redraw.
        """
        self.tmux('send-keys', '-t', 'main', 'C-l')

    # ---- process introspection -------------------------------------

    def pane_pid(self) -> int:
        out = self.tmux('display-message', '-p', '#{pane_pid}').stdout.strip()
        return int(out)

    def fg_pid(self):
        """Return PID of bash's foreground child (the running program)."""
        bash_pid = self.pane_pid()
        try:
            out = subprocess.run(
                ['ps', '-o', 'pid=', '--ppid', str(bash_pid)],
                check=True, capture_output=True, text=True).stdout
        except subprocess.CalledProcessError:
            return None
        children = [int(p) for p in out.split() if p.strip()]
        return children[0] if children else None

    def signal(self, sig):
        """Send signal to the foreground child process (e.g., browse-tui)."""
        pid = self.fg_pid()
        if pid is None:
            raise RuntimeError('no foreground child to signal')
        os.kill(pid, sig)

    # ---- output ----------------------------------------------------

    def capture(self, colors=False) -> str:
        args = ['capture-pane', '-t', 'main', '-p']
        if colors:
            args.append('-e')
        return self.tmux(*args).stdout

    def wait_for(self, pattern, timeout=3.0, interval=0.03) -> str:
        """Poll capture-pane until pattern (str or compiled regex) appears.

        Returns the matching capture; raises AssertionError on timeout.
        """
        if isinstance(pattern, str):
            rx = re.compile(re.escape(pattern))
        else:
            rx = pattern
        deadline = time.time() + timeout
        last = ''
        while time.time() < deadline:
            last = self.capture()
            if rx.search(last):
                return last
            time.sleep(interval)
        raise AssertionError(
            f'pattern {rx.pattern!r} not seen within {timeout}s.\n'
            f'last capture:\n{last}')

    def wait_stable(self, dwell=0.05, timeout=3.0) -> str:
        """Wait until two consecutive captures match — render settled."""
        deadline = time.time() + timeout
        prev = self.capture()
        time.sleep(dwell)
        while time.time() < deadline:
            cur = self.capture()
            if cur == prev:
                return cur
            prev = cur
            time.sleep(dwell)
        raise AssertionError('screen never stabilised')

    def resize(self, cols, rows):
        self.tmux('resize-window', '-t', 'main', '-x', str(cols), '-y', str(rows))
        self.cols, self.rows = cols, rows
