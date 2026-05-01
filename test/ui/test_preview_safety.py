"""UI tests: preview pane never emits raw control chars (ticket #82).

Files containing binary data or ANSI escape sequences must not be able
to break renderer column-tracking, change the terminal's colour, beep,
or otherwise inject control sequences into our session. Both the
binary-file case and a literal-ESC preview command are covered.

These tests use ``--children-cmd`` (lazy mode) rather than
``--root-cmd``: the lazy mode fires the initial preview fetch on
startup; the eager mode populates the children cache up front but does
not re-fire ``_update_preview_for_cursor`` for the initial cursor row,
so the preview pane stays blank until a cursor move. We don't need to
exercise eager mode — the sanitiser code path is identical for both.
"""

import os
import shutil
import subprocess
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


class TestPreviewSafety(unittest.TestCase):

    def test_binary_preview_no_garbled_output(self):
        """Previewing a binary file shows '?' markers, never raw control bytes.

        ``head -c 500 /bin/ls`` returns a chunk of an ELF executable — a
        rich source of NULs, control bytes, and stray ESCs. The
        sanitiser should turn every < 32 byte (except tab/LF) into '?'
        before it hits the screen, so the cursor row in the list pane
        stays intact and the preview pane fills up with ASCII '?'
        markers instead of raw bytes.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(
                _BIN,
                '--children-cmd', 'echo /bin/ls',
                '--preview-cmd', 'head -c 500 "$TUI_ID"',
                '--no-children-pane',
            )
            t.wait_for('#/bin/ls /bin/ls')
            # Wait for the preview worker to deliver. ``ELF`` is the
            # first three bytes of any /bin/ls binary on Linux — a cheap
            # signal that the sanitised preview content has reached the
            # screen.
            t.wait_for('ELF', timeout=5.0)
            cap = t.wait_stable()
            # The list-pane cursor row is intact (raw control bytes
            # would have rewritten it).
            self.assertIn('#/bin/ls /bin/ls', cap)
            # The preview body must contain '?' markers — a binary blob
            # of 500 bytes will have many control chars to sanitise.
            # We don't count the '?:help' substring in the info bar:
            # require at least 5 question marks somewhere in the screen.
            self.assertGreaterEqual(
                cap.count('?'), 5,
                'binary preview did not yield enough ? markers — '
                'sanitiser may be inactive',
            )

    def test_ansi_in_preview_does_not_color_screen(self):
        """A literal ANSI escape in preview text becomes harmless '?[…]m'.

        Without sanitisation the preview-cmd output ``\\x1b[31mRED\\x1b[0m``
        would set the terminal's foreground to red, write 'RED', then
        reset — a textbook injection. The sanitiser swaps each ESC
        (\\x1b) for '?' so the rendered text is just "?[31mRED?[0m".
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(
                _BIN,
                '--children-cmd', 'echo file',
                '--preview-cmd', r'printf "\x1b[31mRED\x1b[0m"',
                '--no-children-pane',
            )
            t.wait_for('#file file')
            # Wait for the preview to land — looking for the sanitised
            # marker tells us both that the preview rendered AND that
            # the ESC byte was replaced with '?'.
            t.wait_for('?[31mRED?[0m', timeout=5.0)
            cap = t.wait_stable()
            self.assertIn('?[31mRED?[0m', cap)
            # Negative check: the raw ESC byte must NOT appear anywhere
            # in the captured output (a real ESC there would have been
            # interpreted as an SGR sequence by tmux's emulator and not
            # captured as text — but if it were rendered before the
            # sanitiser sees it, we'd find at least the literal "[31m"
            # without a leading '?'). The positive check above is the
            # primary assertion.
            self.assertNotIn('\x1b[31m', cap)


if __name__ == '__main__':
    unittest.main()
