"""UI tests: wide-char (CJK / emoji) titles must not overflow list pane.

A title containing East Asian Wide / Fullwidth chars (e.g. ``日本語``,
emoji) takes 2 display cells per char, but ``len()`` only counts one.
Without wide-char-aware truncation in ``render_list``, a long wide-char
title would render past the list pane's right boundary and corrupt the
neighbouring pane (children grid or preview).

The test forces a vertical split so the list pane is narrower than the
terminal: any cell drawn past ``rect.right`` of the list pane is
visible in the capture and gets caught by the boundary asserts below.
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


class TestWideCharsListPane(unittest.TestCase):

    def test_cjk_title_does_not_overflow_list_pane(self):
        """A long CJK title must stay inside the list pane's column budget.

        ``split-type=v`` puts the list on the left and preview on the right.
        On an 80-col terminal with default ``list_ratio=0.3`` the list
        pane is ~24 columns wide; the separator ``│`` sits at the boundary.
        A title of seven repetitions of ``日本語`` (42 cells) would
        overflow without wide-char-aware truncation.

        Asserts:
          * The separator ``│`` is present on the cursor (top) row.
          * The substring before the separator contains only the truncated
            prefix — no characters from the item title appear after it.
        """
        with TmuxFixture(cols=80, rows=10) as t:
            t.launch(
                'bash', '-c',
                "printf '日本語日本語"
                "日本語日本語日本語"
                "日本語日本語END\\nshort\\n' | "
                f"{_BIN} --show-ids never --split-type v --root-cmd cat "
                f"--preview",
            )
            # Wait on the status-bar separator (drawn only after the TUI
            # paints) — using 'short' would match the echoed shell command
            # before browse-tui starts rendering.
            t.wait_for('Preview', timeout=3.0)
            t.wait_stable()
            cap = t.capture()
            lines = cap.split('\n')

            # The first list row (cursor) must contain the separator. Any
            # text after the separator must not look like overflowed list
            # content (no CJK chars on the right side of the separator).
            first = lines[0]
            self.assertIn('│', first,
                          f'list/preview separator missing on row 0: {first!r}')
            right_of_sep = first.split('│', 1)[1]
            for ch in '日本語':  # 日本語
                self.assertNotIn(
                    ch, right_of_sep,
                    f'CJK char {ch!r} leaked past list pane right edge: '
                    f'{first!r}')
            # The non-cursor row 'short' renders via the segment writer.
            # Same boundary invariant.
            second = lines[1]
            self.assertIn('│', second,
                          f'separator missing on row 1: {second!r}')

            t.send('q')

    def test_emoji_title_does_not_overflow_list_pane(self):
        """Emoji (East Asian Wide) titles must respect the pane boundary.

        Mirrors the CJK test using a wrench emoji (🔧, EAW=W) so the bug
        repros even on terminals without CJK fonts loaded.
        """
        with TmuxFixture(cols=80, rows=10) as t:
            # 20 wrench emojis + 'END' — 43 cells, far past a 24-col list.
            payload = '\U0001f527' * 20 + 'END'
            t.launch(
                'bash', '-c',
                f"printf '{payload}\\nshort\\n' | "
                f"{_BIN} --show-ids never --split-type v --root-cmd cat "
                f"--preview",
            )
            t.wait_for('Preview', timeout=3.0)
            t.wait_stable()
            cap = t.capture()
            first = cap.split('\n')[0]
            self.assertIn('│', first,
                          f'list/preview separator missing: {first!r}')
            right_of_sep = first.split('│', 1)[1]
            self.assertNotIn(
                '\U0001f527', right_of_sep,
                f'emoji leaked past list pane right edge: {first!r}')

            t.send('q')


if __name__ == '__main__':
    unittest.main()
