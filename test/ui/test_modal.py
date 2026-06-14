"""UI tests for the modal-dialog facility (ticket #976).

End-to-end coverage that drives the real binary and exercises every
dialog kind the modal engine backs, the resize-while-open repaint, and
the stdin-keeps-flowing-while-open guarantee. The picker already has its
own coverage (``test/ui/test_pick.py``: open / drive / close / restore,
including the strict cache-poison-restore check); this module covers the
OTHER kinds plus the two cross-cutting behaviours.

Two harnesses are used, each where it fits:

  * **tmux** (``fixtures/tmux.py``) for keyboard-driven dialogs and the
    resize path — a real terminal whose geometry the test controls.
  * **a private pty + an owned stdin pipe** (the same idiom as
    ``test/ui/test_stdin_channel.py``) for the streaming test, where the
    test must feed fd 0 while keys drive the UI.

Selection / focus inside a dialog is reverse video, which tmux text
capture is blind to, so every OUTCOME is asserted via the recipe's
recorded log (``modal_demo.py`` writes the resolved result to
``$MODAL_LOG``) rather than by reading the highlighted row. Restore is
asserted strictly — the dialog's box border, title, and content must all
vanish AND the regular UI must be back — because that cache-poison
restore is what the whole design hinges on (mirrors
``test_pick.py``'s ``TestPickRedrawOnExit._assert_ui_restored``).
"""

import fcntl
import os
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPES = os.path.join(_REPO, 'test', 'ui', 'recipes')
_DEMO = os.path.join(_RECIPES, 'modal_demo.py')
_STDIN_DEMO = os.path.join(_RECIPES, 'modal_stdin_demo.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _read_log_when_ready(path, timeout=3.0):
    """Poll ``path`` until it exists with non-empty content; return the text."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if content:
                return content
        time.sleep(0.03)
    raise AssertionError(f'log file {path!r} not populated within {timeout}s')


# Box-drawing glyphs the dialog frame is built from (top/bottom/side
# borders) — used both to detect "dialog open" and to assert "dialog
# gone" after a close.
_BORDER_GLYPHS = ('┌', '┐', '└', '┘', '│')


class _ModalUITest(unittest.TestCase):
    """Shared helpers for the keyboard-driven (tmux) dialog tests.

    Subclasses launch ``modal_demo.py`` under tmux over a populated UI
    (rows ALPHA-ROW / BETA-ROW / GAMMA-ROW, a three-line preview), press
    one bound key to open a dialog, drive it, then assert the recorded
    outcome and a strict restore.
    """

    def _assert_ui_restored(self, t):
        """Strict restore check (the cache-poison restore the design hinges on).

        Poll until the dialog's top border ``┌`` disappears — that's the
        actual restore signal — then assert NO border glyph, title, or
        dialog-only content survives on screen, AND the regular UI (rows,
        preview, info-bar hints) is back. Mirrors
        ``test_pick.py``'s ``TestPickRedrawOnExit._assert_ui_restored``.
        """
        deadline = time.time() + 3.0
        cap = t.capture()
        while time.time() < deadline and '┌' in cap:
            time.sleep(0.03)
            cap = t.capture()
        for glyph in _BORDER_GLYPHS:
            self.assertNotIn(glyph, cap,
                             f'dialog border {glyph!r} left on screen:\n{cap}')
        # Dialog-only text (titles + content that isn't part of the UI).
        for leftover in ('Confirm', 'Delete 3 items?', 'Note', 'Heads up',
                         'Name?', 'Rename'):
            self.assertNotIn(leftover, cap,
                             f'dialog text {leftover!r} left on screen:\n{cap}')
        # Regular UI is back: the rows, the preview, and the nav hints.
        self.assertIn('ALPHA-ROW', cap)
        self.assertIn('PREVIEW-LINE-THREE', cap)
        self.assertIn('q:quit', cap)


class TestModalConfirm(_ModalUITest):
    """``ctx.confirm`` — open, activate a button, assert the resolved label."""

    def test_confirm_no_via_hotkey(self):
        """Open confirm, press the ``&No`` hotkey, recipe records ``No``."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('c')
                # Box + a button label confirms the dialog is up.
                t.wait_for('Delete 3 items?')
                t.wait_for('Yes')
                # 'n' is the &No hotkey — activates that button directly.
                t.send('n')
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'No')
                self._assert_ui_restored(t)

    def test_confirm_yes_via_arrow_enter(self):
        """Arrow to a button then Enter; recipe records the focused label.

        First button (Yes) is focused initially; ``right`` moves focus to
        No, ``left`` back to Yes, then Enter activates it. Drives focus by
        keys (reverse-video, invisible to capture) and asserts the OUTCOME.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('c')
                t.wait_for('Delete 3 items?')
                t.send('Right')   # focus -> No
                t.send('Left')    # focus -> Yes
                t.send('Enter')   # activate Yes
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'Yes')
                self._assert_ui_restored(t)

    def test_confirm_cancel_with_esc(self):
        """Esc cancels confirm; recipe records ``<cancelled>`` (None)."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('c')
                t.wait_for('Delete 3 items?')
                t.send('Escape')
                content = _read_log_when_ready(log)
                self.assertEqual(content, '<cancelled>')
                self._assert_ui_restored(t)


class TestModalMenu(_ModalUITest):
    """``ctx.menu`` — anchored, unfiltered selection list."""

    def test_menu_renders_items_and_selects(self):
        """Open the menu (anchored), assert items render, choose one.

        The menu is anchored at the list cursor by default; assert the box
        renders with its items, then Down + Down + Enter lands on the third
        item ('Delete'). The selected row is reverse-video, so the chosen
        value is asserted via the recipe log.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('m')
                # The menu box renders with all items (no filter row).
                cap = t.wait_for('Rename')
                self.assertIn('Open', cap)
                self.assertIn('Delete', cap)
                # A box border confirms it's the anchored dialog, not the UI.
                self.assertTrue(any(g in cap for g in _BORDER_GLYPHS),
                                f'menu box border not found:\n{cap}')
                # Down twice from 'Open' -> 'Delete' (3rd item); Enter picks.
                t.send('Down')
                t.send('Down')
                t.send('Enter')
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'Delete')
                self._assert_ui_restored(t)

    def test_menu_cancel_with_esc(self):
        """Esc cancels the menu; recipe records ``<cancelled>``."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('m')
                t.wait_for('Rename')
                t.send('Escape')
                content = _read_log_when_ready(log)
                self.assertEqual(content, '<cancelled>')
                self._assert_ui_restored(t)


class TestModalInput(_ModalUITest):
    """``ctx.input`` — single-line text entry."""

    def test_input_type_and_enter(self):
        """Open input, type text, Enter; recipe records the string."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('i')
                t.wait_for('Name?')
                t.type('hello')
                t.send('Enter')
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'val:hello')
                self._assert_ui_restored(t)

    def test_input_cancel_with_esc(self):
        """Esc cancels input; recipe records ``<cancelled>`` (None, not '')."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('i')
                t.wait_for('Name?')
                t.type('discarded')
                t.send('Escape')
                content = _read_log_when_ready(log)
                self.assertEqual(content, '<cancelled>')
                self._assert_ui_restored(t)


class TestModalAlert(_ModalUITest):
    """``ctx.alert`` — single-button notification."""

    def test_alert_dismiss_with_enter(self):
        """Open the alert, dismiss with Enter; it closes and the UI restores."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('A')
                t.wait_for('Heads up')
                t.send('Enter')
                # Alert always returns None; the recipe records once dismissed.
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'alert:done')
                self._assert_ui_restored(t)

    def test_alert_dismiss_with_space(self):
        """A single-button alert also accepts ``space`` to dismiss."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('A')
                t.wait_for('Heads up')
                t.send('Space')
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'alert:done')
                self._assert_ui_restored(t)


class TestModalResize(_ModalUITest):
    """Resize while a dialog is open: the box repaints at the new geometry."""

    def _box_left_col(self, cap):
        """Left column (0-based) of the dialog's top-border ``┌`` row.

        Returns the index of ``┌`` within its line — the box's left edge.
        Used to confirm the box re-centers after a resize.
        """
        for line in cap.splitlines():
            i = line.find('┌')
            if i != -1:
                return i
        raise AssertionError(f'no top border on screen:\n{cap}')

    def test_resize_while_alert_open_repaints_and_restores(self):
        """Open an alert, resize the terminal, assert the box survives + moves.

        The resize path clears the screen and repaints ONLY the dialog
        (the panes stay blank until close). Verify the box is still present
        after the resize and re-centered for the new width, then dismiss and
        assert the full UI repaints back at the new geometry.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'modal.log')
            with TmuxFixture(cols=80, rows=24, env={'MODAL_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _DEMO)
                t.wait_for('ALPHA-ROW')
                t.send('A')
                t.wait_for('Heads up')
                before = t.wait_stable()
                left_before = self._box_left_col(before)

                # Grow the terminal while the dialog sits open.
                t.resize(120, 40)
                after = t.wait_stable()
                # The box must still be on screen (resize repainted the dialog).
                self.assertIn('Heads up', after)
                self.assertTrue(any(g in after for g in _BORDER_GLYPHS),
                                f'dialog box gone after resize:\n{after}')
                # Re-centered: a wider screen pushes the centered box's left
                # edge further right than it was at 80 cols.
                left_after = self._box_left_col(after)
                self.assertGreater(
                    left_after, left_before,
                    f'box not re-centered after resize '
                    f'({left_before} -> {left_after}):\n{after}')

                # Close and confirm the full UI repaints at the new geometry.
                t.send('Enter')
                content = _read_log_when_ready(log)
                self.assertEqual(content, 'alert:done')
                self._assert_ui_restored(t)


# ---------------------------------------------------------------------------
# Streaming input keeps flowing while a dialog is open
# ---------------------------------------------------------------------------


class _PtyModalStdinApp:
    """The streaming-modal recipe on a private pty, stdin piped from the test.

    Same idiom as ``test/ui/test_stdin_channel.py``'s ``_PtyStdinApp``: the
    test owns the pty master (keys in, frames out) AND the stdin pipe (feeds
    records). ``$MODAL_STDIN_LOG`` points at a file the recipe appends to as
    each record is ingested — observable the instant the ``on_stdin`` hook
    runs, independent of the suppressed screen repaint, so the test can prove
    ingestion continued WHILE a dialog held the screen.
    """

    def __init__(self, log_path, rows=30, cols=100):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
        self.stdin_r, self.stdin_w = os.pipe()
        env = dict(os.environ)
        env['MODAL_STDIN_LOG'] = log_path
        self.proc = subprocess.Popen(
            [_BIN, '--run-py', _STDIN_DEMO, '--tty', os.ttyname(self.slave)],
            stdin=self.stdin_r,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env)
        os.close(self.stdin_r)   # the child holds its own copy
        self.stdin_r = -1
        self._screen = b''

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stderr is not None and not self.proc.stderr.closed:
            self.proc.stderr.close()
        if self.stdin_w >= 0:
            os.close(self.stdin_w)
        os.close(self.master)
        os.close(self.slave)

    def feed(self, data):
        os.write(self.stdin_w, data)

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
                    break   # slave side gone (app exited)
        raise AssertionError(
            f'{needle!r} never appeared on the pty screen; last '
            f'{min(len(self._screen), 2000)} screen bytes:\n'
            f'{self._screen[-2000:]!r}')

    def stderr_text(self):
        return self.proc.stderr.read().decode('utf-8', 'replace')


def _wait_log_lines(path, predicate, timeout=5.0):
    """Poll ``path`` until ``predicate(lines)`` holds; return the lines.

    ``lines`` is the file's current non-empty lines (the recipe appends one
    per ingested record). Raises ``AssertionError`` on timeout.
    """
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        if os.path.exists(path):
            with open(path) as f:
                last = [ln for ln in f.read().splitlines() if ln]
            if predicate(last):
                return last
        time.sleep(0.03)
    raise AssertionError(
        f'log {path!r} never satisfied predicate within {timeout}s; '
        f'last lines: {last!r}')


class TestModalStreamingWhileOpen(unittest.TestCase):
    """A streaming-input recipe keeps ingesting while a dialog is open.

    The modal loop services ``_stdin`` events (calls ``browser._pump_stdin``),
    so an ``on_stdin`` hook must keep firing while a dialog blocks the panes.
    Proving this needs an observation channel independent of the screen (the
    dialog suppresses pane repaints): the recipe appends each ingested record
    to ``$MODAL_STDIN_LOG`` the instant the hook runs.

    Determinism note: rather than racing screen repaints, the test feeds a
    record AFTER the alert box is confirmed on screen and waits for that
    record to land in the log WHILE the box is still up — a direct,
    repaint-independent proof that ingestion continued during the dialog.
    """

    def test_stdin_ingested_while_alert_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'stdin.log')
            app = _PtyModalStdinApp(log)
            try:
                app.wait_screen('ready')
                # Feed one record BEFORE the dialog so the stream is proven
                # live first (ingested via the main loop).
                app.feed(b'before\n')
                _wait_log_lines(log, lambda ls: 'before' in ls)

                # Open the alert; wait until its box is up on the pty screen.
                app.keys('a')
                app.wait_screen('Heads up')

                # Now feed records WHILE the alert holds the screen. They are
                # serviced only by the modal loop's _stdin handling. The log
                # lines appear the instant the hook runs — assert they land
                # while the box is STILL up (no key sent to close it yet).
                app.feed(b'during1\nduring2\n')
                lines = _wait_log_lines(
                    log, lambda ls: 'during1' in ls and 'during2' in ls)
                self.assertEqual(
                    lines, ['before', 'during1', 'during2'],
                    'records fed during the dialog were not ingested in order')

                # The dialog is still open: dismiss it and confirm the UI
                # comes back and shows the rows that streamed in (post-close
                # repaint of the accumulated upserts).
                app.keys('\r')
                app.wait_screen('rec:during2')

                # One more record after close still streams (main loop again).
                app.feed(b'after\n')
                _wait_log_lines(
                    log, lambda ls: 'after' in ls,
                    timeout=5.0)

                app.keys('q')   # quit from the keyboard (cancel code)
                rc = app.proc.wait(timeout=10)
                err = app.stderr_text()
            finally:
                app.close()
            self.assertNotIn('Traceback', err)
            # 'q' at top level quits with the cancel code (1).
            self.assertEqual(rc, 1, f'unexpected exit; stderr:\n{err}')


if __name__ == '__main__':
    unittest.main()
