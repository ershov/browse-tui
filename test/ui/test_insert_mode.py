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
            t.launch(_BIN, '--run-py', _RECIPE)
            t.wait_for('a a')
            t.send('c')
            # Marker appears.
            screen = t.wait_for('-- create --')
            self.assertIn('-- create --', screen)

    def test_enter_confirms_with_after_relation(self):
        """Enter resolves to ('after', <cursor_id>) for the default placement."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'insert.log')
            with TmuxFixture(cols=120, rows=40, env={'INSERT_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('a a')
                t.send('c')
                t.wait_for('-- create --')
                t.send('Enter')
                content = _read_log_when_ready(log)
            # Cursor was on 'a' (has_children, depth 0). 'a' is
            # collapsed in the list (visible_items doesn't pre-expand),
            # so vis = [a, b, c]. Default insert_pos = cursor + 1 = 1
            # (between 'a' and 'b'); both at depth 0 → ('after', 'a').
            self.assertEqual(content, 'after:a')

    def test_marker_indents_on_right_and_outdents_on_left(self):
        """Right pushes the marker into the row above's subtree; Left undoes.

        Cursor starts on 'a' (a branch). Insert mode places the marker
        between 'a' and 'b' at depth 0 (rendered with the standard 2-col
        gutter). Right indents the marker into a's subtree (a expands;
        marker becomes a child of 'a' at depth 1, with an extra 2 cols
        of indentation). Left outdents and the marker moves above 'a'
        (depth 0 again, sitting at gap 0 since a's expanded children
        no longer host it).

        We assert on column position of the ``--`` marker token to keep
        the test resilient to surrounding text changes (id/title format).
        """
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--run-py', _RECIPE)
            t.wait_for('a a')
            t.wait_stable()
            t.send('c')
            t.wait_for('-- create --')
            initial = t.wait_stable()
            base_col = self._marker_col(initial)

            # Right indents — marker moves rightward.
            t.send('Right')
            indented = t.wait_stable()
            indent_col = self._marker_col(indented)
            self.assertGreater(
                indent_col, base_col,
                f'Right did not indent marker: base={base_col} '
                f'after_right={indent_col}\n{indented}')
            # Right also expanded 'a' (▼), so its kids should now show.
            self.assertIn('a1 a1', indented)

            # Left outdents — marker moves back leftward (to ≤ base col).
            t.send('Left')
            outdented = t.wait_stable()
            outdent_col = self._marker_col(outdented)
            self.assertLess(
                outdent_col, indent_col,
                f'Left did not outdent marker: '
                f'after_right={indent_col} after_left={outdent_col}\n{outdented}')

            # Cancel + quit.
            t.send_bytes('\x1b')
            t.send('x')

    @staticmethod
    def _marker_col(screen):
        """Column index (0-based) of the '--' opener of the create marker.

        Searches each line for the substring ' -- create -- ' the
        renderer emits (note the leading and trailing spaces — those
        come from the marker title's literal format ``' -- {} -- '``)
        and returns the column of the first ``-`` of the leading dashes.
        """
        for line in screen.splitlines():
            idx = line.find('-- create --')
            if idx >= 0:
                return idx
        raise AssertionError(f'create marker not found in screen:\n{screen}')

    def test_esc_cancels(self):
        """Esc cancels insert mode without firing the callback."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'insert.log')
            with TmuxFixture(cols=120, rows=40, env={'INSERT_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('a a')
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
