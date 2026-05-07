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
                '--show-ids', 'always',
            )
            t.wait_for('/bin/ls /bin/ls')
            # Wait for the preview worker to deliver. ``ELF`` is the
            # first three bytes of any /bin/ls binary on Linux — a cheap
            # signal that the sanitised preview content has reached the
            # screen.
            t.wait_for('ELF', timeout=5.0)
            cap = t.wait_stable()
            # The list-pane cursor row is intact (raw control bytes
            # would have rewritten it).
            self.assertIn('/bin/ls /bin/ls', cap)
            # The preview body must contain '?' markers — a binary blob
            # of 500 bytes will have many control chars to sanitise.
            # We don't count the '?:help' substring in the info bar:
            # require at least 5 question marks somewhere in the screen.
            self.assertGreaterEqual(
                cap.count('?'), 5,
                'binary preview did not yield enough ? markers — '
                'sanitiser may be inactive',
            )

    def test_ansi_in_preview_passes_through_to_terminal(self):
        """ANSI SGR in preview content reaches the terminal as colour codes.

        Per ticket #240 the preview pane intentionally renders SGR
        sequences. The default contract is: preview-cmd output
        ``\\x1b[31mRED\\x1b[0m`` colours "RED" red on the user's screen.
        Non-SGR escapes are still stripped by the walker (tested in
        the unit suite); other control bytes are still sanitised by
        ``_sanitize_preview`` (covered by the binary-preview test
        above). Toggling colours off via ``--no-preview-ansi`` /
        capital-R is covered by the dedicated ANSI UI tests.
        """
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(
                _BIN,
                '--children-cmd', 'echo file',
                '--preview-cmd', r'printf "\x1b[31mRED\x1b[0m"',
                '--no-children-pane',
                '--show-ids', 'always',
            )
            t.wait_for('file file')
            # The text reaches the screen — tmux's emulator interprets
            # the SGR codes, so they don't appear as literal characters
            # in the captured pane content.
            t.wait_for('RED', timeout=5.0)
            cap = t.wait_stable()
            self.assertIn('RED', cap)
            # The old sanitised marker must NOT appear: ESC is no
            # longer mapped to '?'.
            self.assertNotIn('?[31m', cap)
            self.assertNotIn('?[0m', cap)


if __name__ == '__main__':
    unittest.main()
