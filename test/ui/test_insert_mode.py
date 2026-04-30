"""UI tests for insert mode — placement marker + (relation, dest_id) (ticket #21).

Drives the insert flow end-to-end: launches ``insert_demo`` in a real
terminal under tmux, presses the bound key 'c' to enter insert mode,
then exercises:

  * marker-visible:  the ``-- create --`` row appears on screen.
  * confirm-after:   pressing enter resolves to ``after:<cursor_id>``.
  * cancel-esc:      pressing esc returns to nav mode without firing
                     the callback (we then exercise a separate 'x'
                     action that writes ``<cancelled>`` to confirm we
                     actually left insert mode).
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'insert_demo.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _read_log_when_ready(path, timeout=3.0):
    """Poll ``path`` until it exists with non-empty content."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read()
            if content:
                return content
        time.sleep(0.03)
    raise AssertionError(
        f'log file {path!r} not populated within {timeout}s')


class TestInsertMode(unittest.TestCase):

    def test_insert_marker_appears(self):
        """Press 'c' — the '-- create --' marker shows up in the list."""
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--python', _RECIPE)
            t.wait_for('#a')
            t.send('c')
            # Marker appears.
            screen = t.wait_for('-- create --')
            self.assertIn('-- create --', screen)

    def test_enter_confirms_with_after_relation(self):
        """Enter resolves to ('after', <cursor_id>) for the default placement."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'insert.log')
            with TmuxFixture(cols=120, rows=40, env={'INSERT_LOG': log}) as t:
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('#a')
                t.send('c')
                t.wait_for('-- create --')
                t.send('Enter')
                content = _read_log_when_ready(log)
            # Cursor was on 'a' (has_children, depth 0). 'a' is
            # collapsed in the list (visible_items doesn't pre-expand),
            # so vis = [a, b, c]. Default insert_pos = cursor + 1 = 1
            # (between 'a' and 'b'); both at depth 0 → ('after', 'a').
            self.assertEqual(content, 'after:a')

    def test_esc_cancels(self):
        """Esc cancels insert mode without firing the callback."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'insert.log')
            with TmuxFixture(cols=120, rows=40, env={'INSERT_LOG': log}) as t:
                t.launch(_BIN, '--python', _RECIPE)
                t.wait_for('#a')
                t.send('c')
                t.wait_for('-- create --')
                t.send_bytes('\x1b')
                # Poll for the marker to disappear — confirms ESC was
                # delivered as bare ``\x1b`` (and not coalesced with a
                # subsequent byte into ``alt-*`` via the terminal
                # layer's 50ms peek timeout).
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if '-- create --' not in t.capture():
                        break
                    time.sleep(0.05)
                else:
                    self.fail('insert marker did not disappear after Esc')
                # Now press 'x' (a nav-mode-only action) so the program
                # writes the ``<cancelled>`` log and quits — confirms
                # we left insert mode cleanly.
                t.send('x')
                content = _read_log_when_ready(log)
            self.assertEqual(content, '<cancelled>')


if __name__ == '__main__':
    unittest.main()
