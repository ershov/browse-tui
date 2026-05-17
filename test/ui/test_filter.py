"""UI tests: filter-mode prompt entry / typing / commit / clear.

Covers the `&` keybinding flow:
  * `&` enters filter-edit mode and shows the `&` prompt in the info bar
  * typed characters narrow the visible list live
  * Enter commits the filter; the prompt closes but the narrowing stays
  * `&` again stacks another filter (AND semantics)
  * Ctrl-X clears all filters and exits filter-edit mode
  * Ctrl-C cancels the in-progress edit, keeping committed filters
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


class TestFilter(unittest.TestCase):

    def test_ampersand_enters_filter_mode_and_shows_prompt(self):
        """`&` then text shows the prompt and narrows the list."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            # Prompt visible in info bar.
            t.wait_for('& app_', timeout=2.0)
            screen = t.wait_stable()
            # Non-matching items dropped from view.
            self.assertNotIn('banana', screen)
            self.assertNotIn('cherry', screen)
            self.assertIn('apple', screen)

    def test_enter_commits_filter_and_closes_prompt(self):
        """After Enter the prompt loses its underscore but stays visible
        as a committed filter."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            t.wait_for('& app_', timeout=2.0)
            t.send('Enter')
            # Committed display: trailing underscore is gone.
            t.wait_for('& app', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('& app_', screen)
            self.assertNotIn('banana', screen)

    def test_ctrl_x_clears_all_filters(self):
        """Ctrl-X drops every committed filter and exits filter-edit.

        Ctrl-X is bound inside FILTER_EDIT mode, so the user re-enters
        with ``&`` before pressing it.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            t.send('Enter')
            t.wait_for('& app', timeout=2.0)
            # Re-enter filter-edit then Ctrl-X clears.
            t.send('&')
            t.send_bytes('\x18')   # ctrl-x
            # Filter prompt gone; banana / cherry back.
            t.wait_for('banana', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('& app', screen)

    def test_ctrl_c_cancels_in_progress_keeps_committed(self):
        """Ctrl-C drops the in-progress filter but keeps committed ones."""
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch('bash', '-c',
                     f"printf 'apple\\nbanana\\ncherry\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('apple apple')
            t.send('&')
            t.type('app')
            t.send('Enter')
            # Now start another filter but cancel.
            t.send('&')
            t.type('xyz')
            t.wait_for('& app & xyz_', timeout=2.0)
            t.send_bytes('\x03')   # ctrl-c
            t.wait_for('& app', timeout=2.0)
            screen = t.capture()
            self.assertNotIn('xyz', screen)
            self.assertNotIn('banana', screen)


if __name__ == '__main__':
    unittest.main()
