"""UI tests: ``--split-type`` auto resolution + explicit overrides.

Drives the binary through a tmux fixture at known pane sizes and asserts
the rendered layout matches the requested split mode. The simplest signal
we look at is the presence (or absence) of a vertical pane separator
``│`` in the body region:

* ``h`` (horizontal): list top / preview bottom, **no** ``│`` in the body.
* ``v`` (vertical), ``m`` (mixed), ``pc`` (preview-children):
  list on one side and content on the other, separated by a vertical
  ``│`` running down the body.

For ``--split-type=auto`` the binary resolves to ``v`` when the
terminal is at least 230 columns wide, else ``h``. We launch in a 240-
col pane to verify wide → vertical and an 80-col pane to verify narrow
→ horizontal.

Distinguishing ``v`` from ``m`` from ``pc`` from the captured screen
alone is fragile (their separator placement is similar for a small
input). We assert only on the ``vertical-or-not`` axis for the explicit
overrides; the unit tests in ``test/unit/test_cli.py`` cover the
short/long-form alias mapping and ``Browser.split`` wiring directly.
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


def _has_vertical_separator(screen: str) -> bool:
    """Return True if the body (everything above the info bar) contains ``│``.

    The info bar at the bottom uses ``─`` runs and may include the
    ``Preview`` legend with its own decorations; checking only the
    body avoids false positives. We treat the LAST line as the info
    bar — the renderer reserves exactly one row for it.
    """
    lines = screen.splitlines()
    if not lines:
        return False
    body = '\n'.join(lines[:-1])
    return '│' in body


def _launch_with_data(t: TmuxFixture, *extra_args):
    """Pipe a tiny three-row dataset into browse-tui.

    Using ``--root-cmd cat`` keeps the input deterministic (no tree
    expansion to wait on); ``--show-ids always`` pins the row layout
    so the rendered text is stable across runs.

    ``--preview`` is supplied so the preview pane is forced visible
    — the split layout assertions all depend on a preview pane
    being part of the layout (the auto rule would hide it because
    no ``--preview-cmd`` is set).
    """
    cmd = (
        "printf 'a\\nb\\nc\\n' | "
        f"{_BIN} --show-ids always --root-cmd cat --preview"
    )
    for a in extra_args:
        cmd += ' ' + a
    t.send_line(cmd)


class TestAutoSplitResolution(unittest.TestCase):
    """``--split-type=auto`` (the default) reads live terminal width."""

    def test_auto_split_vertical_in_wide_terminal(self):
        """240-col pane → auto resolves to vertical (``│`` in body)."""
        with TmuxFixture(cols=240, rows=40) as t:
            _launch_with_data(t)
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'expected vertical layout (│ in body) at 240 cols; '
                f'got:\n{screen}',
            )

    def test_auto_split_horizontal_in_narrow_terminal(self):
        """80-col pane → auto resolves to horizontal (no ``│`` in body)."""
        with TmuxFixture(cols=80, rows=24) as t:
            _launch_with_data(t)
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertFalse(
                _has_vertical_separator(screen),
                f'expected horizontal layout (no │ in body) at 80 cols; '
                f'got:\n{screen}',
            )

    def test_auto_split_horizontal_just_below_threshold(self):
        """229-col pane (one shy of the 230-col threshold) → horizontal."""
        with TmuxFixture(cols=229, rows=40) as t:
            _launch_with_data(t)
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertFalse(
                _has_vertical_separator(screen),
                f'expected horizontal layout at 229 cols (below 230 '
                f'threshold); got:\n{screen}',
            )

    def test_auto_split_vertical_at_exact_threshold(self):
        """230-col pane (exact threshold) → vertical."""
        with TmuxFixture(cols=230, rows=40) as t:
            _launch_with_data(t)
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'expected vertical layout at 230 cols (exact threshold); '
                f'got:\n{screen}',
            )

    def test_auto_split_vertical_at_242_cols(self):
        """Regression for ticket #167: 242 cols (above 230) → vertical.

        This pins the exact width the user reported the bug at —
        ``--split-type=auto`` was selecting horizontal even though
        the pane was clearly above the threshold. The detector must
        find the live terminal width even when the std fds are not
        the controlling terminal (which is the case here: bash spawns
        the binary inside a tmux pane and there's no pipe involved,
        but the regression is about the detector reading the wrong
        source).
        """
        with TmuxFixture(cols=242, rows=40) as t:
            _launch_with_data(t)
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'expected vertical layout at 242 cols (well above 230 '
                f'threshold); got:\n{screen}',
            )

    def test_auto_split_under_pipeline_at_242_cols(self):
        """Regression for #167: detector survives a piped stdout.

        Reproduces the harder failure mode: the user pipes browse-tui's
        stdout to ``cat`` (a no-op consumer) while still running it in
        a 242-col tmux pane. ``os.get_terminal_size()`` defaulting to
        stdout would read 0/raise, so the detector must fall back to
        ``/dev/tty`` (or stderr) to find the real width.
        """
        with TmuxFixture(cols=242, rows=40) as t:
            cmd = (
                "printf 'a\\nb\\nc\\n' | "
                f"{_BIN} --show-ids always --root-cmd cat "
                f"--preview | cat"
            )
            t.send_line(cmd)
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'expected vertical layout at 242 cols even with stdout '
                f'piped through cat; got:\n{screen}',
            )

    def test_debug_env_var_emits_trace(self):
        """``BROWSE_TUI_DEBUG_AUTO=1`` writes a one-line trace to stderr.

        Verifies the diagnostic instrumentation: when the env var is
        set, the detector logs each probe's result before returning.
        We invoke the binary through bash so we can redirect stderr
        to a temp file and read it after the TUI exits.
        """
        import tempfile
        with tempfile.NamedTemporaryFile(mode='r', suffix='.log',
                                         delete=False) as logf:
            log_path = logf.name
        try:
            with TmuxFixture(cols=242, rows=40) as t:
                cmd = (
                    f"printf 'a\\nb\\nc\\n' | "
                    f"BROWSE_TUI_DEBUG_AUTO=1 "
                    f"{_BIN} --show-ids always --root-cmd cat "
                    f"2> {log_path}"
                )
                t.send_line(cmd)
                t.wait_for('a a')
                # Quit cleanly so the redirected stderr is flushed.
                t.send('q')
                # Give bash a moment to write the log + return to prompt.
                t.wait_for(re.compile(r'(?m)^\$ *$'), timeout=3.0)
            with open(log_path) as f:
                contents = f.read()
            self.assertIn('[browse-tui auto]', contents,
                          f'expected debug trace in stderr; got: {contents!r}')
            # The trace should mention at least one probe result.
            self.assertTrue(
                'tty_ioctl=' in contents or 'os_termsize_' in contents,
                f'expected probe trace in stderr; got: {contents!r}',
            )
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass


class TestExplicitSplitOverrides(unittest.TestCase):
    """``--split-type=h|v|m|pc`` overrides ignore terminal width."""

    def test_explicit_split_h_in_wide_terminal(self):
        """``--split-type=h`` forces horizontal even at 240 cols."""
        with TmuxFixture(cols=240, rows=40) as t:
            _launch_with_data(t, '--split-type=h')
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertFalse(
                _has_vertical_separator(screen),
                f'--split-type=h must stay horizontal at 240 cols; '
                f'got:\n{screen}',
            )

    def test_explicit_split_v_in_narrow_terminal(self):
        """``--split-type=v`` forces vertical even at 80 cols.

        Note: at 80 cols the renderer may down-fall to horizontal if
        there isn't enough width for a sensible side-by-side split
        (see ``_layout_vertical`` in 050-render.py). We launch at a
        midrange width (120 cols) which is comfortably above the
        ``list_w + 2`` minimum so the override actually takes effect
        and the assertion is meaningful.
        """
        with TmuxFixture(cols=120, rows=30) as t:
            _launch_with_data(t, '--split-type=v')
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'--split-type=v at 120 cols must produce vertical '
                f'layout; got:\n{screen}',
            )

    def test_explicit_split_m(self):
        """``--split-type=m`` (mixed) → vertical separator present."""
        with TmuxFixture(cols=160, rows=30) as t:
            _launch_with_data(t, '--split-type=m')
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'--split-type=m must produce a vertical separator '
                f'(list+children left, preview right); got:\n{screen}',
            )

    def test_explicit_split_pc(self):
        """``--split-type=pc`` (preview-children) → vertical separator."""
        with TmuxFixture(cols=160, rows=30) as t:
            _launch_with_data(t, '--split-type=pc')
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'--split-type=pc must produce a vertical separator '
                f'(list left, children-above-preview right); got:\n{screen}',
            )

    def test_explicit_split_long_forms(self):
        """Long-form aliases ``horizontal``/``vertical`` work the same."""
        with TmuxFixture(cols=240, rows=40) as t:
            _launch_with_data(t, '--split-type=horizontal')
            t.wait_for('a a')
            screen = t.wait_stable(timeout=3.0)
            self.assertFalse(
                _has_vertical_separator(screen),
                f'--split-type=horizontal must force horizontal at 240; '
                f'got:\n{screen}',
            )


class TestSplitCycle(unittest.TestCase):
    """Layout split keys swap modes — ``h`` → vertical via Alt-1.

    Since #1061 the layout keys are Alt-1..4 (``\\`` permanently triggers
    the context menu instead); Alt-1 jumps straight to the vertical mode.
    """

    def test_alt1_switches_from_horizontal_to_vertical(self):
        """At startup ``h``, pressing Alt-1 switches into the vertical mode.

        Verifies the runtime layout action actually swaps the layout —
        independently of how the initial split was resolved. We assert the
        body acquires a ``│`` (which it can't have under layout ``h``) once
        Alt-1 lands us in vertical, in a wide-enough pane.
        """
        # 120 cols is wide enough for vertical to actually render
        # (it has the down-fall to 'h' if the pane is too narrow).
        with TmuxFixture(cols=120, rows=30) as t:
            _launch_with_data(t, '--split-type=h')
            t.wait_for('a a')
            initial = t.wait_stable(timeout=3.0)
            self.assertFalse(
                _has_vertical_separator(initial),
                f'expected horizontal at startup; got:\n{initial}',
            )
            # Alt-1 jumps directly to the vertical layout.
            t.send('M-1')
            screen = t.wait_stable(timeout=3.0)
            self.assertTrue(
                _has_vertical_separator(screen),
                f'Alt-1 from h did not produce a vertical layout; '
                f'screen:\n{screen}',
            )


if __name__ == '__main__':
    unittest.main()
