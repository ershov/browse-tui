"""UI test: resize the tmux window mid-session and confirm the TUI reflows."""

import os
import re
import shutil
import subprocess
import time
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GRID_RECIPE = os.path.join(_REPO, 'test', 'ui', 'recipes', 'children_grid.py')
_ON_RESIZE_RECIPE = os.path.join(
    _REPO, 'test', 'ui', 'recipes', 'on_resize_probe.py')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


class TestResize(unittest.TestCase):

    def test_resize_reflows_and_does_not_crash(self):
        """Shrinking the window from 120x40 to 60x20 reflows without crash."""
        with TmuxFixture(cols=120, rows=40) as t:
            # --show-ids always pins the row layout so 'a a' is a stable
            # substring even though the input has id == title.
            t.launch('bash', '-c',
                     f"printf 'a\\nb\\nc\\n' | {_BIN} "
                     f"--show-ids always --root-cmd cat")
            t.wait_for('a a')
            t.resize(60, 20)
            # SIGWINCH propagation + redraw is asynchronous; wait_stable
            # blocks until two consecutive captures match — i.e. the
            # render has settled at the new dimensions.
            screen = t.wait_stable(timeout=3.0)
            self.assertIn('a a', screen)
            # Every line must fit the new width (with a small slack for
            # the trailing-pad behaviour on some terminals).
            longest = max(len(line) for line in screen.splitlines())
            self.assertLessEqual(longest, 65,
                                 f'longest line {longest} > 65 after resize')

    def test_resize_with_expanded_tree_and_grid(self):
        """Resize works while the tree is hierarchical and a branch is expanded.

        Drives the children_grid recipe (parent → a1, a2; leaf):
          * Launch at 120x40 with the grid pane visible.
          * Cursor lands on 'parent' — the grid populates with a1 / a2.
          * Shrink to 60x20 — both list and grid panes must keep
            rendering legibly without crashing the renderer.
          * Restore to 120x40 — full expanded layout reappears.
        """
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--run-py', _GRID_RECIPE)
            # The recipe pre-renders 'parent' on first row; flush async
            # children-fetch into the screen with a redraw. The recipe
            # pins show_ids='always' so each row has its id segment.
            t.wait_for('parent parent')
            t.redraw()
            t.wait_for('a1', timeout=3.0)
            wide = t.wait_stable()
            # Sanity: the grid is up — Children separator + both kids.
            self.assertIn('Children', wide)
            self.assertIn('a1', wide)
            self.assertIn('a2', wide)

            # Shrink. SIGWINCH triggers reflow; renderer must handle the
            # narrower geometry without raising or producing oversized
            # lines. With only 60 cols the renderer may collapse the
            # grid pane (insufficient width) — that is acceptable; we
            # assert only that nothing crashes and lines fit.
            t.resize(60, 20)
            narrow = t.wait_stable(timeout=3.0)
            self.assertIn('parent', narrow,
                          f'cursor item disappeared after shrink:\n{narrow}')
            longest = max(len(line) for line in narrow.splitlines())
            self.assertLessEqual(longest, 65,
                                 f'longest line {longest} > 65 after shrink')

            # Restore. The grid should come back; cursor must stay on
            # 'parent' (and a1/a2 visible again).
            t.resize(120, 40)
            t.redraw()
            restored = t.wait_stable(timeout=3.0)
            self.assertIn('parent parent', restored)
            self.assertIn('a1', restored)
            self.assertIn('a2', restored)
            self.assertIn('Children', restored)
            t.send('q')


_FIRE_RX = re.compile(r'FIRES=(\d+) W=(\d+)')


class TestOnResizeSelfCompletes(unittest.TestCase):
    """#834: a layout change must fire ``on_resize`` and let the recipe's
    reaction land WITHOUT any extra user input.

    Drives ``on_resize_probe.py``: each ``on_resize`` fire bumps a counter
    and drops the preview cache, and ``get_preview`` reports
    ``FIRES=<n> W=<preview_width>``. So a fresh capture after a resize /
    split toggle reflects whether the broadened ``on_resize`` (#828)
    self-completed — the bug was that the fire lagged into an un-woken loop
    iteration and only landed on the user's NEXT keypress (#829 papered
    over it with ``redraw()``). This test injects NO keypress and NO
    ``redraw()`` between the layout change and the assertion.
    """

    @staticmethod
    def _fire(cap):
        """Return ``(fires, width)`` from the probe's preview, or ``None``."""
        m = _FIRE_RX.search(cap)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _wait_fire(self, t, *, fires, width=None, timeout=6.0):
        """Poll captures (NO redraw, NO keypress) until the probe reports
        ``FIRES=fires`` (and, if given, ``W=width``). Returns the matched
        ``(fires, width)``; fails with the last capture on timeout.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self._fire(t.capture())
            if last is not None and last[0] == fires and (
                    width is None or last[1] == width):
                return last
            time.sleep(0.05)
        self.fail(
            f'probe never reported FIRES={fires}'
            + (f' W={width}' if width is not None else '')
            + f' within {timeout}s without any input (last={last!r}); '
            f'on_resize did not self-complete.\ncapture:\n{t.capture()}')

    def test_resize_and_split_fire_on_resize_without_keypress(self):
        with TmuxFixture(cols=120, rows=40) as t:
            t.launch(_BIN, '--run-py', _ON_RESIZE_RECIPE)
            t.wait_for('FIRES=', timeout=5.0)
            # Startup fires on_resize once; baseline is the full-width 'h'
            # preview at 120 cols.
            self._wait_fire(t, fires=1, width=120)

            # ---- Terminal resize 120 -> 80 (SIGWINCH) ------------------
            # The fire must land + the preview must re-fetch at the new
            # width with NO keypress. Pre-#834 this stayed FIRES=1 W=120
            # until the next key.
            t.resize(80, 40)
            self._wait_fire(t, fires=2, width=80)

            # ---- Split toggle alt-1 (vertical), no terminal resize -----
            # The preview moves to a narrow right-hand pane; cols/rows are
            # unchanged so only the broadened on_resize catches it, and it
            # must self-complete with no input.
            t.send('M-1')
            fires_after_split, width_after_split = self._wait_fire(t, fires=3)
            self.assertLess(
                width_after_split, 80,
                f'alt-1 vertical split should narrow the preview pane, '
                f'but width stayed {width_after_split} (cols unchanged at '
                f'80) — the split-layout on_resize fire did not re-fetch.')

            # ---- No busy/infinite wake loop ----------------------------
            # Idle with no input: the fire count must NOT keep climbing
            # (the baseline advances on each fire, so an unchanged layout
            # emits no further wake).
            time.sleep(1.5)
            steady = self._fire(t.capture())
            self.assertEqual(
                steady[0], 3,
                f'on_resize fired again while idle (count {steady[0]} > 3) '
                f'— the post-render wake is spinning instead of nudging '
                f'once per layout change.')
            t.send('q')
