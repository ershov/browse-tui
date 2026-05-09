"""UI tests: scroll-driven preview generator resume (#274).

Builds on #273 (eager-pull-then-pause). The renderer detects when the
preview viewport is within a few rows of the buffered tail and signals
the paused worker to keep pulling. This test launches a recipe whose
``get_preview`` is a generator that yields ``LINE_001…LINE_200`` and
verifies that scrolling the preview down brings later lines into view
even though the initial pause was at line 30.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RESUME_RECIPE = os.path.join(
    _REPO, 'test', 'ui', 'recipes', 'preview_resume.py'
)


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestPreviewResumeScroll(unittest.TestCase):
    """Scrolling preview near the buffered tail wakes the paused worker."""

    def test_shift_down_reveals_lines_beyond_initial_cap(self):
        # Recipe pauses at LINE_030 (cap=30 lines). The UI test scrolls
        # past that and verifies later lines arrive — they only do so
        # if the renderer's demand-signal woke the paused worker.
        with TmuxFixture(cols=80, rows=40) as t:
            t.launch(_BIN, '--run-py', _RESUME_RECIPE)
            # Wait for early lines to appear in the preview pane.
            t.wait_for('LINE_001', timeout=3.0)
            t.wait_for('LINE_010', timeout=2.0)

            # Scroll the preview down with alt-pgdn until LINE_100
            # comes into view. Each press scrolls by one preview-pane
            # page; the renderer fires the demand signal whenever the
            # viewport bottom is within the threshold of the buffered
            # tail. The wait_for poll between presses lets the worker
            # resume and append new chunks.
            for _ in range(15):
                t.send('M-PageDown')
                screen = t.capture()
                if 'LINE_100' in screen:
                    break
            t.wait_for('LINE_100', timeout=3.0)

            # Continue until the very last line — sanity that resume
            # works through to generator exhaustion.
            for _ in range(15):
                t.send('M-PageDown')
                screen = t.capture()
                if 'LINE_200' in screen:
                    break
            t.wait_for('LINE_200', timeout=3.0)


if __name__ == '__main__':
    unittest.main()
