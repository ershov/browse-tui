"""UI tests for ctx.pick — fzf-style selection in a modal dialog.

Drives the picker end-to-end: launches the pick_demo recipe in a real
terminal under tmux, presses the bound key 's' to invoke the picker,
then exercises filter / cursor / select / cancel paths. The recipe
writes the chosen string (or '<cancelled>') to a tempfile and quits;
each test polls that file to confirm the picker returned the expected
value.

``ctx.pick`` opens a centered modal dialog (ticket #975): the label
renders in the box border (``┌─ Status ─…─┐``) and the filter prompt is
a ``> {query}`` row, so the picker no longer shows the old info-bar
``Status>`` string. Tests detect "picker open" by an option line that
isn't in the underlying UI (``in-progress``), and assert the chosen
value via the recipe's recorded log rather than by the selected row's
text (tmux text capture is blind to the reverse-video highlight).
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
_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'pick_demo.py')
_NOQUIT_RECIPE = os.path.join(
    _REPO, 'test', 'ui', 'recipes', 'pick_noquit_demo.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _read_log_when_ready(path, timeout=3.0):
    """Poll ``path`` until it exists with non-empty content. Returns the text."""
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


class TestPick(unittest.TestCase):

    def test_pick_select_with_enter(self):
        """Type to filter, press enter, recipe records the matching option."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                # Wait for the modal to open — an option line that isn't in
                # the underlying UI confirms the dialog is up.
                t.wait_for('in-progress')
                # 'in' filters to 'in-progress'; enter selects.
                t.send('i')
                t.send('n')
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'in-progress')

    def test_pick_cancel_with_esc(self):
        """Esc cancels the picker; recipe records '<cancelled>'."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('in-progress')
                t.send('Escape')
                content = _read_log_when_ready(log)
            self.assertEqual(content, '<cancelled>')

    def test_pick_filter_narrows_to_unique_match(self):
        """Type 'do' — only 'done' matches; Enter selects it.

        The pick recipe offers ['open', 'in-progress', 'done', 'wontfix'].
        'do' is a substring of 'done' only (not 'open' / 'in-progress' /
        'wontfix'), so the picker filters to one row and Enter picks it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('in-progress')
                # Type the filter then select; assert the recorded OUTCOME
                # rather than the on-screen filtered list (the filter row's
                # repaint isn't a reliable synchronization point under tmux,
                # but the filter logic narrows to the unique match).
                t.type('do')
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'done')

    def test_pick_navigate_with_arrows(self):
        """Arrow keys reposition the picker cursor before enter."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'pick.log')
            with TmuxFixture(cols=120, rows=40, env={'PICK_LOG': log}) as t:
                t.launch(_BIN, '--run-py', _RECIPE)
                t.wait_for('item one')
                t.send('s')
                t.wait_for('in-progress')
                # Down twice from 'open' lands on 'done' (3rd item). The
                # selected row is reverse-video (invisible to text capture),
                # so we assert the OUTCOME via the recipe's recorded log.
                t.send('Down')
                t.send('Down')
                t.send('Enter')
                content = _read_log_when_ready(log)
            self.assertEqual(content, 'done')


class TestPickRedrawOnExit(unittest.TestCase):
    """Regression: after pick() returns to the UI, the dialog repaints away.

    Ticket #719 / #975. The modal dialog paints over the regular UI through
    a private row cache; on close it poisons the intersecting pane-cache
    rows so the next render fully repaints the regular UI over the dialog
    cells (cache-poison restore). If that restore regressed, the dialog's
    box border / title / option rows would survive on screen.

    The no-quit recipe stays running so we can capture the post-pick
    screen — the quit-after-pick path can't show the bug because teardown
    leaves the alternate screen entirely.
    """

    def _assert_ui_restored(self, t):
        # The centered dialog only partially covers the preview pane, so
        # waiting for preview text to reappear would race the restore
        # repaint (the text is visible even while the box is up). Poll for
        # the dialog box's top border to DISAPPEAR — that's the actual
        # cache-poison-restore signal.
        deadline = time.time() + 3.0
        cap = t.capture()
        while time.time() < deadline and '┌' in cap:
            time.sleep(0.03)
            cap = t.capture()
        # Dialog chrome must be gone: no box-border glyphs, no title.
        for glyph in ('┌', '┐', '└', '┘', '│'):
            self.assertNotIn(glyph, cap,
                             f'dialog border {glyph!r} left on screen:\n{cap}')
        self.assertNotIn('Status', cap,
                         f'dialog title left on screen:\n{cap}')
        for opt in ('open', 'in-progress', 'done', 'wontfix'):
            self.assertNotIn(opt, cap,
                             f'picker option {opt!r} left on screen:\n{cap}')
        # Regular UI must be back: rows, preview content, info-bar hints.
        self.assertIn('ALPHA-ROW', cap)
        self.assertIn('PREVIEW-LINE-THREE', cap)
        self.assertIn('q:quit', cap)

    def test_overlay_gone_after_cancel(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _NOQUIT_RECIPE)
            t.wait_for('ALPHA-ROW')
            t.send('s')
            t.wait_for('in-progress')
            t.send('Escape')
            self._assert_ui_restored(t)

    def test_overlay_gone_after_select(self):
        with TmuxFixture(cols=80, rows=24) as t:
            t.launch(_BIN, '--run-py', _NOQUIT_RECIPE)
            t.wait_for('ALPHA-ROW')
            t.send('s')
            t.wait_for('in-progress')
            t.send('Enter')
            self._assert_ui_restored(t)


if __name__ == '__main__':
    unittest.main()
