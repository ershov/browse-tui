"""Full-screen snapshot tests for layout regression.

Each test launches browse-tui at a fixed terminal size with a deterministic
dataset, captures the screen, and compares to a "golden" file in
``test/ui/snapshots/``. Snapshots are stored as plain text — trailing
whitespace is stripped before comparison so that minor capture-pane
differences across tmux versions don't cause spurious diffs.

To regenerate the goldens after an intentional rendering change, run:

    BROWSE_TUI_UPDATE_SNAPSHOTS=1 python3 -m unittest test.ui.test_snapshots -v

then inspect the diff with ``git diff test/ui/snapshots/`` and commit if it
looks right.
"""

import os
import shutil
import subprocess
import unittest

from test.ui.fixtures.tmux import TmuxFixture


_BIN = os.path.abspath('./browse-tui')
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SNAPSHOTS_DIR = os.path.join(os.path.dirname(__file__), 'snapshots')


def setUpModule():
    if not shutil.which('tmux'):
        raise unittest.SkipTest('tmux not available; UI tests skipped')
    if not os.path.exists(_BIN):
        subprocess.run([os.path.join(_REPO, 'build-tui.sh')], check=True)


def _snap(name: str) -> str:
    """Read the golden snapshot file (empty string if absent)."""
    p = os.path.join(_SNAPSHOTS_DIR, name)
    if not os.path.exists(p):
        return ''
    with open(p) as f:
        return f.read()


def _maybe_write_golden(name: str, content: str):
    """When ``BROWSE_TUI_UPDATE_SNAPSHOTS=1``, (re)write the golden file."""
    if os.environ.get('BROWSE_TUI_UPDATE_SNAPSHOTS') == '1':
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
        with open(os.path.join(_SNAPSHOTS_DIR, name), 'w') as f:
            f.write(content)


def _normalise(s: str) -> str:
    """Strip trailing whitespace per line and trailing newlines.

    capture-pane lines are typically padded with spaces to the column
    width — that's noise for layout-regression purposes, and would
    accidentally couple snapshots to the exact column count we picked
    when generating goldens.
    """
    return '\n'.join(line.rstrip() for line in s.splitlines()).strip('\n')


class TestSnapshots(unittest.TestCase):
    """Full-screen snapshot tests.

    Each test pins ``cols``/``rows`` so the layout is deterministic, then
    compares the normalised capture against ``test/ui/snapshots/<name>.txt``.
    Re-run with ``BROWSE_TUI_UPDATE_SNAPSHOTS=1`` to regenerate goldens.
    """

    def _run_snapshot(self, name, launch_argv, *, cols=80, rows=24,
                      wait_for=None, after=None):
        """Drive a TmuxFixture, capture stable screen, compare to golden.

        ``launch_argv`` is the argv passed to ``t.launch`` (the fixture
        shell-quotes it). ``wait_for`` is a substring or regex that must
        appear before we capture; ``after`` is an optional callback that
        receives the fixture and may send keystrokes.
        """
        with TmuxFixture(cols=cols, rows=rows) as t:
            t.launch(*launch_argv)
            if wait_for is not None:
                t.wait_for(wait_for)
            if after is not None:
                after(t)
            screen = _normalise(t.wait_stable())
            # Quit cleanly so the fixture teardown isn't racing the TUI.
            t.send('q')
        _maybe_write_golden(name, screen)
        golden = _snap(name)
        if not golden:
            self.skipTest(
                f'no golden snapshot for {name!r} '
                f'(run with BROWSE_TUI_UPDATE_SNAPSHOTS=1 to create)')
        self.assertEqual(
            screen, _normalise(golden),
            f'snapshot {name!r} drifted; '
            f'rerun with BROWSE_TUI_UPDATE_SNAPSHOTS=1 if intentional')

    # ---- snapshots --------------------------------------------------

    def test_initial_render_three_items(self):
        """Plain three-item flat tree, no preview / no children pane."""
        self._run_snapshot(
            'initial_three_items.txt',
            ('bash', '-c',
             f"printf 'a\\nb\\nc\\n' | {_BIN} --show-ids always --root-cmd cat "
             f"--no-children-pane --no-preview"),
            cols=80, rows=24,
            wait_for='a a',
        )

    def test_search_active(self):
        """Slash search query visible in the info bar."""
        def after(t):
            t.send('/')
            t.type('b')
            t.wait_for('/b')

        self._run_snapshot(
            'search_active.txt',
            ('bash', '-c',
             f"printf 'foo\\nbar\\nbaz\\n' | {_BIN} --show-ids always --root-cmd cat "
             f"--no-children-pane --no-preview"),
            cols=80, rows=24,
            wait_for='foo foo',
            after=after,
        )

    def test_multi_select_active(self):
        """Two rows selected; ``[2]`` badge in info bar; ``*`` markers."""
        def after(t):
            t.send('Space')
            t.wait_for('[1]')
            t.send('Space')
            t.wait_for('[2]')

        self._run_snapshot(
            'multiselect_two.txt',
            ('bash', '-c',
             f"printf 'one\\ntwo\\nthree\\nfour\\n' | {_BIN} --show-ids always --root-cmd cat "
             f"--no-children-pane --no-preview"),
            cols=80, rows=24,
            wait_for='one one',
            after=after,
        )

    def test_help_mode(self):
        """``?`` toggles the help overlay in the preview pane."""
        def after(t):
            t.send('?')
            # Help-text marker: the help body starts with the program
            # title; wait for it to land in the preview pane.
            t.wait_for('NAVIGATION')

        self._run_snapshot(
            'help_mode.txt',
            ('bash', '-c',
             f"printf 'a\\n' | {_BIN} --show-ids always --root-cmd cat "
             f"--no-children-pane --preview"),
            cols=80, rows=24,
            wait_for='a a',
            after=after,
        )

    def test_scoped_view(self):
        """Drilled-into-A scope shows the crumb + a1/a2; no B / no b1."""
        children_cmd = (
            'case "$TUI_ID" in '
            '"") printf "A\\tA\\t1\\nB\\tB\\t1\\n" ;; '
            'A) printf "a1\\ta1\\t0\\na2\\ta2\\t0\\n" ;; '
            'B) printf "b1\\tb1\\t0\\n" ;; '
            'esac'
        )

        def after(t):
            t.send('M-Down')   # scope-down into 'A'
            t.wait_for('a1 a1', timeout=3.0)

        self._run_snapshot(
            'scoped_view.txt',
            (_BIN, '--children-cmd', children_cmd,
             '--fields', 'id,title,has_children',
             '--no-children-pane', '--no-preview',
             '--show-ids', 'always', '--scope-crumb'),
            cols=80, rows=24,
            wait_for='A A',
            after=after,
        )


if __name__ == '__main__':
    unittest.main()
