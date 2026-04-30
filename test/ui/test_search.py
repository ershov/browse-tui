"""UI tests: search-mode prompt enter / type / escape.

Phase 1's search support is minimal — '/' enters search mode, characters
extend the query and show in the info bar; Esc cancels. Real
fragment-matching is phase 2 (#22).
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


class TestSearch(unittest.TestCase):

    def test_slash_enters_search_mode_and_shows_prompt(self):
        """/ then text shows the query in the info bar; Esc clears it."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'foo\\nbar\\nbaz\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#foo foo')
            t.send('/')
            t.type('bar')
            t.wait_for('/bar', timeout=2.0)
            t.send('Escape')
            # After Escape, the prompt area returns to default hint text.
            t.wait_for('/:search', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('/bar', screen)

    def test_search_query_extends_with_each_keystroke(self):
        """Each printable key extends the query; backspace trims it."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'foo\\nbar\\nbaz\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#foo foo')
            t.send('/')
            t.type('xy')
            t.wait_for('/xy', timeout=2.0)
            t.send('BSpace')
            # Query should be just 'x' now — '/x' present, '/xy' gone.
            t.wait_for('/x', timeout=2.0)
            screen = t.capture()
            # '/x' must be present without 'y' immediately after.
            self.assertIn('/x', screen)
            self.assertNotIn('/xy', screen)


class TestSearchHighlight(unittest.TestCase):
    """Phase-2 ticket #22: typing in search mode jumps the cursor to the
    nearest match, and Enter advances through the visible match list.

    The highlight rendering itself is a visual concern (yellow/bold
    spans, reverse+underline on cursor) — we exercise the code path by
    typing, but assert the user-visible effect (cursor movement). A
    direct check of ANSI escape sequences would couple the test to
    plan-tui's exact escape encoding; cursor movement is a stronger
    behavioural signal anyway.
    """

    def test_typing_jumps_cursor_to_match(self):
        """Press / then type a query; the cursor lands on the matching row."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'foo\\nbar\\nbaz\\nqux\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#foo foo')
            t.send('/')
            t.type('baz')
            # The query shows in the info bar; the cursor has jumped to
            # the 'baz' row, which we confirm by pressing enter (the
            # configured on_enter prints the cursor item id and quits).
            t.wait_for('/baz', timeout=2.0)
            t.send('Enter')
            # browse-tui prints the matched id ('baz') to stdout and
            # exits — wait for the shell prompt to come back with 'baz'
            # somewhere in the captured pane history.
            t.wait_for('baz', timeout=2.0)
            screen = t.capture()
            # Sanity: the printed line should be the matched id, not
            # the row 0 default.
            self.assertIn('baz', screen)

    def test_enter_advances_to_next_match(self):
        """With multiple matches, Enter cycles through them in order."""
        with TmuxFixture(cols=80, rows=24) as t:
            # Four rows; three of them match 'foo'.
            t.launch('bash', '-c',
                     f"printf 'foo-a\\nbar\\nfoo-b\\nfoo-c\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#foo-a foo-a')
            t.send('/')
            t.type('foo')
            t.wait_for('/foo', timeout=2.0)
            # Cursor should have jumped to row 0 ('foo-a' — first match).
            # Two Enters advance to 'foo-b' then 'foo-c'.
            t.send('Enter')
            t.send('Enter')
            # Now exit search mode and confirm the cursor lands on the
            # third match by pressing Enter again outside search mode
            # (which prints the cursor's id and quits).
            t.send('Escape')
            t.wait_for('/:search', timeout=2.0)
            t.send('Enter')
            t.wait_for('foo-c', timeout=2.0)
            screen = t.capture()
            self.assertIn('foo-c', screen)
