"""UI tests for multi-select keybindings.

Drives the production binary through tmux and asserts the visible side
effects: the ``*`` row marker, the ``[N]`` selection-count badge in the
info bar, ctrl-a / ctrl-n bulk operations, and that a CLI-installed
``--action`` (which uses ``requires='targets'``) sees the multi-row
selection in ``$TUI_IDS_FILE`` / ``$TUI_IDS_COUNT``.
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


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestMultiSelect(unittest.TestCase):

    def test_space_marks_item_with_asterisk(self):
        """Space marks the cursor row, advances the cursor, and shows '*'."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.wait_stable()
            t.send('Space')
            screen = t.wait_for('* ')
            # The '* ' marker must appear on the #a row (first row).
            # Renderer uses two-space gutter so '*   #a' is the layout.
            self.assertRegex(screen, r'\*\s+#a a')
            # Selection count badge appears in the info bar.
            self.assertIn('[1]', screen)
            t.send('q')

    def test_selection_count_appears_in_info_bar(self):
        """Two space presses bump the [N] badge from [1] to [2]."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.wait_stable()
            t.send('Space')
            t.wait_for('[1]')
            t.send('Space')
            screen = t.wait_for('[2]')
            # Both first two rows have a marker.
            self.assertRegex(screen, r'\*\s+#a a')
            self.assertRegex(screen, r'\*\s+#b b')
            t.send('q')

    def test_ctrl_a_marks_all_visible(self):
        """Ctrl-A selects every visible normal row."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.wait_stable()
            t.send('C-a')
            screen = t.wait_for('[3]')
            self.assertRegex(screen, r'\*\s+#a a')
            self.assertRegex(screen, r'\*\s+#b b')
            self.assertRegex(screen, r'\*\s+#c c')
            t.send('q')

    def test_ctrl_n_clears_selection(self):
        """Ctrl-N drops every marker and removes the [N] badge."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} --root-cmd cat")
            t.wait_for('#a a')
            t.wait_stable()
            t.send('C-a')
            t.wait_for('[3]')
            t.send('C-n')
            # After clear, neither the badge nor any '* '+'#' marker is
            # present on a row. wait_stable settles the cell-diff.
            screen = t.wait_stable()
            self.assertNotIn('[3]', screen)
            self.assertNotIn('[2]', screen)
            self.assertNotIn('[1]', screen)
            self.assertNotRegex(screen, r'\*\s+#a a')
            self.assertNotRegex(screen, r'\*\s+#b b')
            self.assertNotRegex(screen, r'\*\s+#c c')
            t.send('q')

    def test_targets_action_uses_selection(self):
        """A CLI --action (requires='targets') sees the full selection."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'log.txt')
            # Action records TUI_IDS_COUNT and the NUL-separated ids file
            # contents (translated to newlines, with a trailing newline
            # before DONE so the last id sits on its own line).
            action_cmd = (
                f'echo "count=$TUI_IDS_COUNT" >> {log} ; '
                f'tr "\\0" "\\n" < "$TUI_IDS_FILE" >> {log} ; '
                f'echo "" >> {log} ; '
                f'echo DONE >> {log}')
            with TmuxFixture(cols=80, rows=24) as t:
                t.launch('bash', '-c',
                         f"printf 'a\\nb\\nc\\n' | "
                         f"{_BIN} --root-cmd cat "
                         f"--action 'e:Edit:{action_cmd}'")
                t.wait_for('#a a')
                t.wait_stable()
                # Select two rows with space-space, then press 'e'.
                t.send('Space')
                t.wait_for('[1]')
                t.send('Space')
                t.wait_for('[2]')
                t.send('e')
                deadline = time.time() + 3.0
                content = ''
                while time.time() < deadline:
                    if os.path.exists(log):
                        with open(log) as f:
                            content = f.read()
                        if 'DONE' in content:
                            break
                    time.sleep(0.03)
                else:
                    self.fail(f'action did not complete within 3s: {log}')
                t.send('q')
            self.assertIn('count=2', content)
            # Both selected ids landed in $TUI_IDS_FILE (NUL-separated,
            # then translated to newlines).
            lines = set(content.splitlines())
            self.assertIn('a', lines)
            self.assertIn('b', lines)


if __name__ == '__main__':
    unittest.main()
