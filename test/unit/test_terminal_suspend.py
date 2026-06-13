"""Alt-screen entry/suspend control bytes for cross-recipe handoff.

``term_suspend(keep_screen=...)`` stays on the alt screen for a TUI child:
a plain editor/pager needs the terminal back on the primary screen, so the
default suspend emits the leave-alt-screen sequence (``\\033[?1049l``).
Handing off to another full-screen app that owns the alt screen itself
(launching another browse-tui recipe) must NOT leave the alt screen —
otherwise the primary screen flashes between the two UIs — and must emit no
clear. Symmetrically, ``_enter_raw`` clears the alt buffer on entry so the
child handed that kept alt screen starts from a blank canvas. These tests
assert exactly which control bytes each path writes, with the device calls
short-circuited so no real terminal is needed.
"""

import io
import unittest
from unittest import mock

from test.unit._loader import load


_LEAVE_ALT = b'\033[?1049l'
_SHOW_CURSOR = b'\033[?25h'
_MOUSE_OFF = b'\033[?1006l\033[?1000l'


class _Writer:
    """Stand-in for ``_tty_writer``: only ``.buffer`` (a BytesIO) is used."""

    def __init__(self):
        self.buffer = io.BytesIO()


def _term_armed():
    """Load the terminal module in a state where ``_leave_raw`` writes bytes.

    ``_in_raw=True`` so the body runs; ``_saved_termios=None`` so it skips the
    ``tcsetattr`` (no real fd needed); a fake writer captures the bytes.
    """
    term = load('_term_suspend_under_test', '020-terminal.py')
    term._in_raw = True
    term._saved_termios = None
    term._tty_writer = _Writer()
    return term


class TestSuspendKeepScreen(unittest.TestCase):

    def _emit(self, keep_screen, via_suspend=False):
        term = _term_armed()
        if via_suspend:
            term.term_suspend(keep_screen=keep_screen)
        else:
            term._leave_raw(keep_screen)
        self.assertFalse(term._in_raw)   # both modes clear the raw flag
        return term._tty_writer.buffer.getvalue()

    def test_default_leaves_alt_screen(self):
        out = self._emit(keep_screen=False)
        self.assertIn(_LEAVE_ALT, out)        # back to primary, for an editor
        self.assertIn(_SHOW_CURSOR, out)
        self.assertIn(_MOUSE_OFF, out)

    def test_keep_screen_stays_on_alt_screen(self):
        out = self._emit(keep_screen=True)
        self.assertNotIn(_LEAVE_ALT, out)     # stay on alt — no primary flash
        self.assertIn(_SHOW_CURSOR, out)      # but still reset cursor + mouse
        self.assertIn(_MOUSE_OFF, out)
        # And no clear — the child paints over our alt-screen buffer.
        self.assertNotIn(b'\033[2J', out)
        self.assertNotIn(b'\033[3J', out)

    def test_term_suspend_threads_keep_screen(self):
        self.assertNotIn(_LEAVE_ALT, self._emit(keep_screen=True, via_suspend=True))
        self.assertIn(_LEAVE_ALT, self._emit(keep_screen=False, via_suspend=True))


class TestEnterRawClearsScreen(unittest.TestCase):
    """Entering the alt screen clears it (``\\033[2J``).

    ``?1049h`` clears only when it actually switches into the alt buffer, NOT
    when a parent recipe kept the alt screen active for us
    (``run_external(keep_screen=True)``). The explicit clear gives such a
    nested child a blank canvas so the parent's UI doesn't show through.
    """

    def test_enter_raw_clears_after_alt_enter(self):
        term = load('_term_enter_under_test', '020-terminal.py')
        term._saved_termios = object()   # skip tcgetattr (no real fd needed)
        term._in_raw = False
        term._tty_fd_in = -1
        term._tty_writer = _Writer()
        with mock.patch.object(term.tty, 'setraw', lambda fd: None):
            term._enter_raw()
        out = term._tty_writer.buffer.getvalue()
        self.assertIn(b'\033[?1049h', out)        # entered the alt screen
        self.assertIn(b'\033[2J', out)            # ...and cleared it
        self.assertTrue(term._in_raw)
        # The clear must come AFTER the alt-screen enter (right buffer).
        self.assertLess(out.index(b'\033[?1049h'), out.index(b'\033[2J'))


class TestNoAltScreen(unittest.TestCase):
    """``_alt_screen=False`` runs on the current screen with no buffer switch.

    No ``?1049h`` on entry and no ``?1049l`` on leave; the clear still fires
    so the canvas is blank, and on leave the cursor parks at the bottom.
    """

    def _enter(self):
        term = load('_term_noalt_enter', '020-terminal.py')
        term._alt_screen = False
        term._saved_termios = object()   # skip tcgetattr
        term._in_raw = False
        term._tty_fd_in = -1
        term._tty_writer = _Writer()
        with mock.patch.object(term.tty, 'setraw', lambda fd: None):
            term._enter_raw()
        return term._tty_writer.buffer.getvalue()

    def _leave(self):
        term = load('_term_noalt_leave', '020-terminal.py')
        term._alt_screen = False
        term._saved_termios = None       # skip tcsetattr
        term._in_raw = True
        term._tty_fd_in = -1
        term._tty_writer = _Writer()
        term._leave_raw()
        return term._tty_writer.buffer.getvalue()

    def test_enter_skips_alt_switch_keeps_clear(self):
        out = self._enter()
        self.assertNotIn(b'\033[?1049h', out)   # no switch into the alt buffer
        self.assertIn(b'\033[2J', out)          # but still a blank canvas
        self.assertIn(b'\033[?25l', out)        # cursor still hidden for the UI

    def test_leave_skips_alt_switch_parks_cursor(self):
        out = self._leave()
        self.assertNotIn(b'\033[?1049l', out)   # no switch back to primary
        self.assertIn(b'\033[?25h', out)        # cursor shown again
        # Cursor parked at the bottom-left (row from term_size, col 1).
        self.assertRegex(out.decode('latin1'), r'\x1b\[\d+;1H')


if __name__ == '__main__':
    unittest.main()
