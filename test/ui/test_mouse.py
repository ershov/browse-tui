"""UI tests: mouse click + wheel handling end-to-end through tmux.

The tmux fixture sends raw SGR mouse escape sequences via
``send_literal_bytes`` (the ``-l`` flag bypasses tmux's key-name lookup).
SGR encoding (with ``\\033[?1006h`` enabled by browse-tui on startup):

  * Left click at (col=Cx, row=Cy): ``\\033[<0;Cx;CyM``
  * Wheel up at (Cx, Cy):           ``\\033[<64;Cx;CyM``
  * Wheel down at (Cx, Cy):         ``\\033[<65;Cx;CyM``

Note Cx (column) precedes Cy (row) on the wire — both 1-based.

Tests use ``wait_for`` against a post-event marker (rather than
``wait_stable``) because ``wait_stable`` returns immediately if the
event hasn't reached the program yet — the pre-event screen is itself
stable. Pinning to a value that only appears after the event handles
the race deterministically.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _click(t, row, col=5):
    t.send_literal_bytes(f'\x1b[<0;{col};{row}M')


def _wheel_down(t, row, col=5):
    t.send_literal_bytes(f'\x1b[<65;{col};{row}M')


def _wheel_up(t, row, col=5):
    t.send_literal_bytes(f'\x1b[<64;{col};{row}M')


def _cursor_row_text(colored_capture):
    """Return the text after the first reverse-video marker on the cursor row."""
    idx = colored_capture.find('\x1b[7m')
    if idx < 0:
        return ''
    return colored_capture[idx:idx + 120]


class TestMouseClick(unittest.TestCase):

    def test_click_on_third_list_row_moves_cursor_to_c(self):
        """Click at row 3 → cursor on the third item ('c')."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\nd\\ne\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('q:quit')
            t.wait_stable()
            # Initially cursor is on row 1 (item a). After the click it
            # should land on item c — wait until the reverse-video band
            # contains 'c' rather than relying on wait_stable.
            _click(t, row=3)
            for _ in range(20):  # ~1s worst case
                colored = t.capture(colors=True)
                if 'c' in _cursor_row_text(colored):
                    break
                import time as _t
                _t.sleep(0.05)
            after = t.capture(colors=True)
            cursor_window = _cursor_row_text(after)
            self.assertIn('c', cursor_window,
                          f'cursor row should contain c: {cursor_window!r}')

    def test_click_far_below_list_is_noop(self):
        """Clicks beyond the visible items don't move the cursor."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('q:quit')
            t.wait_stable()
            before_window = _cursor_row_text(t.capture(colors=True))
            # Row 22 is well past the 3-item list and into the preview pane.
            _click(t, row=22)
            # Give the program time to process the click (it might
            # momentarily redraw nothing — there's no positive marker
            # to wait for; sleep + final compare is sufficient here).
            import time as _t
            _t.sleep(0.3)
            after_window = _cursor_row_text(t.capture(colors=True))
            self.assertEqual(before_window, after_window)


class TestMouseWheel(unittest.TestCase):

    def _many_items_launch(self, t, n=40):
        items = '\\n'.join(f'item{i:02d}' for i in range(n))
        t.launch('bash', '-c',
                 f"printf '{items}\\n' | {_BIN} "
                 f"--show-ids always --root-cmd cat")

    def test_wheel_down_on_list_scrolls_viewport(self):
        """Wheel-down advances the list past row 0 — item03 becomes visible."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._many_items_launch(t, n=40)
            # Wait for the program's chrome (the help bar) before
            # asserting on item names — the launch command itself shows
            # 'item00' in the shell prompt; matching it would race the
            # program startup. The separator line only appears when
            # browse-tui is rendering.
            t.wait_for('q:quit')
            t.wait_stable()
            _wheel_down(t, row=2)
            # 3-line wheel scroll → item00..item02 leave, item07..item09
            # appear. Wait for item09 specifically (only present after a
            # ≥3-row scroll on a 7-row list pane).
            t.wait_for('item09', timeout=2.0)
            after = t.capture()
            self.assertNotIn('item00', after)

    def test_wheel_down_does_not_move_cursor(self):
        """After wheel scroll, j still advances the cursor by exactly 1 logical row."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._many_items_launch(t, n=40)
            # Wait for the program's chrome (the help bar) before
            # asserting on item names — the launch command itself shows
            # 'item00' in the shell prompt; matching it would race the
            # program startup. The separator line only appears when
            # browse-tui is rendering.
            t.wait_for('q:quit')
            t.wait_stable()
            # One wheel-down notch → scroll forward 3 rows; cursor stays
            # at index 0 logically (off-screen). Wait until item09 is
            # visible to confirm the wheel was processed.
            _wheel_down(t, row=2)
            t.wait_for('item09', timeout=2.0)
            t.send('j')
            # Cursor moves to index 1 → the snap helper rewinds the
            # viewport so item01 is visible again. Wait_for that.
            t.wait_for('item01', timeout=2.0)
            colored = t.capture(colors=True)
            cursor_window = _cursor_row_text(colored)
            self.assertIn('item01', cursor_window,
                          f'after j, cursor should land on item01: {cursor_window!r}')

    def test_wheel_up_clamps_at_top(self):
        """Wheel up at the top is harmless (scroll stays at 0)."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._many_items_launch(t, n=10)
            # Wait for the program's chrome (the help bar) before
            # asserting on item names — the launch command itself shows
            # 'item00' in the shell prompt; matching it would race the
            # program startup. The separator line only appears when
            # browse-tui is rendering.
            t.wait_for('q:quit')
            t.wait_stable()
            _wheel_up(t, row=2)
            # No positive marker — wheel-up at scroll=0 is a no-op. Sleep
            # briefly, then assert the screen is unchanged.
            import time as _t
            _t.sleep(0.3)
            after = t.capture()
            self.assertIn('item00', after)


if __name__ == '__main__':
    unittest.main()
