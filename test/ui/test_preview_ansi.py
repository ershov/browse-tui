"""UI tests: capital-R toggles preview SGR rendering end-to-end (ticket #246).

These tests drive a real terminal session through tmux and assert that
the user-visible preview pane behaves correctly:

  1. With ANSI on (default), a preview emitting ``\\x1b[31mRED\\x1b[0m``
     reaches the screen with the SGR codes intact (captured via
     ``tmux capture-pane -e``).
  2. Capital-R strips the SGR codes from the next paint.
  3. Capital-R again restores the SGR codes.
  4. ``--no-preview-ansi`` startup matches the toggled-off state.

The cache-hit invariant for non-coloured rows (assertion 4 in the
ticket) is covered by unit tests that drive the renderer directly; at
the UI level the primary value is the end-to-end correctness of the
toggle reaching the user's screen.
"""

import os
import re
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


# A red-foreground SGR (``ESC [ 31 m``) followed shortly by the literal
# token "RED". tmux's ``capture-pane -e`` preserves the SGR escape
# sequence, so this pattern matches only when the colour reaches the
# pane unstripped. Allow other SGR attributes (e.g. ``[m``, default
# colour resets) between the opening sequence and "RED" but require
# them to be on the same line and not cancel the foreground colour.
_RED_RE = re.compile(r'\x1b\[(?:[0-9;]*;)?31(?:;[0-9;]*)?m[^\n]*?RED')


def _has_red_sgr(capture: str) -> bool:
    return bool(_RED_RE.search(capture))


class TestPreviewAnsiToggle(unittest.TestCase):
    """End-to-end: capital-R toggles SGR pass-through in the preview pane."""

    def _launch(self, t: TmuxFixture, *extra):
        """Launch browse-tui with two items and a coloured preview command.

        Two items so we have something to navigate; the preview-cmd is
        identical for both (we test toggle behaviour, not per-item
        differences).
        """
        t.launch(
            _BIN,
            '--children-cmd', 'echo file_a; echo file_b',
            '--preview-cmd', r'printf "\x1b[31mRED\x1b[0m"',
            '--no-children-pane',
            '--show-ids', 'always',
            *extra,
        )
        # Confirm the list rendered both items.
        t.wait_for('file_a file_a')
        t.wait_for('file_b file_b')
        # Wait until the preview text reaches the screen.
        t.wait_for('RED', timeout=5.0)
        t.wait_stable()

    def test_default_on_then_R_toggles_off_then_on(self):
        """Default ANSI on → R off → R on (visible SGR flips each press)."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._launch(t)

            # (1) Default-on: SGR codes reach the pane.
            cap_on = t.capture(colors=True)
            self.assertIn('RED', cap_on)
            self.assertTrue(
                _has_red_sgr(cap_on),
                f'expected red SGR before "RED" in default-on capture; '
                f'capture (escapes shown):\n{cap_on!r}',
            )

            # (2) Press capital-R: next paint shows "RED" with no SGR.
            # In raw mode every ESC byte is replaced with '?' (matches
            # the pre-#243 contract — no escape can ever inject), so
            # ``\x1b[31mRED\x1b[0m`` shows up as ``?[31mRED?[0m``.
            t.send('R')
            t.wait_stable(timeout=3.0)
            cap_off = t.capture(colors=True)
            self.assertIn('?[31mRED?[0m', cap_off,
                          f'expected ?[31mRED?[0m sanitised marker after R '
                          f'toggled colours off; capture:\n{cap_off!r}')
            self.assertFalse(
                _has_red_sgr(cap_off),
                f'red SGR still present after capital-R; '
                f'capture (escapes shown):\n{cap_off!r}',
            )

            # (3) Press capital-R again: SGR codes return.
            t.send('R')
            t.wait_stable(timeout=3.0)
            cap_back = t.capture(colors=True)
            self.assertIn('RED', cap_back)
            self.assertTrue(
                _has_red_sgr(cap_back),
                f'red SGR did not return after second capital-R; '
                f'capture (escapes shown):\n{cap_back!r}',
            )

    def test_no_preview_ansi_startup_matches_toggled_off(self):
        """``--no-preview-ansi`` strips SGR codes from the very first paint."""
        with TmuxFixture(cols=80, rows=24) as t:
            self._launch(t, '--no-preview-ansi')

            cap = t.capture(colors=True)
            self.assertIn('?[31mRED?[0m', cap,
                          f'expected ?[31mRED?[0m sanitised marker on '
                          f'--no-preview-ansi startup; capture:\n{cap!r}')
            self.assertFalse(
                _has_red_sgr(cap),
                f'red SGR present despite --no-preview-ansi; '
                f'capture (escapes shown):\n{cap!r}',
            )

            # Sanity: capital-R from this start should turn colours ON.
            t.send('R')
            t.wait_stable(timeout=3.0)
            cap_on = t.capture(colors=True)
            self.assertTrue(
                _has_red_sgr(cap_on),
                f'expected red SGR after capital-R from --no-preview-ansi '
                f'start; capture (escapes shown):\n{cap_on!r}',
            )


if __name__ == '__main__':
    unittest.main()
