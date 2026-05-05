"""UI tests: initial render, j/k navigation, expand/collapse, quit."""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_NAV_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'slow_children.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestNavigation(unittest.TestCase):

    def test_initial_render_three_items(self):
        """A flat 3-item tree renders as a/b/c."""
        # --show-ids always pins the row layout to 'id title' so the
        # 'a a' / 'b b' / 'c c' substrings reliably anchor each row.
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            screen = t.wait_for('a a')
            screen = t.wait_stable()
            self.assertIn('a a', screen)
            self.assertIn('b b', screen)
            self.assertIn('c c', screen)

    def test_down_arrow_moves_cursor(self):
        """Pressing Down moves the cursor (verified via reverse-video region)."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('a a')
            t.wait_stable()
            # In a plain capture the cursor is invisible; capture with
            # ANSI codes shows the [7m reverse-video sequence on the
            # cursor row. We diff before/after to confirm movement.
            before_color = t.capture(colors=True)
            t.send('Down')
            t.wait_stable()
            after_color = t.capture(colors=True)
            self.assertNotEqual(before_color, after_color,
                                'cursor did not move on Down arrow')
            # b should now be on the reverse-video row. The pattern is
            # ESC [ 7 m followed by the row text; we check the cursor
            # marker is on a line that contains 'b'.
            self.assertIn('\x1b[7m', after_color)
            # Find the [7m chunk and confirm "b" follows shortly.
            idx = after_color.index('\x1b[7m')
            window = after_color[idx:idx + 80]
            self.assertIn('b', window,
                          f'cursor row does not contain b: {window!r}')

    def test_q_quits_with_cancel_code(self):
        """q exits the TUI with the cancel exit code (1)."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat ; "
                     f"echo EXIT=$?")
            t.wait_for('a a')
            t.send('q')
            t.wait_for('EXIT=1', timeout=3.0)

    def test_expand_and_collapse_with_children_cmd(self):
        """Right expands a parent; Left collapses it."""
        with TmuxFixture(cols=80, rows=24) as t:
            # The slow_children recipe returns one parent immediately,
            # then a small fixed list when expanded. A small delay keeps
            # the fetch fast for tests but is still long enough that any
            # latent loading state is observable on slower hosts.
            # ``--no-children-pane`` disables the grid pane so this test
            # only inspects the list-pane collapse behaviour.
            t.launch(_BIN, '--python', _NAV_RECIPE, '--',
                     '0.05', '--no-children-pane')
            # The recipe sets id == title for every Item, so show_ids
            # auto-mode renders just the title (no leading id segment).
            t.wait_for('parent')
            t.send('Right')
            t.wait_for('alpha', timeout=3.0)
            t.send('Left')
            # Left collapse happens synchronously on the main thread —
            # no worker round-trip. wait_stable settles the cell-diff.
            screen = t.wait_stable()
            self.assertNotIn('alpha', screen)
            self.assertIn('parent', screen)

    def test_page_down_jump_size_tracks_terminal_height(self):
        """PageDown jumps by list-pane height (ticket #75), not a fixed 10.

        At 24 rows the list pane is ~7 rows tall, so PageDown advances
        the cursor onto roughly the 8th item (index 7). At 40 rows the
        list pane grows to ~12 rows, so PageDown advances further.

        We feed many items (i00..i49) so the page jump can't be clamped
        to the end of the list, and check that the reverse-video cursor
        row contains a different item id at each height — and crucially
        that the bigger terminal jumped further than the smaller one.
        """
        items_input = ''.join(f'i{i:02d}\\n' for i in range(50))
        cmd = (f"printf '{items_input}' | {_BIN} "
               f"--show-ids always --root-cmd cat "
               f"--no-children-pane --no-preview")

        # 24-row run.
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c', cmd)
            t.wait_for('i00 i00')
            t.wait_stable()
            t.send('PageDown')
            t.wait_stable()
            screen_small = t.capture(colors=True)

        # 40-row run.
        with TmuxFixture(cols=80, rows=40) as t:
            t.launch('bash', '-c', cmd)
            t.wait_for('i00 i00')
            t.wait_stable()
            t.send('PageDown')
            t.wait_stable()
            screen_big = t.capture(colors=True)

        # Extract the item id sitting on the cursor (reverse-video row)
        # in each screen. The cursor is rendered with ESC[7m + row text.
        def _cursor_id(screen):
            self.assertIn('\x1b[7m', screen, 'no cursor marker found')
            idx = screen.index('\x1b[7m')
            window = screen[idx:idx + 200]
            # Find iNN in the window — the rendered row format is
            # "  iNN iNN ..." (with --show-ids always) with possibly
            # intervening style codes.
            import re
            m = re.search(r'i(\d{2})', window)
            self.assertIsNotNone(m, f'no item id on cursor row: {window!r}')
            return int(m.group(1))

        cursor_small = _cursor_id(screen_small)
        cursor_big = _cursor_id(screen_big)

        # The 40-row terminal must have jumped strictly further than the
        # 24-row one — the whole point of ticket #75. Both must be > 0
        # (PageDown actually moved) and the small case must not be 10
        # (the old hard-coded value would always land on i10).
        self.assertGreater(cursor_small, 0,
                           'PageDown did not move on 24-row terminal')
        self.assertGreater(cursor_big, cursor_small,
                           f'PageDown on 40-row ({cursor_big}) did not '
                           f'jump further than on 24-row ({cursor_small})')
