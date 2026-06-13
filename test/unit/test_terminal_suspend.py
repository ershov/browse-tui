"""term_suspend(keep_screen=...) — stay on the alt screen for a TUI child.

A plain editor/pager needs the terminal back on the primary screen, so the
default suspend emits the leave-alt-screen sequence (``\\033[?1049l``).
Handing off to another full-screen app that owns the alt screen itself
(launching another browse-tui recipe) must NOT leave the alt screen —
otherwise the primary screen flashes between the two UIs — and must emit no
clear. These tests assert exactly which control bytes each mode writes, with
the ``termios`` call short-circuited (``_saved_termios=None``) so no real
device is needed.
"""

import io
import unittest

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


if __name__ == '__main__':
    unittest.main()
